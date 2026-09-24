import asyncio
import json
import os

import pytest

from dark_army_daemon import vscode_reveal as vr


def test_normalize_tty():
    assert vr._normalize_tty("/dev/ttys006") == "ttys006"
    assert vr._normalize_tty(" ttys006 ") == "ttys006"
    assert vr._normalize_tty("") == ""


def test_find_code_binary_prefers_app_bundle(monkeypatch):
    vr._code_bin_cache = None
    bundle = vr._CODE_CANDIDATES[0]
    monkeypatch.setattr(vr.os.path, "exists", lambda p: p == bundle)
    assert vr.find_code_binary() == bundle


def test_find_code_binary_falls_back_to_path(monkeypatch):
    vr._code_bin_cache = None
    monkeypatch.setattr(vr.os.path, "exists", lambda p: False)
    monkeypatch.setattr(vr.shutil, "which", lambda name: "/usr/local/bin/code" if name == "code" else None)
    assert vr.find_code_binary() == "/usr/local/bin/code"


def test_find_code_binary_missing_is_cached(monkeypatch):
    vr._code_bin_cache = None
    monkeypatch.setattr(vr.os.path, "exists", lambda p: False)
    monkeypatch.setattr(vr.shutil, "which", lambda name: None)
    assert vr.find_code_binary() is None
    assert vr._code_bin_cache == ""  # remembered the miss


def test_read_claude_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_CLAUDE_IDE_DIR", tmp_path)
    payload = {"authToken": "tok", "workspaceFolders": ["/a/b"]}
    (tmp_path / "18224.lock").write_text(json.dumps(payload))
    assert vr.read_claude_lock("18224") == payload
    assert vr.read_claude_lock("99999") is None
    assert vr.read_claude_lock("") is None


def test_bob_ext_locks_skips_malformed(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps({"port": 1, "authToken": "a"}))
    (tmp_path / "2.lock").write_text("{ not json")
    (tmp_path / "3.lock").write_text(json.dumps({"port": 3}))  # no token
    locks = vr._bob_ext_locks()
    assert [l["port"] for l in locks] == [1]


def test_bob_ext_locks_rejects_unusable_ports(tmp_path, monkeypatch):
    """A port that is not a usable int is dropped rather than carried forward.

    `_fanout_reveal_terminal` builds gather()'s arguments before gather runs, so an
    int() that raises there escapes `return_exceptions=True` — one truncated lock
    file took down reveal for every session on the machine.
    """
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps({"port": "notanint", "authToken": "tok"}))
    (tmp_path / "2.lock").write_text(json.dumps({"port": 0, "authToken": "tok"}))
    (tmp_path / "3.lock").write_text(json.dumps({"port": 99999, "authToken": "tok"}))
    (tmp_path / "4.lock").write_text(json.dumps({"port": True, "authToken": "tok"}))
    (tmp_path / "5.lock").write_text(json.dumps({"port": 7777, "authToken": "tok"}))
    assert [l["port"] for l in vr._bob_ext_locks()] == [7777]


def test_bob_ext_locks_rejects_header_unsafe_tokens(tmp_path, monkeypatch):
    """The token goes straight into a request header, so CR/LF cannot survive."""
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    smuggle = "x\r\nContent-Length: 0\r\n\r\nPOST /admin HTTP/1.1"
    (tmp_path / "1.lock").write_text(json.dumps({"port": 7777, "authToken": smuggle}))
    (tmp_path / "2.lock").write_text(json.dumps({"port": 7778, "authToken": ["a"]}))
    (tmp_path / "3.lock").write_text(json.dumps({"port": 7779, "authToken": "a1b2c3"}))
    assert [l["port"] for l in vr._bob_ext_locks()] == [7779]


def test_fanout_survives_a_malformed_lock(tmp_path, monkeypatch):
    """One bad lock must not stop the good windows from being asked."""
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps({"port": "notanint", "authToken": "tok"}))
    (tmp_path / "2.lock").write_text(json.dumps({"port": 7777, "authToken": "tok"}))

    asked = []

    async def fake_post(port, token, body, timeout=3.0):
        asked.append(port)
        return {"matched": True, "matchedBy": "pid"}

    monkeypatch.setattr(vr, "_post_json", fake_post)
    got = asyncio.run(vr._fanout_reveal_terminal(1234, "ttys006"))
    assert asked == [7777]
    assert got["matched"] is True


def test_frontmost_returns_only_matches_from_a_focused_window(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps({"port": 7777, "authToken": "tok"}))
    (tmp_path / "2.lock").write_text(json.dumps({"port": 7778, "authToken": "tok"}))

    async def fake_post(port, token, body, timeout=3.0):
        # The unfocused window still owns pid 300 — and must not suppress it.
        if port == 7777:
            return {"focused": False, "matched": [300]}
        return {"focused": True, "matched": [200]}

    monkeypatch.setattr(vr, "_post_json", fake_post)
    got = asyncio.run(vr.frontmost_session_pids([100, 200, 300]))
    assert got == {200}


