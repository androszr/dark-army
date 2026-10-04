"""The daemon's per-session state: what each hook event does to a session,
when a card is raised or held back, when a session is forgotten, and what
survives a restart.

Most cases are tables over `_handle_message`, so each row reads as one
event and the state it must leave behind.
"""

import asyncio
import json
import time
import pytest
from unittest.mock import MagicMock, patch

from dark_army_daemon import grok_roster
from dark_army_daemon import session_stats as ss
from dark_army_daemon.agents_poll import AgentRecord
from dark_army_daemon.daemon import (
    BobDaemon,
    DEFAULT_SESSION_STALENESS_SECONDS,
    PIDLESS_GRACE_SECONDS,
    _tool_to_anim,
)

ANCIENT = 9999.0


def _daemon(**held) -> BobDaemon:
    d = BobDaemon()
    d._session_states.update(held)
    return d


def _hold(d, sid, entry):
    d._session_states[sid] = entry


def _now(state, **extra):
    return {"state": state, "last_event": time.time(), **extra}


def _silent(state, seconds=ANCIENT, *, wall=None, **extra):
    """A session whose monotonic clock says it has been quiet `seconds`."""
    wall_age = seconds if wall is None else wall
    return {"state": state, "last_event": time.time() - wall_age,
            "last_event_monotonic": time.monotonic() - seconds, **extra}


def _counts(**figures):
    return {"working": 0, "idle": 0, "attention": 0, "subagents": 0, **figures}


def _stop(hook="Stop", sid="one", **extra):
    return {"event": "add", "hook": hook, "session_id": sid, "project": "shop",
            "message": "Waiting for input", **extra}


def _prompt(sid="one"):
    return {"event": "dismiss", "hook": "UserPromptSubmit", "session_id": sid}


def _end(sid="one"):
    return {"event": "dismiss", "hook": "SessionEnd", "session_id": sid}


def _tool(event="tool_use", tool="", sid="one", **extra):
    return {"event": event, "session_id": sid, "tool_name": tool, **extra}


def _start(sid="one", **extra):
    return {"event": "session_start", "session_id": sid, **extra}


def _child(event, agent, sid="one"):
    return {"event": event, "session_id": sid, "agent_id": agent}


async def _send(d, *messages):
    for message in messages:
        await d._handle_message(dict(message))


def _sweep(d, timeout):
    d._session_staleness_timeout = timeout
    d._evict_stale_sessions()


def _processes_gone():
    """Every pid reads as a process that has exited."""
    return patch("dark_army_daemon.daemon.os.kill", side_effect=ProcessLookupError)


# --- What the counts say about a fleet ---------------------------------------

@pytest.mark.parametrize("state, figures", [
    (None, {}),
    ("registered", {"idle": 1}),
    ("idle", {"idle": 1}),
    ("thinking", {"working": 1}),
    ("working", {"working": 1}),
    ("waiting", {"attention": 1}),
    ("error", {"attention": 1}),
])
def test_a_lone_session_lands_in_one_bucket(state, figures):
    d = _daemon(**({} if state is None else {"one": _now(state)}))
    assert d._activity_counts() == _counts(**figures)


@pytest.mark.asyncio
async def test_a_session_that_started_and_ran_a_tool_counts_as_working():
    d = _daemon()
    await _send(d, _start(), _tool())
    assert d._activity_counts() == _counts(working=1)


# --- One event, one state ----------------------------------------------------

@pytest.mark.parametrize("before, message, after", [
    (None, _start(), "registered"),
    ("idle", _prompt(), "thinking"),
    ("error", _prompt(), "thinking"),
    ("confused", _prompt(), "thinking"),
    ("registered", _tool(), "working"),
    ("thinking", _tool(), "working"),
    ("error", _tool(), "working"),
    ("waiting", _tool(tool="Edit"), "working"),
    ("working", _tool(tool="AskUserQuestion"), "waiting"),
    ("waiting", _tool("tool_done", "AskUserQuestion"), "thinking"),
    ("working", _tool("tool_done", "Bash"), "working"),
    ("working", _tool("permission", "Bash"), "waiting"),
    ("working", _tool("tool_failed", "Read"), "confused"),
    ("thinking", _tool("tool_failed", "Bash"), "confused"),
    ("working", {"event": "compact", "session_id": "one"}, "working"),
    ("working", _stop(), "idle"),
    ("idle", _stop("Notification"), "confused"),
    ("working", _stop("StopFailure", message="Rate limited"), "error"),
    (None, _tool(), "working"),
    (None, _child("subagent_start", "kid-1"), "working"),
], ids=lambda value: value.get("hook") or value.get("event") if isinstance(value, dict) else None)
@pytest.mark.asyncio
async def test_one_event_leaves_the_session_in_this_state(before, message, after):
    d = _daemon(**({} if before is None else {"one": _now(before)}))
    await _send(d, message)
    assert d._session_states["one"]["state"] == after


@pytest.mark.parametrize("message", [
    _tool("tool_done", "AskUserQuestion"),
    _tool("permission", "Bash"),
    _tool("tool_failed", "Read"),
    {"event": "compact", "session_id": "one"},
], ids=["answer", "permission", "tool-failure", "compact"])
@pytest.mark.asyncio
async def test_a_late_event_for_a_session_nobody_holds_creates_nothing(message):
    """PreToolUse always comes first for a real session, so any of these on
    its own is a straggler for one that has already ended."""
    d = _daemon()
    await _send(d, message)
    assert "one" not in d._session_states
    assert "one" not in d._active_notifications


@pytest.mark.parametrize("messages, after", [
    ([_tool("permission", "Bash"), _tool(tool="Bash")], "working"),
    ([_tool("permission", "Bash"), _stop()], "idle"),
    ([_tool("tool_failed", "Read"), _prompt()], "thinking"),
    ([_stop(), _stop("StopFailure", message="Rate limited")], "error"),
], ids=["approved-then-ran", "approved-then-stopped", "failed-then-prompted",
        "stop-then-api-error"])
@pytest.mark.asyncio
async def test_a_short_sequence_ends_where_the_last_event_says(messages, after):
    d = _daemon(one=_now("working"))
    await _send(d, *messages)
    assert d._session_states["one"]["state"] == after


@pytest.mark.parametrize("entry", [
    _now("idle"), _now("error"), _now("working", subagents={"kid-1", "kid-2"}),
], ids=["idle", "error", "with-children"])
@pytest.mark.asyncio
async def test_session_end_forgets_the_session_whatever_it_was_doing(entry):
    d = _daemon(one=entry)
    await _send(d, _end())
    assert "one" not in d._session_states
    # A child's stop arriving after the end is a no-op, not a resurrection.
    await _send(d, _child("subagent_stop", "kid-1"))
    assert "one" not in d._session_states


# --- Whether a stop is a card -----------------------------------------------

@pytest.mark.parametrize("entry, hook, card, after", [
    (_now("working", subagents={"kid-1"}), "Stop", False, "working"),
    (_now("working", subagents={"kid-1"}), "Notification", False, "working"),
    (_now("working"), "Stop", True, "idle"),
    (_now("working"), "Notification", True, "confused"),
    # An API error is news even while children run: never a parked hook.
    (_now("working", subagents={"kid-1"}), "StopFailure", True, "error"),
    # No `prompted` key (restored, or older than the key) reads as asked.
    (_now("idle"), "Notification", True, "confused"),
], ids=["stop-while-children-run", "idle-prompt-while-children-run", "plain-stop",
        "plain-idle-prompt", "api-error-while-children-run", "never-saw-it-start"])
@pytest.mark.asyncio
async def test_a_stop_is_a_card_unless_the_turn_is_parked(entry, hook, card, after):
    d = _daemon(one=entry)
    await _send(d, _stop(hook))
    assert ("one" in d._active_notifications) is card
    assert d._session_states["one"]["state"] == after
    if "subagents" in entry:
        assert d._session_states["one"]["subagents"] == {"kid-1"}


