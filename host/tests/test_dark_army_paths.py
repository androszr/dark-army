"""Dark Army's installed paths, and what still names the old folder.

The names the helpers are installed under; the daemon and the kill switch
recognising Dark Army's own processes by their current names alone and a
stranger — the old package name included — as a stranger; a hook group
naming the old folder's helper path being nobody's now (left in place, never
pruned as ours); the status line replaced and never chained to itself; one
bridge per window; and every remaining `.bob-companion` literal in runtime
source accounted for — the per-project key folder, and the walk-up that skips
the home directory because `~/.bob-companion` stays there as a link. Every
path is a temp one; every subprocess is stubbed.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

import dark_army_daemon.paths as paths
from dark_army_daemon import daemon as daemon_mod
from dark_army_menubar import (channel_install, hooks, kill_switch,
                                   launchd, logsetup, statusline,
                                   vscode_extension)

REPO = Path(__file__).resolve().parents[2]


# ── the names ────────────────────────────────────────────────────────────────


def test_the_installed_names_are_dark_army():
    assert paths.STATE_DIR.name == ".dark-army"
    assert paths.NOTIFY_SCRIPT_PATH.name == "dark-army-notify"
    assert paths.SHUNT_SCRIPT_PATH.name == "dark-army-shunt"
    assert paths.STATUSLINE_SCRIPT_PATH.name == "dark-army-statusline"
    assert paths.CLOSE_OUT_SCRIPT_PATH.name == "dark-army-close-out"
    assert channel_install.CHANNEL_SCRIPT.name == "dark-army-channel"
    assert str(logsetup.log_file_path()).endswith("DarkArmy/dark-army.log")
    assert launchd.PLIST_LABEL == "com.dark-army.menubar"
    assert hooks.GROK_HOOKS_PATH.name == "dark-army.json"
    assert hooks.GROK_RULES_PATH.name == "dark-army.md"
    assert vscode_extension.EXTENSION_ID == "dark-army.dark-army-ide"


def test_our_own_frozen_daemon_is_recognised_under_the_new_name():
    proc = types.SimpleNamespace(
        cmdline=lambda: ["/Applications/Dark Army.app/Contents/MacOS/Dark Army"],
        exe=lambda: "/Applications/Dark Army.app/Contents/MacOS/Dark Army")
    assert daemon_mod._looks_like_bob_daemon(proc) is True
    stranger = types.SimpleNamespace(cmdline=lambda: ["/usr/bin/safari"],
                                     exe=lambda: "/usr/bin/safari")
    assert daemon_mod._looks_like_bob_daemon(stranger) is False


def _proc(argv, exe):
    return types.SimpleNamespace(cmdline=lambda: list(argv), exe=lambda: exe)


def test_words_on_a_command_line_never_make_a_daemon():
    """Free text is anybody's: an agent whose prompt names the product, a
    `tail` of the log folder. Only the executable and `-m` count."""
    claude = "/Users/someone/.local/bin/claude"
    for argv, exe in (
            ([claude, "--agents", '{"mission-control": "Dark Army\'s chief of staff"}'], claude),
            ([claude, "-p", "Dark Army daemon, dark_army_daemon"], claude),
            (["tail", "-f", "/Users/someone/Library/Logs/DarkArmy/x.log"], "/usr/bin/tail"),
            (["python3", "-m", "dark_army_daemon.pty_broker"], "/usr/bin/python3"),
            (["python3", "-mdark_army_daemon"], "/usr/bin/python3"),
            (["python3", "-m", "dark_army_menubarish.app"], "/usr/bin/python3"),
            (["/Applications/Dark Army.app.backup/Contents/MacOS/Dark Army x"],
             "/Applications/Dark Army.app.backup/Contents/MacOS/Dark Army x")):
        assert daemon_mod._looks_like_bob_daemon(_proc(argv, exe)) is False, argv


def test_our_frozen_app_and_source_runs_are_still_recognised():
    for argv, exe in (
            (["/Applications/Dark Army.app/Contents/MacOS/Dark Army"],
             "/Applications/Dark Army.app/Contents/MacOS/Dark Army"),
            # The LaunchAgent's command line, and a Restart's: the bundle's python.
            (["/Applications/Dark Army.app/Contents/MacOS/python", "-m",
              "dark_army_menubar.app"],
             "/Applications/Dark Army.app/Contents/MacOS/python"),
            (["/usr/bin/python3", "-m", "dark_army_menubar"], "/usr/bin/python3"),
            (["/usr/bin/python3", "-m", "dark_army_menubar.app"], "/usr/bin/python3"),
            (["/usr/bin/python3", "-m", "dark_army_daemon"], "/usr/bin/python3"),
            (["/usr/bin/python3", "-m", "dark_army_daemon.daemon"], "/usr/bin/python3")):
        assert daemon_mod._looks_like_bob_daemon(_proc(argv, exe)) is True, argv


def test_a_daemon_under_the_old_package_name_is_a_stranger():
    """Only the current module names count. A source run under any other
    package — the one before the 22 Sep 2026 rename included, which this
    file may not spell (the product-name guard proves it absent from the
    tree) — is a stranger when it is not inside the bundle. The installed
    app's own processes are recognised by the bundle, never by the module."""
    for module in ("somebody_elses_menubar.app", "somebody_elses_daemon.daemon",
                   "http.server"):
        stranger = _proc(["/usr/bin/python3", "-m", module], "/usr/bin/python3")
        assert daemon_mod._looks_like_bob_daemon(stranger) is False, module
    assert daemon_mod._OUR_DAEMON_MODULES == (
        "dark_army_menubar", "dark_army_menubar.app",
        "dark_army_daemon", "dark_army_daemon.daemon")
    # Whatever module a process inside the bundle runs, the bundle vouches.
    inside = _proc(["/Applications/Dark Army.app/Contents/MacOS/python", "-m",
                    "somebody_elses_menubar.app"],
                   "/Applications/Dark Army.app/Contents/MacOS/python")
    assert daemon_mod._looks_like_bob_daemon(inside) is True


