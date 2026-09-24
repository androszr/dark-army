"""Tests for the `finished` bucket — agents that stopped working, kept around.

Two populations share the bucket and the tests below keep them straight: a
*tombstone* (the session is gone, `alive` False) and a *live-idle* session (still
tracked, just quiet past the grace, `alive` True). The interesting property is
that one becomes the other without the row moving or its clock restarting.

No clock faking here, per the house style: build state with `time.time() - N`
offsets and move the thresholds rather than the clock.
"""

import time

import pytest

from dark_army_daemon.agents_poll import AgentRecord
from dark_army_daemon.daemon import (
    BobDaemon,
    FINISHED_IDLE_GRACE_SECONDS,
    FINISHED_RETENTION_SECONDS,
    MAX_FINISHED_RECORDS,
)


def make_daemon():
    return BobDaemon()


def _live(d, sid, *, state="idle", idle=0.0, pid=4242, project="proj"):
    """A hook-tracked session that last did something `idle` seconds ago."""
    d._session_states[sid] = {
        "state": state,
        "project": project,
        "pid": pid,
        "last_event": time.time() - idle,
        "last_event_monotonic": time.monotonic() - idle,
    }


def _finished_bucket(d):
    return d.detailed_snapshot()["finished"]


# ── capture ──────────────────────────────────────────────────────────────────

def test_session_end_leaves_a_tombstone():
    d = make_daemon()
    _live(d, "s1")
    d._update_session_state("dismiss", "SessionEnd", "s1")

    assert "s1" not in d._session_states
    assert d._finished["s1"]["end_reason"] == "ended"


def test_staleness_eviction_leaves_a_tombstone():
    d = make_daemon()
    _live(d, "s1", idle=9999)
    d._session_staleness_timeout = 1
    d._evict_stale_sessions()

    assert d._finished["s1"]["end_reason"] == "evicted"


def test_dead_pid_is_reported_as_no_process(monkeypatch):
    d = make_daemon()
    _live(d, "s1", pid=999999)
    monkeypatch.setattr("dark_army_daemon.daemon.psutil.pid_exists",
                        lambda pid: False)

    assert d._check_liveness() == ["s1"]
    assert d._finished["s1"]["end_reason"] == "no process"


def test_pidless_ghost_is_reported_as_evicted_not_as_a_dead_process():
    """It never had a PID, so 'no process' would be a claim we cannot make."""
    d = make_daemon()
    _live(d, "s1", idle=9999, pid=None)

    assert d._check_liveness() == ["s1"]
    assert d._finished["s1"]["end_reason"] == "evicted"


def test_clearing_a_session_tombstones_the_one_it_replaced():
    """PID dedup: /clear starts a new session on the same terminal."""
    d = make_daemon()
    _live(d, "old", pid=777)
    d._session_metrics["old"] = {"cost_usd": 1.0}

    # The dedup path lives in _handle_message; drive it the way a hook would.
    import asyncio
    asyncio.run(d._handle_message(
        {"event": "session_start", "session_id": "new", "pid": 777}
    ))

    assert "old" not in d._session_states
    assert d._finished["old"]["end_reason"] == "cleared"


# ── the clock ────────────────────────────────────────────────────────────────

def test_the_clock_runs_from_when_work_stopped_not_from_removal():
    """The row must not restart its duration when the evictor finally runs.

    A session that went quiet four minutes ago and is evicted *now* has been
    finished for four minutes, not for zero. Getting this wrong is invisible in
    the store and glaring in the panel, where a row that read "finished 4m ago"
    snaps back to "0s" the moment the evictor fires.
    """
    d = make_daemon()
    _live(d, "s1", idle=240)
    d._session_staleness_timeout = 1
    d._evict_stale_sessions()

    age = time.time() - d._finished["s1"]["finished_at"]
    assert 235 < age < 245


def test_a_tombstone_keeps_counting_up():
    d = make_daemon()
    _live(d, "s1", idle=300)
    d._update_session_state("dismiss", "SessionEnd", "s1")

    entry = _finished_bucket(d)[0]
    assert entry["alive"] is False
    assert 295 < entry["idle_seconds"] < 305


# ── the live half ────────────────────────────────────────────────────────────

def test_a_quiet_session_moves_out_of_sleeping_once_past_the_grace():
    d = make_daemon()
    _live(d, "s1", idle=FINISHED_IDLE_GRACE_SECONDS + 60)

    snap = d.detailed_snapshot()
    assert [e["session_id"] for e in snap["sleeping"]] == []
    assert [e["session_id"] for e in snap["finished"]] == ["s1"]
    assert snap["finished"][0]["alive"] is True