def test_a_parked_row_is_still_counted_as_running():
    # The state a parked turn keeps is one `categorize` banks as running.
    assert ss.categorize("working", 1, False) == "running"


@pytest.mark.parametrize("between, hook, after", [
    (_prompt(), "Notification", "confused"),
    (_tool(), "Stop", "idle"),
    (None, "StopFailure", "error"),
], ids=["typed-prompt", "tool-call-stands-in-for-the-prompt", "api-error-before-any-prompt"])
@pytest.mark.asyncio
async def test_a_session_somebody_asked_for_something_gets_its_card(between, hook, after):
    d = _daemon()
    await _send(d, _start(), *([between] if between else []), _stop(hook))
    assert "one" in d._active_notifications
    assert d._session_states["one"]["state"] == after


@pytest.mark.parametrize("card_hook, message, stays", [
    ("Stop", _tool(tool="Bash"), False),
    ("Stop", _tool("tool_done", "Bash"), False),
    ("Stop", _tool("tool_failed", "Bash"), False),
    ("Stop", _child("subagent_start", "kid-1"), False),
    # An API error card outlives the retry, which is itself a tool call.
    ("StopFailure", _tool(tool="Bash"), True),
    # The ask tool starts a wait rather than ending one.
    ("Stop", _tool(tool="AskUserQuestion"), True),
], ids=["tool-use", "tool-done", "tool-failed", "subagent-start", "error-card", "ask-tool"])
@pytest.mark.asyncio
async def test_a_session_visibly_busy_again_loses_its_waiting_card(card_hook, message, stays):
    d = _daemon(one=_now("idle" if card_hook == "Stop" else "error"))
    d._active_notifications["one"] = {"event": "add", "hook": card_hook,
                                      "session_id": "one", "message": "Waiting for input"}
    await _send(d, message)
    assert ("one" in d._active_notifications) is stays
    if message["event"] == "subagent_start":
        assert d._session_states["one"]["subagents"] == {"kid-1"}


@pytest.mark.asyncio
async def test_a_tool_failure_raises_no_card():
    d = _daemon()
    await _send(d, _tool("tool_failed", "Read"))
    assert d._active_notifications == {}


# --- The staleness sweep -----------------------------------------------------

@pytest.mark.parametrize("entry, timeout, kept", [
    (_silent("idle"), 1, False),
    (_silent("waiting"), 1, True),
    (_silent("error"), 1, True),
    # Children only keep a parent alive through the parent's own clock.
    (_silent("idle", subagents={"kid-1"}), 1, False),
    (_silent("idle", subagents={"orphan-1", "orphan-2"}), 1, False),
    (_silent("idle", 0.0, subagents={"kid-1"}), 600, True),
    # The monotonic clock decides: sleep moves the wall clock, not this one.
    (_silent("idle", 0.0, wall=ANCIENT), 600, True),
    (_silent("idle", ANCIENT, wall=0.0), 1, False),
], ids=["quiet-idle", "waiting", "api-error", "stale-children", "orphaned-children",
        "fresh-with-children", "old-wall-fresh-monotonic", "fresh-wall-old-monotonic"])
def test_the_staleness_sweep(entry, timeout, kept):
    d = _daemon(one=entry)
    d._session_staleness_timeout = timeout
    d._evict_stale_sessions()
    assert ("one" in d._session_states) is kept
    if not kept:
        assert d._activity_counts() == _counts()


