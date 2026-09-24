"""Behavioural tests of the real `.claude/skills/ship/close-out.sh`.

The seam is a stub loopback HTTP server on an **ephemeral port** (never a fixed
one — that is the `test_api_server.py` contention lesson) plus `HOME` on a temp
path for the token file. That is enough to exercise the script's own ancestry
walk, its reply parsing and its exit codes without a daemon, a terminal or an
editor anywhere near it.

**A finished run leaves its terminal open.** Run bare — the end of every
`/ship` run, for Claude, Codex and Grok alike — the script sends nothing at
all, not even a read, and prints one line the daemon files as "finished,
waiting on nobody". Only `--close`, run when the person asks in words, sends
one close request with today's identity checks; a refusal is reported and
never turned into `wrap_up` / `/clear`. A lost reply is the SIGHUP race — the
daemon disposes the tab before answering — and nothing follows it. Codex must
match one exact row and have an attachment receipt on the daemon.
"""
import json
import os
import shlex
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from dark_army_daemon import session_stats

REPO_SCRIPT = (Path(__file__).resolve().parents[2]
               / ".claude" / "skills" / "ship" / "close-out.sh")
SESSION_ID = "sess-close-out"
CODEX_ID = "01a07176-a834-70a3-8ab5-d1c408a3b8f4"
CODEX_SID = "codex:" + CODEX_ID
CODEX_SKIP = "close-out: terminal left open. No attachment."
LEFT_OPEN_LINE = ("close-out: terminal left open; close it in Dark Army when "
                  "you have read it.")
MARKER = "# close-out-contract: leaves-open"
# The second marker: an installed copy that knows `--plan`.
PLAN_MARKER = "# close-out-mode: plan"
CLOSE = ("--close",)


@pytest.fixture
def script():
    """The script under test: the repo file. The installer's copy is
    byte-identical (`test_the_installed_copy_is_the_repo_script`), so running
    every case a second time against it proved nothing more and doubled the
    file's cost — each case boots bash, ten `python3 -c` and three curls."""
    return REPO_SCRIPT


def test_the_installed_copy_is_the_repo_script(tmp_path, monkeypatch):
    from dark_army_menubar import hooks
    installed = tmp_path / "installer-home" / "dark-army-close-out"
    installed.parent.mkdir()
    monkeypatch.setattr(hooks, "CLOSE_OUT_SCRIPT_PATH", installed)
    monkeypatch.setattr(hooks, "ensure_state_dir", lambda: installed.parent)
    hooks.install_close_out_script()
    assert installed.read_bytes() == REPO_SCRIPT.read_bytes()