def test_a_session_between_turns_stays_in_sleeping():
    """Sleeping is what a short pause looks like; promoting it would flap."""
    d = make_daemon()
    _live(d, "s1", idle=FINISHED_IDLE_GRACE_SECONDS - 30)

    snap = d.detailed_snapshot()
    assert [e["session_id"] for e in snap["sleeping"]] == ["s1"]
    assert snap["finished"] == []


def test_a_session_quiet_for_longer_than_the_window_is_sleeping_again():
    """Alive and quiet for half an hour is sleeping, not recently finished."""
    d = make_daemon()
    _live(d, "s1", idle=FINISHED_RETENTION_SECONDS + 60)

    snap = d.detailed_snapshot()
    assert [e["session_id"] for e in snap["sleeping"]] == ["s1"]
    assert snap["finished"] == []


def test_a_background_agent_is_not_promoted_by_its_own_age():
    """`idle_is_age` marks a row whose number is age since start, not silence —
    a background agent still being listed has not stopped, however old it is."""
    d = make_daemon()
    d._agent_records = {
        "bg": AgentRecord("bg", kind="background", activity="idle",
                          started_at=time.time() - 9999),
    }
    snap = d.detailed_snapshot()
    assert [e["session_id"] for e in snap["sleeping"]] == ["bg"]
    assert snap["finished"] == []


# ── background agents ────────────────────────────────────────────────────────

def test_a_background_agent_that_stops_being_listed_is_finished():
    """Vanishing from the poll is the only 'it finished' signal one ever emits."""
    d = make_daemon()
    rec = AgentRecord("bg", kind="background", activity="busy", name="nightly")
    d._on_agent_records([rec])
    d._on_agent_records([])

    assert d._finished["bg"]["end_reason"] == "completed"
    assert d._finished["bg"]["stub"]["cli_name"] == "nightly"


def test_a_hook_tracked_session_is_not_tombstoned_by_the_poll():
    """Its own removal paths record a truer verdict; the poll must not beat them
    to it with a vaguer one."""
    d = make_daemon()
    _live(d, "s1")
    d._on_agent_records([AgentRecord("s1", kind="interactive", activity="busy")])
    d._on_agent_records([])

    assert d._finished == {}


def test_the_poll_does_not_overwrite_a_verdict_it_already_has():
    """The poll lags the hooks by up to 10s, so a session that ended cleanly is
    still listed for one more tick. Tombstoning it again would replace "ended"
    with the vaguer "completed" and — the real damage — restart its clock at zero,
    so a row reading "finished 5m ago" would snap back to 0s ten seconds later."""
    d = make_daemon()
    _live(d, "s1", idle=300)
    d._on_agent_records([AgentRecord("s1", kind="interactive", activity="idle")])
    d._update_session_state("dismiss", "SessionEnd", "s1")
    stopped_at = d._finished["s1"]["finished_at"]

    d._on_agent_records([])                      # next poll drops it

    assert d._finished["s1"]["end_reason"] == "ended"
    assert d._finished["s1"]["finished_at"] == stopped_at


def test_deleting_an_abandoned_agent_does_not_resurrect_it_as_finished(monkeypatch):
    """A graveyard row the user deliberately deleted must stay deleted."""
    d = make_daemon()
    rec = AgentRecord("bg", kind="background", activity="blocked", short_id="ab12")
    d._on_agent_records([rec])
    d._blocked_since["bg"] = time.time() - 99999      # stale enough to be abandoned
    monkeypatch.setattr("dark_army_daemon.jobs_store.delete_job", lambda s: True)

    ok, _ = d.delete_abandoned_agent("bg")
    assert ok
    d._on_agent_records([])

    assert d._finished == {}


# ── revival ──────────────────────────────────────────────────────────────────

def test_a_new_prompt_takes_a_session_back_out_of_finished():
    d = make_daemon()
    _live(d, "s1")
    d._update_session_state("dismiss", "SessionEnd", "s1")
    assert "s1" in d._finished

    d._update_session_state("dismiss", "UserPromptSubmit", "s1")

    assert d._finished == {}
    assert d._session_states["s1"]["state"] == "thinking"