def test_frontmost_ignores_a_pid_it_did_not_ask_about(tmp_path, monkeypatch):
    """The answer comes from another process; only what was asked is believed."""
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps({"port": 7777, "authToken": "tok"}))

    async def fake_post(port, token, body, timeout=3.0):
        return {"focused": True, "matched": [200, 999, "200"]}

    monkeypatch.setattr(vr, "_post_json", fake_post)
    assert asyncio.run(vr.frontmost_session_pids([200])) == {200}


def test_frontmost_is_empty_without_an_answer(tmp_path, monkeypatch):
    """No extension, an old one, or a window that times out: suppress nothing.

    Failing towards *alerting* is the whole disposition of this path — a missed
    interruption is the expensive way to be wrong.
    """
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    assert asyncio.run(vr.frontmost_session_pids([200])) == set()

    (tmp_path / "1.lock").write_text(json.dumps({"port": 7777, "authToken": "tok"}))

    async def unknown_op(port, token, body, timeout=3.0):
        return {"error": "unknown op: frontmost"}       # a pre-0.1.4 extension

    monkeypatch.setattr(vr, "_post_json", unknown_op)
    assert asyncio.run(vr.frontmost_session_pids([200])) == set()

    async def dead(port, token, body, timeout=3.0):
        return None

    monkeypatch.setattr(vr, "_post_json", dead)
    assert asyncio.run(vr.frontmost_session_pids([200])) == set()


def test_frontmost_asks_nothing_when_there_is_nobody_to_suppress(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps({"port": 7777, "authToken": "tok"}))

    async def boom(port, token, body, timeout=3.0):
        raise AssertionError("asked with no candidates")

    monkeypatch.setattr(vr, "_post_json", boom)
    assert asyncio.run(vr.frontmost_session_pids([])) == set()


def _run(coro):
    return asyncio.run(coro)


def test_reveal_non_vscode_is_noop(monkeypatch):
    async def fake_env(pid):
        return {"TERM_PROGRAM": "Apple_Terminal"}  # no SSE port, not vscode

    monkeypatch.setattr(vr, "_ps_env", fake_env)
    ok, detail = _run(vr.reveal(1234))
    assert ok is False
    assert "not a VS Code session" in detail


def test_reveal_happy_path_activates_without_new_window(monkeypatch):
    calls = {}

    async def fake_env(pid):
        return {"CLAUDE_CODE_SSE_PORT": "18224", "TERM_PROGRAM": "vscode"}

    async def fake_tty(field, pid):
        return "ttys009"

    async def fake_fanout(pid, tty):
        calls["fanout"] = (pid, tty)
        return {"matched": True, "matchedBy": "ancestry", "workspaceFolders": ["/repo"]}

    async def fake_activate():
        calls["activate"] = True
        return True

    monkeypatch.setattr(vr, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vr, "_ps_env", fake_env)
    monkeypatch.setattr(vr, "_ps_field", fake_tty)
    monkeypatch.setattr(vr, "_fanout_reveal_terminal", fake_fanout)
    monkeypatch.setattr(vr, "_activate_vscode", fake_activate)

    ok, detail = _run(vr.reveal(4321))
    assert ok is True
    assert calls.get("activate") is True
    assert calls["fanout"] == (4321, "ttys009")


def test_reveal_never_uses_code_even_when_extension_absent(monkeypatch):
    """When no window claims the terminal, VS Code is still raised via open -a —
    but `code` (the only thing that can open a NEW window) is never invoked."""
    calls = {}

    async def fake_env(pid):
        return {"CLAUDE_CODE_SSE_PORT": "18224", "TERM_PROGRAM": "vscode"}

    async def fake_tty(field, pid):
        return "ttys009"

    async def no_match(pid, tty):
        return None  # no window's extension claimed it

    async def fake_activate():
        calls["activate"] = True
        return True

    monkeypatch.setattr(vr, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vr, "_ps_env", fake_env)
    monkeypatch.setattr(vr, "_ps_field", fake_tty)
    monkeypatch.setattr(vr, "_fanout_reveal_terminal", no_match)
    monkeypatch.setattr(vr, "_activate_vscode", fake_activate)
    # `code` must not be discovered/used on any reveal path.
    monkeypatch.setattr(vr, "find_code_binary", lambda: (_ for _ in ()).throw(AssertionError("code used")))

    ok, detail = _run(vr.reveal(4321))
    assert ok is True
    assert calls.get("activate") is True
    assert "reload" in detail.lower()