def test_a_session_the_sweep_forgets_leaves_the_file_too(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    _hold(d, "one", _silent("idle"))
    _sweep(d, 1)
    assert "one" not in json.loads((tmp_path / "sessions.json").read_text())["sessions"]


# --- The live subagent set ---------------------------------------------------

@pytest.mark.parametrize("messages, live", [
    ([_child("subagent_start", "kid-1")], {"kid-1"}),
    ([_child("subagent_start", "kid-1"), _child("subagent_start", "kid-2")], {"kid-1", "kid-2"}),
    ([_child("subagent_start", "kid-1"), _child("subagent_start", "kid-2"),
      _child("subagent_stop", "kid-1")], {"kid-2"}),
    ([_child("subagent_start", "kid-1"), _child("subagent_start", "kid-1")], {"kid-1"}),
    ([_child("subagent_stop", "stranger")], set()),
    ([_child("subagent_start", "")], set()),
], ids=["one", "two", "two-then-one-leaves", "same-twice", "unknown-stop", "nameless-start"])
@pytest.mark.asyncio
async def test_the_live_set_follows_starts_and_stops(messages, live):
    d = _daemon(one=_now("working"))
    await _send(d, *messages)
    assert set(d._session_states["one"].get("subagents") or ()) == live
    assert d._session_states["one"]["state"] == "working"


@pytest.mark.parametrize("message", [
    _child("subagent_start", "kid-1"), _child("subagent_stop", "kid-1"),
], ids=["start", "stop"])
@pytest.mark.asyncio
async def test_a_child_event_moves_the_parents_clock(message):
    long_ago = time.time() - 500
    d = _daemon(one={"state": "working", "last_event": long_ago, "subagents": {"kid-1"}})
    await _send(d, message)
    assert d._session_states["one"]["last_event"] > long_ago


# --- What reaches sessions.json ----------------------------------------------

@pytest.mark.asyncio
async def test_a_new_session_is_written_down(tmp_path):
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    await _send(d, _start())
    assert json.loads(path.read_text())["sessions"]["one"]["state"] == "registered"


@pytest.mark.parametrize("before, message, after", [
    ("thinking", _tool(), "working"),
    ("waiting", _tool("tool_done", "AskUserQuestion"), "thinking"),
])
@pytest.mark.asyncio
async def test_a_change_of_state_is_written_down(tmp_path, before, message, after):
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    _hold(d, "one", _now(before))
    await _send(d, message)
    assert json.loads(path.read_text())["sessions"]["one"]["state"] == after


@pytest.mark.asyncio
async def test_a_clock_moving_alone_does_not_rewrite_the_file(tmp_path):
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    _hold(d, "one", _now("working"))
    d._persist_sessions()
    written = path.stat().st_mtime_ns
    time.sleep(0.01)
    await _send(d, _tool())
    assert path.stat().st_mtime_ns == written


@pytest.mark.asyncio
async def test_an_ended_session_is_removed_from_the_file(tmp_path):
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    await _send(d, _start(), _end())
    assert "one" not in json.loads(path.read_text())["sessions"]


@pytest.mark.asyncio
async def test_the_question_a_session_is_stopped_on_is_written_down(tmp_path):
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    await _send(d, dict(ASK_MSG))
    assert json.loads(path.read_text())["pending_questions"]["s1"]["text"] == \
        "Postgres or SQLite?"


# --- Coming back after a restart ---------------------------------------------

def _saved(tmp_path, document):
    path = tmp_path.joinpath("sessions.json")
    path.write_text(json.dumps(document))
    return BobDaemon(sessions_path=path)


def test_the_older_flat_file_comes_back(tmp_path):
    d = _saved(tmp_path, {"one": _now("working")})
    assert d._session_states["one"]["state"] == "working"


def test_live_children_come_back_as_a_set(tmp_path):
    d = _saved(tmp_path, {"one": _now("idle", subagents=["kid-1", "kid-2"])})
    assert d._session_states["one"]["subagents"] == {"kid-1", "kid-2"}
    assert isinstance(d._session_states["one"]["subagents"], set)


@pytest.mark.parametrize("age", [ANCIENT, 3600.0])
def test_a_session_long_quiet_before_the_restart_is_not_brought_back(tmp_path, age):
    d = _saved(tmp_path, {"sessions": {
        "old": {"state": "working", "last_event": time.time() - age},
        "new": _now("idle"),
    }})
    assert set(d._session_states) == {"new"}


def test_what_comes_back_gets_a_fresh_monotonic_stamp(tmp_path):
    d = _saved(tmp_path, {"sessions": {"one": _now("idle")}})
    assert isinstance(d._session_states["one"]["last_event_monotonic"], float)


def test_a_question_whose_session_did_not_come_back_is_dropped(tmp_path):
    path = tmp_path.joinpath("sessions.json")
    from dark_army_daemon.session_store import save_sessions
    save_sessions({"s1": {"state": "idle", "last_event": time.time() - 86400}}, path,
                  {"s1": {"text": "Postgres or SQLite?", "id": "toolu_1"}})
    d = BobDaemon(sessions_path=path)
    assert (d._session_states, d._pending_questions) == ({}, {})


# --- The pid and the monotonic clock on every event --------------------------

@pytest.mark.asyncio
async def test_the_pid_a_message_carries_is_kept_with_a_monotonic_stamp():
    d = _daemon()
    await _send(d, _start(pid=4242))
    assert d._session_states["one"]["pid"] == 4242
    assert isinstance(d._session_states["one"]["last_event_monotonic"], float)


@pytest.mark.asyncio
async def test_a_later_pid_replaces_the_earlier_one():
    d = _daemon(one={"state": "working", "last_event": 1.0, "pid": 1111,
                     "last_event_monotonic": 0.0})
    await _send(d, _tool(tool="Edit", pid=4242))
    assert d._session_states["one"]["pid"] == 4242
    assert d._session_states["one"]["last_event_monotonic"] > 0.0


@pytest.mark.asyncio
async def test_a_message_with_no_pid_leaves_none():
    d = _daemon()
    await _send(d, _start())
    assert d._session_states["one"].get("pid") is None


# --- /clear: a new session on a pid a fresh session already holds ------------

@pytest.mark.parametrize("quiet_for, message, earlier_kept", [
    (0.0, _start("new", pid=4242), False),
    # Past the freshness window the number has more likely been reused.
    (120.0, _start("new", pid=4242), True),
    (0.0, _start("new"), True),
    (0.0, _tool(tool="Edit", sid="new", pid=4242), True),
], ids=["clear", "stale-pid", "no-pid", "not-a-start"])
@pytest.mark.asyncio
async def test_a_start_on_a_fresh_sessions_pid_ends_that_session(quiet_for, message, earlier_kept):
    d = _daemon(old=_silent("idle", quiet_for, wall=0.0, pid=4242))
    await _send(d, message)
    held = set(d._session_states)
    assert held == ({"old", "new"} if earlier_kept else {"new"})


@pytest.mark.asyncio
async def test_the_cleared_sessions_card_goes_with_it():
    d = _daemon(old=_silent("idle", 0.0, wall=0.0, pid=4242))
    d._active_notifications["old"] = _stop(sid="old")
    await _send(d, _start("new", pid=4242))
    assert set(d._active_notifications) == set()


# --- The liveness pass -------------------------------------------------------

@pytest.mark.parametrize("state", ["idle", "confused"])
def test_a_session_whose_process_is_gone_is_forgotten(state):
    d = _daemon(one=_silent(state, 0.0, wall=0.0, pid=4242))
    with _processes_gone():
        d._check_liveness()
    assert "one" not in d._session_states


def _denied(*_a, **_kw):
    import psutil as _psutil
    raise _psutil.AccessDenied(4242)


@pytest.mark.parametrize("process", [lambda *_a, **_kw: _FakeLiveProc(), _denied],
                         ids=["live-claude", "access-denied"])
def test_a_process_that_is_ours_or_unreadable_keeps_its_session(process):
    """Unreadable is assumed alive: a permissions failure is no reason to
    take a running session off the screen."""
    d = _daemon(one=_silent("idle", 0.0, wall=0.0, pid=4242))
    with patch("dark_army_daemon.daemon.psutil.Process", side_effect=process):
        d._check_liveness()
    assert "one" in d._session_states


@pytest.mark.parametrize("quiet_for, kept", [
    (0.0, True),
    (PIDLESS_GRACE_SECONDS + 1, False),
], ids=["inside-grace", "past-grace"])
def test_a_session_that_never_named_a_pid_has_a_grace(quiet_for, kept):
    d = _daemon(one=_silent("working", quiet_for, wall=0.0))
    with _processes_gone():
        forgotten = d._check_liveness()
    assert ("one" in d._session_states) is kept
    assert ("one" in forgotten) is not kept


def test_a_session_the_liveness_pass_forgets_leaves_the_file_too(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    _hold(d, "one", _silent("idle", 0.0, wall=0.0, pid=4242))
    with _processes_gone():
        d._check_liveness()
    assert "one" not in json.loads((tmp_path / "sessions.json").read_text()).get("sessions", {})



# --- Nobody has asked this session anything yet ---

@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["Stop", "Notification"])
async def test_a_session_nobody_prompted_is_not_waiting_on_you(hook):
    """The `/clear` case, from a real log. `/clear` ends the old session and
    starts a fresh id (`SessionEnd reason=clear` then `SessionStart source=clear`,
    75ms apart); 60s later Claude Code fires its own idle_prompt at the new,
    empty session — "Claude is waiting for your input" about a turn that was
    never taken. Nothing dismisses it either, since only UserPromptSubmit does
    and that is the event that has not happened, so the row sat under "Needs you"
    until staleness eviction minutes later."""
    d = _daemon()
    await _send(d, _start("s1"))
    await _send(d, {
        "event": "add", "hook": hook, "session_id": "s1",
        "project": "arpg-web", "message": "Claude is waiting for your input",
    })
    # No card, and the state half matters as much: `categorize` reads `idle`
    # and `confused` as `waiting` and would bank the row under Needs you.
    assert ("s1" in d._active_notifications, d._session_states["s1"]["state"]) == (
        False, "registered")


# --- Waiting hysteresis ------------------------------------------------------
#
# The parked-turn suppression above only helps while the parent's subagent set
# is non-empty, and most SubagentStops in a measured session had no matching
# start — so a busy fleet still flipped its parent in and out of "Needs you"
# on every event, destroying the panel's row view between mouse-down and
# mouse-up. The complementary rule: *any* event fresher than
# WAITING_HYSTERESIS_SECONDS vetoes `waiting`, and the matching card is
# withheld (never dismissed) for the same window, so category and card cannot
# disagree about where the row belongs.


def _mono_session(state, age=0.0, **extra):
    return {"state": state, "last_event": time.time() - age,
            "last_event_monotonic": time.monotonic() - age, **extra}


def test_a_fresh_event_vetoes_waiting():
    d = _daemon()
    d._session_states["s1"] = _mono_session("waiting", age=1.0)
    assert d._reconciled_categories() == {"s1": "running"}


def test_a_quiet_wait_is_believed():
    d = _daemon()
    d._session_states["s1"] = _mono_session("waiting", age=4.0)
    assert d._reconciled_categories() == {"s1": "waiting"}


def test_a_session_without_the_stamp_is_not_vetoed():
    """No monotonic stamp means no clock reading, not a fresh one. In
    production every event stamps it; a hand-built state (and nothing else)
    can lack it, and must not have its wait disbelieved."""
    d = _daemon()
    _hold(d, "s1", _now("waiting"))
    assert d._reconciled_categories() == {"s1": "waiting"}


@pytest.mark.asyncio
async def test_a_fresh_wait_card_is_withheld_not_dismissed():
    """The card is the other half of "Needs you": the panel extracts any row
    carrying one, so a card shown while the category is vetoed would put the
    row exactly where the hysteresis keeps it out of."""
    d = _daemon()
    d._session_states["s1"] = _mono_session("working")
    await _send(d, _stop("Stop", "s1", project="proj", message="Waiting for input"))
    assert "s1" in d._active_notifications          # held, not dropped
    assert d._notification_snapshot() == []          # not shown yet
    assert d._reconciled_categories()["s1"] == "running"

    # 4s of quiet later, the same reading surfaces the same card on its own.
    d._session_states["s1"]["last_event_monotonic"] -= 4.0
    assert [e["session_id"] for e in d._notification_snapshot()] == ["s1"]
    assert d._reconciled_categories()["s1"] == "waiting"


@pytest.mark.asyncio
async def test_an_error_card_is_never_delayed():
    """Only the generic wait cards are gated. A StopFailure is an API error the
    human wants the moment it happens — same line the parked-turn rule draws."""
    d = _daemon()
    d._session_states["s1"] = _mono_session("working")
    await _send(d, _stop("StopFailure", "s1", project="proj", message="API error"))
    assert [e["session_id"] for e in d._notification_snapshot()] == ["s1"]


@pytest.mark.asyncio
async def test_a_rate_limit_stop_failure_lands_in_needs_you_with_its_card():
    """A `StopFailure` whose machine token is `rate_limit` is an `error`
    state, its card surfaces at once (`PARKED_CARD_HOOKS` excludes the hook),
    and once the 3s state-side hysteresis has run out the row is banked under
    `waiting` — the bucket the panel calls Needs you. Pinned here because the
    Low priority button (`can_low_priority`) is offered only on that row."""
    d = _daemon()
    d._session_states["s1"] = _mono_session("working", age=10)
    await _send(d, {
        "event": "add", "hook": "StopFailure", "session_id": "s1",
        "project": "proj", "message": "Rate limited — wait and retry",
        "error_kind": "rate_limit",
    })
    assert d._session_states["s1"]["state"] == "error"
    cards = d._notification_snapshot()
    assert [c["session_id"] for c in cards] == ["s1"]
    assert cards[0]["error_kind"] == "rate_limit"
    # Age the stamps past the hysteresis window, or the state reads `running`
    # for three seconds and the assertion fails for the wrong reason.
    st = d._session_states["s1"]
    st["last_event"] -= ss.WAITING_HYSTERESIS_SECONDS + 1
    st["last_event_monotonic"] -= ss.WAITING_HYSTERESIS_SECONDS + 1
    assert d._reconciled_categories()["s1"] == "waiting"
    assert d._activity_counts()["attention"] == 1


@pytest.mark.asyncio
async def test_suppression_arms_the_repush_timer():
    """A vetoed wait must wake something later: the session being blocked on a
    human, no further event will recompute the category. Both suppression
    sites arm the coalesced one-shot."""
    d = _daemon()
    d._session_states["s1"] = _mono_session("waiting")
    d._reconciled_categories()
    armed = d.__dict__.get("_hysteresis_timer")
    assert armed is not None
    armed[0].cancel()

    d2 = _daemon()
    d2._session_states["s2"] = _mono_session("idle")
    d2._active_notifications["s2"] = {"hook": "Stop", "message": "Waiting"}
    d2._notification_snapshot()
    armed = d2.__dict__.get("_hysteresis_timer")
    assert armed is not None
    armed[0].cancel()


@pytest.mark.asyncio
async def test_a_genuine_wait_surfaces_on_its_own():
    """End to end on a real loop: the window closes and the re-push timer —
    not some later event — delivers the card and the category flip."""
    seen = []

    class Observer:
        def on_notification_change(self, notifications):
            seen.append([n["session_id"] for n in notifications])

    with patch.object(ss, "WAITING_HYSTERESIS_SECONDS", 0.1):
        d = BobDaemon(observer=Observer())
        d._session_states["s1"] = _mono_session("working")
        await _send(d, _stop("Stop", "s1", project="proj", message="Waiting for input"))
        assert seen and seen[-1] == []               # withheld at arrival
        await asyncio.sleep(0.3)                     # the window closes
        assert seen[-1] == ["s1"]                    # ...and the timer pushed
        assert d._reconciled_categories()["s1"] == "waiting"
        armed = d.__dict__.get("_hysteresis_timer")
        if armed is not None:
            armed[0].cancel()


# --- The startup prune and the timeout ---

def test_startup_prune_and_the_loop_share_five_minutes(tmp_path):
    """The one-shot wall-clock cut used to measure 600s, then startup wrote
    the stored 300s and stamped survivors with a fresh monotonic clock —
    a session quiet for 9 minutes came back as live with a new five-minute
    grace. Prune and runtime now agree on the same default."""
    assert DEFAULT_SESSION_STALENESS_SECONDS == 300
    now = time.time()
    path = tmp_path.joinpath("sessions.json")
    path.write_text(json.dumps({"sessions": {
        "old": {"state": "idle", "last_event": now - 400},
        "fresh": {"state": "idle", "last_event": now - 60},
    }}))
    d = BobDaemon(sessions_path=path)
    assert "old" not in d._session_states
    assert "fresh" in d._session_states
    assert d._session_staleness_timeout == 300


def test_startup_prune_keeps_a_waiting_session_older_than_five_minutes(tmp_path):
    """A Needs-you row idle past the timeout must survive Restart — the
    terminal dialog is still up. Same exemption as `_evict_stale_sessions`;
    `_check_liveness` is the backstop if the PID is dead."""
    now = time.time()
    path = tmp_path.joinpath("sessions.json")
    path.write_text(json.dumps({"sessions": {
        "wait": {"state": "waiting", "last_event": now - 400},
        "idle": {"state": "idle", "last_event": now - 400},
    }}))
    d = BobDaemon(sessions_path=path)
    assert "wait" in d._session_states
    assert "idle" not in d._session_states


def test_startup_prune_drops_a_cardless_confused_session_like_the_loop(tmp_path):
    """The restore prune carries the same predicate as the loop. Cards are
    not persisted, so the card arm is always False here and the state string
    alone decides: `error` survives a Restart, and a `confused` row — which
    counts as waiting only beside a card since 22 Sep 2026 (RC3) — is
    pruned exactly as the staleness sweep would take it a moment later."""
    now = time.time()
    path = tmp_path.joinpath("sessions.json")
    path.write_text(json.dumps({"sessions": {
        "confused": {"state": "confused", "last_event": now - 400},
        "error": {"state": "error", "last_event": now - 400},
        "idle": {"state": "idle", "last_event": now - 400},
    }}))
    d = BobDaemon(sessions_path=path)
    assert "confused" not in d._session_states
    assert "error" in d._session_states
    assert "idle" not in d._session_states


def test_startup_prune_honours_a_stored_thirty_minute_timeout(tmp_path):
    """Runtime still honours 1800; Restart must measure the same number or
    an 8-minute quiet session vanishes on relaunch."""
    now = time.time()
    path = tmp_path.joinpath("sessions.json")
    path.write_text(json.dumps({"sessions": {
        "kept": {"state": "idle", "last_event": now - 400},
        "old": {"state": "idle", "last_event": now - 2000},
    }}))
    dropped = BobDaemon(sessions_path=path)
    assert "kept" not in dropped._session_states
    d = BobDaemon(sessions_path=path, session_timeout=1800)
    assert "kept" in d._session_states
    assert "old" not in d._session_states
    assert d._session_staleness_timeout == 1800


def test_session_title_off_does_not_queue_a_name():
    d = _daemon()
    d.session_title_enabled = True
    d._consider_title("s1", "", {"_category": "working"},
                      request="hello there this is a prompt")
    assert d._title_queue == [("s1", "hello there this is a prompt")]
    d2 = _daemon()
    d2.session_title_enabled = False
    d2._consider_title("s1", "", {"_category": "working"},
                       request="hello there this is a prompt")
    assert d2._title_queue == []


def test_set_session_timeout_treats_never_as_five_minutes():
    """The old picker's 0 meant Never. That picker is gone, and 0 would
    drop every non-waiting session on the next tick."""
    d = _daemon()
    d.set_session_timeout(0)
    assert d._session_staleness_timeout == 300
    d.set_session_timeout(-1)
    assert d._session_staleness_timeout == 300
    d.set_session_timeout(600)
    assert d._session_staleness_timeout == 600
    d.set_session_timeout(1800)
    assert d._session_staleness_timeout == 1800


def test_activity_observer_exception_does_not_escape():
    """_notify_activity runs inside the staleness/liveness loops. An observer that
    raises (e.g. one left on an older signature) must not kill those tasks —
    nothing awaits them, so eviction would stop for the life of the daemon."""
    d = _daemon()
    observer = MagicMock()
    observer.on_activity_change.side_effect = TypeError("stale signature")
    d._observer = observer
    _hold(d, "s1", {"state": "working", "last_event": time.time()})

    d._notify_activity()  # must not raise

    observer.on_activity_change.assert_called_once()


# --- Every way of waiting on a person survives the sweep ---

def _park(d, sid, state, *, card=False):
    """A session quiet far past any timeout, optionally holding a card."""
    _hold(d, sid, _silent(state))
    if card:
        d._active_notifications[sid] = {
            "session_id": sid, "message": "Waiting for input",
        }


@pytest.mark.parametrize("state,card", [
    ("waiting", False),   # AskUserQuestion — the shape that already worked
    ("confused", True),   # Notification / idle_prompt, its card on screen
    ("error", False),     # StopFailure
    ("idle", True),       # Stop + a card on screen
])
def test_staleness_spares_every_way_of_waiting_on_a_person(state, card):
    """The exemption is `categorize()`'s waiting bucket, not one state string.

    Only two paths ever wrote `state == "waiting"`, so the three commonest ways
    a session parks on a person were evicted 300s after the card arrived — the
    card going with them and the board card falling into "cards without a
    session"."""
    d = _daemon()
    _park(d, "s1", state, card=card)
    _sweep(d, 1)
    assert "s1" in d._session_states
    if card:
        assert "s1" in d._active_notifications


def test_staleness_evicts_a_snag_with_no_card():
    """A `confused` row with no card — a failed tool call, or an idle
    reminder whose card was dismissed — is not waiting on a person
    (22 Sep 2026, RC3): it sleeps, so the staleness sweep may take it."""
    d = _daemon()
    _park(d, "s1", "confused", card=False)
    _sweep(d, 1)
    assert "s1" not in d._session_states


# --- Grok's children and their parents ---

@pytest.mark.asyncio
async def test_grok_stop_on_child_id_removes_from_unique_parent():
    """Grok SubagentStop fires with the child's session id. If only one
    parent holds that type, the discard still lands."""
    d = _daemon()
    _hold(d, "parent", {
        **_now("working"),
        "provider": "grok", "subagents": {"bc-planner", "bc-implementer"},
    })
    await _send(d, {
        "event": "subagent_stop", "session_id": "child",
        "agent_id": "bc-planner", "provider": "grok",
    })
    assert d._session_states["parent"]["subagents"] == {"bc-implementer"}
    assert "child" not in d._session_states


@pytest.mark.asyncio
async def test_grok_stop_does_not_guess_when_two_parents_share_a_type():
    d = _daemon()
    _hold(d, "p1", {
        **_now("working"),
        "provider": "grok", "subagents": {"bc-planner"},
    })
    _hold(d, "p2", {
        **_now("working"),
        "provider": "grok", "subagents": {"bc-planner"},
    })
    await _send(d, {
        "event": "subagent_stop", "session_id": "child",
        "agent_id": "bc-planner", "provider": "grok",
    })
    assert d._session_states["p1"]["subagents"] == {"bc-planner"}
    assert d._session_states["p2"]["subagents"] == {"bc-planner"}


@pytest.mark.asyncio
async def test_parent_session_id_on_stop_beats_the_child_id():
    d = _daemon()
    _hold(d, "parent", {
        **_now("working"),
        "provider": "grok", "subagents": {"bc-planner"},
    })
    _hold(d, "other", {
        **_now("working"),
        "provider": "grok", "subagents": {"bc-planner"},
    })
    await _send(d, {
        "event": "subagent_stop", "session_id": "child",
        "parent_session_id": "parent",
        "agent_id": "bc-planner", "provider": "grok",
    })
    assert d._session_states["parent"]["subagents"] == set()
    assert d._session_states["other"]["subagents"] == {"bc-planner"}


@pytest.mark.asyncio
async def test_unique_id_start_drops_the_type_key():
    d = _daemon()
    _hold(d, "parent", {
        **_now("working"),
        "provider": "grok", "subagents": {"bc-planner"},
    })
    await _send(d, {
        "event": "subagent_start", "session_id": "parent",
        "agent_id": "01child", "subagent_type": "bc-planner",
        "provider": "grok",
    })
    assert d._session_states["parent"]["subagents"] == {"01child"}


def test_grok_chat_history_replaces_stale_type_set():
    d = _daemon()
    _hold(d, "parent", {
        **_now("working"),
        "provider": "grok",
        "subagents": {"bc-planner", "bc-implementer", "bc-verifier", "bc-bug-auditor"},
    })
    d._grok_records["parent"] = grok_roster.GrokRecord(
        session_id="parent", cwd="/proj")

    def fake_live(sid, cwd="", sessions_dir=None):
        assert sid == "parent"
        return ["live-child"]

    with patch.object(grok_roster, "live_subagent_ids", side_effect=fake_live):
        d._sync_grok_subagents("parent", d._session_states["parent"])
        snap = d._enrich_agent_stubs(d._collect_agent_stubs())
        d._apply_grok_live_subagents(snap)
    parent = next(r for rows in snap.values() if isinstance(rows, list)
                  for r in rows if r.get("session_id") == "parent")
    assert parent["subagents"] == 1
    assert parent["subagent_ids"] == ["live-child"]
    assert "_subs_reconciled" not in parent
    assert d._session_states["parent"]["subagents"] == {"live-child"}


# --- HUD breakdown counts (working / sleeping / attention) ---


def test_counts_bucket_every_session_exactly_once():
    """The three buckets are disjoint and cover every session, including any the
    strip has no room to draw a face for — the counts are the whole picture."""
    d = _daemon()
    _hold(d, "w1", {"state": "working", "last_event": time.time()})
    _hold(d, "w2", {"state": "thinking", "last_event": time.time()})
    _hold(d, "i1", {"state": "idle", "last_event": time.time()})
    _hold(d, "i2", {"state": "registered", "last_event": time.time()})
    _hold(d, "a1", {"state": "waiting", "last_event": time.time()})
    _hold(d, "a2", {"state": "error", "last_event": time.time()})

    state = d._activity_counts()
    assert state == {"working": 2, "idle": 2, "attention": 2, "subagents": 0}
    assert sum(state.values()) == len(d._session_states)


def test_subagents_counted_across_all_sessions():
    d = _daemon()
    _hold(d, "s1", {**_now("working"),
                           "subagents": {"a1", "a2"}})
    _hold(d, "s2", {**_now("idle"),
                           "subagents": {"a3"}})

    state = d._activity_counts()
    assert state["subagents"] == 3
    # A live subagent makes its parent "working" even when the parent is idle.
    assert state == {"working": 2, "idle": 0, "attention": 0, "subagents": 3}


def test_activity_counts_match_the_menu_bar_push():
    """One bucketing feeds both surfaces — the observer push and the wire payload
    must never disagree."""
    seen = []

    class Observer:
        def on_notification_change(self, notifications): pass
        def on_activity_change(self, working, idle, attention, subagents):
            seen.append((working, idle, attention, subagents))

    d = BobDaemon(observer=Observer())
    _hold(d, "s1", {"state": "working", "last_event": time.time()})
    _hold(d, "s2", {"state": "waiting", "last_event": time.time()})
    _hold(d, "s3", {"state": "idle", "last_event": time.time()})

    d._notify_activity()
    counts = d._activity_counts()
    assert seen[-1] == (counts["working"], counts["idle"], counts["attention"], 0)
    assert seen[-1] == (1, 1, 1, 0)


# --- Slots for sessions only the reconciler can see -------------------------
#
# The HUD counts every session `claude agents --json` reports; the slots used to
# come from the hook stream alone. On a machine with four background/foreign
# sessions that read as "5 agents" over a single face. These pin the two together.


def _rec(sid, activity="busy", *, kind="interactive", age=0.0, name="n"):
    return AgentRecord(sid, kind=kind, activity=activity, name=name,
                       started_at=time.time() - age)


def _blocked_rec(sid, age):
    return _rec(sid, activity="blocked", kind="background", age=age)


def test_abandoned_agents_alone_still_sleep_the_display():
    d = _daemon()
    d._on_agent_records([_blocked_rec("zombie", 20 * 86400)])
    assert d._activity_counts() == _counts()


# --- Which animation a tool maps to (kept for the tests that read it) ---


def test_edit_tools_map_to_typing():
    """Base mapping, before the per-session costume pick — hence no session id."""
    assert _tool_to_anim("Edit") == "typing"
    assert _tool_to_anim("Write") == "typing"
    assert _tool_to_anim("NotebookEdit") == "typing"


def test_unknown_and_missing_tool_fall_back_to_typing():
    assert _tool_to_anim("SomeFutureTool") == "typing"
    assert _tool_to_anim("") == "typing"


# --- and what the question actually says, which only the hook knows in time ---

_QUESTION = {"text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"],
             "header": "Database", "id": "toolu_01"}


