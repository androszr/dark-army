"""The hook handler as it really runs: `NOTIFY_SCRIPT` written to a file,
started as a process with a payload on stdin, heard by a stand-in daemon
listening on loopback.
"""

import contextlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from dark_army_menubar.hooks import NOTIFY_SCRIPT
from dark_army_daemon.protocol import hook_payload_to_daemon_message
from tests.free_ports import free_port


def _free_port() -> int:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_port()


def _run_notify_script(payload, port, *, home=None, grok_session=None,
                       env_extra=None, timeout=30.0):
    """NOTIFY_SCRIPT as an installed file, run once with `payload` on stdin.

    The environment is this process's minus two things it may be carrying
    because the suite itself runs inside a session: a Grok session id (the
    script would stamp every message `provider=grok`) and a Dark Army origin
    stamp (copied onto every message). HOME is a temp folder unless given, so
    the pid memo never lands in the real state folder."""
    env = dict(os.environ, BOB_COMPANION_PORT=str(port),
               HOME=str(home) if home is not None else tempfile.mkdtemp())
    # And the live hook socket's address, which a hosted terminal carries and
    # which, named, would be the script's one address over the stand-in port.
    for inherited in ("GROK_SESSION_ID", "BOB_COMPANION_ORIGIN",
                      "DARK_ARMY_HOOK_SOCKET"):
        env.pop(inherited, None)
    if grok_session:
        env["GROK_SESSION_ID"] = grok_session
    env.update(env_extra or {})
    fd, script = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(NOTIFY_SCRIPT)
    os.chmod(script, 0o755)
    try:
        stdin = json.dumps(payload).encode("utf-8")
        return subprocess.run([sys.executable, script], env=env, timeout=timeout,
                              input=stdin, capture_output=True)
    finally:
        os.unlink(script)


def _sent_by_script(payload: dict, port: int, home=None, grok_session=None) -> dict:
    """What the script sends for `payload`: the last JSON line a stand-in
    daemon listening on `port` heard, or {} when nothing arrived.

    Records every connection: a PermissionRequest first sends
    ``permission_ask``, then on silence falls back to the legacy
    ``permission`` message, and tests of that conversion need the last one.
    """
    received = []
    stop = threading.Event()
    # Bound and listening before the script is spawned: binding inside the
    # thread raced the script's connect on a loaded machine, and a missed
    # connection reads as "nobody heard", which is a different test.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    srv.settimeout(0.2)

    def server():
        try:
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                data = b""
                conn.settimeout(2.0)
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    data += chunk
                    # One line per connection: closing on it is Dark Army's "no
                    # reply", and the script moves on at once instead of the
                    # two-second recv timeout every exchange used to cost.
                    if b"\n" in data:
                        break
                conn.close()
                text = data.decode("utf-8").strip()
                if text:
                    received.append(json.loads(text))
        finally:
            srv.close()

    listener = threading.Thread(target=server)
    listener.start()

    try:
        _run_notify_script(payload, port, home=home, grok_session=grok_session)
    finally:
        stop.set()

    listener.join(timeout=3.0)
    return received[-1] if received else {}


# --- What the installed script sends, one hook at a time ---

@pytest.mark.parametrize("hook, expected", [
    ({"hook_event_name": "SessionStart", "session_id": "one", "source": "startup"},
     {"event": "session_start", "source": "startup"}),
    ({"hook_event_name": "Stop", "session_id": "one"}, {"event": "add", "hook": "Stop"}),
    ({"hook_event_name": "SessionEnd", "session_id": "one", "reason": "logout"},
     {"event": "dismiss", "hook": "SessionEnd", "reason": "logout"}),
    ({"hook_event_name": "PostToolUse", "session_id": "one", "tool_name": "AskUserQuestion"},
     {"event": "tool_done", "tool_name": "AskUserQuestion"}),
    ({"hook_event_name": "PermissionRequest", "session_id": "one", "tool_name": "Bash"},
     {"event": "permission", "tool_name": "Bash"}),
    ({"hook_event_name": "PostToolUseFailure", "session_id": "one", "tool_name": "Read"},
     {"event": "tool_failed", "tool_name": "Read"}),
    ({"hookEventName": "stop", "sessionId": "one", "reason": "channel_closed"},
     {"event": "dismiss", "hook": "SessionEnd", "reason": "channel_closed"}),
], ids=["start", "stop", "end", "tool-done", "permission", "tool-failed", "grok-teardown"])
def test_the_installed_script_sends_the_event_with_a_walked_pid(hook, expected, tmp_path):
    sent = _sent_by_script(dict(hook, cwd=str(tmp_path)), _free_port())
    assert {key: sent.get(key) for key in expected} == expected
    assert sent.get("session_id") == "one"
    # The pid is the script's own finding, never the payload's.
    assert isinstance(sent.get("pid"), int) and sent["pid"] > 0


def test_a_hook_the_daemon_has_no_use_for_sends_nothing():
    assert _sent_by_script(
        {"hook_event_name": "AHookFromTheFuture", "session_id": "one"}, _free_port()) == {}


def test_under_grok_the_script_says_so_whatever_the_walk_found(tmp_path):
    sent = _sent_by_script(
        {"hookEventName": "session_start", "sessionId": "g-7", "cwd": str(tmp_path)},
        _free_port(), home=tmp_path, grok_session="g-7")
    assert (sent.get("event"), sent.get("provider"), sent.get("session_id")) == (
        "session_start", "grok", "g-7")


def test_notify_script_does_not_treat_the_grok_leader_as_a_session():
    """Keep the embedded walk in step with pid_resolver.is_grok_leader.

    The script is a string, not an import — a missing regex here is how
    every live Grok hook started stamping the leader pid again.
    """
    assert "agent\\s+leader" in NOTIFY_SCRIPT
    assert "2026-09-23-private-hook-socket" in NOTIFY_SCRIPT
    # Walking past the leader onto its parent TUI is the bug that stamped
    # one tab's pid on every Grok session.
    assert "if prefer_grok:" in NOTIFY_SCRIPT
    assert "break" in NOTIFY_SCRIPT


# --- The script and the daemon-side converter read a payload the same way ---
# The script imports nothing of ours, so it carries its own copy of the
# conversion; every payload below goes through both and must come out equal.

_FOLDER = "/w/sample-app"