# --- window identity: which path `code` will reuse a window for -----------------
#
# The bug these cover: both windows on the machine that exposed it were *untitled
# workspaces* holding one folder each, which `workspaceFolders` reports identically
# to a plain folder window. Targeting the folder opened a second window. Targeting
# workspace.json (the next attempt) was treated by the CLI as a *file* and focused
# the last-active window — Jump looked dead whenever another VS Code was already
# in front.

def test_folder_window_targets_its_folder(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    target = vr._window_target(
        {"matched": True, "workspaceFolders": [str(folder)],
         "workspaceUri": None, "workspaceFsPath": None}
    )
    assert target == str(folder)


def test_saved_workspace_targets_the_workspace_file(tmp_path):
    ws = tmp_path / "two.code-workspace"
    ws.write_text("{}")
    target = vr._window_target(
        {"matched": True, "workspaceFolders": [str(tmp_path / "a"), str(tmp_path / "b")],
         "workspaceUri": ws.as_uri(), "workspaceFsPath": str(ws)}
    )
    # The folders are NOT the identity — the workspace file is.
    assert target == str(ws)


def test_untitled_workspace_cannot_be_named_for_code(tmp_path):
    """`code Workspaces/<id>/workspace.json` is a file open, not a window reuse.
    The raiser has to go through `-g` on a file inside the folder instead."""
    folder = tmp_path / "demo-app"
    folder.mkdir()
    (folder / "README.md").write_text("x")
    match = {"matched": True, "workspaceFolders": [str(folder)],
             "workspaceUri": "untitled:1783150756357", "workspaceFsPath": None}
    assert vr._window_target(match) is None
    assert vr._goto_target(match) == str(folder / "README.md")


def test_goto_prefers_a_root_readme_over_a_random_file(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "zzz.py").write_text("x")
    (folder / "README.md").write_text("hi")
    assert vr._goto_target({"workspaceFolders": [str(folder)]}) == str(folder / "README.md")


def test_goto_prefers_an_already_open_editor_over_readme(tmp_path):
    """-g on a closed README opens it and used to leave the tab behind."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "README.md").write_text("hi")
    src = folder / "app.py"
    src.write_text("x")
    assert vr._goto_target({
        "workspaceFolders": [str(folder)],
        "openEditors": [str(src)],
    }) == str(src)


def test_goto_falls_back_to_git_head_when_the_root_is_empty(tmp_path):
    folder = tmp_path / "repo"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main")
    assert vr._goto_target({"workspaceFolders": [str(folder)]}) == str(folder / ".git" / "HEAD")


def test_goto_skips_missing_folders():
    assert vr._goto_target({"workspaceFolders": ["/no/such/path"]}) is None
    assert vr._goto_target({"workspaceFolders": []}) is None


def test_remote_or_virtual_workspace_falls_back():
    """A scheme we cannot resolve to a local path names nothing."""
    assert vr._window_target(
        {"matched": True, "workspaceFolders": ["/r"],
         "workspaceUri": "vscode-remote://ssh-remote+box/r", "workspaceFsPath": None}
    ) is None


def test_old_extension_without_identity_never_guesses(tmp_path):
    """A 0.1.1 extension omits the identity keys. A folder window and an untitled
    workspace holding one folder look identical through that gap, so the answer is
    "fall back", not "assume a folder" — guessing here reopens the original bug for
    every window not yet reloaded after the update."""
    folder = tmp_path / "repo"
    folder.mkdir()
    assert vr._window_target(
        {"matched": True, "matchedBy": "ancestry", "workspaceFolders": [str(folder)]}
    ) is None


def test_reveal_prefers_the_identity_raise_over_open_a(tmp_path, monkeypatch):
    """End to end: a matched window with an identity is raised by naming it, and
    `open -a` — which cannot tell two windows apart — is not consulted at all."""
    folder = tmp_path / "repo"
    folder.mkdir()
    calls = {}

    async def fake_env(pid):
        return {"CLAUDE_CODE_SSE_PORT": "18224", "TERM_PROGRAM": "vscode"}

    async def fake_tty(field, pid):
        return "ttys009"

    async def fake_fanout(pid, tty):
        return {"matched": True, "matchedBy": "ancestry",
                "workspaceFolders": [str(folder)],
                "workspaceUri": None, "workspaceFsPath": None}

    async def fake_raise(target):
        calls["raised"] = target
        return True

    async def fake_activate():
        calls["open_a"] = True
        return True

    monkeypatch.setattr(vr, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vr, "_ps_env", fake_env)
    monkeypatch.setattr(vr, "_ps_field", fake_tty)
    monkeypatch.setattr(vr, "_fanout_reveal_terminal", fake_fanout)
    monkeypatch.setattr(vr, "_raise_window", fake_raise)
    monkeypatch.setattr(vr, "_activate_vscode", fake_activate)

    ok, detail = _run(vr.reveal(4321))
    assert ok is True
    assert calls["raised"] == str(folder)
    assert "open_a" not in calls          # the imprecise raiser never ran


def test_reveal_untitled_uses_goto_not_workspace_json(tmp_path, monkeypatch):
    """The live failure: two untitled workspaces, the other window already
    frontmost. `code workspace.json` cannot name the target, so we `-g` a file
    inside its folder (findWindowOnFile + window.focus) and never `open -a`."""
    folder = tmp_path / "finance-demo"
    folder.mkdir()
    readme = folder / "README.md"
    readme.write_text("x")
    calls = {"fanouts": 0}

    async def fake_env(pid):
        return {"CLAUDE_CODE_SSE_PORT": "18224", "TERM_PROGRAM": "vscode"}

    async def fake_tty(field, pid):
        return "ttys009"

    async def fake_fanout(pid, tty):
        calls["fanouts"] += 1
        return {"matched": True, "matchedBy": "ancestry",
                "workspaceFolders": [str(folder)],
                "workspaceUri": "untitled:1786186326489", "workspaceFsPath": None}

    async def fake_raise(target):
        calls["raised"] = target
        return True

    async def fake_goto(path):
        calls["goto"] = path
        return True

    async def fake_activate():
        calls["open_a"] = True
        return True

    async def fake_close(path):
        calls["closed"] = path

    monkeypatch.setattr(vr, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vr, "_ps_env", fake_env)
    monkeypatch.setattr(vr, "_ps_field", fake_tty)
    monkeypatch.setattr(vr, "_fanout_reveal_terminal", fake_fanout)
    monkeypatch.setattr(vr, "_raise_window", fake_raise)
    monkeypatch.setattr(vr, "_goto_file", fake_goto)
    monkeypatch.setattr(vr, "_fanout_close_editor", fake_close)
    monkeypatch.setattr(vr, "_activate_vscode", fake_activate)

    ok, detail = _run(vr.reveal(4321))
    assert ok is True
    assert "raised" not in calls          # never named workspace.json
    assert calls["goto"] == str(readme)
    assert calls["closed"] == str(readme)  # we opened it, so we take it away
    assert "open_a" not in calls
    assert calls["fanouts"] == 2          # tab, then tab again after -g
    assert "-g" in detail


def test_reveal_does_not_close_a_file_the_window_already_had(tmp_path, monkeypatch):
    folder = tmp_path / "repo"
    folder.mkdir()
    src = folder / "app.py"
    src.write_text("x")
    (folder / "README.md").write_text("x")
    calls = {"closed": None}

    async def fake_env(pid):
        return {"CLAUDE_CODE_SSE_PORT": "18224", "TERM_PROGRAM": "vscode"}

    async def fake_tty(field, pid):
        return "ttys009"

    async def fake_fanout(pid, tty):
        return {"matched": True, "matchedBy": "ancestry",
                "workspaceFolders": [str(folder)],
                "workspaceUri": "untitled:1", "workspaceFsPath": None,
                "openEditors": [str(src)]}

    async def fake_goto(path):
        calls["goto"] = path
        return True

    async def fake_close(path):
        calls["closed"] = path

    monkeypatch.setattr(vr, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vr, "_ps_env", fake_env)
    monkeypatch.setattr(vr, "_ps_field", fake_tty)
    monkeypatch.setattr(vr, "_fanout_reveal_terminal", fake_fanout)
    monkeypatch.setattr(vr, "_raise_window", lambda t: (_ for _ in ()).throw(AssertionError("named")))
    monkeypatch.setattr(vr, "_goto_file", fake_goto)
    monkeypatch.setattr(vr, "_fanout_close_editor", fake_close)
    monkeypatch.setattr(vr, "_activate_vscode", lambda: (_ for _ in ()).throw(AssertionError("open -a")))

    ok, _ = _run(vr.reveal(4321))
    assert ok is True
    assert calls["goto"] == str(src)
    assert calls["closed"] is None


def test_etime_seconds_parses_ps_formats():
    assert vr._etime_seconds("05") == 5
    assert vr._etime_seconds("01:02") == 62
    assert vr._etime_seconds("01:02:03") == 3723
    assert vr._etime_seconds("2-01:00:00") == 2 * 86400 + 3600
    assert vr._etime_seconds("") == 0
    assert vr._etime_seconds("nope") == 0


def test_run_process_kills_the_group_on_timeout():
    """A timeout must not leave the child (or its children) running.

    The production bug was `code` spawning `cli.js` and only the script being
    killed, so we assert the whole process group is gone."""
    marker = "dark-army-run-process-timeout-test"
    result = vr._run_process(
        ["/bin/sh", "-c", f"sleep 30; echo {marker}"],
        timeout=0.3,
    )
    assert result is None
    leftover = __import__("subprocess").check_output(
        ["ps", "-ax", "-o", "command="], text=True,
    )
    assert marker not in leftover


_CLI = "/Applications/Visual Studio Code.app/Contents/Resources/app/out/cli.js"
_WRAPPER = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"


def test_reap_stale_code_cli_kills_every_wedged_cli(monkeypatch):
    """Not just our installs. Any wedged CLI holds the single-instance pipe, and
    while it is held the next `code` blocks — which is what broke Jump machine-wide.
    `--wait` is the one long-lived-by-design form and is spared."""
    killed = []

    def fake_ps(*args, **kwargs):
        return (
            f" 111 00:05 /MacOS/Code {_CLI} --install-extension /x/dark-army-ide-0.1.2.vsix\n"
            f" 222 05:00 /MacOS/Code {_CLI} --install-extension /x/dark-army-ide-0.1.2.vsix\n"
            f" 333 05:00 /MacOS/Code {_CLI} --install-extension /other/ext.vsix --force\n"
            f" 444 05:00 /MacOS/Code {_CLI} --list-extensions --show-versions\n"
            f" 555 05:00 bash {_WRAPPER} /Users/x/repo.code-workspace\n"
            f" 666 05:00 /MacOS/Code {_CLI} --wait /tmp/COMMIT_EDITMSG\n"
            " 777 05:00 /usr/bin/python3 -m something_else\n"
        ).encode()

    monkeypatch.setattr(vr.subprocess, "check_output", fake_ps)
    monkeypatch.setattr(vr.os, "kill", lambda pid, sig: killed.append(pid))
    assert vr.reap_stale_code_cli(min_age_s=120) == 4
    assert killed == [222, 333, 444, 555]


def test_reap_survives_non_ascii_in_someone_elses_command_line(monkeypatch):
    """The live total failure: `text=True` under an ASCII locale turned one
    accented byte anywhere in `ps` output into a UnicodeDecodeError — a
    ValueError, which escaped the caller's except and aborted every reveal."""
    killed = []

    def fake_ps(*args, **kwargs):
        return (
            " 111 05:00 /usr/bin/vim /Users/x/caf\xe9/notes.txt\n".encode("latin-1")
            + f" 222 05:00 /MacOS/Code {_CLI} --list-extensions\n".encode()
        )

    monkeypatch.setattr(vr.subprocess, "check_output", fake_ps)
    monkeypatch.setattr(vr.os, "kill", lambda pid, sig: killed.append(pid))
    assert vr.reap_stale_code_cli(min_age_s=120) == 1
    assert killed == [222]


def test_raisers_prefer_open_over_the_code_cli(monkeypatch):
    """`code` is Electron and can deadlock forever depending on who spawned the
    daemon; `open` is LaunchServices and cannot. The CLI is the fallback only."""
    opened = []

    async def fake_open(path):
        opened.append(path)
        return True

    monkeypatch.setattr(vr, "_open_in_vscode", fake_open)
    monkeypatch.setattr(vr, "_run_code", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the code CLI must not run when open succeeds")))

    assert _run(vr._raise_window("/repo")) is True
    assert _run(vr._goto_file("/repo/README.md")) is True
    assert opened == ["/repo", "/repo/README.md"]


def test_a_failed_targeted_raise_is_reported_not_papered_over(tmp_path, monkeypatch):
    """The bug that made a machine-wide outage look session-specific: when both
    targeted raisers failed we fell through to `open -a`, which raises whichever
    window was already frontmost and then reports success. Jump therefore
    'worked' for the window you were already looking at and silently did nothing
    for every other one."""
    folder = tmp_path / "repo"
    folder.mkdir()
    calls = {}

    async def fake_env(pid):
        return {"CLAUDE_CODE_SSE_PORT": "18224", "TERM_PROGRAM": "vscode"}

    async def fake_tty(field, pid):
        return "ttys009"

    async def fake_fanout(pid, tty):
        return {"matched": True, "matchedBy": "ancestry",
                "workspaceFolders": [str(folder)],
                "workspaceUri": None, "workspaceFsPath": None}

    async def failed_raise(target):
        calls["tried"] = target
        return False

    async def fake_activate():
        calls["open_a"] = True
        return True

    monkeypatch.setattr(vr, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vr, "_ps_env", fake_env)
    monkeypatch.setattr(vr, "_ps_field", fake_tty)
    monkeypatch.setattr(vr, "_fanout_reveal_terminal", fake_fanout)
    monkeypatch.setattr(vr, "_raise_window", failed_raise)
    monkeypatch.setattr(vr, "_goto_target", lambda match: None)
    monkeypatch.setattr(vr, "_activate_vscode", fake_activate)

    ok, detail = _run(vr.reveal(4321))
    assert ok is False                    # the caller can say so
    assert "open_a" not in calls          # and we did not raise the wrong window
    assert calls["tried"] == str(folder)
    assert "would not come forward" in detail


# --- send_text: type into a terminal without raising the window ----------------


def test_parse_ext_version():
    assert vr._parse_ext_version("0.1.6") == (0, 1, 6)
    assert vr._parse_ext_version("0.1.5") == (0, 1, 5)
    assert vr._parse_ext_version("0.10.0") == (0, 10, 0)
    assert vr._parse_ext_version(None) == (0,)
    assert vr._parse_ext_version("nope") == (0,)
    assert vr._lock_can_send({"extensionVersion": "0.1.6"}) is True
    assert vr._lock_can_send({"extensionVersion": "0.1.5"}) is False
    assert vr._lock_can_send({}) is False
    # close_terminal is 0.1.9: an older lock answers `{error: unknown op}` with
    # HTTP 200, which must never read as a closed terminal.
    assert vr._lock_can_close({"extensionVersion": "0.1.9"}) is True
    assert vr._lock_can_close({"extensionVersion": "0.1.8"}) is False
    assert vr._lock_can_close({"extensionVersion": "nope"}) is False
    assert vr._lock_can_close({}) is False


def test_send_text_skips_locks_older_than_0_1_6(tmp_path, monkeypatch):
    """A 0.1.5 window answers `{error: unknown op}` with HTTP 200. Asking it
    would look like a miss, not a success, but we must not even try — a
    mixed-version machine would burn the timeout on every old window."""
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7776, "authToken": "old", "extensionVersion": "0.1.5"}))
    (tmp_path / "2.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "new", "extensionVersion": "0.1.6"}))
    asked = []

    async def fake_post(port, token, body, timeout=3.0):
        asked.append((port, body["op"], body["text"]))
        return {"matched": True, "sent": True, "matchedBy": "pid"}

    monkeypatch.setattr(vr, "_post_json", fake_post)
    got = asyncio.run(vr.send_text(1234, "ttys006", "/compact"))
    assert asked == [(7777, "send_text", "/compact")]
    assert got["sent"] is True


def test_send_text_first_match_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "a", "extensionVersion": "0.1.6"}))
    (tmp_path / "2.lock").write_text(json.dumps(
        {"port": 7778, "authToken": "b", "extensionVersion": "0.1.6"}))

    async def fake_post(port, token, body, timeout=3.0):
        if port == 7777:
            return {"matched": False, "sent": False}
        return {"matched": True, "sent": True, "terminalName": "zsh"}

    monkeypatch.setattr(vr, "_post_json", fake_post)
    got = asyncio.run(vr.send_text(1234, "ttys006", "/compact"))
    assert got["terminalName"] == "zsh"