async def _ask(d, sid="s1", question=None):
    await _send(d, {
        "event": "tool_use", "session_id": sid, "tool_name": "AskUserQuestion",
        "question": dict(_QUESTION if question is None else question),
    })


@pytest.mark.asyncio
async def test_the_hook_carries_the_question_the_transcript_has_not_written():
    """Claude Code writes the asking turn only when the answer arrives, so for
    the whole time the dialog is up the transcript says nothing — which is how
    a row with a named choice on screen showed a bare "Waiting"."""
    d = _daemon()
    await _ask(d)
    assert d._pending_questions["s1"]["text"] == "Postgres or SQLite?"
    assert d._pending_questions["s1"]["options"] == ["Postgres", "SQLite"]
    assert d._pending_questions["s1"]["id"] == "toolu_01"


@pytest.mark.asyncio
async def test_the_answer_clears_it():
    d = _daemon()
    await _ask(d)
    await _send(d, _tool("tool_done", "AskUserQuestion", "s1"))
    assert "s1" not in d._pending_questions


@pytest.mark.asyncio
async def test_a_companion_permission_event_does_not_clear_it():
    """Claude Code fires PermissionRequest for AskUserQuestion itself, a few
    milliseconds behind the PreToolUse that set the slot (see
    `_update_session_state`'s `permission` branch, which gives it the same
    `waiting` treatment as the ask tool for the same reason). That is not proof
    the dialog left the screen, so it must not pop what `tool_use` just set —
    observed live: the row lost its question and sat on a stale card for the
    whole 8-minute wait, empty-handed 50ms after being filled."""
    d = _daemon()
    await _ask(d)
    await _send(d, _tool("permission", "AskUserQuestion", "s1"))
    assert d._pending_questions["s1"]["text"] == "Postgres or SQLite?"