def _claude(event, **fields):
    return {"hook_event_name": event, "session_id": "sess-x", "cwd": _FOLDER, **fields}


def _grok(event, **fields):
    return {"hookEventName": event, "sessionId": "sess-x", "cwd": _FOLDER, **fields}


_CROSS_CHECK_HOOKS = [
    _claude("SessionStart", source="startup"),
    _claude("PreToolUse", tool_name="Bash"),
    _claude("PostToolUse", tool_name="AskUserQuestion"),
    # The dialog's words ride PreToolUse, trimmed by each side: long option
    # descriptions clipped, previews left behind, non-ASCII kept.
    _claude("PreToolUse", tool_name="AskUserQuestion", tool_use_id="toolu_77", tool_input={
        "questions": [{
            "question": "Którą poprawkę wdrożyć?",
            "header": "Poprawka",
            "multiSelect": False,
            "options": [
                {"label": "Chip projektu w nagłówku", "description": "d" * 400,
                 "preview": "p" * 900},
                {"label": "Nagłówki projektów w paśmie", "description": "d"},
            ],
        }]}),
    _grok("pre_tool_use", toolName="ask_user_question", toolInput={
        "questions": [{"question": "Pick one?", "options": [{"label": "A"}, {"label": "B"}]}]}),
    _claude("PermissionRequest", tool_name="Bash"),
    _claude("PostToolUseFailure", tool_name="Read"),
    _claude("PreCompact"),
    _claude("Stop"),
    _claude("StopFailure", error="boom"),
    _claude("StopFailure", error="rate_limit"),
    _claude("StopFailure", error="rate_limit", error_details="raw",
            last_assistant_message="You've hit your limit · resets 1:40pm\nmore"),
    _claude("Notification", notification_type="idle_prompt", message="idle now"),
    _claude("UserPromptSubmit"),
    _claude("SessionEnd", reason="logout"),
    _claude("SubagentStart", agent_id="kid"),
    _claude("SubagentStop", agent_id="kid"),
    _grok("session_start", source="startup"),
    _grok("pre_tool_use", toolName="run_terminal_command"),
    _grok("stop", reason="end_turn"),
    _grok("stop_cancelled", reason="user_interrupt"),
    _grok("notification", notificationType="idle_prompt", message="idle now"),
    _grok("notification", notificationType="permission_prompt", toolName="run_terminal_command"),
    _grok("subagent_start", sessionId="child", parentSessionId="parent",
          subagentId="01aabbcc-0000-1111-2222-333344445555", subagentType="bc-planner"),
    _grok("subagent_stop", sessionId="child", parentSessionId="parent", subagentType="bc-planner"),
    _grok("session_end", sessionId="child", parentSessionId="parent", subagentType="bc-planner"),
]


# --- The TL;DR hint: SessionStart stdout becomes the session's context -------
# Claude Code adds a SessionStart hook's stdout to the model's context, which
# is how the bob-tldr instruction reaches every monitored session without
# editing any file the user owns. These tests pin the gates: delivery to a
# live daemon, Claude only, the preference, and SessionStart only.


def _run_script_stdout(payload: dict, port: int, home, listen: bool = True,
                       grok: bool = False, whole: bool = False):
    """Run NOTIFY_SCRIPT with a controlled HOME and return its stdout. With
    ``listen`` a stand-in daemon accepts the delivery, which is the hint's
    only-when-someone-heard gate."""
    def server(srv):
        try:
            conn, _ = srv.accept()
            conn.settimeout(2.0)
            with contextlib.suppress(socket.timeout):
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
            conn.close()
        except socket.timeout:
            pass
        finally:
            srv.close()

    t = None
    if listen:
        # Bound before the script starts (see _sent_by_script).
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(5.0)
        t = threading.Thread(target=server, args=(srv,))
        t.start()

    proc = _run_notify_script(payload, port, home=home,
                              grok_session="g1" if grok else None)
    if t is not None:
        t.join(timeout=3.0)
    if whole:
        return proc
    return proc.stdout.decode("utf-8")


_START = {"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"}


def test_session_start_injects_the_tldr_hint(tmp_path):
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert "<!-- bob-tldr:" in out


def test_session_start_asks_for_the_offered_answers_too(tmp_path):
    """The two ends of one contract: the marker the hint asks for has to be
    the marker `session_stats` parses, or the panel draws no buttons and
    nobody finds out why."""
    from dark_army_daemon import session_stats as ss

    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert "<!-- bob-actions:" in out
    example = out.split("<!-- bob-actions:", 1)[1].split("-->", 1)[0]
    assert ss._parse_actions(example) == ["Accept", "Iterate"]


def test_session_start_prints_both_hints_whatever_the_file_says(tmp_path):
    """A preferences.json holding both hints off still yields both."""
    prefs = tmp_path / ".dark-army"
    prefs.mkdir()
    (prefs / "preferences.json").write_text(
        json.dumps({"tldr_hint": False, "work_report": False}))
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert "<!-- bob-tldr:" in out
    assert "## Work done" in out


# --- The work-done report hint: the sibling instruction, opposite direction --
# TLDR_HINT marks turns waiting on somebody; WORK_REPORT_HINT marks turns
# waiting on nobody — finished work ends with a report the human reads in the
# terminal. Same vehicle, same gates, its own switch.


def _script_namespace() -> dict:
    """Execute NOTIFY_SCRIPT's definitions (main() is guarded by __name__)
    so a test can assert on a constant's own text rather than on combined
    stdout, which legitimately carries TLDR_HINT's marker examples."""
    ns = {"__name__": "dark_army_notify_under_test"}
    exec(compile(NOTIFY_SCRIPT, "dark-army-notify", "exec"), ns)
    return ns


def test_session_start_injects_the_work_report_hint(tmp_path):
    """The labels the hint dictates verbatim are the standardisation, so they
    are pinned literally."""
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert "## Work done" in out
    for label in ("**Asked:**", "**Changed:**", "**Verified:**",
                  "**Unchecked:**", "**Card:**"):
        assert label in out


def test_the_work_report_hint_carries_no_marker_literals():
    """The contract test above splits stdout on the first `<!-- bob-actions:`
    literal — a second example inside this hint would break it. The bare
    words without comment syntax are how the hint names the markers it
    forbids."""
    hint = _script_namespace()["WORK_REPORT_HINT"]
    assert "<!-- bob-tldr:" not in hint
    assert "<!-- bob-actions:" not in hint
    # But it does forbid them by name, so the instruction lands.
    assert "bob-tldr" in hint
    assert "bob-actions" in hint


