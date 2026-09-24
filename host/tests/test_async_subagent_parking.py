"""A session waiting on an async subagent is not waiting on you.

The bug this pins, measured 7 Sep 2026 on Claude Code 2.1.263: an `Agent(...)`
spawned to run in the background has its `SubagentStop` hook fired when the
*spawning turn* ends — seconds in, while the child works on for minutes. The
parent's live `subagents` set empties, its own Stop then finds nothing to park
on, and a session merely waiting for its own child is raised as "Waiting for
input" and lands under Needs you.

Two halves are tested here: `subagent_watch`'s reading of a child transcript,
and the daemon rung that consults it at the one place a card is raised.

The reading asks **whether the file moved after the stop hook**. See that
module's docstring for the measurement; what these tests exist to stop coming
back is the reading that preceded it, which asked only whether the last record
said `end_turn`. That is sound where it fires and absent from 42% of finished
children — so an ordinary synchronous spawn suppressed its parent's real card
for fifteen minutes, which is this rung's own failure mode inverted.
"""

import asyncio
import json
import os
import time

import pytest
from pathlib import Path

from dark_army_daemon import subagent_watch
from dark_army_daemon.daemon import BobDaemon


# Mid-run. `stop_reason` is null on nearly every line a real child writes.
RUNNING = json.dumps({"type": "assistant", "message": {
    "content": [{"type": "tool_use", "name": "Bash"}]},
    "stop_reason": None})
# Mid-run, and the shape that defeats a structural reading: an opening
# narration, on its own line, indistinguishable from a final answer.
NARRATING = json.dumps({"type": "assistant", "message": {
    "content": [{"type": "text", "text": "Right, let me look."}]},
    "stop_reason": None})
# Returned.
FINISHED = json.dumps({"type": "assistant", "message": {
    "content": [{"type": "text", "text": "done"}]},
    "stop_reason": "end_turn"})


# The harness's own string, as a hook carries it. Note the dot: a path
# rebuilt from the cwd would mangle it to `-` and miss the directory
# entirely, which is the bug this signature exists to make impossible.
def _session_transcript(tmp_path, session_id="s"):
    d = tmp_path / ".claude" / "projects" / "-Users-me--work-proj.v2"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{session_id}.jsonl")


def _transcript(tmp_path, session_id, agent_id, body, wrote_after=60.0):
    """A child transcript last written `wrote_after` seconds past its stop
    hook. The default is the async case — the child went on working."""
    tp = _session_transcript(tmp_path, session_id)
    d = Path(tp).parent / session_id / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    p.write_text(body)
    when = STOPPED_AT + wrote_after
    os.utime(p, (when, when))
    return p


# Every child here stopped at this instant; the fixtures differ in what their
# file did afterwards, which is the only thing the reading asks.
STOPPED_AT = time.time() - 120


# ── subagent_watch: reading one child's transcript ──────────────────────────

def test_the_path_is_composed_from_the_harnesss_own_string(tmp_path):
    """Never rebuilt from the cwd. Claude Code mangles `.` to `-` as well as
    `/`, so `/Users/me/.dark-army` lives under `-Users-me--dark-army`
    — a re-derived directory is silently wrong for every dotted path, and a
    rung that finds nothing reads exactly like one that is working."""
    tp = "/h/.claude/projects/-Users-me--work-proj.v2/sess.jsonl"
    assert subagent_watch.transcript_for(tp, "ag") == Path(
        "/h/.claude/projects/-Users-me--work-proj.v2/sess/subagents"
        "/agent-ag.jsonl")


@pytest.mark.parametrize("tp,aid", [
    ("", "ag"), ("/h/p/sess.jsonl", ""), ("/h/p/sess.jsonl", ".."),
    ("/h/p/sess.jsonl", "../../etc"), ("/h/p/sess.jsonl", "a/b"),
])
def test_nothing_underivable_or_traversing_yields_a_path(tp, aid):
    """`agent_id` arrives from a hook payload, which authenticates nothing.
    A path is composed or it is not; it is never repaired."""
    assert subagent_watch.transcript_for(tp, aid) is None