def test_send_text_unknown_op_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.6"}))

    async def unknown(port, token, body, timeout=3.0):
        return {"error": "unknown op: send_text"}

    monkeypatch.setattr(vr, "_post_json", unknown)
    assert asyncio.run(vr.send_text(1234, "ttys006", "/compact")) is None


def test_send_text_empty_text_is_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.6"}))

    async def boom(port, token, body, timeout=3.0):
        raise AssertionError("must not POST empty text")

    monkeypatch.setattr(vr, "_post_json", boom)
    assert asyncio.run(vr.send_text(1234, "ttys006", "")) is None


def test_can_send_text_needs_a_0_1_6_lock_and_a_vscode_pid(tmp_path, monkeypatch):
    vr._invalidate_can_send_cache()
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    monkeypatch.setattr(vr, "_session_in_vscode", lambda pid: pid == 1234)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.5"}))
    assert vr.can_send_text(1234, "ttys006") is False

    vr._invalidate_can_send_cache()
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.6"}))
    assert vr.can_send_text(1234, "ttys006") is True
    assert vr.can_send_text(9999, "ttys006") is False  # not in vscode
    assert vr.can_send_text(0) is False


def test_can_send_text_is_cached_across_a_snapshot(tmp_path, monkeypatch):
    vr._invalidate_can_send_cache()
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.6"}))
    calls = {"n": 0}

    def counted(pid):
        calls["n"] += 1
        return True

    monkeypatch.setattr(vr, "_session_in_vscode", counted)
    assert vr.can_send_text(1234) is True
    assert vr.can_send_text(1234) is True
    assert calls["n"] == 1