def test_the_unchecked_section_is_dictated_as_numbered_steps():
    """The whole point of the change: an Unchecked item is instructions for
    somebody who will do the checking, not a hint about the shape of a test.
    The three parts are pinned because they are the convention that has to
    survive being read in a project that has never heard of this plan."""
    hint = _script_namespace()["WORK_REPORT_HINT"]
    lowered = hint.lower()
    # Numbered, imperative, one action per line, naming the surface.
    assert "numbered imperative steps" in lowered
    assert "one action per line" in lowered
    # The required reason line, spelled exactly as the report should carry it.
    assert "Why not automated:" in hint
    assert "required" in lowered


def test_the_work_report_hint_pushes_the_check_back_into_code():
    """The minimisation half. A leftover check has to justify itself against
    the seam that would have automated it, and a report handing back a pile of
    them is told to look again — the two-item soft ceiling, in words."""
    hint = _script_namespace()["WORK_REPORT_HINT"]
    lowered = hint.lower()
    assert "cannot be made in code" in lowered
    assert "more than two" in lowered
    # And the board half: an outstanding check is flagged where a person sees it.
    assert "flagging your card" in lowered


def test_the_hint_is_not_injected_when_no_daemon_heard(tmp_path):
    """No daemon means no row: a standing instruction to annotate for nobody
    is context spent on silence."""
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(),
                             tmp_path, listen=False)
    assert out == ""


def test_the_hint_is_claude_only(tmp_path):
    """Grok's hook contract does not promise that stdout becomes context."""
    out = _run_script_stdout(
        {"hookEventName": "session_start", "sessionId": "g1", "cwd": str(tmp_path)},
        _free_port(), tmp_path, grok=True)
    assert out == ""


def test_the_hint_rides_session_start_only(tmp_path):
    out = _run_script_stdout(
        {"hook_event_name": "Stop", "session_id": "s", "cwd": str(tmp_path)},
        _free_port(), tmp_path)
    assert out == ""


# --- The search-scope hint: the third standing instruction ------------------
# Where an agent may search, with the folders Dark Army knows read fresh from
# ~/.dark-army/search-scope.json at session start (search_scope.py writes it).


def _scope_listing(out: str) -> list:
    """The roots block of the printed hint: the lines between the sentence
    that introduces the folders and the exact-path sentence after it."""
    after = out.split("either direction):", 1)[1]
    block = after.split("Reading a file at a known exact path", 1)[0]
    return [line for line in block.splitlines() if line.strip()]


def _write_scope(home, payload) -> None:
    folder = home / ".dark-army"
    folder.mkdir(exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (folder / "search-scope.json").write_text(text, encoding="utf-8")


def test_the_search_scope_hint_names_the_onboarded_roots(tmp_path):
    """The success criterion: two onboarded folders from a throw-away HOME's
    list arrive verbatim in a new Claude session's opening context, with the
    rule around them."""
    one, two = "/Volumes/Work/alpha-project", "/opt/src/beta-project"
    _write_scope(tmp_path, {"version": 1, "roots": [one, two]})
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert one in out and two in out
    assert _scope_listing(out) == ["  - " + one, "  - " + two]
    for words in ("find", "plan_path", "Dark Army", "~/.dark-army"):
        assert words in out
    # The protected home folders are named as banned, never as a root.
    assert ("Documents, Desktop, Downloads, Pictures, Music, Movies or "
            "Library inside it") in out
    assert not any("Documents" in line for line in _scope_listing(out))


def test_the_search_scope_hint_falls_back_with_no_file(tmp_path):
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert "has not written its folder list yet" in out
    listing = _scope_listing(out)
    assert len(listing) == 1
    assert "/Users" not in listing[0] and str(tmp_path) not in listing[0]


@pytest.mark.parametrize("payload", [{"roots": "x"}, "not json at all {",
                                     {"roots": ["/a", ""]}, ["/a"]])
def test_a_malformed_search_scope_file_falls_back_quietly(tmp_path, payload):
    _write_scope(tmp_path, payload)
    proc = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(),
                              tmp_path, whole=True)
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0
    assert proc.stderr == b""
    assert "has not written its folder list yet" in out
    assert len(_scope_listing(out)) == 1


def test_the_search_scope_hint_comes_after_the_work_report(tmp_path):
    """Printed third, so stdout still splits on the first bob-actions marker
    exactly as before: the marker is TLDR_HINT's, and nothing after it adds
    another."""
    from dark_army_daemon import session_stats as ss

    _write_scope(tmp_path, {"version": 1, "roots": ["/opt/src/alpha"]})
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert out.index("## Work done") < out.index("Where you may search")
    assert out.count("<!-- bob-actions:") == 1
    assert out.count("<!-- bob-tldr:") == 1
    example = out.split("<!-- bob-actions:", 1)[1].split("-->", 1)[0]
    assert ss._parse_actions(example) == ["Accept", "Iterate"]


def test_the_search_scope_hint_carries_no_marker_literals():
    ns = _script_namespace()
    for name in ("SEARCH_SCOPE_HINT", "SEARCH_SCOPE_FALLBACK"):
        assert "<!-- bob-tldr" not in ns[name]
        assert "<!-- bob-actions" not in ns[name]
    # One slot, so the roots are the only thing format() fills.
    assert ns["SEARCH_SCOPE_HINT"].count("{") == 1


def _cross_check_id(hook):
    name = hook.get("hook_event_name") or hook.get("hookEventName")
    extra = (hook.get("notification_type") or hook.get("notificationType")
             or hook.get("tool_name") or hook.get("toolName") or "")
    return f"{name}:{extra}" if extra else name


# Fields only one side can know: the pid (walked by the script, read off the
# payload by the converter), the provider (the script reads it from Grok's
# environment) and the enrolment key (a file inside the project).
_ONE_SIDED = ("pid", "provider", "key")


@pytest.mark.parametrize("hook", _CROSS_CHECK_HOOKS, ids=_cross_check_id)
def test_notify_script_matches_protocol_converter(hook, tmp_path):
    """Both conversions of one payload agree on every field both can know."""
    def comparable(msg):
        return {k: v for k, v in (msg or {}).items() if k not in _ONE_SIDED}

    from_script = _sent_by_script(hook, _free_port())
    assert comparable(from_script) == comparable(hook_payload_to_daemon_message(hook))