class _Stub:
    """One `/api/state` row carrying this test process's own pid, so the
    script's bounded parent walk finds it (bash is the child, pytest the
    grandparent), plus a recorder for every `/api/action` body and a count
    of every read."""

    def __init__(self, replies, rows=None, raw_state=None):
        self.replies = replies          # action -> bytes body (b"" = no body)
        self.rows = rows if rows is not None else {
            "running": [{"session_id": SESSION_ID, "pid": os.getpid()}],
        }
        self.raw_state = raw_state
        self.actions = []               # actions seen, in order
        self.session_ids = []           # session_id on each action, in order
        self.gets = 0                   # every GET, whatever its path
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                stub.gets += 1
                body = (stub.raw_state if stub.raw_state is not None
                        else json.dumps({"agents": stub.rows}).encode())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                action = payload.get("action")
                stub.actions.append(action)
                stub.session_ids.append(payload.get("session_id"))
                body = stub.replies.get(action, b"")
                if body is None:  # dropped transport, with no HTTP response
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        return Handler

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _run(script, home, port=None, extra_env=None, args=()):
    env = dict(os.environ, HOME=str(home))
    # A live provider identity would divert legacy cases from the pid walk.
    # A leaked shim guard would skip the hand-off. Clear before test overrides.
    for name in ("GROK_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                 "BOB_CLOSE_OUT_SHIM", "SCRATCH"):
        env.pop(name, None)
    if port is not None:
        env["BOB_COMPANION_API_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(script), *args], env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".dark-army").mkdir()
    (tmp_path / ".dark-army" / "api-token").write_text("tok")
    return tmp_path


_EVERY_REPLY = {
    "close_terminal": json.dumps({"ok": True}).encode(),
    "close_refinement_terminal": json.dumps({"ok": True}).encode(),
    "wrap_up": json.dumps({"ok": True}).encode(),
}


#: A snapshot with no row for any ancestor of the test: a real Grok's tools
#: parent up to PID 1, so its ancestry walk finds nothing.
NO_ANCESTOR = {"running": []}


def _identity(provider):
    """(env, rows, raw_state) that would tempt each provider's close: the
    Claude ancestry row, a Grok session the board lists as a refinement, a
    valid Codex thread with a `can_close: True` row."""
    if provider == "claude":
        return {}, None, None
    if provider == "grok":
        return {"GROK_SESSION_ID": "grok-sess-1"}, None, json.dumps({
            "agents": {"running": []},
            "board": {"cards": [{"id": "c1", "column_name": "backlog",
                                 "refine_session_id": "grok-sess-1"}]},
        }).encode()
    return {"CODEX_THREAD_ID": CODEX_ID}, _codex_rows(can_close=True), None


# ── the default: close nothing, read nothing ────────────────────────────────


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_default_never_closes_or_clears(script, home, provider):
    env, rows, raw = _identity(provider)
    stub = _Stub(dict(_EVERY_REPLY), rows, raw)
    try:
        result = _run(script, home, stub.port, env)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == []
    assert stub.gets == 0
    assert result.stdout == LEFT_OPEN_LINE + "\n"


def test_default_line_is_the_one_the_daemon_reads(script, tmp_path):
    """The script's `echo` and `session_stats.CLOSE_OUT_LEFT_OPEN` are one
    contract: the line is what files a finished plan or scout run under Idle."""
    result = _run(script, tmp_path)
    line = result.stdout.strip()
    assert session_stats.CLOSE_OUT_LEFT_OPEN in line
    assert session_stats.quiet_close_out(line)
    assert line.startswith("close-out:")          # the Grok demote keys on it
    assert "Dark Army" in line


def test_the_script_never_mentions_wrap_up_and_carries_its_marker():
    text = REPO_SCRIPT.read_text()
    assert "wrap_up" not in text
    assert text.splitlines().count(MARKER) == 1


def test_unknown_option_closes_nothing(script, home):
    stub = _Stub(dict(_EVERY_REPLY))
    try:
        result = _run(script, home, stub.port, args=("--force",))
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == [] and stub.gets == 0
    assert result.stdout == "close-out: unknown option --force; terminal left open.\n"


# ── --close: the person asked ───────────────────────────────────────────────


def test_close_flag_success_sends_one_close(script, home):
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(script, home, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert result.stdout.strip() == "close-out: closed the terminal."
    assert stub.actions == ["close_terminal"]
    assert stub.session_ids == [SESSION_ID]


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_close_flag_refusal_never_clears(script, home, provider):
    env, rows, raw = _identity(provider)
    detail = "That session is waiting on a permission prompt."
    refusal = json.dumps({"ok": False, "detail": detail}).encode()
    stub = _Stub({"close_terminal": refusal, "close_refinement_terminal": refusal,
                  "wrap_up": json.dumps({"ok": True}).encode()}, rows, raw)
    try:
        result = _run(script, home, stub.port, env, args=CLOSE)
    finally:
        stub.close()
    expected = "close_refinement_terminal" if provider == "codex" else "close_terminal"
    assert result.returncode == 0
    assert stub.actions == [expected]
    assert "wrap_up" not in stub.actions
    assert result.stdout.strip() == "close-out: terminal left open. " + detail


def test_close_flag_closes_a_grok_build(script, home):
    """A person asked, so a Grok session the board does *not* list as a
    refinement closes too."""
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()},
                 NO_ANCESTOR)
    try:
        result = _run(script, home, stub.port,
                      {"GROK_SESSION_ID": "grok-build-1"}, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == ["close_terminal"]
    assert stub.session_ids == ["grok-build-1"]
    assert result.stdout.strip() == "close-out: closed the terminal."



def test_an_inherited_grok_id_does_not_steer_a_claude_close(script, home):
    """The id leaks into other sessions' environments. A Claude session that
    inherited one, asked to close, closes its own tab by ancestry."""
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(script, home, stub.port,
                      {"GROK_SESSION_ID": "grok-elsewhere"}, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == ["close_terminal"]
    assert stub.session_ids == [SESSION_ID]

def test_a_lost_reply_is_the_race_and_clears_nothing(script, home):
    """The close took this script's own process group with it. Anything
    sent after it would be the double-act bug."""
    stub = _Stub({})                       # empty body for the close POST
    try:
        result = _run(script, home, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == ["close_terminal"]
    assert "no valid reply came back" in result.stdout
    assert len(result.stdout.strip().splitlines()) == 1


def test_grok_ship_skill_runs_the_same_close_out():
    skill = (Path(__file__).resolve().parents[2]
             / ".agents" / "skills" / "ship" / "SKILL.md")
    text = skill.read_text()
    assert "close-out.sh" in text
    assert "GROK_SESSION_ID" in text


def test_no_token_means_one_line_and_no_traffic(script, tmp_path):
    stub = _Stub({})
    try:
        result = _run(script, tmp_path, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert "no Dark Army token" in result.stdout
    assert stub.actions == []


# ── the hand-off to Dark Army's installed copy ─────────────────────────────────────
#
# Dark Army installs the current script at ~/.dark-army/dark-army-close-out
# on every launch; a project's copy execs it, arguments and all, when it is
# there **and carries the contract marker** — an installed copy from before
# the leave-open rule would close the tab. The installed copy is these same
# bytes, hence the guard.


def _installed(home, text, mode=0o755, folder=".dark-army",
               name="dark-army-close-out"):
    path = home / folder / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _marked(body):
    return "#!/bin/bash\n" + MARKER + "\n" + PLAN_MARKER + "\n" + body


# A Mac still on the old install has only the old folder and the old name:
# a project copy updated first must still hand off there and read the token
# there, and never create the new folder.


def test_a_mac_not_yet_moved_hands_off_to_the_old_installed_copy(tmp_path):
    (tmp_path / ".bob-companion").mkdir()
    (tmp_path / ".bob-companion" / "api-token").write_text("tok")
    _installed(tmp_path, _marked('echo old-handed-off "$BOB_CLOSE_OUT_SHIM"\n'),
               folder=".bob-companion", name="bob-companion-close-out")
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, tmp_path, stub.port)
    finally:
        stub.close()
    assert result.stdout.strip() == "old-handed-off 1"
    assert not (tmp_path / ".dark-army").exists()


def test_a_mac_not_yet_moved_reads_the_old_token(tmp_path):
    (tmp_path / ".bob-companion").mkdir()
    (tmp_path / ".bob-companion" / "api-token").write_text("tok")
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, tmp_path, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert "no Dark Army token" not in result.stdout
    assert stub.actions == ["close_terminal"]


def test_the_new_installed_copy_wins_over_the_old_one(home):
    _installed(home, _marked('echo new\n'))
    _installed(home, _marked('echo old\n'),
               folder=".bob-companion", name="bob-companion-close-out")
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, home, stub.port)
    finally:
        stub.close()
    assert result.stdout.strip() == "new"


def test_the_shim_hands_off_to_the_installed_copy(home):
    _installed(home, _marked('echo handed-off "$BOB_CLOSE_OUT_SHIM"\n'))
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, home, stub.port)
    finally:
        stub.close()
    assert result.returncode == 0
    assert result.stdout.strip() == "handed-off 1"
    assert stub.actions == []


def test_the_shim_forwards_the_close_flag(home):
    _installed(home, _marked('echo args "$#" "$@"\n'))
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, home, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert result.stdout.strip() == "args 1 --close"
    assert stub.actions == []


def test_the_shim_skips_an_installed_copy_without_the_marker(home):
    """An installed helper from before the leave-open rule closes the tab.
    A project copy updated before Dark Army is relaunched runs its own body
    instead, and its default closes nothing."""
    old = ('#!/bin/bash\n'
           'curl -s -X POST "http://127.0.0.1:$BOB_COMPANION_API_PORT/api/action" '
           '-H "X-Bob-Token: tok" -d \'{"action": "close_terminal", '
           '"session_id": "sess-close-out"}\' >/dev/null\n'
           'echo old-closed\n')
    _installed(home, old)
    stub = _Stub(dict(_EVERY_REPLY))
    try:
        result = _run(REPO_SCRIPT, home, stub.port)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == [] and stub.gets == 0
    assert result.stdout == LEFT_OPEN_LINE + "\n"


def test_the_installed_copy_does_not_hand_off_to_itself(home):
    """The installed copy is byte-identical to the repo file. Without the
    guard it execs itself for ever; the 60s timeout in `_run` is the loop
    detector."""
    _installed(home, REPO_SCRIPT.read_text())
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, home, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert "closed the terminal" in result.stdout
    assert stub.actions == ["close_terminal"]


def test_a_non_executable_installed_copy_is_ignored(home):
    _installed(home, REPO_SCRIPT.read_text(), mode=0o644)
    stub = _Stub({"close_terminal": json.dumps({"ok": True}).encode()})
    try:
        result = _run(REPO_SCRIPT, home, stub.port, args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == ["close_terminal"]


# Codex identity is exact, and never implies ownership of its terminal. Every
# Codex case runs under `--close`: the default reads nothing at all.

def _codex_rows(**fields):
    return {"waiting": [{"session_id": CODEX_SID, "pid": None, **fields}],
            "running": [{"session_id": SESSION_ID, "pid": os.getpid(),
                         "cwd": os.getcwd(), "can_close": True}]}


def _assert_codex(result, stub, expected, *, action=False):
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.strip() == expected
    assert len(result.stdout.strip().splitlines()) == 1
    assert stub.actions == (["close_refinement_terminal"] if action else [])
    assert stub.session_ids == ([CODEX_SID] if action else [])


@pytest.mark.parametrize("identity", [
    {"CODEX_THREAD_ID": CODEX_ID},
    {"CODEX_SESSION_ID": CODEX_ID},
    {"CODEX_THREAD_ID": CODEX_ID, "CODEX_SESSION_ID": CODEX_ID},
    {"CODEX_THREAD_ID": CODEX_SID},
    {"CODEX_SESSION_ID": CODEX_SID.upper().replace("CODEX:", "codex:")},
    {"CODEX_THREAD_ID": CODEX_ID.upper(), "CODEX_SESSION_ID": CODEX_SID},
])
def test_codex_exact_environment_identity_with_null_pid(script, home, identity):
    stub = _Stub({"close_refinement_terminal": b'{"ok": false, "detail": "No attachment."}'}, _codex_rows(can_close=False))
    try:
        result = _run(script, home, stub.port, identity, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, CODEX_SKIP, action=True)


@pytest.mark.parametrize("capability", [
    {}, {"can_close": False}, {"can_close": None}, {"can_close": 0},
    {"can_close": 1}, {"can_close": "true"}, {"can_close": "false"},
    {"can_close": []}, {"can_close": {"allowed": True}},
])
def test_codex_scoped_close_ignores_generic_capability(script, home, capability):
    stub = _Stub({"close_refinement_terminal": b'{"ok": false, "detail": "No attachment."}'}, _codex_rows(**capability))
    try:
        result = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, CODEX_SKIP, action=True)


@pytest.mark.parametrize("identity,reason", [
    ({"CODEX_THREAD_ID": ""}, "invalid Codex session identity"),
    ({"CODEX_SESSION_ID": ""}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": CODEX_ID, "CODEX_SESSION_ID": ""}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": "not-a-thread"}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": "codex:" + CODEX_SID}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": " " + CODEX_ID}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": CODEX_ID + "\n"}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": CODEX_ID.replace("-", "")}, "invalid Codex session identity"),
    ({"CODEX_THREAD_ID": CODEX_ID, "CODEX_SESSION_ID": "11a07176-a834-70a3-8ab5-d1c408a3b8f4"},
     "conflicting Codex session identities"),
])
def test_invalid_codex_identity_never_falls_through_to_ancestor(script, home, identity, reason):
    stub = _Stub({}, _codex_rows(can_close=True))
    try:
        result = _run(script, home, stub.port, identity, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, f"close-out: {reason}; left open.")