@pytest.mark.parametrize("event", ["permission", "tool_failed"])
def test_a_stray_late_event_cannot_resurrect_a_finished_session(event):
    """The create=False rule has to keep holding: these events never create a
    session, so they must not empty its tombstone either."""
    d = make_daemon()
    _live(d, "s1")
    d._update_session_state("dismiss", "SessionEnd", "s1")

    d._update_session_state(event, "", "s1", tool_name="Bash")

    assert "s1" not in d._session_states
    assert d._finished["s1"]["end_reason"] == "ended"


# ── retention ────────────────────────────────────────────────────────────────

def test_a_tombstone_expires_past_the_retention_window():
    d = make_daemon()
    _live(d, "s1")
    d._update_session_state("dismiss", "SessionEnd", "s1")
    d._finished["s1"]["finished_mono"] -= FINISHED_RETENTION_SECONDS + 1

    d._prune_finished()

    assert d._finished == {}


def test_the_store_is_bounded_and_keeps_the_newest():
    d = make_daemon()
    now_mono = time.monotonic()
    # Straight into the store: _record_finished prunes on every call, so records
    # made in a loop would be culled before they could be given distinct ages.
    for i in range(MAX_FINISHED_RECORDS + 10):
        d._finished[f"s{i}"] = {
            "finished_at": time.time(), "finished_mono": now_mono + i,
            "end_reason": "ended", "stub": {"session_id": f"s{i}"},
        }

    d._prune_finished()

    assert len(d._finished) == MAX_FINISHED_RECORDS
    assert f"s{MAX_FINISHED_RECORDS + 9}" in d._finished
    assert "s0" not in d._finished


# ── what the row still knows ─────────────────────────────────────────────────

def test_cost_survives_the_session_it_belongs_to():
    """The motivating bug: _collect_agent_stubs garbage-collects metrics for any
    session no longer in a category, so the cost of a run used to be destroyed one
    tick after it ended — exactly when you want to look at it."""
    d = make_daemon()
    _live(d, "s1")
    d._session_metrics["s1"] = {"cost_usd": 4.25, "ctx_used_pct": 88.0}
    d._update_session_state("dismiss", "SessionEnd", "s1")

    entry = _finished_bucket(d)[0]
    assert entry["metrics"]["cost_usd"] == 4.25
    assert "s1" in d._session_metrics       # spared by the sweep while tombstoned


def test_metrics_are_released_once_the_tombstone_expires():
    d = make_daemon()
    _live(d, "s1")
    d._session_metrics["s1"] = {"cost_usd": 4.25}
    d._update_session_state("dismiss", "SessionEnd", "s1")
    d._finished["s1"]["finished_mono"] -= FINISHED_RETENTION_SECONDS + 1

    d._collect_agent_stubs()

    assert d._session_metrics == {}


def test_a_finished_session_is_given_no_advice():
    """Several rules fire on state alone. Telling someone to /compact a session
    that has already stopped is noise, so the bucket is not evaluated at all."""
    d = make_daemon()
    _live(d, "s1")
    d._session_metrics["s1"] = {"exceeds_200k": True, "ctx_used_pct": 99.0}
    d._update_session_state("dismiss", "SessionEnd", "s1")

    # Twice: signals are debounced, so one evaluation could never show one anyway.
    d.detailed_snapshot()
    assert _finished_bucket(d)[0]["signals"] == []


# ── blast radius ─────────────────────────────────────────────────────────────

def test_promoting_a_live_session_changes_nothing_outside_the_panel():
    """The whole design rests on this: the split happens in snapshot assembly, not
    in categorisation, so a session does not lose its face or its HUD count for
    having been quiet for two minutes."""
    d = make_daemon()
    _live(d, "s1", idle=FINISHED_IDLE_GRACE_SECONDS + 60)

    assert d._reconciled_categories() == {"s1": "sleeping"}
    assert d._activity_counts() == {
        "working": 0, "idle": 1, "attention": 0, "subagents": 0
    }

    # ...and it is nonetheless in the finished section.
    assert [e["session_id"] for e in _finished_bucket(d)] == ["s1"]


def test_a_tombstone_gets_no_bob_and_no_count():
    d = make_daemon()
    _live(d, "s1")
    d._update_session_state("dismiss", "SessionEnd", "s1")

    assert d._activity_counts() == {
        "working": 0, "idle": 0, "attention": 0, "subagents": 0
    }


# ── the frozen place ─────────────────────────────────────────────────────────