@pytest.mark.asyncio
async def test_the_nameless_permission_companion_does_not_clear_it():
    """Claude Code sends the permission news *twice*, and only the first names
    a tool. `PermissionRequest` carries `AskUserQuestion`; the `Notification`
    a few seconds later (`notification_type=permission_prompt`) has no tool
    field at all, so it reaches the tracker naming nothing — and the guard
    above, written for the first, let the second through.

    Measured on a live session: question recorded at 11:15:10.856, popped at
    11:15:16.933 by the nameless companion, and gone for the whole four-minute
    wait — nothing restores it, because the transcript only carries the asking
    turn once the answer does. An event that names no tool is evidence about
    no tool.
    """
    d = _daemon()
    await _ask(d)
    await _send(d, {
        "event": "permission", "session_id": "s1", "tool_name": "",
    })
    assert d._pending_questions["s1"]["text"] == "Postgres or SQLite?"
    # And the same with the field absent rather than empty.
    await _send(d, {"event": "permission", "session_id": "s1"})
    assert d._pending_questions["s1"]["text"] == "Postgres or SQLite?"


@pytest.mark.asyncio
async def test_a_permission_event_for_another_tool_still_clears_it():
    d = _daemon()
    await _ask(d)
    await _send(d, _tool("permission", "Bash", "s1"))
    assert "s1" not in d._pending_questions