@pytest.mark.parametrize("duplicate", [False, True])
def test_codex_requires_one_published_match(script, home, duplicate):
    rows = _codex_rows(can_close=True)
    if duplicate:
        rows["finished"] = list(rows["waiting"])
        expected = "close-out: multiple rows match this Codex session; left open."
    else:
        rows["waiting"] = [{"session_id": "codex:11a07176-a834-70a3-8ab5-d1c408a3b8f4",
                            "cwd": os.getcwd(), "can_close": True}]
        expected = "close-out: this Codex session is not in Dark Army's snapshot; left open."
    stub = _Stub({}, rows)
    try:
        result = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, expected)


def test_codex_ignores_malformed_rows_and_non_list_buckets(script, home):
    rows = _codex_rows(can_close=False)
    rows["waiting"][:0] = [None, 7, "row", [], {"session_id": []}, {}]
    rows["finished"] = {"session_id": CODEX_SID, "can_close": True}
    rows["idle"] = None
    stub = _Stub({"close_refinement_terminal": b'{"ok": false, "detail": "No attachment."}'}, rows)
    try:
        result = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, CODEX_SKIP, action=True)


@pytest.mark.parametrize("rows", [[], "bad", 1])
def test_codex_unreadable_snapshot_leaves_terminal_alone(script, home, rows):
    stub = _Stub({}, rows)
    try:
        result = _run(script, home, stub.port, {"CODEX_SESSION_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, "close-out: unreadable Dark Army snapshot; Codex session left open.")


@pytest.mark.parametrize("detail", [
    "no window owns that terminal",
    "That session is waiting on a permission prompt",
    "Codex terminal identity no longer matches",
    "",
    "First line\nsecond line",
])
def test_codex_close_refusal_preserves_history_and_detail(script, home, detail):
    stub = _Stub({"close_refinement_terminal": json.dumps({"ok": False, "detail": detail}).encode()},
                 _codex_rows(can_close=True))
    try:
        result = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    clean_detail = " ".join(detail.split())
    expected = "close-out: terminal left open." + (" " + clean_detail if clean_detail else "")
    _assert_codex(result, stub, expected, action=True)


@pytest.mark.parametrize("reply", [
    b"", b"not-json", b"null", b"[]", b"{}", b'{"ok": "false"}', b'{"ok": 1}', None,
])
def test_codex_unconfirmed_close_is_uncertain_and_never_clears(script, home, reply):
    stub = _Stub({"close_refinement_terminal": reply}, _codex_rows(can_close=True))
    try:
        result = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub,
                  "close-out: asked Dark Army to close this terminal; no valid reply came back. "
                  "The terminal may have closed; no retry or clear was sent.", action=True)


def test_codex_permitted_close_uses_only_exact_session_id(script, home):
    stub = _Stub({"close_refinement_terminal": b'{"ok": true}'}, _codex_rows(can_close=True))
    try:
        result = _run(script, home, stub.port, {"CODEX_SESSION_ID": CODEX_SID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, "close-out: closed the terminal.", action=True)


@pytest.mark.parametrize("can_close", [False, True])
def test_ancestry_discovered_codex_never_clears(script, home, can_close):
    rows = {"running": [{"session_id": CODEX_SID, "pid": os.getpid(), "can_close": can_close}]}
    stub = _Stub({"close_refinement_terminal": b'{"ok": false, "detail": "not owned"}'}, rows)
    try:
        result = _run(script, home, stub.port, args=CLOSE)
    finally:
        stub.close()
    expected = "close-out: terminal left open. not owned"
    _assert_codex(result, stub, expected, action=True)


def test_inherited_codex_identity_is_isolated_but_overrides_still_apply(script, home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "inherited-invalid")
    monkeypatch.setenv("CODEX_SESSION_ID", CODEX_ID)
    stub = _Stub({"close_terminal": b'{"ok": true}',
                  "close_refinement_terminal": b'{"ok": false, "detail": "No attachment."}'},
                 _codex_rows(can_close=False))
    try:
        legacy = _run(script, home, stub.port, args=CLOSE)
        explicit = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    assert legacy.returncode == explicit.returncode == 0
    assert legacy.stdout.strip() == "close-out: closed the terminal."
    assert explicit.stdout.strip() == CODEX_SKIP
    assert legacy.stderr == explicit.stderr == ""
    assert stub.actions == ["close_terminal", "close_refinement_terminal"]
    assert stub.session_ids == [SESSION_ID, CODEX_SID]


def test_grok_precedence_survives_codex_environment_and_never_clears(script, home):
    grok_id = 'grok-quoted-"id'
    stub = _Stub({"close_terminal": b'{"ok": false}', "wrap_up": b'{"ok": true}'},
                 NO_ANCESTOR)
    try:
        result = _run(script, home, stub.port,
                      {"GROK_SESSION_ID": grok_id, "CODEX_THREAD_ID": "invalid"},
                      args=CLOSE)
    finally:
        stub.close()
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.strip() == "close-out: terminal left open."
    assert stub.actions == ["close_terminal"]
    assert stub.session_ids == [grok_id]


def test_codex_snapshot_parser_recursion_failure_never_permits_close(script, home):
    # The exact row forbids Close. An unrelated deep value crashes json.load
    # before it reaches that row; a failed parser must never grant permission.
    state = json.dumps({"agents": _codex_rows(can_close=False)})[:-1].encode()
    state += b', "unrelated": ' + b'[' * 10000 + b'0' + b']' * 10000 + b'}'
    stub = _Stub({"close_terminal": b'{"ok": true}'}, raw_state=state)
    try:
        result = _run(script, home, stub.port, {"CODEX_THREAD_ID": CODEX_ID}, args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, "close-out: could not verify Codex close permission; left open.")


@pytest.mark.parametrize("stage,pattern,reason", [
    ("normalizer", "ids = []", "could not validate Codex session identity"),
    ("gate", "matches = [", "could not verify Codex close permission"),
])
@pytest.mark.parametrize("exit_code", [0, 23])
def test_codex_parser_empty_output_is_never_permission_or_ancestry_fallback(
        script, home, tmp_path, stage, pattern, reason, exit_code):
    # Inject a process failure at either Python seam without replacing bash or
    # HTTP. The real legacy ancestor and Codex row both tempt a close attempt.
    real_python = shutil.which("python3")
    assert real_python
    bin_dir = tmp_path / (stage + "-bin")
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    wrapper.write_text(
        '#!/bin/bash\ncase "$2" in\n*' + shlex.quote(pattern) + '*)\n'
        '  echo "simulated parser diagnostic" >&2\n'
        f'  exit {exit_code} ;;\nesac\n'
        f'exec {shlex.quote(real_python)} "$@"\n'
    )
    wrapper.chmod(0o755)
    stub = _Stub({"close_terminal": b'{"ok": true}'}, _codex_rows(can_close=True))
    try:
        result = _run(script, home, stub.port,
                      {"CODEX_SESSION_ID": CODEX_ID,
                       "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
                      args=CLOSE)
    finally:
        stub.close()
    _assert_codex(result, stub, f"close-out: {reason}; left open.")


@pytest.mark.asyncio
@pytest.mark.parametrize("shim", [False, True])
async def test_running_codex_production_handoff(home, monkeypatch, refinement_handoff, shim):
    import asyncio
    from dark_army_daemon import api_server
    from dark_army_menubar import hooks
    d, store, card, plan, records, processes, posts = refinement_handoff
    attached, detail = await d.attach_plan_by_session(CODEX_SID, str(plan))
    assert attached and attached["column_name"] == "backlog", detail
    monkeypatch.setattr(api_server, "API_TOKEN_PATH", home / ".dark-army/api-token")
    monkeypatch.setattr(api_server, "ensure_state_dir", lambda: home / ".dark-army")
    server = api_server.ApiServer(d, host="127.0.0.1", port=0)
    await server.start()
    try:
        # The production enricher, never a fabricated can_close=True row.
        snapshot = await asyncio.to_thread(d.detailed_snapshot)
        server._agents = snapshot
        rows = snapshot["running"]
        assert {r["session_id"] for r in rows} == {r.session_id for r in records}
        assert all(r["pid"] is None and r["can_close"] is False
                   and r["can_stop"] is False and r["can_type"] is False for r in rows)
        if shim:
            installed = home / ".dark-army/dark-army-close-out"
            monkeypatch.setattr(hooks, "CLOSE_OUT_SCRIPT_PATH", installed)
            monkeypatch.setattr(hooks, "ensure_state_dir", lambda: installed.parent)
            hooks.install_close_out_script()
            assert installed.read_bytes() == REPO_SCRIPT.read_bytes()
        port = server._server.sockets[0].getsockname()[1]
        result = await asyncio.to_thread(_run, REPO_SCRIPT, home, port,
                                         {"CODEX_THREAD_ID": CODEX_ID}, CLOSE)
        assert result.returncode == 0 and result.stderr == ""
        assert len(posts) == 1, result.stdout
        assert posts[0]["op"] == "close_refinement_terminal"
        assert posts[0]["pid"] == processes[0].pid
        assert records[1].session_id in d._codex_records
        kept = store.get(card["id"])
        assert kept["column_name"] == "backlog" and kept["plan_path"] == str(plan)
    finally:
        await server.stop()


# Imported fixture has one owner and is shared with the receipt/action cases.
from tests.test_board_refine import refinement_handoff  # noqa: E402,F401 — pytest fixture