def test_a_process_that_only_names_the_state_folder_is_not_our_daemon():
    """`dark-army` is the state folder's name: the channel helper and an
    editor holding a file there are not a daemon to take over from."""
    home = "/Users/someone/.dark-army"
    for argv, exe in (
            (["python3", f"{home}/dark-army-channel"], "/usr/bin/python3"),
            (["vim", f"{home}/preferences.json"], "/usr/bin/vim")):
        proc = types.SimpleNamespace(cmdline=lambda a=argv: a, exe=lambda e=exe: e)
        assert daemon_mod._looks_like_bob_daemon(proc) is False, argv


def test_the_kill_switch_knows_a_helper_by_its_one_name(tmp_path):
    state = tmp_path / ".dark-army"
    state.mkdir()
    (state / "dark-army-channel").write_text("")
    assert kill_switch.owns(["python3", str(state / "dark-army-channel")],
                            state_dir=str(state)) == "dark-army-channel"
    # The old helper name is no longer one of Dark Army's, even inside the
    # state folder.
    (state / "bob-companion-channel").write_text("")
    assert kill_switch.owns(["python3", str(state / "bob-companion-channel")],
                            state_dir=str(state)) == ""
    assert kill_switch.STATE_HELPERS == (
        "dark-army-channel", "dark-army-notify", "dark-army-close-out")


# ── Claude Code's settings: one Dark Army group per event ────────────────────