def test_the_card_close_is_mandated_by_name():
    """The supply side of the review banner: an agent working a bound card is
    told that finishing the work *means* calling `dark_army_close_card` (or
    `bob_close_card`, in a session born before the rename) — whether or not
    a check is left for a person — with a note distilled from the report
    rather than the report pasted (the store clamps the note at 400
    characters). Pinned by name so the hint cannot drift back to
    report-formatting advice."""
    hint = _script_namespace()["WORK_REPORT_HINT"]
    assert "dark_army_close_card" in hint
    # The dual-name window: a session born under the legacy name has only
    # the `bob_*` spelling, and the hint has to name that one too.
    assert "bob_close_card" in hint
    lowered = hint.lower()
    assert "whether or not" in lowered
    assert "never paste the report" in lowered
    # A card with an open check goes to Done: flag, *then* close.
    assert "then call" in lowered
    assert "do not call it" not in lowered
    assert "only when every check passed" not in lowered
    # The check's home and its checker.
    assert "manual-check/" in hint
    assert "manual_check.py" in hint


# ── the project's enrolment key ────────────────────────────────────────────────
#
# Dark Army only watches projects the user enrolled, and the key that says so is a
# file inside the project. These test the walk-up through `_script_namespace()`
# (the same `exec` seam the hint tests use) plus one subprocess case proving
# the key actually reaches the wire.


def _write_key(root, value: str, folder: str = ".dark-army") -> None:
    folder = root / folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "key").write_text(value, encoding="utf-8")


def test_the_key_walk_finds_a_key_at_the_repo_root(tmp_path):
    ns = _script_namespace()
    root = tmp_path / "proj"
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    _write_key(root, "the-key\n")
    assert ns["_project_key"](str(nested)) == "the-key"


def test_the_key_walk_finds_nothing_when_there_is_no_key(tmp_path):
    ns = _script_namespace()
    nested = tmp_path / "proj" / "a"
    nested.mkdir(parents=True)
    assert ns["_project_key"](str(nested)) == ""


def test_the_key_walk_finds_a_key_under_the_old_name(tmp_path):
    """A project enrolled before the move still holds only
    `.bob-companion/key`; it keeps sending its key for the read window."""
    ns = _script_namespace()
    root = tmp_path / "proj"
    nested = root / "a"
    nested.mkdir(parents=True)
    _write_key(root, "old-key\n", folder=".bob-companion")
    assert ns["_project_key"](str(nested)) == "old-key"


def test_the_key_walk_prefers_the_new_name_when_both_exist(tmp_path):
    ns = _script_namespace()
    root = tmp_path / "proj"
    root.mkdir()
    _write_key(root, "new-key", folder=".dark-army")
    _write_key(root, "old-key", folder=".bob-companion")
    assert ns["_project_key"](str(root)) == "new-key"


def test_the_key_walk_falls_back_past_an_empty_new_key(tmp_path):
    ns = _script_namespace()
    root = tmp_path / "proj"
    root.mkdir()
    _write_key(root, "  \n", folder=".dark-army")
    _write_key(root, "old-key", folder=".bob-companion")
    assert ns["_project_key"](str(root)) == "old-key"


def test_the_key_walk_never_reads_the_home_state_directory(tmp_path,
                                                           monkeypatch):
    """`~/.dark-army` is Dark Army's own state directory and `~/.bob-companion`
    a link to it; both have the name a project's key folder has. Without the
    skip, a session anywhere under $HOME would enrol the whole home directory
    — the most damaging bug available. Planted under **both** names."""
    from pathlib import Path as _Path
    ns = _script_namespace()
    home = tmp_path / "home"
    nested = home / "code" / "somebody-elses-clone"
    nested.mkdir(parents=True)
    _write_key(home, "bobs-own-state-secret", folder=".dark-army")
    _write_key(home, "bobs-old-state-secret", folder=".bob-companion")
    # The script reads the home directory through os.path.expanduser("~").
    monkeypatch.setenv("HOME", str(home))
    assert ns["_project_key"](str(nested)) == ""
    assert isinstance(_Path(str(home)), _Path)