def test_can_close_terminal_needs_a_0_1_9_lock(tmp_path, monkeypatch):
    """A 0.1.8 window can type but cannot dispose a tab — `{error: 'unknown
    op'}` is HTTP 200, so the version gate is the fail-closed half."""
    vr._invalidate_can_send_cache()
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    monkeypatch.setattr(vr, "_session_in_vscode", lambda pid: True)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.8"}))
    assert vr.can_send_text(1234) is True
    assert vr.can_close_terminal(1234) is False

    vr._invalidate_can_send_cache()
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.9"}))
    assert vr.can_send_text(1234) is True
    assert vr.can_close_terminal(1234) is True
    assert vr.can_close_terminal(0) is False
    assert vr.can_close_terminal(None) is False


def test_can_send_and_can_close_share_one_ps(tmp_path, monkeypatch):
    """Two probes for one pid cost one `ps` — `_in_vscode` is the shared cache."""
    vr._invalidate_can_send_cache()
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.9"}))
    calls = {"n": 0}

    def counted(*args, **kwargs):
        calls["n"] += 1
        return b"TERM_PROGRAM=vscode command"

    monkeypatch.setattr(vr.subprocess, "check_output", counted)
    assert vr.can_send_text(1234) is True
    assert vr.can_close_terminal(1234) is True
    assert calls["n"] == 1