@pytest.fixture
def harness(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    grok_hooks = tmp_path / "grok-hooks"
    grok_rules = tmp_path / "grok-rules"
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(hooks, "GROK_HOOKS_DIR", grok_hooks)
    monkeypatch.setattr(hooks, "GROK_HOOKS_PATH", grok_hooks / "dark-army.json")
    monkeypatch.setattr(hooks, "GROK_RULES_DIR", grok_rules)
    monkeypatch.setattr(hooks, "GROK_RULES_PATH", grok_rules / "dark-army.md")
    monkeypatch.setattr(hooks, "GROK_CONFIG_PATH", tmp_path / "grok-config.toml")
    monkeypatch.setattr(hooks, "CODEX_CONFIG_PATH", tmp_path / "codex-config.toml")
    monkeypatch.setattr(hooks, "CODEX_HOOKS_PATH", tmp_path / "codex-hooks.json")
    monkeypatch.setattr(statusline, "CLAUDE_SETTINGS_PATH", settings)
    return {"settings": settings, "grok_hooks": grok_hooks,
            "grok_rules": grok_rules, "tmp": tmp_path}


def _commands(groups) -> list[str]:
    return [h.get("command", "") for g in groups for h in g.get("hooks", [])]


def test_an_old_path_hook_group_is_nobodys():
    """A hook group naming the helper under the old folder is not Dark
    Army's any more: the rule that pruned it retired with the move. So it
    is treated as the person's own — left exactly where it is, beside our
    group — which is the documented consequence of retiring that rule."""
    old = f"{Path.home()}/.bob-companion/bob-companion-notify"
    for command in (old, f"/usr/bin/env -u PYTHONHOME python3 {old}",
                    f"{old} --flag"):
        assert hooks._command_is_ours(command) is False, command
    group = {"hooks": [{"type": "command", "command": old}]}
    merged = hooks._merge_hook_config({"Stop": [json.loads(json.dumps(group))]},
                                      hooks.HOOKS_CONFIG)
    assert merged["Stop"][0] == group
    stop = _commands(merged["Stop"])
    assert old in stop and hooks.HOOK_COMMAND in stop


# ── the status line: replaced, never chained ────────────────────────────────


@pytest.fixture
def status(harness, tmp_path, monkeypatch):
    script = tmp_path / ".dark-army" / "dark-army-statusline"
    script.parent.mkdir(exist_ok=True)
    chain = script.parent / "statusline-chain"
    monkeypatch.setattr(statusline, "STATUSLINE_SCRIPT_PATH", script)
    monkeypatch.setattr(statusline, "STATUSLINE_CHAIN_PATH", chain)
    monkeypatch.setattr(statusline, "STATUSLINE_COMMAND", str(script))
    monkeypatch.setattr(statusline, "ensure_state_dir", lambda: script.parent)
    return {"settings": harness["settings"], "script": script, "chain": chain}


def test_the_persons_own_status_line_is_still_chained(status):
    status["settings"].write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "~/bin/my-line"}}))
    statusline.install_statusline()
    assert status["chain"].read_text().strip() == "~/bin/my-line"


def _collector_home(tmp_path):
    """A home with the collector installed at its name."""
    home = tmp_path / "home"
    state = home / ".dark-army"
    state.mkdir(parents=True)
    script = state / "dark-army-statusline"
    script.write_text(statusline.STATUSLINE_SCRIPT)
    script.chmod(0o755)
    return home, state, script


def _run_collector(home, script):
    import socket
    import subprocess
    import sys
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]      # nothing will listen there
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "BOB_COMPANION_PORT": str(port)}
    return subprocess.run([sys.executable, str(script)],
                          input=json.dumps({"model": {"display_name": "claude-x"}}),
                          capture_output=True, text=True, timeout=20, env=env)


def test_an_indirect_loop_back_to_the_collector_runs_one_level(tmp_path):
    """A chain that reaches the collector through a wrapper cannot be seen
    by path: the depth guard stops the nested run from chaining again."""
    import sys
    home, state, script = _collector_home(tmp_path)
    counter = tmp_path / "runs"
    wrapper = tmp_path / "their-line.sh"
    wrapper.write_text(f"echo run >> {counter}\nexec {sys.executable} {script}\n")
    (state / "statusline-chain").write_text(f"sh {wrapper}\n")
    done = _run_collector(home, script)
    assert done.returncode == 0 and done.stdout.strip(), done.stderr
    assert counter.read_text().count("run") == 1


def test_install_drops_a_chain_that_names_our_collector(status):
    status["chain"].write_text(statusline.STATUSLINE_COMMAND + "\n")
    status["settings"].write_text(json.dumps({"statusLine": {
        "type": "command", "command": str(status["script"])}}))
    statusline.install_statusline()
    assert not status["chain"].exists()


def test_uninstall_never_restores_our_own_collector(status):
    status["settings"].write_text(json.dumps({"statusLine": {
        "type": "command", "command": str(status["script"])}}))
    status["chain"].write_text(statusline.STATUSLINE_COMMAND + "\n")
    assert statusline.uninstall_statusline() is True
    after = json.loads(status["settings"].read_text())
    assert "statusLine" not in after
    assert not status["chain"].exists()