@pytest.mark.asyncio
async def test_any_other_tool_call_clears_it():
    """A dialog cannot be up while the model is running a tool: the answer was
    given, and only a missed PostToolUse hid it."""
    d = _daemon()
    await _ask(d)
    await _send(d, _tool("tool_use", "Bash", "s1"))
    assert "s1" not in d._pending_questions


@pytest.mark.asyncio
async def test_a_typed_prompt_and_a_new_session_clear_it():
    d = _daemon()
    await _ask(d)
    await _send(d, {"event": "dismiss", "session_id": "s1",
                             "hook": "UserPromptSubmit"})
    assert "s1" not in d._pending_questions
    await _ask(d)
    await _send(d, _start("s1"))
    assert "s1" not in d._pending_questions


@pytest.mark.asyncio
async def test_a_second_question_replaces_the_first_even_unreadable():
    """An installed hook handler older than this daemon sends no payload. The
    slot is emptied anyway — the previous question standing under a new dialog
    would send somebody to answer the wrong thing."""
    d = _daemon()
    await _ask(d)
    await _send(d, _tool("tool_use", "AskUserQuestion", "s1"))
    assert "s1" not in d._pending_questions


@pytest.mark.asyncio
async def test_the_payload_is_re_cut_on_arrival():
    """The handler on disk trimmed it, but it is a file outside the bundle and
    may be any age — the caps are re-applied here rather than trusted."""
    d = _daemon()
    await _ask(d, question={"text": "q" * 900, "header": "h" * 500,
                            "options": ["o" * 300, "b", "c", "d", "e"],
                            "id": "toolu_02"})
    held = d._pending_questions["s1"]
    assert len(held["text"]) == ss.MAX_QUESTION_CHARS
    assert len(held["options"][0]) == ss.MAX_OPTION_CHARS
    assert len(held["options"]) == ss.MAX_OPTIONS


@pytest.mark.asyncio
async def test_a_question_free_ask_leaves_no_slot():
    d = _daemon()
    await _ask(d, question={"text": "", "options": []})
    assert "s1" not in d._pending_questions


@pytest.mark.asyncio
async def test_grok_ask_user_question_tool_use_sets_waiting():
    d = _daemon()
    await _send(d, {
        "event": "tool_use", "session_id": "g1",
        "tool_name": "ask_user_question", "provider": "grok",
    })
    assert d._session_states["g1"]["state"] == "waiting"
    assert d._session_states["g1"]["provider"] == "grok"