def test_reap_only_matches_the_executable_or_script_never_a_later_argument(monkeypatch):
    """The substring-anywhere match aimed the reaper at any process that merely
    *mentioned* a CLI path — a `grep -r /app/out/cli.js`, an editor with that
    file open — and SIGKILLed it at two minutes old. The mark must name what the
    process *is* (argv[0] or the script it runs), not what it is looking at."""
    killed = []

    def fake_ps(*args, **kwargs):
        return (
            f" 111 05:00 grep -r /app/out/cli.js /tmp\n"
            f" 222 05:00 /usr/bin/vim /Users/x/notes/app/out/cli.js.md\n"
            f" 333 05:00 emacs --insert {_CLI}\n"
            f" 444 05:00 /MacOS/Code {_CLI} --list-extensions\n"
            f" 555 05:00 bash {_WRAPPER} /Users/x/repo.code-workspace\n"
        ).encode()

    monkeypatch.setattr(vr.subprocess, "check_output", fake_ps)
    monkeypatch.setattr(vr.os, "kill", lambda pid, sig: killed.append(pid))
    assert vr.reap_stale_code_cli(min_age_s=120) == 2
    assert killed == [444, 555]


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['0.1.11', '', None, 'garbage'])
async def test_refinement_transport_refuses_old_bridge(monkeypatch, version):
    monkeypatch.setattr(vr, '_bob_ext_locks', lambda: [{'port': 1, 'authToken': 't',
                        'workspaceFolders': ['/code/bob'], 'extensionVersion': version}])
    async def forbidden(*args, **kwargs):
        pytest.fail('old bridge must receive no validation/close request')
    monkeypatch.setattr(vr, '_post_json', forbidden)
    assert await vr.close_refinement_terminal(701, '/dev/ttys040', '/code/bob', forbidden) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('valid', [False, True])