def test_a_child_still_writing_after_its_stop_hook_is_running(tmp_path):
    """The async case, and the whole point of the rung: the hook fired when
    the parent's turn ended, and the child worked on."""
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n", wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is not None


def test_a_child_frozen_at_its_stop_hook_has_finished(tmp_path):
    """The synchronous case — 399 of the 447 real stops measured on this
    machine — and the one the previous reading got wrong. Its last line says
    `stop_reason: null`, exactly like a running child's; only the mtime
    separates them.

    Getting this wrong is not a missed optimisation. It suppresses the card
    *and* the state for every ordinary spawn, so the row reads `working` with
    nobody told, and the only thing that clears it is a prompt the person was
    never asked for.
    """
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n", wrote_after=0)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_a_child_narrating_mid_run_is_still_running(tmp_path):
    """A `text` record on its own line is what a child writes before its next
    tool call, and it is byte-shaped like a final answer — the harness writes
    one record per content block. Nothing about the line can settle it."""
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n" + NARRATING + "\n")
    assert subagent_watch.still_running(p, STOPPED_AT) is not None


def test_a_child_with_no_terminal_marker_is_still_read_by_its_mtime(tmp_path):
    """506 of the 1,194 finished children on this machine end `stop_reason:
    null` — Opus, Fable and Sonnet, not one model's quirk. The mtime answers
    them both ways round; the markers below are an early release only."""
    p = _transcript(tmp_path, "s", "a", NARRATING + "\n", wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is not None
    p = _transcript(tmp_path, "s", "b", NARRATING + "\n", wrote_after=0)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_end_turn_releases_a_child_that_is_still_being_written(tmp_path):
    """The markers can only ever let a card through *sooner*, which is the
    safe direction. A file touched a minute after its hook would otherwise
    read as running."""
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n" + FINISHED + "\n",
                    wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_a_child_the_person_interrupted_is_finished(tmp_path):
    """The other ending: 16 of the 411 subagent transcripts sampled end on a
    `user` record, 11 of them this. Such a child writes no `end_turn`."""
    stopped = json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": "[Request interrupted by user]"}]}})
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n" + stopped + "\n",
                    wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_a_pending_id_with_no_stop_time_falls_back_to_freshness(tmp_path):
    """A `sessions.json` written before the stop times went in beside the ids
    restores a bare list. It must still read, not throw."""
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n", wrote_after=0)
    assert subagent_watch.still_running(p, None) is not None
    got = subagent_watch.running_ids(_session_transcript(tmp_path), ["a"])
    assert list(got) == ["a"]


def test_the_gap_either_side_of_the_threshold_is_real(tmp_path):
    """`WROTE_AFTER_STOP_SECONDS` sits inside a measured empty band — nothing
    on this machine landed between 1s and 7s past its hook — so it is not a
    tuned number and neither edge of the band may drift onto it."""
    assert 1.0 < subagent_watch.WROTE_AFTER_STOP_SECONDS < 7.0
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n", wrote_after=1)
    assert subagent_watch.still_running(p, STOPPED_AT) is None
    p = _transcript(tmp_path, "s", "b", RUNNING + "\n", wrote_after=7)
    assert subagent_watch.still_running(p, STOPPED_AT) is not None


def test_a_trailing_blank_line_does_not_hide_the_marker(tmp_path):
    p = _transcript(tmp_path, "s", "a", FINISHED + "\n\n\n", wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_the_marker_is_read_on_the_last_line_only(tmp_path):
    """An `end_turn` further up the file is some earlier turn's."""
    p = _transcript(tmp_path, "s", "a", FINISHED + "\n" + NARRATING + "\n",
                    wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is not None


def test_whitespace_around_the_marker_still_matches(tmp_path):
    p = _transcript(tmp_path, "s", "a", '{"stop_reason" : "end_turn"}\n',
                    wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_a_missing_file_is_not_running(tmp_path):
    """Failure fails **open**: a True here suppresses a card a person would
    otherwise be shown, so anything unreadable must read as finished."""
    assert subagent_watch.still_running(tmp_path / "nope.jsonl", 0) is None
    assert subagent_watch.still_running(None, 0) is None


def test_an_empty_file_is_not_running(tmp_path):
    p = _transcript(tmp_path, "s", "a", "", wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is None


def test_a_fifo_at_that_name_cannot_hang_the_daemon(tmp_path):
    """The path is composed from two strings a hook payload chose, and the
    hook socket authenticates nothing beyond the project key. A FIFO left
    there blocks `open()` for ever — on the asyncio loop, so no SSE, no API,
    no hooks — and `sessions.json` would restore the state and re-hang it on
    the next start. The open is non-blocking and the type is checked on the
    descriptor.

    The test would not fail; it would never return. So it is run with an
    alarm, and a hang is reported as one rather than as a stalled suite.
    """
    import signal
    tp = _session_transcript(tmp_path, "s")
    d = Path(tp).parent / "s" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    os.mkfifo(d / "agent-a.jsonl")

    def _bang(*_):
        raise AssertionError("still_running blocked on a FIFO")

    old = signal.signal(signal.SIGALRM, _bang)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        assert subagent_watch.running_ids(tp, {"a": STOPPED_AT}) == {}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_a_character_device_is_not_a_transcript(tmp_path, monkeypatch):
    """`/dev/zero` reports `st_size` 0, so the tail seek never fires and the
    read never ends. Refused by type, not by size."""
    monkeypatch.setattr(subagent_watch, "transcript_for",
                        lambda *_: Path("/dev/zero"))
    assert subagent_watch.running_ids("x", {"a": STOPPED_AT}) == {}


def test_a_stale_transcript_stops_holding_its_parent(tmp_path):
    """The backstop: a child killed outright writes nothing more, so only
    this ever releases its parent."""
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n")
    old = time.time() - subagent_watch.STALE_SECONDS - 60
    os.utime(p, (old, old))
    assert subagent_watch.still_running(p, old - 600) is None


def test_the_freshness_window_outlasts_a_long_silent_tool_call(tmp_path):
    """A child running this repo's own test suite writes nothing for ~7
    minutes. It must not be read as finished while it does."""
    assert subagent_watch.STALE_SECONDS >= 10 * 60
    p = _transcript(tmp_path, "s", "a", RUNNING + "\n")
    quiet = time.time() - 8 * 60
    os.utime(p, (quiet, quiet))
    assert subagent_watch.still_running(p, quiet - 600) is not None


def test_only_the_tail_is_read(tmp_path):
    """The read is bounded. A final record longer than the window arrives
    sliced with its `stop_reason` cut off, so it reads as *running* and is
    released by the freshness window instead."""
    huge = json.dumps({"stop_reason": "end_turn",
                       "x": "y" * (subagent_watch.TAIL_BYTES * 2)})
    p = _transcript(tmp_path, "s", "a", huge + "\n", wrote_after=60)
    assert subagent_watch.still_running(p, STOPPED_AT) is not None


def test_running_ids_keeps_order_and_dedupes(tmp_path):
    one = _transcript(tmp_path, "s", "one", RUNNING + "\n", wrote_after=60)
    _transcript(tmp_path, "s", "two", FINISHED + "\n", wrote_after=60)
    three = _transcript(tmp_path, "s", "three", NARRATING + "\n",
                        wrote_after=60)
    _transcript(tmp_path, "s", "four", NARRATING + "\n", wrote_after=0)
    pending = {"one": STOPPED_AT, "two": STOPPED_AT, "three": STOPPED_AT,
               "four": STOPPED_AT}
    got = subagent_watch.running_ids(_session_transcript(tmp_path), pending)
    assert list(got) == ["one", "three"]
    # The value is the mtime just observed, so the caller stores it back as
    # the next `since` without a second `stat`.
    assert got["one"] == pytest.approx(one.stat().st_mtime)
    assert got["three"] == pytest.approx(three.stat().st_mtime)


def test_a_child_that_stops_writing_releases_its_parent_next_time(tmp_path):
    """The reading is a liveness test, not a latch.

    An async child writes past its stop hook — that is what parks its parent.
    When it later finishes without a terminal marker (42% of them do), the
    only thing left to release the parent must not be `STALE_SECONDS`, or the
    genuine ask is suppressed for fifteen minutes. Storing the observed mtime
    back as the next `since` is what makes the second look ask for *new*
    movement.

    This pins `running_ids`' contract and **cannot** catch a daemon-side
    regression — it hands the returned map back by hand, so it passes even
    when the daemon forgets to store it.
    `test_a_park_restamps_so_the_next_stop_is_not_held_by_a_latch` is the
    only guard on that store, and must not be weakened.
    """
    tp = _session_transcript(tmp_path)
    child = _transcript(tmp_path, "s", "a", NARRATING + "\n", wrote_after=60)
    first = subagent_watch.running_ids(tp, {"a": STOPPED_AT})
    assert list(first) == ["a"]
    # Nothing more is written: the child has finished, marker or no marker.
    assert subagent_watch.running_ids(tp, first) == {}
    # And a child that *does* write on is still held.
    os.utime(child, (STOPPED_AT + 300, STOPPED_AT + 300))
    assert list(subagent_watch.running_ids(tp, first)) == ["a"]


# ── the daemon rung ─────────────────────────────────────────────────────────

def _daemon():
    return BobDaemon()


def _stop_hook(session_id):
    return {"event": "add", "hook": "Stop", "session_id": session_id,
            "message": "Claude is waiting for your input"}


def test_a_stop_hook_stops_discarding_the_id_it_cannot_prove_finished():
    """The live set still empties — that half is unchanged — but the id is
    kept so the transcript can be asked later."""
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time(),
                              "subagents": {"ag"}}
    d._update_session_state("subagent_stop", "SubagentStop", "s",
                            agent_id="ag")
    assert d._session_states["s"]["subagents"] == set()
    # The time is the payload, not just the id: it is what separates a child
    # that stopped because it finished from one whose hook fired early.
    pending = d._session_states["s"]["subagents_async"]
    assert list(pending) == ["ag"]
    assert abs(pending["ag"] - time.time()) < 5


def test_a_stop_on_a_restored_list_does_not_throw():
    """`session_store` keeps a bare list rather than dropping it, so the
    writer has to accept one. `setdefault(...)[id] = t` on a list raises
    `TypeError` — swallowed by the socket server, which would silently drop
    every later stop hook for that session."""
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time(),
                              "subagents": {"ag"},
                              "subagents_async": ["old"]}
    d._update_session_state("subagent_stop", "SubagentStop", "s",
                            agent_id="ag")
    pending = d._session_states["s"]["subagents_async"]
    assert isinstance(pending, dict)
    assert sorted(pending) == ["ag", "old"]
    assert all(isinstance(v, float) for v in pending.values())


def test_a_typed_prompt_clears_the_pending_list():
    """A prompt was taken, so no child of the previous turn is still owed an
    answer — the same argument that already clears the live set."""
    d = _daemon()
    d._session_states["s"] = {"state": "idle", "last_event": time.time(),
                              "subagents": {"ag"},
                              "subagents_async": {"ag": time.time()},
                              "async_park_at": time.monotonic()}
    d._update_session_state("dismiss", "UserPromptSubmit", "s")
    assert "subagents_async" not in d._session_states["s"]
    assert "async_park_at" not in d._session_states["s"]


def test_a_still_running_async_child_parks_the_parents_stop(tmp_path):
    _transcript(tmp_path, "s", "ag", RUNNING + "\n", wrote_after=60)
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time(),
                              "subagents": set(),
                              "transcript_path": _session_transcript(tmp_path),
                              "subagents_async": {"ag": STOPPED_AT}}
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["state"] != "idle"


def test_a_park_restamps_so_the_next_stop_is_not_held_by_a_latch(tmp_path):
    """The parent's second Stop must ask a fresh question.

    Parking on "it wrote past its hook" would stay true for ever, so a child
    that finished without a terminal marker would hold its parent's genuine
    ask for `STALE_SECONDS`. The park stores the mtime it saw, and the next
    look needs movement past *that*.
    """
    child = _transcript(tmp_path, "s", "ag", NARRATING + "\n",
                        wrote_after=60)
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time(),
                              "subagents": set(),
                              "transcript_path": _session_transcript(tmp_path),
                              "subagents_async": {"ag": STOPPED_AT}}
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["subagents_async"]["ag"] == pytest.approx(
        child.stat().st_mtime)
    # The child then finishes, writing no marker. The very next Stop raises
    # the card rather than waiting fifteen minutes for the file to go stale.
    d._session_states["s"]["state"] = "working"
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" in d._active_notifications
    assert "subagents_async" not in d._session_states["s"]


def test_a_finished_child_lets_the_card_through_and_empties_the_list(
        tmp_path):
    _transcript(tmp_path, "s", "ag", FINISHED + "\n", wrote_after=60)
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time(),
                              "subagents": set(),
                              "transcript_path": _session_transcript(tmp_path),
                              "subagents_async": {"ag": STOPPED_AT}}
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" in d._active_notifications
    assert "subagents_async" not in d._session_states["s"]