def test_the_delivered_message_carries_the_project_key(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _write_key(root, "wire-key")
    msg = _sent_by_script({
        "hook_event_name": "SessionStart",
        "session_id": "sess-key",
        "cwd": str(root),
    }, _free_port())
    assert msg.get("key") == "wire-key"


def test_the_delivered_message_carries_the_old_name_key(tmp_path):
    """On the wire, too: a project with only the old folder is not dropped."""
    root = tmp_path / "proj"
    root.mkdir()
    _write_key(root, "old-wire-key", folder=".bob-companion")
    msg = _sent_by_script({
        "hook_event_name": "SessionStart",
        "session_id": "sess-old-key",
        "cwd": str(root),
    }, _free_port())
    assert msg.get("key") == "old-wire-key"


def test_an_unenrolled_project_sends_an_empty_key(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    msg = _sent_by_script({
        "hook_event_name": "SessionStart",
        "session_id": "sess-nokey",
        "cwd": str(root),
    }, _free_port())
    assert msg.get("key") == ""


def test_the_delivered_message_carries_the_working_folder(tmp_path):
    """The full folder, not just its basename: the daemon needs it to resolve a
    subdirectory session to its VS Code workspace."""
    root = tmp_path / "proj" / "host"
    root.mkdir(parents=True)
    msg = _sent_by_script({
        "hook_event_name": "SessionStart",
        "session_id": "sess-cwd",
        "cwd": str(root),
    }, _free_port())
    assert msg.get("cwd") == str(root)
    assert msg.get("project") == "host"


def test_the_working_folder_rides_every_shape(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    msg = _sent_by_script({
        "hook_event_name": "PreToolUse",
        "session_id": "sess-cwd-2",
        "tool_name": "Bash",
        "cwd": str(root),
    }, _free_port())
    assert msg.get("event") == "tool_use"
    assert msg.get("cwd") == str(root)


def test_a_payload_with_no_folder_sends_no_cwd_key():
    msg = _sent_by_script({
        "hook_event_name": "SessionStart",
        "session_id": "sess-nocwd",
    }, _free_port())
    assert msg.get("session_id") == "sess-nocwd"
    assert not msg.get("cwd")


# ── one `ps` per hook invocation ─────────────────────────────────────────────
#
# The pid walk used to run up to three `ps -o <field>= -p <pid>` subprocesses
# per ancestor, on every hook event. It now reads the whole table once.


def test_the_script_runs_ps_exactly_once():
    """One `-axo` listing; no per-pid `ps -o field= -p pid` calls left."""
    assert NOTIFY_SCRIPT.count('["ps"') == 1
    assert '"pid=,ppid=,command="' in NOTIFY_SCRIPT
    assert '"-axo"' in NOTIFY_SCRIPT
    assert '"-o", field' not in NOTIFY_SCRIPT


def test_the_process_table_survives_argv_with_spaces(monkeypatch):
    """pid and ppid are the two leading numeric fields; everything after is
    the command, spaces and all."""
    ns = _script_namespace()

    class _Done:
        stdout = (
            "  1     0 /sbin/launchpad\n"
            "  42    1 /Applications/Visual Studio Code.app/Contents/MacOS/Electron\n"
            " 43   42 claude --resume abc\n"
            "garbage line\n"
            " 44\n"
        )

    monkeypatch.setitem(ns, "subprocess", type(
        "S", (), {"run": staticmethod(lambda *a, **k: _Done()),
                  "TimeoutExpired": subprocess.TimeoutExpired}))
    table = ns["_process_table"]()
    assert table[42] == (1, "/Applications/Visual Studio Code.app/Contents/MacOS/Electron")
    assert table[43] == (42, "claude --resume abc")
    assert 44 not in table


class _FakeOS:
    """Just the two things the walk reads from `os`."""
    def __init__(self, ppid, environ=None):
        self._ppid = ppid
        self.environ = environ or {}

    def getppid(self):
        return self._ppid


def _walk(table, ppid, environ=None):
    ns = _script_namespace()
    ns["_process_table"] = lambda: table
    ns["os"] = _FakeOS(ppid, environ)
    return ns["_find_session_pid"]()


def test_the_walk_finds_the_claude_ancestor_from_the_table():
    table = {
        50: (40, "/bin/zsh"),
        40: (30, "node /usr/local/bin/claude"),
        30: (1, "/sbin/launchpad"),
    }
    assert _walk(table, 50) == (40, "claude")


def test_the_walk_still_breaks_at_the_grok_leader():
    """Walking past the shared leader onto its parent TUI is the bug that
    stamped one tab's pid on every Grok session."""
    table = {
        60: (55, "/bin/zsh"),
        55: (50, "grok agent leader"),
        50: (1, "grok"),  # some other tab's TUI
    }
    pid, provider = _walk(table, 60, environ={"GROK_SESSION_ID": "g1"})
    assert provider == "grok"
    assert pid == 60  # the fallback start, never the leader or its parent


def test_the_walk_falls_back_to_the_start_pid_when_the_table_is_empty():
    assert _walk({}, 77) == (77, "claude")


# ── the pid memo ─────────────────────────────────────────────────────────────
#
# The walk's answer is remembered per session under ~/.dark-army/hook-pids
# and believed only while the named pid's kernel identity (start time, comm)
# still matches. These pin behaviour, never the stopwatch: a second event does
# not fork, the memo equals the walk, a fallback is never written, a stale
# identity is refused, a live stranger off this hook's ancestor chain is
# refused, and a broken store still answers.


def _memo_namespace(monkeypatch, tmp_path, table_lines=None):
    """NOTIFY_SCRIPT's namespace with HOME at tmp_path, a counting stand-in
    for `subprocess` whose `ps` names **real** pids (so the identity probe
    succeeds without being stubbed), and os.getppid() pointing at the harness
    row of that table."""
    ns = _script_namespace()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GROK_SESSION_ID", raising=False)
    me, parent = os.getpid(), os.getppid()
    if table_lines is None:
        table_lines = [
            f" {me} {parent} node /usr/local/bin/claude",
            f" {parent} 1 /bin/zsh",
        ]
    calls = []

    class _Done:
        stdout = "\n".join(table_lines) + "\n"

    def run(*a, **k):
        calls.append(a)
        return _Done()

    monkeypatch.setitem(ns, "subprocess", type(
        "S", (), {"run": staticmethod(run),
                  "TimeoutExpired": subprocess.TimeoutExpired}))
    monkeypatch.setattr(ns["os"], "getppid", lambda: me)
    return ns, calls


def _memo_dir(tmp_path):
    return tmp_path / ".dark-army" / "hook-pids"


def test_a_second_event_does_not_fork_ps(monkeypatch, tmp_path):
    ns, calls = _memo_namespace(monkeypatch, tmp_path)
    first = ns["_session_pid_cached"]("sid-1", False)
    second = ns["_session_pid_cached"]("sid-1", False)
    assert len(calls) == 1
    assert first == second == (os.getpid(), "claude")


def test_the_cached_answer_equals_the_walk(monkeypatch, tmp_path):
    ns, _calls = _memo_namespace(monkeypatch, tmp_path)
    ns["_session_pid_cached"]("sid-2", False)
    cached = ns["_session_pid_cached"]("sid-2", False)
    assert cached == ns["_find_session_pid"]()


def test_a_fallback_pid_is_never_cached(monkeypatch, tmp_path):
    """An empty table makes the walk name the hook's own parent — a
    short-lived shell that must not be remembered."""
    ns, calls = _memo_namespace(monkeypatch, tmp_path, table_lines=[])
    answer = ns["_session_pid_cached"]("sid-3", False)
    assert answer == (os.getpid(), "claude")
    assert not _memo_dir(tmp_path).exists() or not any(_memo_dir(tmp_path).iterdir())
    ns["_session_pid_cached"]("sid-3", False)
    assert len(calls) == 2


def test_a_stale_entry_is_refused(monkeypatch, tmp_path):
    """A recycled pid cannot share a start time to the microsecond with the
    process it replaced; a wrong identity triple sends the script back to the
    walk and the entry is rewritten."""
    ns, calls = _memo_namespace(monkeypatch, tmp_path)
    d = _memo_dir(tmp_path)
    d.mkdir(parents=True)
    path = d / "c-sid-4.json"
    path.write_text(json.dumps({"v": 1, "pid": os.getpid(), "provider": "grok",
                                "id": [1, 2, "nope"], "at": time.time()}))
    answer = ns["_session_pid_cached"]("sid-4", False)
    assert len(calls) == 1
    assert answer == (os.getpid(), "claude")
    entry = json.loads(path.read_text())
    assert entry["provider"] == "claude"
    assert entry["id"] == list(ns["_proc_identity"](os.getpid()))


def test_a_live_non_ancestor_entry_is_refused(monkeypatch, tmp_path):
    """Identity alone is not enough: two harnesses can share one session id
    (`--resume` of a session another terminal `/clear`ed), and the memo the
    first wrote must not answer for the second. An entry naming a live process
    that is not on this hook's ancestor chain sends the script back to the
    walk, and the entry is rewritten to the walk's own answer."""
    ns, calls = _memo_namespace(monkeypatch, tmp_path)
    stranger = subprocess.Popen(["sleep", "30"])
    try:
        ident = ns["_proc_identity"](stranger.pid)
        assert ident is not None
        assert ns["_memo_on_our_chain"](stranger.pid) is False
        assert ns["_memo_on_our_chain"](os.getpid()) is True
        d = _memo_dir(tmp_path)
        d.mkdir(parents=True)
        path = d / "c-sid-7.json"
        path.write_text(json.dumps({"v": 1, "pid": stranger.pid,
                                    "provider": "claude", "id": list(ident),
                                    "at": time.time()}))
        answer = ns["_session_pid_cached"]("sid-7", False)
        assert len(calls) == 1
        assert answer == (os.getpid(), "claude")
        assert json.loads(path.read_text())["pid"] == os.getpid()
    finally:
        stranger.kill()
        stranger.wait()


def test_an_unwritable_cache_still_answers(monkeypatch, tmp_path):
    """HOME pointing at a file: the store cannot be made, and nothing raises."""
    ns, calls = _memo_namespace(monkeypatch, tmp_path)
    blocker = tmp_path / "not-a-home"
    blocker.write_text("x")
    monkeypatch.setenv("HOME", str(blocker))
    assert ns["_session_pid_cached"]("sid-5", False) == (os.getpid(), "claude")
    assert ns["_session_pid_cached"]("sid-5", False) == (os.getpid(), "claude")
    assert len(calls) == 2


def test_the_entry_is_written_0600(monkeypatch, tmp_path):
    ns, _calls = _memo_namespace(monkeypatch, tmp_path)
    ns["_session_pid_cached"]("sid/6 odd", False)
    files = list(_memo_dir(tmp_path).iterdir())
    assert len(files) == 1
    assert files[0].name == "c-sid_6_odd.json"
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600


def test_the_probe_reads_this_process():
    """Fails loudly if the proc_bsdinfo layout is wrong on this machine."""
    ns = _script_namespace()
    ident = ns["_proc_identity"](os.getpid())
    assert ident is not None
    sec, usec, comm = ident
    assert isinstance(comm, str) and comm
    assert 0 < sec <= time.time()
    assert 0 <= usec < 1_000_000
    assert ns["_proc_identity"](0) is None
    assert ns["_proc_identity"](-1) is None


# ── the PermissionRequest broker ─────────────────────────────────────────────
#
# The one hook branch that *waits*. Everything here runs the real installed
# script as a subprocess against a stand-in daemon that replies, because the
# thing being pinned is the script's manners under a slow or silent Dark Army: a
# hook that hangs is an agent that hangs, and the whole safety case is that
# every wait is bounded and every failure prints nothing at all.


class _BrokerDaemon:
    """A stand-in daemon that answers, connection by connection.

    `replies` is a list, one entry per accepted connection, each a dict to
    send back or None to close without a line. Anything beyond the list is a
    close with no line, which is the script's cue to give up silently.
    """

    def __init__(self, port, replies):
        self.port = port
        self.replies = list(replies)
        self.received = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._srv.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(3.0)
                buf = b""
                try:
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                except (socket.timeout, OSError):
                    continue
                line = buf.split(b"\n", 1)[0].decode("utf-8").strip()
                if line:
                    self.received.append(json.loads(line))
                index = len(self.received) - 1
                reply = (self.replies[index]
                         if 0 <= index < len(self.replies) else None)
                if reply is not None:
                    try:
                        conn.sendall(json.dumps(reply).encode("utf-8") + b"\n")
                    except OSError:
                        pass


def _run_broker(payload, port, *, home=None, timeout=30.0, env_extra=None):
    """Run the script against whatever is (or is not) on `port`."""
    return _run_notify_script(payload, port, home=home, env_extra=env_extra,
                              timeout=timeout)


def _ask_payload(tmp_path, file_path="/etc/hosts"):
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": "broker-1",
        "cwd": str(tmp_path),
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
    }


def test_the_broker_registers_the_ask_with_a_request_id_and_a_claim(tmp_path):
    port = _free_port()
    with _BrokerDaemon(port, [{"hold": 0}]) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.returncode == 0
    assert proc.stdout == b""
    ask = daemon.received[0]
    assert ask["event"] == "permission_ask"
    assert ask["tool_name"] == "Read"
    assert ask["request_id"].startswith("hook-")
    assert len(ask["claim"]) >= 16
    assert ask["claim"] != ask["request_id"]
    assert ask["description"] == "/etc/hosts"
    assert json.loads(ask["input_preview"]) == {"file_path": "/etc/hosts"}
    assert "key" in ask


def test_the_broker_clamps_a_long_description(tmp_path):
    port = _free_port()
    with _BrokerDaemon(port, [{"hold": 0}]) as daemon:
        _run_broker(_ask_payload(tmp_path, file_path="/x" * 900), port)
    ask = daemon.received[0]
    assert len(ask["description"]) == 300
    assert ask["description"].endswith("…")
    assert len(ask["input_preview"]) == 400


def test_the_broker_prints_the_allow_decision_when_bob_allows(tmp_path):
    port = _free_port()
    replies = [{"hold": 570}, {"verdict": None}, {"verdict": "allow"}]
    with _BrokerDaemon(port, replies) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.returncode == 0
    assert json.loads(proc.stdout.decode("utf-8")) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    polls = [m for m in daemon.received if m["event"] == "permission_poll"]
    assert polls, "the broker never polled"
    assert polls[0]["request_id"] == daemon.received[0]["request_id"]
    assert polls[0]["claim"] == daemon.received[0]["claim"]


def test_the_broker_prints_the_deny_decision_with_its_message(tmp_path):
    port = _free_port()
    with _BrokerDaemon(port, [{"hold": 570}, {"verdict": "deny"}]):
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"][
        "decision"] == {"behavior": "deny", "message": "Denied from Dark Army"}


def test_a_refused_hold_ends_the_branch_at_once(tmp_path):
    """`{"hold": 0}` is Dark Army saying "carry on without me" — and it has to be
    quick, because the agent is stopped behind this hook."""
    port = _free_port()
    start = time.monotonic()
    with _BrokerDaemon(port, [{"hold": 0}]) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.stdout == b""
    assert proc.returncode == 0
    assert time.monotonic() - start < 5.0
    assert len(daemon.received) == 1


def test_a_daemon_that_closes_without_a_line_falls_back_to_the_legacy_event(tmp_path):
    """Silence is indistinguishable from a daemon too old to know the verb —
    one whose `LIFECYCLE_EVENTS` has no `permission_ask`, which therefore also
    never ran the state effect that puts the session in `waiting`. Returning
    here without sending anything would make a new script against an old
    daemon *worse* than today: the ask would vanish from Dark Army altogether. So
    the legacy message goes out instead, and the branch is degraded to today."""
    port = _free_port()
    start = time.monotonic()
    with _BrokerDaemon(port, [None, None]) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.stdout == b""
    assert proc.returncode == 0
    assert time.monotonic() - start < 5.0
    assert [m["event"] for m in daemon.received] == [
        "permission_ask", "permission"]
    assert daemon.received[1]["tool_name"] == "Read"


def test_a_reply_that_is_not_an_object_falls_back_to_the_legacy_event(tmp_path):
    """Same case, arriving as an unusable line rather than as a closed socket:
    nothing came back that the script can read as an answer, so nothing was
    recorded on Dark Army's side either."""
    port = _free_port()
    with _BrokerDaemon(port, ["not json at all", None]) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.stdout == b""
    assert proc.returncode == 0
    assert [m["event"] for m in daemon.received] == [
        "permission_ask", "permission"]


def test_a_refused_hold_does_not_send_the_legacy_event_as_well(tmp_path):
    """`{"hold": 0}` is a *deliberate* refusal from a daemon that ran the
    state effect itself while deciding it. A second message would be one
    dialog counted twice."""
    port = _free_port()
    with _BrokerDaemon(port, [{"hold": 0}, None]) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.returncode == 0
    assert [m["event"] for m in daemon.received] == ["permission_ask"]


def test_no_daemon_at_all_ends_the_branch(tmp_path):
    """Nothing listening is the ordinary case on a machine with Dark Army off. The
    legacy send is attempted and fails the same way every other event's does —
    silently, quickly, printing nothing."""
    port = _free_port()
    start = time.monotonic()
    proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.stdout == b""
    assert proc.returncode == 0
    assert time.monotonic() - start < 5.0


def test_grok_never_takes_the_broker_branch(tmp_path):
    """`_EVENT_ALIASES` maps Grok's own `permission_request` onto the same
    name, and `grok_hooks_config()` registers the event — so the branch is
    gated on the provider rather than on the event name alone. A Grok build
    that started firing it would otherwise be handed a Claude-shaped
    `hookSpecificOutput` on its stdout, which is not its contract."""
    port = _free_port()
    payload = dict(_ask_payload(tmp_path))
    payload["hook_event_name"] = "permission_request"
    with _BrokerDaemon(port, [{"hold": 570}, {"verdict": "allow"}]) as daemon:
        proc = _run_broker(payload, port, home=tmp_path,
                           env_extra={"GROK_SESSION_ID": "grok-1"})
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert [m["event"] for m in daemon.received] == ["permission"]
    assert daemon.received[0]["provider"] == "grok"


def test_a_gone_verdict_ends_the_branch_silently(tmp_path):
    """Answered at the desk, or reaped: either way the dialog is not ours."""
    port = _free_port()
    with _BrokerDaemon(port, [{"hold": 570}, {"verdict": "gone"}]) as daemon:
        proc = _run_broker(_ask_payload(tmp_path), port)
    assert proc.stdout == b""
    assert proc.returncode == 0
    assert len(daemon.received) == 2


def test_the_broker_runs_with_the_preference_off_on_disk(tmp_path):
    """A `permission_broker: false` file still sends `permission_ask` first."""
    home = tmp_path / "home"
    (home / ".dark-army").mkdir(parents=True)
    (home / ".dark-army" / "preferences.json").write_text(
        json.dumps({"permission_broker": False}), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    port = _free_port()
    with _BrokerDaemon(port, [{"hold": 570}]) as daemon:
        proc = _run_broker(_ask_payload(project), port, home=home)
    assert proc.returncode == 0
    assert daemon.received
    assert daemon.received[0]["event"] == "permission_ask"


def test_the_broker_never_asks_for_a_permission_rule():
    """An allow from Dark Army is an allow *once*. A rule update from a surface
    showing a clamped preview is the wrong affordance, so the string that
    would carry one is not in the script at all."""
    assert "updatedPermissions" not in NOTIFY_SCRIPT
    assert "updatedInput" not in NOTIFY_SCRIPT
    assert "hookSpecificOutput" in NOTIFY_SCRIPT


# --- The address rule: the private socket, the bridge port, and silence ---
#
# Every case below chooses the environment itself: `_run_notify_script`
# always names a port, which is the bridge leg and not what these pin.

class _StandIn:
    """A stand-in daemon on a Unix socket path or a loopback port, taking one
    line per connection and answering from `replies` (one per connection,
    None or past the end closes with no line)."""

    def __init__(self, *, path=None, port=None, replies=()):
        if path is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._srv.bind(path)
        else:
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv.bind(("127.0.0.1", port))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self.replies = list(replies)
        self.received = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._srv.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(3.0)
                buf = b""
                try:
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                except (socket.timeout, OSError):
                    continue
                line = buf.split(b"\n", 1)[0].decode("utf-8").strip()
                if line:
                    self.received.append(json.loads(line))
                index = len(self.received) - 1
                reply = (self.replies[index]
                         if 0 <= index < len(self.replies) else None)
                if reply is not None:
                    try:
                        conn.sendall(json.dumps(reply).encode("utf-8") + b"\n")
                    except OSError:
                        pass


@pytest.fixture
def short_home():
    """A HOME short enough for `~/.dark-army/hook.sock` to bind: AF_UNIX
    paths are capped near 104 bytes and pytest's tmp_path is longer."""
    import shutil
    path = tempfile.mkdtemp(prefix="nh-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run_with_env(payload, home, **named):
    """NOTIFY_SCRIPT with every hook-address variable cleared, then exactly
    the ones named here set."""
    env = dict(os.environ, HOME=str(home))
    for inherited in ("GROK_SESSION_ID", "BOB_COMPANION_ORIGIN",
                      "DARK_ARMY_HOOK_SOCKET", "BOB_COMPANION_PORT",
                      "CLAWD_TANK_PORT"):
        env.pop(inherited, None)
    env.update({k: str(v) for k, v in named.items()})
    fd, script = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(NOTIFY_SCRIPT)
    try:
        return subprocess.run([sys.executable, script], env=env, timeout=30.0,
                              input=json.dumps(payload).encode("utf-8"),
                              capture_output=True)
    finally:
        os.unlink(script)


_TOOL = {"hook_event_name": "PreToolUse", "session_id": "addr-1",
         "tool_name": "Bash", "tool_input": {"command": "ls"}}


def test_socket_named_in_the_environment_is_the_only_address(short_home):
    """An explicit socket is exclusive: the message goes there, and a port
    named beside it hears nothing."""
    home = short_home
    path = os.path.join(home, "named", "hook.sock")
    port = _free_port()
    with _StandIn(path=path) as unix, _StandIn(port=port) as tcp:
        proc = _run_with_env(_TOOL, home, DARK_ARMY_HOOK_SOCKET=path,
                             BOB_COMPANION_PORT=port)
    assert proc.returncode == 0
    assert [m["event"] for m in unix.received] == ["tool_use"]
    assert unix.received[0]["session_id"] == "addr-1"
    assert tcp.received == []


def test_socket_with_nothing_named_the_default_in_home_takes_the_message(short_home):
    home = short_home
    path = os.path.join(home, ".dark-army", "hook.sock")
    with _StandIn(path=path) as unix:
        proc = _run_with_env(_TOOL, home)
    assert proc.returncode == 0
    assert [m["session_id"] for m in unix.received] == ["addr-1"]


def test_tcp_a_named_port_is_the_only_address(short_home):
    """The bridge leg: a port in the environment is dialled and the default
    private socket in HOME hears nothing — never a second try."""
    home = short_home
    port = _free_port()
    with _StandIn(path=os.path.join(home, ".dark-army", "hook.sock")) as unix, \
            _StandIn(port=port) as tcp:
        proc = _run_with_env(_TOOL, home, BOB_COMPANION_PORT=port)
    assert proc.returncode == 0
    assert [m["session_id"] for m in tcp.received] == ["addr-1"]
    assert unix.received == []


def test_socket_the_quiet_sentinel_reaches_nobody(short_home):
    """`/dev/null` is the quiet helpers' socket: exit 0, nothing on either
    door, and no standing hint printed (the delivered gate)."""
    home = short_home
    port = _free_port()
    start = {"hook_event_name": "SessionStart", "session_id": "quiet",
             "source": "startup"}
    with _StandIn(path=os.path.join(home, ".dark-army", "hook.sock")) as unix, \
            _StandIn(port=port) as tcp:
        started = time.monotonic()
        proc = _run_with_env(start, home, DARK_ARMY_HOOK_SOCKET="/dev/null",
                             BOB_COMPANION_PORT=port)
        took = time.monotonic() - started
    assert proc.returncode == 0
    assert unix.received == [] and tcp.received == []
    assert proc.stdout.decode("utf-8") == ""
    assert took < 10.0


def test_tcp_an_old_wrapper_port_one_alone_reaches_nobody(short_home):
    """A project's older shunt wrapper sets only `BOB_COMPANION_PORT=1`:
    under the rule that still means nobody, never the default socket."""
    home = short_home
    start = {"hook_event_name": "SessionStart", "session_id": "old-quiet",
             "source": "startup"}
    with _StandIn(path=os.path.join(home, ".dark-army", "hook.sock")) as unix:
        proc = _run_with_env(start, home, BOB_COMPANION_PORT="1")
    assert proc.returncode == 0
    assert unix.received == []
    assert proc.stdout.decode("utf-8") == ""


def test_socket_the_permission_broker_reads_its_reply_over_the_socket(tmp_path, short_home):
    """`_broker_exchange` is request and reply: a deliberate `{"hold": 0}`
    comes back through the socket and ends the branch without the legacy
    event."""
    home = short_home
    path = os.path.join(home, ".dark-army", "hook.sock")
    with _StandIn(path=path, replies=[{"hold": 0}]) as unix:
        proc = _run_with_env(_ask_payload(tmp_path), home)
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8") == ""
    assert [m["event"] for m in unix.received] == ["permission_ask"]


def test_no_client_script_names_the_bridge_port_as_a_default():
    """The finding closes only if nothing dials 19873 unless an environment
    names it."""
    from dark_army_menubar import statusline
    from dark_army_daemon import channel_server
    import inspect
    for text in (NOTIFY_SCRIPT, statusline.STATUSLINE_SCRIPT,
                 inspect.getsource(channel_server)):
        assert "19873" not in text


# --- The next-step hint: the fourth standing instruction ----------------------
# One line beside the work report saying Dark Army itself needs a rebuild. The
# daemon parses it (`session_stats._NEXT_RE`); this end only asks for it.


def test_session_start_asks_for_the_rebuild_marker_once(tmp_path):
    out = _run_script_stdout(dict(_START, cwd=str(tmp_path)), _free_port(), tmp_path)
    assert out.count("<!-- dark-army-next: rebuild -->") == 1
    assert out.index("## Work done") < out.index("dark-army-next")


def test_the_next_step_hint_carries_no_bob_marker_literals():
    hint = _script_namespace()["NEXT_STEP_HINT"]
    assert "<!-- bob-tldr:" not in hint
    assert "<!-- bob-actions:" not in hint
    assert "bob" not in hint.lower()


def test_the_requested_marker_is_the_one_the_daemon_parses():
    from dark_army_daemon import session_stats as ss
    hint = _script_namespace()["NEXT_STEP_HINT"]
    found = ss._NEXT_RE.findall(hint)
    assert found == ["rebuild"]
    assert found[0] in ss.NEXT_STEPS