async def test_refinement_transport_final_validation_after_preparation(monkeypatch, valid):
    events = []
    monkeypatch.setattr(vr, '_bob_ext_locks', lambda: [{'port': 123, 'authToken': 't',
                        'workspaceFolders': ['/code/bob'], 'extensionVersion': '0.1.12'}])
    monkeypatch.setattr(vr, '_session_in_vscode', lambda pid: events.append('prepare') or True)
    async def validate():
        events.append('validate')
        return valid
    async def post(port, token, body, **kwargs):
        if kwargs.get('before_write') and not await kwargs['before_write']():
            return None
        assert body == {'op': 'close_refinement_terminal', 'pid': 701, 'tty': '/dev/ttys040'}
        events.append('post')
        return {'matched': True, 'closed': True}
    monkeypatch.setattr(vr, '_post_json', post)
    result = await vr.close_refinement_terminal(701, '/dev/ttys040', '/code/bob', validate)
    assert events == (['prepare', 'validate', 'post'] if valid else ['prepare', 'validate'])
    assert bool(result) is valid


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['refuse', 'cancel', 'accept'])
async def test_refinement_post_validates_after_connect_and_always_closes_writer(monkeypatch, outcome):
    events = []
    class Writer:
        def write(self, data): events.append('write')
        async def drain(self): pass
        def close(self): events.append('closed')
        async def wait_closed(self): pass
    class Reader:
        async def read(self): return b'HTTP/1.1 200 OK\r\n\r\n{"matched": true, "closed": true}'
    async def connect(*args):
        events.append('connect')
        await asyncio.sleep(0)
        return Reader(), Writer()
    async def validate():
        events.append('validate')
        if outcome == 'cancel': raise asyncio.CancelledError()
        return outcome == 'accept'
    monkeypatch.setattr(vr.asyncio, 'open_connection', connect)
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await vr._post_json(123, 'fixture', {}, before_write=validate)
    else:
        result = await vr._post_json(123, 'fixture', {}, before_write=validate)
        assert bool(result) is (outcome == 'accept')
    assert events == ['connect', 'validate'] + (['write'] if outcome == 'accept' else []) + ['closed']