def test_a_session_with_no_children_is_untouched(tmp_path):
    """The rung must cost nothing and change nothing for the ordinary
    session, which is nearly all of them."""
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time(),
                              "transcript_path": _session_transcript(tmp_path),
                              "subagents": set()}
    assert d._async_subagent_reason("s") == ""
    assert d._async_subagent_reason("nobody") == ""
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" in d._active_notifications


def test_the_daemon_stores_the_transcript_path_a_hook_carries(tmp_path):
    """The rung is only as good as this: without the harness's own string it
    would have to guess the directory, which is finding 2 all over again.

    Driven with the shape the *installed* handler actually sends. Of the ten
    messages `NOTIFY_SCRIPT` can produce, exactly three carry the path — Stop,
    StopFailure and the idle-prompt Notification — and all three are the card
    branch itself. A test written against a `tool_use` payload passes while
    the rung stays inert, because nothing ever sends one.
    """
    d = _daemon()
    d._session_states["s"] = {"state": "working", "last_event": time.time()}
    msg = _stop_hook("s")
    msg["transcript_path"] = "/h/.claude/projects/-a-b.c/s.jsonl"
    asyncio.run(d._handle_message(msg))
    assert d._session_states["s"]["transcript_path"] == \
        "/h/.claude/projects/-a-b.c/s.jsonl"