@pytest.mark.asyncio
async def test_a_grok_permission_already_allowed_is_not_a_wait(tmp_path):
    """Grok fires `permission_prompt` for every tool call and resolves the
    allowed ones in milliseconds; a row that went `waiting` on each one
    read "waiting for you" for the whole length of a shell command with no
    dialog anywhere (Proxy and Quiet, 21 Sep). The session's own log is
    asked first, by the hook's own id and cwd."""
    import json as _json
    d = _daemon()
    cwd = str(tmp_path / "proj")
    directory = tmp_path / "sessions" / grok_roster.quote(cwd, safe="") / "g1"
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text("".join(_json.dumps(r) + "\n" for r in [
        {"ts": "2026-09-21T09:21:31.639Z", "type": "permission_requested",
         "tool_name": "run_terminal_command"},
        {"ts": "2026-09-21T09:21:31.652Z", "type": "permission_resolved",
         "tool_name": "run_terminal_command", "decision": "allow", "wait_ms": 13},
    ]))
    d._session_states["g1"] = {**_now("working"),
                               "tool_name": "run_terminal_command",
                               "provider": "grok"}
    with patch.object(grok_roster, "SESSIONS_DIR", tmp_path / "sessions"):
        await _send(d, {"event": "permission", "session_id": "g1",
                                 "tool_name": "", "provider": "grok",
                                 "cwd": cwd})
    assert d._session_states["g1"]["state"] == "working"
    # The genuine prompt — a request the log has not resolved — still waits.
    with (directory / "events.jsonl").open("a") as fh:
        fh.write(_json.dumps({"ts": "2026-09-21T09:22:00.000Z",
                              "type": "permission_requested",
                              "tool_name": "run_terminal_command"}) + "\n")
    with patch.object(grok_roster, "SESSIONS_DIR", tmp_path / "sessions"):
        await _send(d, {"event": "permission", "session_id": "g1",
                                 "tool_name": "", "provider": "grok",
                                 "cwd": cwd})
    assert d._session_states["g1"]["state"] == "waiting"


class _FakeLiveProc:
    """Stands in for psutil.Process in the liveness identity check."""

    def __init__(self, name="claude", cmdline=None):
        self._name = name
        self._cmdline = cmdline if cmdline is not None else ["claude"]

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline


def test_liveness_evicts_a_recycled_pid():
    """The PID is alive but belongs to a stranger — the session is gone.

    A bare pid_exists() reads a recycled PID as alive forever. That mattered most
    for a 'waiting' session, which is exempt from wall-clock staleness *because*
    this check is meant to catch it, and which stop_session also refuses to signal
    once the identity no longer matches — so the row became unremovable.
    """
    from unittest.mock import patch
    d = _daemon()
    _hold(d, "s1", _silent("waiting", 0.0, wall=0.0, pid=4242))

    with patch("dark_army_daemon.daemon.psutil.Process",
               return_value=_FakeLiveProc(name="nginx", cmdline=["nginx", "-g"])):
        d._check_liveness()

    assert list(d._session_states) == []


def test_liveness_keeps_stale_waiting_session_without_pid():
    """A no-pid 'waiting' session (blocked on the human) is not evicted by grace.

    Exempt from the short pidless grace only — see the sibling below for the
    wall-clock bound it does get.
    """
    from dark_army_daemon.daemon import PIDLESS_GRACE_SECONDS
    d = _daemon()
    _hold(d, "s1", {
        **_now("waiting"),
        "last_event_monotonic": time.monotonic() - PIDLESS_GRACE_SECONDS - 1,
    })

    d._check_liveness()

    assert "s1" in d._session_states


def test_liveness_evicts_pidless_waiting_session_past_staleness_timeout():
    """A no-pid 'waiting' session is not immortal.

    `_evict_stale_sessions` skips `waiting` too, so before this rule a pidless
    waiter had *no* eviction path at all and sat under "Needs you" for ever.
    Kept inside the staleness timeout, evicted beyond it.
    """
    d = _daemon()
    timeout = d._session_staleness_timeout
    _hold(d, "kept", {
        **_now("waiting"),
        "last_event_monotonic": time.monotonic() - timeout + 30,
    })
    _hold(d, "gone", {
        **_now("waiting"),
        "last_event_monotonic": time.monotonic() - timeout - 1,
    })

    evicted = d._check_liveness()

    assert "kept" in d._session_states
    assert "gone" not in d._session_states
    assert "gone" in evicted


def test_liveness_pidless_confused_session_keeps_the_waiter_ladder():
    """A `Notification`-parked pidless session is on the same ladder as a
    `waiting` one: exempt from the 90s grace, evicted past the wall clock.

    Widening the eviction exemption without widening this one would make it
    immortal — the failure the pidless branch exists to prevent."""
    d = _daemon()
    timeout = d._session_staleness_timeout
    assert timeout > PIDLESS_GRACE_SECONDS
    _hold(d, "kept", {
        **_now("confused"),
        "last_event_monotonic": time.monotonic() - PIDLESS_GRACE_SECONDS - 30,
    })
    # Parked beside its card: that is what makes it a waiter (22 Sep 2026).
    d._active_notifications["kept"] = {"session_id": "kept",
                                       "message": "Waiting for input"}
    _hold(d, "gone", {
        **_now("confused"),
        "last_event_monotonic": time.monotonic() - timeout - 1,
    })
    _hold(d, "plain", {
        **_now("idle"),
        "last_event_monotonic": time.monotonic() - PIDLESS_GRACE_SECONDS - 30,
    })
    # A snag with no card is not waiting on a person: the 90s grace takes it
    # like any other quiet pidless row.
    _hold(d, "snag", {
        **_now("confused"),
        "last_event_monotonic": time.monotonic() - PIDLESS_GRACE_SECONDS - 30,
    })

    evicted = d._check_liveness()

    assert "kept" in d._session_states
    assert "gone" not in d._session_states
    assert "plain" not in d._session_states
    assert "snag" not in d._session_states
    assert {"gone", "plain", "snag"} <= set(evicted)


# --- Per-session animation variants ---


def test_variant_is_sticky_per_session():
    """A session keeps the same costume for its whole life — a face that reshuffled
    its sprite on every push would read as a state change that never happened."""
    for sid in ("aaa", "s1", "some-uuid-4242"):
        assert len({_tool_to_anim("Edit", sid) for _ in range(50)}) == 1


def test_variant_index_is_not_process_salted():
    """Pinned literals: session ids outlive the daemon, so the mapping has to
    survive a restart. Python's builtin hash() would not (PYTHONHASHSEED)."""
    from dark_army_daemon.daemon import _variant_index

    assert _variant_index("aaa", 2) == 0
    assert _variant_index("s1", 2) == 1


def test_both_typing_variants_are_reachable():
    got = {_tool_to_anim("Edit", f"session-{i}") for i in range(200)}
    assert got == {"typing", "tongue_zap"}


def test_variants_only_apply_to_pooled_animations():
    """Animations with no pool are untouched by session id — a variant must never
    invent a costume for a state that has only one look."""
    for sid in ("aaa", "s1", "zzz"):
        assert _tool_to_anim("Bash", sid) == "building"
        assert _tool_to_anim("Read", sid) == "debugger"
        assert _tool_to_anim("mcp__x__y", sid) == "beacon"



# ── A Grok subagent is not a session ─────────────────────────────────────────
#
# Grok addresses its children by their own session id: a child has a session
# directory on disk and fires its own PreToolUse / UserPromptSubmit /
# SessionEnd, stamped with that id and — verified on a live fleet — no
# `parentSessionId`. Left alone the first of those opened a row, so one
# terminal was drawn as two agents with two nicknames, and the parent went
# quiet underneath its own child and was evicted as stale.

CHILD = "01a00e3d-6809-7c11-bb63-78bcccc6caf5"
PARENT = "01a00cb0-0e74-75f3-ae20-4cee03a981a1"


def _grok_parent_with_child(d):
    d._session_states[PARENT] = {
        "state": "working", "last_event": time.time() - 500,
        "provider": "grok", "subagents": {CHILD},
    }
    return d


@pytest.mark.asyncio
async def test_a_childs_tool_call_lands_on_its_parent():
    d = _grok_parent_with_child(_daemon())
    old = d._session_states[PARENT]["last_event"]
    await _send(d, {
        "event": "tool_use", "session_id": CHILD, "tool_name": "read_file",
        "provider": "grok",
    })
    assert CHILD not in d._session_states, "a subagent must not become a row"
    # The parent is what the work belongs to — and this is what keeps it off
    # the staleness evictor while the child does the work.
    assert d._session_states[PARENT]["last_event"] > old
    assert d._session_states[PARENT]["state"] == "working"