def test_bob_ext_locks_deletes_a_lock_whose_window_is_dead(tmp_path, monkeypatch):
    """After a reboot the extension's `deactivate()` never ran, so a dead
    window's lock outlived it. `spawn_agent` addresses one window (`locks[0]`),
    so with the stale and the live lock both naming the same folder, a card
    Start posted to a port nobody listened on and reported "no VS Code window
    could start it" while the window was right there. A dead pid is dropped
    *and* its file removed; a lock without a pid (older extension) is kept."""
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    dead = {"port": 61888, "authToken": "tok", "extHostPid": 2 ** 22 + 12345,
            "workspaceFolders": [str(tmp_path)]}
    live = {"port": 64894, "authToken": "tok", "extHostPid": os.getpid(),
            "workspaceFolders": [str(tmp_path)]}
    old = {"port": 51333, "authToken": "tok", "workspaceFolders": [str(tmp_path)]}
    (tmp_path / "61888.lock").write_text(json.dumps(dead))
    (tmp_path / "64894.lock").write_text(json.dumps(live))
    (tmp_path / "51333.lock").write_text(json.dumps(old))
    ports = sorted(l["port"] for l in vr._bob_ext_locks())
    assert ports == [51333, 64894]
    assert not (tmp_path / "61888.lock").exists()
    assert (tmp_path / "64894.lock").exists()
    assert (tmp_path / "51333.lock").exists()


@pytest.mark.asyncio
async def test_spawn_agent_ignores_a_dead_window_owning_the_same_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    root = str(tmp_path)
    (tmp_path / "61888.lock").write_text(json.dumps({
        "port": 61888, "authToken": "tok", "extHostPid": 2 ** 22 + 12345,
        "extensionVersion": "0.1.11", "workspaceFolders": [root]}))
    (tmp_path / "64894.lock").write_text(json.dumps({
        "port": 64894, "authToken": "tok", "extHostPid": os.getpid(),
        "extensionVersion": "0.1.15", "workspaceFolders": [root]}))
    asked = []

    async def fake_post(port, token, body, timeout=3.0, **kw):
        asked.append(port)
        return {"spawned": True, "terminalName": "agent"}

    monkeypatch.setattr(vr, "_post_json", fake_post)
    reply = await vr.spawn_agent(root, ["claude"], "agent")
    assert reply == {"spawned": True, "terminalName": "agent"}
    assert asked == [64894]


def test_in_vscode_asks_ps_once_per_process_life(tmp_path, monkeypatch):
    """The daemon's steadiest spawn: every push re-forked `ps -E` for every
    session once `_CAN_SEND_TTL` lapsed. A yes is fixed for the life of the
    process (its start environment cannot change), so a simulated minute of
    pushes over six sessions costs six `ps`, not ~70."""
    vr._invalidate_can_send_cache()
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    (tmp_path / "1.lock").write_text(json.dumps(
        {"port": 7777, "authToken": "tok", "extensionVersion": "0.1.9"}))
    spawned = []
    monkeypatch.setattr(vr, "_session_in_vscode", lambda pid: spawned.append(pid) or True)
    started = {pid: 1000.0 + pid for pid in range(101, 107)}
    monkeypatch.setattr(vr, "_process_started", lambda pid: started.get(pid))
    clock = [0.0]
    monkeypatch.setattr(vr.time, "monotonic", lambda: clock[0])
    for _ in range(60):                       # a push a second for a minute
        for pid in started:
            assert vr.can_send_text(pid) is True
            assert vr.can_close_terminal(pid) is True
        clock[0] += 1.0
    assert len(spawned) == len(started)
    # A reused pid is a different process: asked again.
    started[101] = 5000.0
    clock[0] += vr._CAN_SEND_TTL
    assert vr.can_send_text(101) is True
    assert spawned.count(101) == 2


def test_in_vscode_keeps_re_asking_a_no_and_an_unreadable_process(tmp_path, monkeypatch):
    """A no may be a `ps` that failed, and a process whose start time cannot
    be read has no identity to pin: both keep the old expiry."""
    vr._invalidate_can_send_cache()
    asked = []
    monkeypatch.setattr(vr, "_session_in_vscode", lambda pid: asked.append(pid) or pid == 7)
    monkeypatch.setattr(vr, "_process_started", lambda pid: None if pid == 7 else 42.0)
    clock = [0.0]
    monkeypatch.setattr(vr.time, "monotonic", lambda: clock[0])
    for pid in (7, 8):
        assert vr._in_vscode(pid) is (pid == 7)
        assert vr._in_vscode(pid) is (pid == 7)
    assert asked == [7, 8]
    clock[0] += vr._CAN_SEND_TTL
    vr._in_vscode(7)
    vr._in_vscode(8)
    assert asked == [7, 8, 7, 8]