def test_a_tombstone_freezes_the_snapshot_label_and_cwd():
    """The tombstone keeps the workspace label its live row wore — not the raw
    hook-time basename — and carries the folder, so `_enrich_agent_stubs`'
    workspace ladder runs on tombstones of every provider, not only those with
    a readable transcript. The cwd on the stub also means the enrolment sweep
    now applies to tombstones: one of an un-enrolled project is dropped, the
    same direction un-enrolment already points for live rows."""
    d = make_daemon()
    _live(d, "s1", project="host")
    d._agents_snapshot_cache = {
        "running": [{"session_id": "s1",
                     "cwd": "/tmp/dark-army/host",
                     "project": "dark-army"}],
    }
    d._update_session_state("dismiss", "SessionEnd", "s1")

    stub = d._finished["s1"]["stub"]
    assert stub["project"] == "dark-army"
    assert stub["cwd"] == "/tmp/dark-army/host"


def test_a_tombstone_with_no_snapshot_row_keeps_the_state_label():
    """Never *less* labelled than before: with no snapshot row and no state
    cwd, the stub falls back to the old ``state["project"]`` and an empty
    cwd — which the snapshot sweep deliberately keeps."""
    d = make_daemon()
    _live(d, "s1", project="proj")
    d._update_session_state("dismiss", "SessionEnd", "s1")

    stub = d._finished["s1"]["stub"]
    assert stub["project"] == "proj"
    assert stub["cwd"] == ""


def test_a_tombstones_folder_is_not_a_dispatchable_root(monkeypatch):
    """A tombstone carries its folder so the row keeps its project label — but
    a run that has ended is not a live session, and its folder must not stay
    dispatchable for the half hour the tombstone survives. The quiet-live rows
    promoted into the same bucket carry ``alive`` and do still count."""
    from dark_army_daemon import dispatch, enrollment, workspace

    monkeypatch.setattr(workspace, "windows", lambda: [])
    monkeypatch.setattr(enrollment, "root_enrolled", lambda root: True)

    d = make_daemon()
    d._agents_snapshot_cache = {
        "running": [{"session_id": "s1", "cwd": "/tmp/live-proj"}],
        "finished": [
            {"session_id": "s2", "cwd": "/tmp/dead-proj"},
            {"session_id": "s3", "cwd": "/tmp/quiet-proj", "alive": True},
        ],
    }

    roots = d._known_project_roots()

    norm = dispatch.normalise_root
    assert norm("/tmp/dead-proj") not in roots
    assert norm("/tmp/live-proj") in roots
    assert norm("/tmp/quiet-proj") in roots


def test_collaboration_retains_only_published_exact_address_and_expires(monkeypatch):
    from dark_army_daemon import collaboration
    d = make_daemon()
    _live(d, "known")
    d._agents_snapshot_cache = {"running": [{
        "session_id": "known", "provider": "claude", "address": "observed-name",
        "nickname": "NeverGuess", "cwd": "/project", "inbox_path": "/private", "token": "secret"}]}
    d._record_finished("known", d._session_states["known"], "ended")
    tomb = d._finished["known"]
    assert tomb["stub"]["retained_address"] == "observed-name"
    assert tomb["stub"]["retained_address_source"] == "published_registry"
    assert "inbox_path" not in tomb["stub"] and "token" not in tomb["stub"]
    d._record_finished("cold", {"provider": "claude"}, "evicted")
    assert "retained_address" not in d._finished["cold"]["stub"]
    sender = {"session_id": "sender", "provider": "claude",
              "sent_to": {"observed-name": {"count": 1}}}
    evidence = collaboration.build({"running": [sender], "finished": [
        dict(tomb["stub"], end_reason=tomb["end_reason"], alive=False)]})
    assert any(n["lifecycle"] == "ended" for n in evidence["nodes"])
    tomb["finished_mono"] = time.monotonic() - FINISHED_RETENTION_SECONDS - 1
    d._prune_finished()
    assert "known" not in d._finished
    after = collaboration.build({"running": [sender]})
    assert any(n["resolution"] == "unresolved" for n in after["nodes"])


def test_collaboration_wrong_provider_and_conflicting_addresses_are_not_retained():
    d = make_daemon()
    d._agents_snapshot_cache = {"running": [
        {"session_id": "s", "provider": "codex", "address": "wrong"},
        {"session_id": "s", "provider": "claude", "address": "one"},
        {"session_id": "s", "provider": "claude", "address": "two"}]}
    d._record_finished("s", {"provider": "claude"}, "ended")
    assert "retained_address" not in d._finished["s"]["stub"]