def test_the_stop_that_raises_the_card_parks_on_its_own_transcript_path(
        tmp_path):
    """The whole incident, end to end, seeded with nothing.

    The path is read at the card decision and written by the same message, so
    the order of those two matters more than either: written afterwards, the
    rung consults an empty string on the one turn it exists for and the false
    card is raised anyway. Every other test here pre-seeds the key into
    session state, which is exactly the assumption that hid it.
    """
    _transcript(tmp_path, "s", "ag", RUNNING + "\n", wrote_after=60)
    tp = _session_transcript(tmp_path, "s")
    d = _daemon()
    asyncio.run(d._handle_message({
        "event": "session_start", "hook": "SessionStart", "session_id": "s",
        "cwd": "/work/proj"}))
    asyncio.run(d._handle_message(
        {"event": "dismiss", "hook": "UserPromptSubmit", "session_id": "s"}))
    asyncio.run(d._handle_message({
        "event": "tool_use", "hook": "PreToolUse", "session_id": "s",
        "tool_name": "Task"}))
    asyncio.run(d._handle_message({
        "event": "subagent_start", "hook": "SubagentStart", "session_id": "s",
        "agent_id": "ag"}))
    asyncio.run(d._handle_message({
        "event": "subagent_stop", "hook": "SubagentStop", "session_id": "s",
        "agent_id": "ag"}))
    # ...and then the child writes on, which is what makes it async. The stop
    # hook fired a moment ago, so the next line it writes lands past it.
    child = Path(tp).parent / "s" / "subagents" / "agent-ag.jsonl"
    wrote = time.time() + subagent_watch.WROTE_AFTER_STOP_SECONDS + 1
    os.utime(child, (wrote, wrote))
    stop = _stop_hook("s")
    stop["transcript_path"] = tp
    asyncio.run(d._handle_message(stop))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["state"] != "idle"