# ── one bridge per window ────────────────────────────────────────────────────


def test_a_window_running_both_bridges_answers_once(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr
    old = {"port": 5001, "extHostPid": 700, "extensionVersion": "0.1.18",
           "startedAt": 10}
    new = {"port": 5002, "extHostPid": 700, "extensionVersion": "0.1.19",
           "extensionId": vr.DARK_ARMY_EXTENSION_ID, "startedAt": 5}
    other = {"port": 5003, "extHostPid": 800, "extensionVersion": "0.1.18"}
    kept = vr._one_bridge_per_window([old, other, new])
    assert [lock["port"] for lock in kept] == [5002, 5003]
    # Without an id the higher version wins; a pid-less lock is its own window.
    a = {"port": 1, "extHostPid": 9, "extensionVersion": "0.1.18"}
    b = {"port": 2, "extHostPid": 9, "extensionVersion": "0.1.17"}
    c = {"port": 3, "extensionVersion": "0.1.18"}
    d = {"port": 4, "extensionVersion": "0.1.18"}
    assert [lock["port"] for lock in vr._one_bridge_per_window([b, a, c, d])] == [1, 3, 4]


def test_the_extension_survives_a_duplicate_command_and_stamps_its_id():
    ts = (REPO / "vscode-extension/src/extension.ts").read_text()
    # The command and its hidden alias (the dual-name window) are each
    # registered inside a try of their own: a duplicate id from a copy that
    # has not reloaded must neither abort activation nor cost the other id.
    for command in ("darkArmy.showThisSession", "bobCompanion.showThisSession"):
        reg = ts.index(f"registerCommand('{command}'")
        # Inside a try: the nearest brace-opening before it is the `try {`.
        assert ts[:reg].rstrip().rsplit("\n", 3)[-3].strip() == "try {", command
        assert "catch (err)" in ts[reg:reg + 200], command
    assert "extensionId: EXTENSION_ID" in ts
    assert "const EXTENSION_ID = 'dark-army.dark-army-ide'" in ts


# ── what still says .bob-companion, and why ──────────────────────────────────

#: The only lines of runtime source that may still name the old folder.
#: Anything else naming it is a path that did not move with the rest.
_ALLOWED = (
    "KEY_DIR_NAME",                  # the per-project key folder
    "KEY_RELATIVE",
    "GITIGNORE_LINE",
    # The old key folder, read-only behind `.dark-army/key` while the read
    # window is open (plans/2026-09-23-move-enrolment-key-to-dark-army.md).
    "LEGACY_KEY_DIR_NAME",
    "LEGACY_KEY_RELATIVE",
    "KEY_DIR_NAMES",
    # The shunt wrappers' fallback for a Mac whose state folder has not moved.
    "old = os.path.join(home, \".bob-companion\")",
)


def test_every_remaining_old_folder_literal_is_accounted_for():
    offenders = []
    for root in ("host/dark_army_daemon", "host/dark_army_menubar"):
        for path in sorted((REPO / root).rglob("*.py")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if not re.search(r"""["']\.bob-companion["'/]""", code):
                    continue
                if any(token in code for token in _ALLOWED):
                    continue
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert offenders == []


def test_the_panel_and_the_extension_name_the_new_folder():
    swift = (REPO / "panel/Sources/BobPanel/StateDirectory.swift").read_text()
    assert '".dark-army"' in swift and '".bob-companion"' not in swift
    ts = (REPO / "vscode-extension/src/extension.ts").read_text()
    assert "'.dark-army'" in ts
    # The legacy folder only as the fallback for a Mac not yet moved.
    assert ts.index("'.dark-army'") < ts.index("'.bob-companion'")


def test_the_hook_socket_lives_in_the_private_state_folder():
    """The hook door is a file only this account can open: inside the 0700
    folder, and on the 0600 re-check ring beside the other private files."""
    assert paths.HOOK_SOCK_PATH.parent == paths.STATE_DIR
    assert paths.HOOK_SOCK_PATH.name == "hook.sock"
    assert "hook.sock" in paths._PRIVATE_FILES