@pytest.mark.asyncio
async def test_a_childs_own_end_does_not_end_the_parent():
    d = _grok_parent_with_child(_daemon())
    await _send(d, {
        "event": "dismiss", "hook": "SessionEnd", "session_id": CHILD,
        "provider": "grok",
    })
    assert PARENT in d._session_states


@pytest.mark.asyncio
async def test_a_childs_stop_raises_no_card():
    """The spawn prompt is not a human typing, and a child parking its turn is
    not a request for one."""
    d = _grok_parent_with_child(_daemon())
    await _send(d, {
        "event": "add", "hook": "Stop", "session_id": CHILD,
        "message": "Waiting for input", "provider": "grok",
    })
    assert not d._active_notifications


@pytest.mark.asyncio
async def test_a_childs_prompt_does_not_dismiss_the_parents_card():
    d = _grok_parent_with_child(_daemon())
    d._active_notifications[PARENT] = {"hook": "Stop", "session_id": PARENT}
    await _send(d, {
        "event": "dismiss", "hook": "UserPromptSubmit", "session_id": CHILD,
        "provider": "grok",
    })
    assert PARENT in d._active_notifications


@pytest.mark.asyncio
async def test_a_childs_own_stop_still_clears_it_from_the_parents_set():
    """Grok fires SubagentStop inside the child: the child id arrives as both
    the session and the agent_id, with no parent field."""
    d = _grok_parent_with_child(_daemon())
    await _send(d, {
        "event": "subagent_stop", "session_id": CHILD, "agent_id": CHILD,
        "provider": "grok",
    })
    assert CHILD not in d._session_states[PARENT].get("subagents", set())


@pytest.mark.asyncio
async def test_a_stray_child_row_is_dropped_without_a_tombstone():
    """A row opened by a daemon from before this rule, restored out of
    sessions.json. It is not a run that finished, so it must not surface in
    Recently finished."""
    d = _grok_parent_with_child(_daemon())
    d._session_states[CHILD] = {**_now("working"),
                                "provider": "grok"}
    await _send(d, {
        "event": "tool_use", "session_id": CHILD, "tool_name": "grep",
        "provider": "grok",
    })
    assert CHILD not in d._session_states
    assert CHILD not in d._finished


@pytest.mark.asyncio
async def test_a_session_nobody_claims_is_left_alone():
    """The redirect fires on an id a parent actually holds, and on nothing
    else — a plain session must never be swallowed by one."""
    d = _daemon()
    await _send(d, {
        "event": "tool_use", "session_id": "s-plain", "tool_name": "Read",
    })
    assert "s-plain" in d._session_states


def test_two_claimants_are_not_guessed_between():
    """Not something Grok can produce, but picking one would move somebody
    else's work onto the wrong row."""
    d = _daemon()
    for sid in ("p1", "p2"):
        d._session_states[sid] = {**_now("working"),
                                  "subagents": {CHILD}}
    assert d._parent_of_child(CHILD) == ""


# ── a name is not released by going quiet ────────────────────────────────────

def _parsed(*sids):
    """The (stub, transcript, stats) triples _assign_nicknames reads."""
    return [({"session_id": sid}, "", None) for sid in sids]


def test_a_name_survives_a_snapshot_the_session_is_missing_from(tmp_path):
    """The rename this fixes. A session evicted for going quiet comes back the
    moment its human types again — one did, after seven hours — and used to
    re-pick, landing on a different character while its terminal tab still wore
    the old badge."""
    d = BobDaemon(identities_path=tmp_path / "identities.json")
    first = d._assign_nicknames(_parsed("gone-quiet", "busy"))["gone-quiet"]
    d._assign_nicknames(_parsed("busy"))                       # evicted
    d._assign_nicknames(_parsed("other"))                      # and again
    assert d._assign_nicknames(_parsed("gone-quiet"))["gone-quiet"] == first


def test_a_remembered_name_is_not_withheld_from_the_living(tmp_path):
    """`taken` is built from what is on screen, so an absent session's name is
    still offered to a new one — and the returning session is the one that
    yields, so the cast never runs short and no two rows share a name."""
    d = BobDaemon(identities_path=tmp_path / "identities.json")
    d._assign_nicknames(_parsed("away"))
    live = [f"live{i}" for i in range(6)]
    names = d._assign_nicknames(_parsed(*live))
    assert len(set(names.values())) == 6

    back = d._assign_nicknames(_parsed("away", *live))
    assert len(set(back.values())) == 7
    for sid in live:
        assert back[sid] == names[sid], "a row on screen must never be renamed"


def test_a_returning_session_yields_a_name_a_live_row_is_wearing(tmp_path):
    """Both hold `Cipher`: the one already showing it keeps it, and the one
    that just walked back in re-picks. Renaming the row on screen would be the
    bug this whole rule exists to prevent."""
    d = BobDaemon(identities_path=tmp_path / "identities.json")
    away = d._assign_nicknames(_parsed("away"))["away"]
    d._identities._sessions["squatter"] = away          # taken while away
    d._assign_nicknames(_parsed("squatter"))            # and drawn under it

    both = d._assign_nicknames(_parsed("squatter", "away"))
    assert both["squatter"] == away
    assert both["away"] != away
    assert len(set(both.values())) == 2


# --- A pending question survives a restart -----------------------------------
#
# `AskUserQuestion`'s text exists only in the PreToolUse hook while the dialog
# is up (the transcript gets the asking turn alongside the answer), so a
# restart mid-dialog left the blocked row with nothing to say — observed on a
# live session as eight minutes of the *previous* turn's prose under
# "Needs you".

ASK_MSG = {
    "event": "tool_use",
    "session_id": "s1",
    "tool_name": "AskUserQuestion",
    "question": {"text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"],
                 "header": "Store", "id": "toolu_1"},
}


@pytest.mark.asyncio
async def test_pending_question_restored_by_a_new_daemon(tmp_path):
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    await d._handle_message(dict(ASK_MSG))
    revived = BobDaemon(sessions_path=path)
    assert revived._pending_questions["s1"]["text"] == "Postgres or SQLite?"
    assert revived._pending_questions["s1"]["options"] == ["Postgres", "SQLite"]


@pytest.mark.asyncio
async def test_answered_question_is_cleared_on_disk_too(tmp_path):
    """The clear has to reach the file, or the restart resurrects it."""
    path = tmp_path.joinpath("sessions.json")
    d = BobDaemon(sessions_path=path)
    await d._handle_message(dict(ASK_MSG))
    await _send(d, {"event": "tool_done", "session_id": "s1",
                             "tool_name": "AskUserQuestion"})
    assert d._pending_questions == {}
    assert json.loads(path.read_text()).get("pending_questions", {}) == {}
    assert BobDaemon(sessions_path=path)._pending_questions == {}


def test_restore_skips_a_session_that_is_not_waiting(tmp_path):
    """A restored question is only news while the dialog can still be up."""
    path = tmp_path.joinpath("sessions.json")
    from dark_army_daemon.session_store import save_sessions
    save_sessions(
        {"s1": {"state": "working", "last_event": time.time()}},
        path,
        {"s1": {"text": "Postgres or SQLite?", "id": "toolu_1"}},
    )
    assert BobDaemon(sessions_path=path)._pending_questions == {}


def test_a_non_list_subagent_spawns_is_dropped_on_restore(tmp_path):
    """`subagent_spawns` is read by iterating it; a string from a damaged
    file would be walked a character at a time. A list survives."""
    from dark_army_daemon.session_store import load_sessions, save_sessions
    path = tmp_path / "sessions.json"
    now = time.time()
    save_sessions({
        "bad": {"state": "idle", "last_event": now, "subagent_spawns": "oops"},
        "good": {"state": "idle", "last_event": now,
                 "subagent_spawns": [["bc-implementer", 5.0]]},
    }, path)
    loaded = load_sessions(path)
    assert "subagent_spawns" not in loaded["bad"]
    assert loaded["good"]["subagent_spawns"] == [["bc-implementer", 5.0]]