def test_an_agent_id_that_cannot_be_a_path_is_not_running(tmp_path):
    """`agent_id` rides an unauthenticated hook payload. A NUL byte makes
    `stat()` raise `ValueError`, not `OSError`; escaping, that would leave the
    id on the pending list and raise on every later message for the session —
    Dark Army blind to it for the rest of its life."""
    tp = _session_transcript(tmp_path, "s")
    assert subagent_watch.running_ids(tp, ["a\x00b", "\udc80"]) == {}


def test_the_pending_list_round_trips_and_a_corrupt_one_is_dropped(tmp_path):
    from dark_army_daemon import session_store
    path = tmp_path / "sessions.json"
    session_store.save_sessions(
        {"a": {"state": "idle", "last_event": 1.0,
               "subagents_async": {"one": 10.0, "two": 20.0},
               "async_park_at": 5.0},
         "b": {"state": "idle", "last_event": 1.0,
               "subagents_async": "one,two"},
         "c": {"state": "idle", "last_event": 1.0,
               "subagents_async": ["one"]}}, path=path)
    back = session_store.load_sessions(path)
    assert back["a"]["subagents_async"] == {"one": 10.0, "two": 20.0}
    # A monotonic stamp means nothing across a restart.
    assert "async_park_at" not in back["a"]
    # A string would be iterated per character, minting an agent id per letter.
    assert "subagents_async" not in back["b"]
    # The shape an older build wrote is kept and read, stop times or not.
    assert back["c"]["subagents_async"] == ["one"]


def test_a_parked_parent_is_not_evicted_out_from_under_its_own_park(tmp_path):
    """The 300-second evictor reaches a parked parent long before the
    15-minute window can release it. Parking leaves the state at `working`
    and raises no card, so `_waiting_on_human` says False and the session
    goes to *Recently finished* with its ask still to come — which is a
    quieter version of the bug this whole rung exists to fix.
    """
    _transcript(tmp_path, "s", "ag", RUNNING + "\n", wrote_after=60)
    d = _daemon()
    d._session_states["s"] = {
        "state": "working", "last_event": time.time(), "subagents": set(),
        "transcript_path": _session_transcript(tmp_path),
        "subagents_async": {"ag": STOPPED_AT},
        # Silent for longer than the staleness window: one `pytest` run.
        "last_event_monotonic": time.monotonic() - 10 * 60,
    }
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert d._session_states["s"].get("async_park_at") is not None
    d._evict_stale_sessions()
    assert "s" in d._session_states

    # And the exemption cannot outlive its reason.
    d._session_states["s"]["async_park_at"] = \
        time.monotonic() - subagent_watch.STALE_SECONDS - 60
    d._session_states["s"]["last_event_monotonic"] = time.monotonic() - 10 * 60
    d._evict_stale_sessions()
    assert "s" not in d._session_states
