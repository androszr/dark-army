# host/tests/test_agents_poll.py
"""Tests for the `claude agents --json` reconciler.

The sample payloads below are trimmed from real output on this machine (Claude
Code 2.1.212) — interactive rows carry `pid`/`status`, background rows carry
`id`/`state`, and the two shapes are not interchangeable.
"""

import asyncio
import json
import os
import stat
import sys
import time

import pytest

from dark_army_daemon import agents_poll
from dark_army_daemon.agents_poll import (
    AgentRecord,
    AgentsPoller,
    find_claude_binary,
    parse_agents_json,
    query_agents,
)

REAL_PAYLOAD = json.dumps([
    {
        "id": "b3bbde6d",
        "cwd": "/Users/x/Documents/demo-app",
        "kind": "background",
        "startedAt": 1783287005464,
        "sessionId": "b3bbde6d-a1a8-454e-8198-d4eb4995667d",
        "name": "Analyze and reduce excessive automation tests",
        "state": "blocked",
    },
    {
        "pid": 20126,
        "cwd": "/Users/x/Documents/shop-front",
        "kind": "interactive",
        "startedAt": 1785027806415,
        "sessionId": "fceab7fa-13c4-4832-be31-2dfb456472ec",
        "name": "shop-front-fb",
        "status": "busy",
    },
])


def test_parses_both_row_shapes():
    records = parse_agents_json(REAL_PAYLOAD)
    assert records is not None and len(records) == 2
    bg, inter = records

    assert bg.kind == "background"
    assert bg.activity == "blocked"        # from `state`
    assert bg.pid is None
    assert bg.short_id == "b3bbde6d"
    assert bg.is_background and bg.needs_attention
    assert bg.project == "demo-app"
    assert bg.started_at == pytest.approx(1783287005.464)

    assert inter.kind == "interactive"
    assert inter.activity == "busy"        # from `status`
    assert inter.pid == 20126
    assert not inter.is_background and not inter.needs_attention
    assert inter.name == "shop-front-fb"


def test_only_blocked_needs_attention():
    for activity, expected in [("blocked", True), ("busy", False), ("idle", False),
                               ("running", False), ("", False)]:
        assert AgentRecord("s", activity=activity).needs_attention is expected


def test_activity_is_lowercased():
    payload = json.dumps([{"sessionId": "s1", "state": "BLOCKED"}])
    assert parse_agents_json(payload)[0].needs_attention


@pytest.mark.parametrize("payload", ["not json", "", "null", '{"sessions": []}', "42"])
def test_unexpected_output_returns_none_not_empty(payload):
    """None and [] mean different things: 'could not ask' vs 'no sessions'. A
    contract change in the CLI must make us go quiet, not report zero agents."""
    assert parse_agents_json(payload) is None


def test_empty_array_is_a_real_answer():
    assert parse_agents_json("[]") == []


def test_rows_without_session_id_are_skipped():
    payload = json.dumps([{"kind": "interactive"}, {"sessionId": "s1"}, "garbage"])
    records = parse_agents_json(payload)
    assert [r.session_id for r in records] == ["s1"]


def test_non_integer_pid_is_dropped():
    payload = json.dumps([{"sessionId": "s1", "pid": "20126"}])
    assert parse_agents_json(payload)[0].pid is None


def test_find_claude_prefers_path(monkeypatch):
    monkeypatch.setattr(agents_poll.shutil, "which", lambda _: "/somewhere/claude")
    assert find_claude_binary() == "/somewhere/claude"


def test_find_claude_falls_back_to_known_locations(tmp_path, monkeypatch):
    """A Finder-launched .app gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, which has no
    `claude` on it — the fallback list is what keeps the poller working in a bundle."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(agents_poll.shutil, "which", lambda _: None)
    monkeypatch.setattr(agents_poll, "CLAUDE_CANDIDATES", (str(fake),))
    assert find_claude_binary() == str(fake)


def test_find_claude_ignores_non_executable(tmp_path, monkeypatch):
    plain = tmp_path / "claude"
    plain.write_text("not executable")
    monkeypatch.setattr(agents_poll.shutil, "which", lambda _: None)
    monkeypatch.setattr(agents_poll, "CLAUDE_CANDIDATES", (str(plain),))
    assert find_claude_binary() is None


@pytest.mark.asyncio
async def test_query_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: None)
    assert await query_agents() is None


def _fake_cli(tmp_path, body: str, name: str = "claude"):
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.mark.asyncio
async def test_query_parses_real_cli_output(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "cat <<'EOF'\n%s\nEOF\n" % REAL_PAYLOAD)
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: cli)
    records = await query_agents()
    assert [r.short_id for r in records] == ["b3bbde6d", "fceab7fa"]


@pytest.mark.asyncio
async def test_query_returns_none_on_nonzero_exit(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "echo 'not logged in' >&2\nexit 1\n")
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: cli)
    assert await query_agents() is None


@pytest.mark.asyncio
async def test_query_kills_and_reaps_on_timeout(tmp_path, monkeypatch):
    """A hung CLI must be killed *and waited on*. This runs every 10s for the life
    of the app, so an unreaped child per timeout is an unbounded zombie leak."""
    cli = _fake_cli(tmp_path, "sleep 30\n")
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: cli)

    spawned = []
    real_exec = agents_poll.asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(agents_poll.asyncio, "create_subprocess_exec", spy)

    assert await query_agents(timeout=0.2) is None
    assert len(spawned) == 1
    # returncode is populated only once the child has been waited on.
    assert spawned[0].returncode is not None, "child was killed but never reaped"


@pytest.mark.asyncio
async def test_poller_reports_availability(monkeypatch):
    seen = []
    poller = AgentsPoller(on_records=seen.append)
    assert poller.available is None

    monkeypatch.setattr(agents_poll, "query_agents", _const(None))
    await poller.poll_once()
    assert poller.available is False and seen == []

    monkeypatch.setattr(agents_poll, "query_agents", _const([AgentRecord("s1")]))
    await poller.poll_once()
    assert poller.available is True and len(seen) == 1


def _const(value):
    async def _fn(*a, **kw):
        return value
    return _fn


@pytest.mark.asyncio
async def test_poller_survives_a_throwing_callback(monkeypatch):
    """This task is the only thing keeping background agents visible; a bad
    consumer must not end it."""
    def explode(_records):
        raise RuntimeError("boom")

    monkeypatch.setattr(agents_poll, "query_agents", _const([AgentRecord("s1")]))
    poller = AgentsPoller(on_records=explode)
    await poller.poll_once()          # must not raise
    assert poller.available is True


@pytest.mark.asyncio
async def test_poller_loop_survives_a_failing_poll(monkeypatch):
    calls = []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient")
        return [AgentRecord("s1")]

    monkeypatch.setattr(agents_poll, "query_agents", flaky)
    seen = []
    poller = AgentsPoller(on_records=seen.append, interval=0.05)
    poller.start()
    await asyncio.sleep(0.2)
    await poller.stop()
    assert len(calls) >= 2, "loop died on the first failure"
    assert seen, "recovered polls never reached the callback"


@pytest.mark.asyncio
async def test_poll_soon_wakes_the_loop_early(monkeypatch):
    monkeypatch.setattr(agents_poll, "query_agents", _const([AgentRecord("s1")]))
    seen = []
    poller = AgentsPoller(on_records=seen.append, interval=30.0)
    poller.start()
    await asyncio.sleep(0.05)
    before = len(seen)
    poller.poll_soon()
    await asyncio.sleep(0.05)
    await poller.stop()
    assert len(seen) > before, "poll_soon did not short-circuit the 30s interval"


@pytest.mark.asyncio
async def test_an_unchanging_listing_slows_the_poll_down(monkeypatch):
    """Each poll forks the Claude Code CLI — ~240ms of CPU for a listing that
    changes a few times an hour. A steady listing must stop paying for that."""
    calls = []

    async def counting(on_error=None):
        calls.append(1)
        return [AgentRecord("s1", kind="interactive", activity="idle", pid=1)]

    monkeypatch.setattr(agents_poll, "query_agents", counting)
    monkeypatch.setattr(agents_poll, "POLL_BACKOFF_AFTER", 2)
    monkeypatch.setattr(agents_poll, "POLL_MAX_INTERVAL_SECONDS", 10.0)

    poller = AgentsPoller(on_records=lambda r: None, interval=0.02)
    poller.start()
    await asyncio.sleep(0.4)
    await poller.stop()
    # Without backoff, 0.4s at a 20ms interval would be ~20 polls.
    assert len(calls) < 12, f"no backoff: {len(calls)} polls in 0.4s"


@pytest.mark.asyncio
async def test_a_changed_listing_restores_the_fast_poll(monkeypatch):
    """Backing off must not mean going to sleep through activity."""
    state = {"n": 0}

    async def changing(on_error=None):
        state["n"] += 1
        # Steady for a while, then the listing moves.
        pid = 1 if state["n"] < 6 else state["n"]
        return [AgentRecord("s1", kind="interactive", activity="idle", pid=pid)]

    monkeypatch.setattr(agents_poll, "query_agents", changing)
    monkeypatch.setattr(agents_poll, "POLL_BACKOFF_AFTER", 2)

    poller = AgentsPoller(on_records=lambda r: None, interval=0.02)
    poller.start()
    await asyncio.sleep(0.5)
    n_at_change = state["n"]
    await asyncio.sleep(0.2)
    await poller.stop()
    # Once it started moving the interval snaps back, so the second window polls
    # at least as often as a fully backed-off loop would have.
    assert state["n"] - n_at_change >= 3, "did not recover the fast poll"


@pytest.mark.asyncio
async def test_a_failing_poll_does_not_count_as_unchanged(monkeypatch):
    """A None reading is no reading, not a steady one — backing off on failures
    would slow recovery exactly when the CLI starts working again."""
    async def always_failing(on_error=None):
        return None

    monkeypatch.setattr(agents_poll, "query_agents", always_failing)
    monkeypatch.setattr(agents_poll, "POLL_BACKOFF_AFTER", 2)
    calls = []
    real = agents_poll.AgentsPoller._fingerprint
    monkeypatch.setattr(agents_poll.AgentsPoller, "_fingerprint",
                        staticmethod(lambda r: calls.append(r) or real(r)))

    poller = AgentsPoller(on_records=lambda r: None, interval=0.02)
    poller.start()
    await asyncio.sleep(0.25)
    await poller.stop()
    assert all(c is None for c in calls)
    assert len(calls) >= 6, f"backed off on failures: only {len(calls)} polls"


# --- Daemon-side reconciliation ---------------------------------------------

from dark_army_daemon import daemon as daemon_mod  # noqa: E402
from dark_army_daemon.daemon import (  # noqa: E402
    BobDaemon, UNNAMED_SESSION, _is_default_slug,
)


def _daemon_with_records(*records, tmp_path=None):
    d = BobDaemon()
    d._agent_records = {r.session_id: r for r in records}
    return d


def test_blocked_background_agent_is_counted_even_though_hooks_never_saw_it():
    """The motivating case: a background agent parked on `blocked` emits no hook
    events at all, so before the reconciler it was invisible on every surface."""
    d = _daemon_with_records(
        AgentRecord("bg1", kind="background", activity="blocked", name="forgotten"),
    )
    assert d._session_states == {}
    assert d._activity_counts() == {
        "working": 0, "idle": 0, "attention": 1, "subagents": 0
    }
    stubs = d._collect_agent_stubs()
    assert [(s["session_id"], s["_category"]) for s in stubs] == [("bg1", "waiting")]
    assert stubs[0]["kind"] == "background"


def test_blocked_overrides_the_hook_derived_category():
    """Hooks last said 'idle'; Claude says the session is blocked on a human.
    No hook reports `blocked`, so the reconciler has to win."""
    d = _daemon_with_records(AgentRecord("s1", kind="background", activity="blocked"))
    d._session_states["s1"] = {"state": "idle", "last_event": 0}
    assert d._reconciled_categories() == {"s1": "waiting"}
    assert d._activity_counts()["attention"] == 1
    assert d._activity_counts()["idle"] == 0


def test_untracked_records_map_busy_to_working_and_idle_to_sleeping():
    d = _daemon_with_records(
        AgentRecord("a", kind="interactive", activity="busy"),
        AgentRecord("b", kind="interactive", activity="idle"),
        AgentRecord("c", kind="interactive", activity="something-new"),
    )
    counts = d._activity_counts()
    assert (counts["working"], counts["idle"], counts["attention"]) == (1, 2, 0)


def test_reconciler_does_not_override_a_tracked_working_session():
    """Only `blocked` overrides. A session the hooks are actively tracking keeps
    its fine-grained state — the poll is 10s stale by construction."""
    d = _daemon_with_records(AgentRecord("s1", kind="interactive", activity="idle"))
    d._session_states["s1"] = {"state": "working", "last_event": 0}
    assert d._reconciled_categories() == {"s1": "running"}


def test_reconciler_never_evicts_a_tracked_session():
    """Reconciliation is additive. A session missing from the poll (a race, a
    filtered view) must not vanish from the counts."""
    d = _daemon_with_records(AgentRecord("other", activity="busy"))
    d._session_states["s1"] = {"state": "working", "last_event": 0}
    assert "s1" in d._reconciled_categories()
    assert sum(d._activity_counts()[k] for k in ("working", "idle", "attention")) == 2


def test_subagent_totals_come_only_from_tracked_sessions():
    d = _daemon_with_records(AgentRecord("bg1", kind="background", activity="blocked"))
    d._session_states["s1"] = {"state": "working", "last_event": 0, "subagents": {"a", "b"}}
    assert d._activity_counts()["subagents"] == 2


def test_records_backfill_pid_and_project_on_tracked_sessions():
    d = _daemon_with_records(
        AgentRecord("s1", pid=4242, cwd="/Users/x/Documents/my-repo", name="cli-name"),
    )
    d._session_states["s1"] = {"state": "idle", "last_event": 0}
    stub = d._collect_agent_stubs()[0]
    assert stub["pid"] == 4242
    assert stub["project"] == "my-repo"
    assert stub["cli_name"] == "cli-name"


def test_cli_name_beats_the_transcript_derived_title():
    d = BobDaemon()
    stub = {
        "session_id": "00000000-0000-0000-0000-000000000000",
        "project": "proj", "state": "idle", "subagents": 0, "subagent_ids": [],
        "cli_name": "what the human called it", "_category": "sleeping",
    }
    entry = d._enrich_agent_stubs([stub])["sleeping"][0]
    assert entry["name"] == "what the human called it"


def _question_row(daemon, pending=None, transcript=None):
    """One enriched row for a session stopped on a question, from either source."""
    sid = "00000000-0000-0000-0000-000000000000"
    if pending is not None:
        daemon._pending_questions[sid] = pending
    stub = {"session_id": sid, "project": "proj", "state": "waiting",
            "subagents": 0, "subagent_ids": [], "_category": "waiting"}
    if transcript is not None:
        from dark_army_daemon import session_stats as ss

        stats = ss.SessionStats()
        stats.question = transcript

        class _Cache:
            def get(self, _path):
                return stats

        daemon.__dict__["_stats_cache"] = _Cache()
    return daemon._enrich_agent_stubs([stub])["waiting"][0]


def test_the_hook_question_is_published_when_the_transcript_has_none():
    """The whole point of holding it: the transcript is silent for exactly as
    long as the dialog is up, which is the only time the row needs it."""
    d = BobDaemon()
    q = {"text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"],
         "header": "Database", "id": "toolu_01"}
    entry = _question_row(d, pending=q)
    assert entry["question"] == q
    # And the answer verbs read the same value, or the buttons are inert. The
    # mirror holds the flat keys unchanged plus the whole dialog under
    # `questions` — here a one-question dialog, wrapped (see
    # test_multi_question.py for the several-question half).
    held = d._questions[entry["session_id"]]
    assert {k: v for k, v in held.items() if k != "questions"} == q
    assert held["questions"] == [dict(q, index=0)]


def test_the_transcript_supersedes_the_hook_copy():
    """It carries the real tool_use id, which is what lets a late verdict miss
    rather than land on the next question."""
    d = BobDaemon()
    entry = _question_row(
        d,
        pending={"text": "from the hook", "options": ["A"], "header": "", "id": ""},
        transcript={"text": "from the transcript", "options": ["A"],
                    "header": "", "id": "toolu_real"},
    )
    assert entry["question"]["text"] == "from the transcript"
    assert entry["question"]["id"] == "toolu_real"


def test_every_row_is_dated_and_the_date_does_not_move():
    """The panel orders a section by `started_at`, so it has to be on every row
    and it has to be the *same number* on the next snapshot — a start time that
    drifts is the reshuffling this replaced."""
    d = BobDaemon()
    d._session_states["s1"] = {"state": "working", "last_event": time.time() - 30}
    first = d._enrich_agent_stubs(d._collect_agent_stubs())
    row = [e for c in first.values() for e in c
           if isinstance(e, dict) and e.get("session_id") == "s1"][0]
    assert row["started_at"] > 0
    # Seeded from the last event, not from `now`: a session recovered from
    # sessions.json did not start when this daemon did.
    assert row["started_at"] <= time.time() - 29

    again = d._enrich_agent_stubs(d._collect_agent_stubs())
    row2 = [e for c in again.values() for e in c
            if isinstance(e, dict) and e.get("session_id") == "s1"][0]
    assert row2["started_at"] == row["started_at"]


def test_a_background_agent_is_dated_from_the_roster():
    """It has no hook events and no transcript of its own — the roster's own
    start time is the only honest answer, and it is already on the record."""
    d = BobDaemon()
    started = time.time() - 3600
    d._on_agent_records([AgentRecord("bg", activity="busy", name="n",
                                     started_at=started)])
    snapshot = d._enrich_agent_stubs(d._collect_agent_stubs())
    row = [e for c in snapshot.values() for e in c
           if isinstance(e, dict) and e.get("session_id") == "bg"][0]
    assert row["started_at"] == started


def test_poll_callback_only_wakes_surfaces_when_something_moved():
    """Fires every 10s for the life of the app; an unconditional push would
    rebuild the menu and re-render the strip on every tick for nothing."""
    d = BobDaemon()
    pushes = []
    d._notify_activity = lambda: pushes.append("activity")
    d._schedule_agents_push = lambda: pushes.append("agents")

    d._on_agent_records([AgentRecord("s1", activity="busy", name="n")])
    assert pushes == ["activity", "agents"]

    pushes.clear()
    d._on_agent_records([AgentRecord("s1", activity="busy", name="n")])
    assert pushes == [], "identical poll result should be a no-op"

    d._on_agent_records([AgentRecord("s1", activity="blocked", name="n")])
    assert pushes == ["activity", "agents"]


# --- Stale blocked agents ----------------------------------------------------
#
# Regression guard. `claude agents --json` lists abandoned background sessions
# forever: on the machine this was built on, two had been blocked for 13 and 20
# days with no transcript write since. Counting those as "needs you" pinned a
# warning to the menu bar permanently, which is how you teach someone to ignore
# warnings.

from dark_army_daemon import agents_poll as ap  # noqa: E402


def _blocked(sid, age_seconds):
    return AgentRecord(sid, kind="background", activity="blocked",
                       started_at=time.time() - age_seconds)


def test_an_agent_blocked_since_before_we_started_is_dated_from_session_start():
    d = BobDaemon()
    d._on_agent_records([_blocked("zombie", 20 * 86400)])
    assert d._blocked_is_stale("zombie")
    assert d._reconciled_categories()["zombie"] == "abandoned"
    assert d._activity_counts() == {"working": 0, "idle": 0, "attention": 0,
                                    "subagents": 0}


def test_a_recent_block_still_raises_attention():
    d = BobDaemon()
    d._on_agent_records([_blocked("fresh", 60)])
    assert not d._blocked_is_stale("fresh")
    assert d._reconciled_categories()["fresh"] == "waiting"
    assert d._activity_counts()["attention"] == 1


def test_a_transition_into_blocked_is_dated_from_now_not_session_start():
    """An agent that has been running for days and blocks *right now* is the most
    urgent case there is — it must not inherit the session's age."""
    d = BobDaemon()
    running = AgentRecord("a", kind="background", activity="running",
                          started_at=time.time() - 20 * 86400)
    d._on_agent_records([running])
    assert "a" not in d._blocked_since

    d._on_agent_records([_blocked("a", 20 * 86400)])
    assert not d._blocked_is_stale("a")
    assert d._reconciled_categories()["a"] == "waiting"


def test_leaving_blocked_clears_the_timestamp():
    d = BobDaemon()
    d._on_agent_records([_blocked("a", 60)])
    d._on_agent_records([AgentRecord("a", kind="background", activity="running")])
    assert "a" not in d._blocked_since


def test_blocked_since_does_not_leak_for_vanished_agents():
    d = BobDaemon()
    d._on_agent_records([_blocked("a", 60)])
    d._on_agent_records([])
    assert d._blocked_since == {}


def test_abandoned_agents_leave_the_menu_but_stay_in_the_panel():
    """A background agent blocked for 20 days was cluttering the Sleeping section
    with rows reading "Idle 493h". It belongs in its own bucket: the menu renders
    the three live categories by name, so it drops out of the dropdown, while the
    panel still lists it so the leftovers can be found and cleaned up."""
    d = BobDaemon()
    d._on_agent_records([_blocked("zombie", 20 * 86400)])

    stub = [s for s in d._collect_agent_stubs() if s["session_id"] == "zombie"][0]
    assert stub["_category"] == "abandoned"
    assert stub["agent_activity"] == "blocked"
    assert stub["kind"] == "background"

    snapshot = d._enrich_agent_stubs(d._collect_agent_stubs())
    assert [e["session_id"] for e in snapshot["abandoned"]] == ["zombie"]
    for live in ("waiting", "running", "sleeping"):
        assert snapshot[live] == [], f"tombstone leaked into {live}"


def test_a_hook_tracked_session_is_never_abandoned():
    """Only reconciler-only records qualify. If the hooks are seeing events, it is
    a live session whatever the CLI says about a stale block."""
    d = BobDaemon()
    d._session_states["zombie"] = {"state": "working", "last_event": time.time()}
    d._on_agent_records([_blocked("zombie", 20 * 86400)])
    assert d._reconciled_categories()["zombie"] == "running"


def test_threshold_boundary():
    d = BobDaemon()
    d._on_agent_records([_blocked("just-under", ap.STALE_BLOCKED_SECONDS - 60)])
    d._on_agent_records([_blocked("just-under", ap.STALE_BLOCKED_SECONDS - 60),
                         _blocked("just-over", ap.STALE_BLOCKED_SECONDS + 60)])
    assert not d._blocked_is_stale("just-under")
    assert d._blocked_is_stale("just-over")


# --- Deleting an abandoned agent ---------------------------------------------
#
# `claude agents --json` has no --kill and lists a blocked background agent
# forever, so the panel gets to retire one. The category check is the safety
# property: `abandoned` is the only bucket that is provably a tombstone, and no
# sequence of clicks may delete a session someone is still using.

from dark_army_daemon import jobs_store  # noqa: E402


def _job_record(root, short_id):
    d = root / short_id
    d.mkdir(parents=True)
    (d / "state.json").write_text('{"state":"blocked"}')
    return d


def _blocked_with_job(sid, short_id, age_seconds):
    return AgentRecord(sid, kind="background", activity="blocked",
                       short_id=short_id, started_at=time.time() - age_seconds)


def test_deleting_an_abandoned_agent_removes_its_job_record(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_DIR", tmp_path)
    job = _job_record(tmp_path, "b3bbde6d")
    d = BobDaemon()
    d._on_agent_records([_blocked_with_job("zombie", "b3bbde6d", 20 * 86400)])
    assert d._reconciled_categories()["zombie"] == "abandoned"

    ok, detail = d.delete_abandoned_agent("zombie")
    assert (ok, detail) == (True, "deleted")
    assert not job.exists()
    # Forgotten immediately rather than at the next 10s poll, so the push that
    # repaints the panel no longer carries the row it was asked to delete.
    assert "zombie" not in d._agent_records
    assert "zombie" not in d._blocked_since
    assert d._enrich_agent_stubs(d._collect_agent_stubs())["abandoned"] == []


def test_a_freshly_blocked_agent_cannot_be_deleted(tmp_path, monkeypatch):
    """It is `waiting`, not `abandoned` — someone may be about to answer it."""
    monkeypatch.setattr(jobs_store, "JOBS_DIR", tmp_path)
    job = _job_record(tmp_path, "fresh001")
    d = BobDaemon()
    d._on_agent_records([_blocked_with_job("fresh", "fresh001", 60)])

    ok, detail = d.delete_abandoned_agent("fresh")
    assert ok is False
    assert "abandoned" in detail
    assert job.exists()


def test_a_live_session_cannot_be_deleted(tmp_path, monkeypatch):
    """The dangerous case. Even with a stale block on record, a session the hooks
    are seeing is live (see test_a_hook_tracked_session_is_never_abandoned) — and
    its job record must survive a delete aimed at it."""
    monkeypatch.setattr(jobs_store, "JOBS_DIR", tmp_path)
    job = _job_record(tmp_path, "live0001")
    d = BobDaemon()
    d._session_states["live"] = {"state": "working", "last_event": time.time()}
    d._on_agent_records([_blocked_with_job("live", "live0001", 20 * 86400)])

    ok, _ = d.delete_abandoned_agent("live")
    assert ok is False
    assert job.exists()
    assert "live" in d._agent_records


def test_deleting_an_unknown_session_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_DIR", tmp_path)
    d = BobDaemon()
    ok, detail = d.delete_abandoned_agent("nobody")
    assert (ok, detail) == (False, "no such agent")


def test_an_abandoned_agent_with_no_job_directory_settles_quietly(tmp_path,
                                                                  monkeypatch):
    """Nothing to delete is a success: the record is already retired, and the
    reconciler will stop reporting it on the next poll."""
    monkeypatch.setattr(jobs_store, "JOBS_DIR", tmp_path)
    d = BobDaemon()
    d._on_agent_records([_blocked_with_job("zombie", "gone0001", 20 * 86400)])
    ok, detail = d.delete_abandoned_agent("zombie")
    assert (ok, detail) == (True, "already gone")


def test_a_record_with_no_short_id_is_refused_not_guessed(tmp_path, monkeypatch):
    """AgentRecord.short_id falls back to sessionId[:8], but a record that somehow
    carries none must not be turned into a path."""
    monkeypatch.setattr(jobs_store, "JOBS_DIR", tmp_path)
    d = BobDaemon()
    d._on_agent_records([_blocked_with_job("zombie", "", 20 * 86400)])
    ok, _ = d.delete_abandoned_agent("zombie")
    assert ok is False


# --- Session naming ----------------------------------------------------------


def test_cli_slug_never_replaces_a_transcript_title(tmp_path, monkeypatch):
    """`claude agents --json` reports the *default display name* for interactive
    sessions — "shop-front-fb", project plus two hex. Letting it win turned every
    readable session name in the menu into a slug."""
    from dark_army_daemon import session_stats as ss

    transcript = tmp_path / "s1.jsonl"
    # Record shape taken from a real transcript: the field is `aiTitle`.
    transcript.write_text(json.dumps(
        {"type": "ai-title", "aiTitle": "Readable title", "sessionId": "s1"}
    ) + "\n")
    monkeypatch.setattr(ss, "resolve_transcript", lambda sid: str(transcript))

    d = BobDaemon()
    stub = {"session_id": "s1", "project": "shop-front", "state": "idle",
            "subagents": 0, "subagent_ids": [], "cli_name": "shop-front-fb",
            "_category": "sleeping"}
    assert d._enrich_agent_stubs([stub])["sleeping"][0]["name"] == "Readable title"


def test_cli_name_is_used_when_there_is_no_transcript(monkeypatch):
    """Background agents have descriptive CLI names and may not resolve to a
    transcript — that is the case the fallback exists for."""
    from dark_army_daemon import session_stats as ss
    monkeypatch.setattr(ss, "resolve_transcript", lambda sid: None)

    d = BobDaemon()
    stub = {"session_id": "bg", "project": "demo-app", "state": "blocked",
            "subagents": 0, "subagent_ids": [],
            "cli_name": "Analyze and reduce excessive automation tests",
            "_category": "sleeping"}
    entry = d._enrich_agent_stubs([stub])["sleeping"][0]
    assert entry["name"] == "Analyze and reduce excessive automation tests"


# --- failure reporting -------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_reason_is_reported_not_swallowed(monkeypatch):
    """Every failure path used to be a debug log, which made an unavailable
    reconciler undiagnosable in a built app: background agents simply never
    appeared and nothing anywhere said why."""
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: None)
    poller = AgentsPoller(on_records=lambda r: None)
    await poller.poll_once()
    assert poller.available is False
    assert "not found" in poller.last_error


@pytest.mark.asyncio
async def test_nonzero_exit_reason_includes_stderr(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "echo 'please run /login' >&2\nexit 1\n")
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: cli)
    seen = []
    assert await query_agents(on_error=seen.append) is None
    assert "exited 1" in seen[0] and "please run /login" in seen[0]


@pytest.mark.asyncio
async def test_unparseable_output_is_reported(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "echo 'not json'\n")
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: cli)
    seen = []
    assert await query_agents(on_error=seen.append) is None
    assert "cannot parse" in seen[0]


@pytest.mark.asyncio
async def test_repeated_identical_failures_log_once(monkeypatch, caplog):
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: None)
    poller = AgentsPoller(on_records=lambda r: None)
    with caplog.at_level("WARNING", logger="dark-army.agents"):
        for _ in range(4):
            await poller.poll_once()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "a failure every 10s must not spam the log"


@pytest.mark.asyncio
async def test_recovery_clears_the_error(monkeypatch):
    poller = AgentsPoller(on_records=lambda r: None)
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: None)
    await poller.poll_once()
    assert poller.last_error

    monkeypatch.setattr(agents_poll, "query_agents", _const([AgentRecord("s1")]))
    await poller.poll_once()
    assert poller.last_error == "" and poller.available is True


@pytest.mark.asyncio
async def test_poll_survives_a_deleted_working_directory(tmp_path, monkeypatch):
    """The bug this was written for. `build.sh` does `rm -rf build dist` before
    recreating the bundle, so a rebuilt-and-relaunched app runs with a cwd whose
    inode is gone. Children inherit it and die; Claude Code, being a Bun binary,
    reports only "ENOENT: Bun could not find a file" — naming neither the file nor
    the directory. Spawning with an explicit cwd is what makes it survivable."""
    # The stand-in must *need* a live working directory, or the test proves
    # nothing: /bin/sh happily runs without one, which is how the first version of
    # this test passed with the fix removed. getcwd() is what actually fails.
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(REAL_PAYLOAD)
    cli = _fake_cli(tmp_path, (
        '%s -c "import os,sys; os.getcwd(); '
        'sys.stdout.write(open(sys.argv[1]).read())" %s\n'
        % (sys.executable, payload_file)
    ))
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: cli)

    doomed = tmp_path / "doomed"
    doomed.mkdir()
    original = os.getcwd()
    os.chdir(doomed)
    doomed.rmdir()                       # cwd now points at a deleted directory
    try:
        records = await query_agents()
    finally:
        os.chdir(original)

    assert records is not None and len(records) == 2


# --- Session naming precedence -----------------------------------------------


def _stub(sid="s1", **kw):
    base = {"session_id": sid, "project": "myproj", "state": "idle",
            "subagents": 0, "subagent_ids": [], "_category": "sleeping"}
    base.update(kw)
    return base


def _name(daemon, stub, transcript=None, monkeypatch=None):
    from dark_army_daemon import session_stats as ss
    monkeypatch.setattr(ss, "resolve_transcript", lambda sid: transcript)
    return daemon._enrich_agent_stubs([stub])["sleeping"][0]["name"]


def _transcript(tmp_path, sid, title=None):
    path = tmp_path / f"{sid}.jsonl"
    path.write_text(
        json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": sid}) + "\n"
        if title else ""
    )
    return str(path)


def test_statusline_name_outranks_everything(tmp_path, monkeypatch):
    """Claude Code populates session_name from --name / /rename, falling back to
    the AI title, and omits it when it would only be the default slug. Present
    means real."""
    d = BobDaemon()
    d._session_metrics["s1"] = {"session_name": "What I called it"}
    assert _name(d, _stub(cli_name="myproj-s1"),
                 _transcript(tmp_path, "s1", "Transcript title"),
                 monkeypatch) == "What I called it"


def test_transcript_title_beats_the_cli_slug(tmp_path, monkeypatch):
    d = BobDaemon()
    assert _name(d, _stub(cli_name="myproj-s1"),
                 _transcript(tmp_path, "s1", "Transcript title"),
                 monkeypatch) == "Transcript title"


def test_a_nameless_session_says_so(tmp_path, monkeypatch):
    """Neither a session id ("baea1baa") nor the CLI's default slug
    ("shop-front-00") is a name — both are disguises, and the slug only repeats
    the project column one line below."""
    d = BobDaemon()
    stub = _stub("baea1baa11", project="shop-front", cli_name="shop-front-00")
    assert _name(d, stub, _transcript(tmp_path, "baea1baa11"),
                 monkeypatch) == UNNAMED_SESSION


def test_the_nameless_placeholder_is_english():
    """Every other assertion about this constant is symbolic — they compare the
    ladder's answer against the imported symbol, so they pass for any value and
    pin nothing about the wording. This one reads the wording itself, so a
    placeholder set back to a non-English word fails here."""
    assert UNNAMED_SESSION == "New session"
    assert UNNAMED_SESSION.isascii()


@pytest.mark.parametrize("cli_name,project,is_slug", [
    ("shop-front-00", "shop-front", True),
    ("demo-app-63", "demo-app", True),
    ("t-f3", "T", True),                    # cwd basename case need not match
    ("SHOP-FRONT-FB", "shop-front", True),
    ("release-4f", "shop-front", False),    # a real name that happens to look slug-ish
    ("shop-front-zz", "shop-front", False), # not hex
    ("shop-front-000", "shop-front", False),
    ("Analyze and reduce excessive tests", "demo-app", False),
])
def test_default_slug_detection(cli_name, project, is_slug):
    assert _is_default_slug(cli_name, project) is is_slug


def test_a_descriptive_cli_name_survives(tmp_path, monkeypatch):
    """Background agents get real names from the CLI — those must not be thrown
    away with the slugs."""
    d = BobDaemon()
    stub = _stub("bg", project="demo-app",
                 cli_name="Analyze and reduce excessive automation tests")
    assert _name(d, stub, None, monkeypatch) == \
        "Analyze and reduce excessive automation tests"


def test_blank_statusline_name_is_not_a_name(tmp_path, monkeypatch):
    d = BobDaemon()
    d._session_metrics["s1"] = {"session_name": "   "}
    assert _name(d, _stub(cli_name="myproj-11"), None, monkeypatch) == UNNAMED_SESSION


def test_a_title_appearing_later_replaces_the_placeholder(tmp_path, monkeypatch):
    """The placeholder must not stick: the moment the transcript gains a title,
    the next snapshot has to show it."""
    from dark_army_daemon import session_stats as ss
    d = BobDaemon()
    path = tmp_path / "s1.jsonl"
    path.write_text("")
    monkeypatch.setattr(ss, "resolve_transcript", lambda sid: str(path))
    stub = _stub("s1", project="myproj", cli_name="myproj-11")

    assert d._enrich_agent_stubs([dict(stub)])["sleeping"][0]["name"] == UNNAMED_SESSION

    path.write_text(json.dumps(
        {"type": "ai-title", "aiTitle": "Now it has a title", "sessionId": "s1"}) + "\n")
    assert d._enrich_agent_stubs([dict(stub)])["sleeping"][0]["name"] == "Now it has a title"


# --- Stopping a live session -------------------------------------------------
#
# The mirror image of the delete tests above. Deletion is guarded by "is this a
# tombstone"; stopping is aimed at live sessions on purpose, so its guard is
# identity: the PID on record must still BE the Claude process we recorded.


class _FakeProc:
    """Stands in for psutil.Process. `terminate` records rather than signals —
    a test that actually sent SIGTERM would be aiming at the test runner."""

    def __init__(self, name="claude", cmdline=("claude",)):
        self._name, self._cmdline = name, list(cmdline)
        self.terminated = False
        self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _daemon_with_pid(sid, pid, **state):
    d = BobDaemon()
    d._session_states[sid] = {"state": "idle", "last_event": time.time(),
                              "pid": pid, **state}
    return d


def test_stopping_a_session_signals_the_process(monkeypatch):
    proc = _FakeProc()
    d = _daemon_with_pid("live", 4242)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    ok, detail = d.stop_session("live")
    assert (ok, detail) == (True, "stopping")
    assert proc.terminated is True


def test_a_recycled_pid_is_never_signalled(monkeypatch):
    """The property this whole method exists for. Our record can be minutes stale
    and PIDs get reused, so a session whose PID now belongs to something else must
    be refused — not SIGTERM'd on the strength of pid_exists alone."""
    proc = _FakeProc(name="Dock", cmdline=("/System/.../Dock",))
    d = _daemon_with_pid("stale", 4242)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    ok, detail = d.stop_session("stale")
    assert ok is False
    assert "no longer a Claude session" in detail
    assert proc.terminated is False
    # Still listed: refusing to kill it is not a reason to forget it.
    assert "stale" in d._session_states


def test_a_node_wrapped_claude_is_recognised(monkeypatch):
    """`ps -o comm=` reports "node" for a node-wrapped install, so identity has to
    fall through to argv — the same rule pid_resolver walks ancestors with."""
    proc = _FakeProc(name="node", cmdline=("node", "/usr/local/bin/claude"))
    d = _daemon_with_pid("wrapped", 4242)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    assert d.stop_session("wrapped") == (True, "stopping")
    assert proc.terminated is True


def test_stopping_a_grok_session_signals_the_process(monkeypatch):
    proc = _FakeProc(name="grok", cmdline=("grok",))
    d = _daemon_with_pid("g1", 5555, provider="grok")
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    assert d.stop_session("g1") == (True, "stopping")
    assert proc.terminated is True


def test_stop_uses_the_roster_tui_when_hooks_stamped_the_leader(monkeypatch):
    """A leader pid on hook state must never be the thing we SIGTERM."""
    from dark_army_daemon.grok_roster import GrokRecord

    leader_proc = _FakeProc(
        name="grok",
        cmdline=("/Users/me/.grok/bin/grok", "agent", "leader",
                 "--no-exit-on-disconnect"),
    )
    tui_proc = _FakeProc(name="grok", cmdline=("grok",))
    procs = {9498: leader_proc, 95994: tui_proc}
    d = _daemon_with_pid("g1", 9498, provider="grok")
    d._grok_records["g1"] = GrokRecord(
        session_id="g1", pid=95994, cwd="/tmp/proj",
    )
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: procs[pid])
    monkeypatch.setattr(daemon_mod, "_process_cwd", lambda pid: "/tmp/proj")

    assert d.stop_session("g1") == (True, "stopping")
    assert leader_proc.terminated is False
    assert tui_proc.terminated is True


def test_a_grok_session_refuses_a_recycled_pid(monkeypatch):
    proc = _FakeProc(name="Dock", cmdline=("/System/.../Dock",))
    d = _daemon_with_pid("g1", 5555, provider="grok")
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    ok, detail = d.stop_session("g1")
    assert ok is False
    assert "Grok session" in detail
    assert proc.terminated is False


def test_stop_refuses_a_grok_pid_in_a_different_tree(monkeypatch):
    """Identity is this session's grok, not 'a grok exists at this number'."""
    from dark_army_daemon.grok_roster import GrokRecord

    proc = _FakeProc(name="grok", cmdline=("grok",))
    d = _daemon_with_pid("g-stale", 57149, provider="grok")
    d._grok_records["g-stale"] = GrokRecord(
        session_id="g-stale", pid=57149, cwd="/tmp/arpg-web",
    )
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)
    monkeypatch.setattr(daemon_mod, "_process_cwd", lambda pid: "/tmp/dark-army")

    ok, detail = d.stop_session("g-stale")
    assert ok is False
    assert "Grok session" in detail
    assert proc.terminated is False


def test_stopping_a_session_with_no_pid_is_refused():
    d = BobDaemon()
    d._session_states["ghost"] = {"state": "idle", "last_event": time.time()}
    ok, detail = d.stop_session("ghost")
    assert ok is False
    assert "no PID" in detail


def test_stopping_an_unknown_session_is_already_stopped():
    """A stale panel frame is the usual caller: the row is on screen, the
    daemon already dropped it. That click means 'get this off the screen'."""
    ok, detail = BobDaemon().stop_session("nobody")
    assert (ok, detail) == (True, "already stopped")


def test_stopping_an_already_dead_session_forgets_it(monkeypatch):
    """Nothing to signal is a success, and the row must not survive it."""
    def gone(pid):
        raise daemon_mod.psutil.NoSuchProcess(pid)
    d = _daemon_with_pid("dead", 4242)
    monkeypatch.setattr(daemon_mod.psutil, "Process", gone)

    assert d.stop_session("dead") == (True, "already stopped")
    assert "dead" not in d._session_states


def test_a_reconciler_only_session_can_be_stopped(monkeypatch):
    """The case that started this: a session the hooks never saw, whose PID comes
    from `claude agents --json`. It has no hook state to evict, so `_agent_records`
    is the only thing keeping its row alive."""
    proc = _FakeProc()
    d = BobDaemon()
    d._on_agent_records([AgentRecord("foreign", kind="interactive",
                                     activity="idle", pid=6269,
                                     started_at=time.time())])
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    assert d.stop_session("foreign") == (True, "stopping")
    assert proc.terminated is True
    # Not yet forgotten — _confirm_stop does that once the process is actually
    # gone, which is what keeps the panel honest if the SIGTERM is ignored.
    assert "foreign" in d._agent_records
    d._forget_stopped("foreign")
    assert "foreign" not in d._agent_records
    assert d._reconciled_categories() == {}


def _no_sleeping(monkeypatch):
    """_confirm_stop's waits are ~7s of real time. The escalation is what the
    tests are about, not the clock, so the sleeps become no-ops."""
    async def instant(_delay):
        return None
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_a_session_that_ignores_sigterm_is_killed(monkeypatch):
    """The case this exists for: a wedged session that never runs its shutdown
    path. SIGTERM changes nothing, so a second one would change nothing twice."""
    _no_sleeping(monkeypatch)
    proc = _FakeProc()
    alive = iter([True] * 4 + [False])  # survives the SIGTERM window, dies on kill
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: next(alive))
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    d = _daemon_with_pid("wedged", 4242)
    await d._confirm_stop("wedged", 4242)

    assert proc.killed is True
    assert "wedged" not in d._session_states


@pytest.mark.asyncio
async def test_a_session_that_honours_sigterm_is_never_killed(monkeypatch):
    _no_sleeping(monkeypatch)
    proc = _FakeProc()
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: False)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    d = _daemon_with_pid("polite", 4242)
    await d._confirm_stop("polite", 4242)

    assert proc.killed is False
    assert "polite" not in d._session_states


@pytest.mark.asyncio
async def test_escalation_re_checks_identity_before_sigkill(monkeypatch):
    """Six seconds is long enough for the session to exit and the PID to be
    recycled. SIGKILL to a stranger is worse than the SIGTERM already was."""
    _no_sleeping(monkeypatch)
    proc = _FakeProc(name="Dock", cmdline=("/System/.../Dock",))
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    d = _daemon_with_pid("recycled", 4242)
    await d._confirm_stop("recycled", 4242)

    assert proc.killed is False
    # Still listed: refusing to kill it is not a reason to forget it.
    assert "recycled" in d._session_states


# --- Codex capabilities, Hide, Jump, and strict native Stop ------------------


class _CodexProc:
    def __init__(self, pid=7001, cwd="/code/bob", thread_id="abc",
                 exe="/opt/codex", created=100.0):
        self.pid = pid
        self.info = {
            "pid": pid, "exe": exe, "cwd": cwd, "create_time": created,
            "cmdline": ["codex", "resume", thread_id],
        }
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _codex_root(thread_id="abc", *, revision=(10, 20), path=None):
    from pathlib import Path
    from dark_army_daemon import codex_rollouts

    return codex_rollouts.CodexRecord(
        session_id=f"codex:{thread_id}", thread_id=thread_id,
        path=path or Path(f"{thread_id}.jsonl"), cwd="/code/bob",
        originator="codex-tui", source_kind="cli", thread_source="user",
        revision=revision, last_event=time.time(),
    )


def _attach_codex(record, proc):
    from dark_army_daemon import codex_rollouts
    codex_rollouts.attach_process_ids([record], [proc])
    return record


def test_capabilities_are_published_without_pid_inference(monkeypatch):
    from dark_army_daemon import codex_rollouts

    explicit = _attach_codex(_codex_root("explicit"), _CodexProc(thread_id="explicit"))
    unique = _codex_root("unique")
    unique_proc = _CodexProc(pid=7002, thread_id="other")
    unique_proc.info["cmdline"] = ["codex"]
    codex_rollouts.attach_process_ids([unique], [unique_proc])
    passive = _codex_root("passive")
    d = BobDaemon()
    d._codex_records = {r.session_id: r for r in (explicit, unique, passive)}
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)

    rows = {row["session_id"]: row for row in d._collect_agent_stubs()}
    assert {key: rows["codex:explicit"][key] for key in
            ("can_stop", "can_jump", "can_hide", "can_resume")} == {
        "can_stop": True, "can_jump": True, "can_hide": False, "can_resume": False,
    }
    assert {key: rows["codex:unique"][key] for key in
            ("can_stop", "can_jump", "can_hide", "can_resume")} == {
        "can_stop": False, "can_jump": True, "can_hide": True, "can_resume": False,
    }
    assert {key: rows["codex:passive"][key] for key in
            ("can_stop", "can_jump", "can_hide", "can_resume")} == {
        "can_stop": False, "can_jump": False, "can_hide": True, "can_resume": False,
    }
    assert d._session_capabilities({
        "session_id": "claude", "provider": "claude", "pid": 42,
        "kind": "interactive",
    }) == {"can_stop": True, "can_jump": True,
           "can_hide": False, "can_resume": True}
    assert d._session_capabilities({
        "session_id": "grok", "provider": "grok", "pid": 43,
        "kind": "interactive",
    }) == {"can_stop": True, "can_jump": True,
           "can_hide": False, "can_resume": False}
    assert d._session_capabilities({
        "session_id": "codex:explicit", "provider": "codex", "pid": 7001,
        "kind": "interactive", "alive": False,
    }) == {"can_stop": False, "can_jump": False,
           "can_hide": False, "can_resume": False}


def test_title_only_codex_target_never_grants_or_reaches_process_control(monkeypatch):
    from dark_army_daemon import codex_rollouts

    record = _codex_root("wrapper")
    proc = _CodexProc(thread_id="wrapper")

    class Writer:
        calls = []

        def apply(self, snapshot, codex_ttys):
            self.calls.append((snapshot, codex_ttys))

    d = BobDaemon()
    d._codex_records[record.session_id] = record
    d._titles = Writer()
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)
    monkeypatch.setattr(
        codex_rollouts, "resolve_title_ttys",
        lambda roots: {roots[0].session_id: "/dev/ttys004"},
    )
    monkeypatch.setattr(
        codex_rollouts,
        "matching_process_identity",
        lambda *args, **kwargs: pytest.fail("title target reached identity matching"),
    )

    title_roots = codex_rollouts.project_title_roots([record])
    d._apply_terminal_titles({"running": []}, title_roots)
    assert d._titles.calls[0][1] == {record.session_id: "/dev/ttys004"}

    row = next(
        item for item in d._collect_agent_stubs()
        if item["session_id"] == record.session_id
    )
    assert row["pid"] is None
    assert {key: row[key] for key in
            ("can_stop", "can_jump", "can_hide", "can_resume")} == {
        "can_stop": False, "can_jump": False,
        "can_hide": True, "can_resume": False,
    }
    assert "title_tty" not in row and "codex_ttys" not in row
    assert d._resolve_session_pid(record.session_id) is None
    assert d.stop_codex_session(record.session_id) == (
        False, "this Codex session has no controllable CLI identity"
    )
    assert proc.terminated is False and proc.killed is False


@pytest.mark.asyncio
async def test_agents_push_resolves_private_copied_codex_roots_on_worker(monkeypatch):
    from pathlib import Path
    import threading

    from dark_army_daemon import codex_rollouts

    record = _codex_root("wrapper")
    record.path = Path("before.jsonl")

    class Observer:
        def __init__(self):
            self.snapshots = []

        def on_agents_change(self, snapshot):
            self.snapshots.append(snapshot)

    class Writer:
        def __init__(self):
            self.calls = []

        def apply(self, snapshot, codex_ttys):
            self.calls.append((snapshot, codex_ttys))

    observer = Observer()
    writer = Writer()
    d = BobDaemon(observer=observer)
    d._codex_records[record.session_id] = record
    # The rosters are current: the push must not re-read the (empty) journals
    # over the hand-built record before it copies the roots.
    d._roster_refreshed_at = time.monotonic()
    d._titles = writer
    d._collect_agent_stubs = lambda: [{
        "session_id": record.session_id,
        "provider": "codex",
        "pid": None,
    }]

    loop_thread = threading.get_ident()
    resolved = []

    def resolve(roots):
        resolved.append((roots, threading.get_ident()))
        return {roots[0].session_id: "/dev/ttys004"}

    monkeypatch.setattr(codex_rollouts, "resolve_title_ttys", resolve)

    def enrich(stubs):
        record.path = Path("after.jsonl")
        record.cwd = "/code/after"
        return {"running": [dict(stubs[0])], "waiting": [], "sleeping": []}

    d._enrich_agent_stubs = enrich
    d._apply_grok_live_subagents = lambda snapshot: None

    async def no_async_work():
        return None

    d._flush_auto_compacts = no_async_work
    d._flush_session_titles = no_async_work
    d._deliver_alerts = lambda: None
    d._drain_pending_agents_push = lambda: None

    await d._push_agents_snapshot()

    roots, resolver_thread = resolved[0]
    assert roots == (codex_rollouts.CodexTitleRoot(
        session_id=record.session_id, thread_id="wrapper",
        path=Path("before.jsonl"), cwd="/code/bob",
    ),)
    assert resolver_thread != loop_thread
    assert writer.calls[0][1] == {record.session_id: "/dev/ttys004"}
    assert observer.snapshots == [writer.calls[0][0]]
    assert all(
        "title_tty" not in row and "codex_ttys" not in row
        for row in observer.snapshots[0]["running"]
    )


@pytest.mark.asyncio
async def test_agents_push_still_delivers_snapshot_when_title_resolution_fails(
    monkeypatch,
):
    from dark_army_daemon import codex_rollouts

    class Observer:
        def __init__(self):
            self.snapshots = []

        def on_agents_change(self, snapshot):
            self.snapshots.append(snapshot)

    observer = Observer()
    d = BobDaemon(observer=observer)
    d._codex_records["codex:abc"] = _codex_root()
    d._collect_agent_stubs = lambda: [{
        "session_id": "codex:abc", "provider": "codex", "pid": None,
    }]
    d._enrich_agent_stubs = lambda stubs: {
        "running": [dict(stubs[0])], "waiting": [], "sleeping": [],
    }
    d._apply_grok_live_subagents = lambda snapshot: None
    monkeypatch.setattr(
        codex_rollouts, "resolve_title_ttys",
        lambda _roots: (_ for _ in ()).throw(RuntimeError("denied")),
    )

    async def no_async_work():
        return None

    d._flush_auto_compacts = no_async_work
    d._flush_session_titles = no_async_work
    d._deliver_alerts = lambda: None
    d._drain_pending_agents_push = lambda: None

    await d._push_agents_snapshot()

    assert len(observer.snapshots) == 1
    assert observer.snapshots[0]["running"][0]["session_id"] == "codex:abc"


def test_pid_backed_codex_snapshot_never_publishes_can_type(monkeypatch):
    record = _attach_codex(_codex_root(), _CodexProc())
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)
    monkeypatch.setattr(
        daemon_mod.vscode_reveal, "can_send_text",
        lambda _pid: pytest.fail("Codex must not probe the unsupported typing route"),
    )

    snapshot = d._enrich_agent_stubs(d._collect_agent_stubs())
    entry = snapshot["waiting"][0]

    assert entry["provider"] == "codex"
    assert entry["pid"] == record.pid
    assert entry["can_type"] is False


def test_codex_stop_signals_only_a_fully_matching_native_resume(monkeypatch):
    from dark_army_daemon import codex_rollouts

    proc = _CodexProc()
    record = _attach_codex(_codex_root(), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    assert d.stop_codex_session(record.session_id) == (True, "stopping")
    assert proc.terminated is True

    proc.terminated = False
    proc.info["create_time"] = 101.0
    ok, detail = d.stop_codex_session(record.session_id)
    assert ok is False and "identity changed" in detail
    assert proc.terminated is False


@pytest.mark.asyncio
async def test_codex_sigkill_rechecks_creation_time(monkeypatch):
    from dark_army_daemon import codex_rollouts

    _no_sleeping(monkeypatch)
    proc = _CodexProc()
    record = _attach_codex(_codex_root(), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    proc.info["create_time"] = 999.0
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    await d._confirm_codex_stop(record.session_id, record.process_identity)
    assert proc.killed is False


@pytest.mark.asyncio
async def test_codex_sigkill_settles_recycled_pid_without_signalling(monkeypatch):
    from dark_army_daemon import codex_rollouts

    class _RecycledProcess:
        @staticmethod
        def create_time():
            return 999.0

    _no_sleeping(monkeypatch)
    proc = _CodexProc()
    record = _attach_codex(_codex_root(revision=(11, 22)), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: _RecycledProcess())
    proc.info["create_time"] = 999.0
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda **_kwargs: [record])

    await d._confirm_codex_stop(record.session_id, record.process_identity)

    assert proc.killed is False
    assert record.session_id not in d._codex_records
    assert d._hidden_codex[record.session_id] == (record.path, record.revision)


@pytest.mark.asyncio
async def test_codex_sigkill_keeps_row_when_identity_is_inaccessible(monkeypatch):
    from dark_army_daemon import codex_rollouts

    class _DeniedProcess:
        @staticmethod
        def create_time():
            raise daemon_mod.psutil.AccessDenied()

    _no_sleeping(monkeypatch)
    proc = _CodexProc()
    record = _attach_codex(_codex_root(revision=(11, 22)), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: _DeniedProcess())
    proc.info.pop("create_time")
    proc.create_time = _DeniedProcess.create_time
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])

    await d._confirm_codex_stop(record.session_id, record.process_identity)

    assert proc.killed is False
    assert d._codex_records[record.session_id] is record
    assert record.session_id not in d._hidden_codex


@pytest.mark.asyncio
async def test_codex_post_sigkill_settles_a_recycled_pid(monkeypatch):
    from dark_army_daemon import codex_rollouts

    class _RecycledProcess:
        @staticmethod
        def create_time():
            return 999.0

    _no_sleeping(monkeypatch)
    proc = _CodexProc()
    record = _attach_codex(_codex_root(revision=(11, 22)), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: _RecycledProcess())
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [record])

    def kill_and_recycle():
        proc.killed = True
        proc.info["create_time"] = 999.0

    proc.kill = kill_and_recycle

    await d._confirm_codex_stop(record.session_id, record.process_identity)

    assert proc.killed is True
    assert record.session_id not in d._codex_records
    assert d._hidden_codex[record.session_id] == (record.path, record.revision)


@pytest.mark.asyncio
async def test_codex_post_sigkill_retains_row_when_introspection_is_denied(monkeypatch):
    from dark_army_daemon import codex_rollouts

    class _DeniedProcess:
        @staticmethod
        def create_time():
            raise daemon_mod.psutil.AccessDenied()

    _no_sleeping(monkeypatch)
    proc = _CodexProc()
    record = _attach_codex(_codex_root(revision=(11, 22)), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: _DeniedProcess())
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])

    def kill_then_deny():
        proc.killed = True
        proc.info.pop("create_time")
        proc.create_time = _DeniedProcess.create_time

    proc.kill = kill_then_deny

    await d._confirm_codex_stop(record.session_id, record.process_identity)

    assert proc.killed is True
    assert d._codex_records[record.session_id] is record
    assert record.session_id not in d._hidden_codex


def test_absent_codex_process_hides_settled_revision(monkeypatch):
    from dark_army_daemon import codex_rollouts

    proc = _CodexProc()
    record = _attach_codex(_codex_root(revision=(11, 22)), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [])
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: False)
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [record])
    assert d.stop_codex_session(record.session_id) == (True, "already stopped")
    assert record.session_id not in d._codex_records
    assert d._hidden_codex[record.session_id] == (record.path, record.revision)


def test_hide_is_revision_scoped_idempotent_and_resets_on_restart(monkeypatch):
    from pathlib import Path
    from dark_army_daemon import codex_rollouts

    record = _codex_root(revision=(10, 20))
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    assert d.hide_codex_session(record.session_id)[0] is True
    assert record.session_id not in d._codex_records
    assert d.hide_codex_session(record.session_id) == (True, "already hidden")

    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [record])
    d._refresh_codex_records()
    assert record.session_id not in d._codex_records

    changed = _codex_root(revision=(10, 21))
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [changed])
    d._refresh_codex_records()
    assert d._codex_records[record.session_id] is changed
    assert record.session_id not in d._hidden_codex

    d._hidden_codex[record.session_id] = (record.path, record.revision)
    replacement = _codex_root(revision=record.revision, path=Path("replacement.jsonl"))
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [replacement])
    d._refresh_codex_records()
    assert d._codex_records[record.session_id] is replacement
    assert BobDaemon()._hidden_codex == {}

    other = BobDaemon()
    other._session_states["claude"] = {"state": "idle"}
    assert other.hide_codex_session("claude")[0] is False


def test_codex_usage_read_does_not_refresh_loop_owned_roster(monkeypatch):
    from dark_army_daemon import codex_rollouts

    record = _codex_root(revision=(10, 20))
    record.limit_bars = [{"kind": "codex_primary", "percent": 42}]
    d = BobDaemon()
    d._hidden_codex[record.session_id] = (record.path, record.revision)
    monkeypatch.setattr(
        codex_rollouts, "_load_usage_limits",
        lambda **_kwargs: [(record.last_event, record.limit_bars)],
    )
    monkeypatch.setattr(
        d, "_refresh_codex_records",
        lambda: pytest.fail("worker usage read refreshed the loop-owned roster"),
    )

    snapshot = d.codex_usage_snapshot()

    assert snapshot["available"] is True
    assert snapshot["bars"][0]["percent"] == 42
    assert d._codex_records == {}
    assert d._hidden_codex == {
        record.session_id: (record.path, record.revision),
    }


def test_codex_reveal_rechecks_identity(monkeypatch):
    from dark_army_daemon import codex_rollouts

    proc = _CodexProc()
    record = _attach_codex(_codex_root(), proc)
    d = BobDaemon()
    d._codex_records[record.session_id] = record
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    assert d._resolve_session_pid(record.session_id) == proc.pid
    proc.info["cwd"] = "/code/other"
    assert d._resolve_session_pid(record.session_id) is None


# --- The session's own folder, from the hook stream to the workspace label ---

def _ide_dir(tmp_path, monkeypatch):
    """A stand-in for the extension's lock directory, with the workspace cache
    cleared around it (the `test_workspace.py` fixture, reused here)."""
    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon import workspace as ws

    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    ws.invalidate()
    return ws


def _write_lock(ide_dir, port, **extra):
    ide_dir.joinpath(f"{port}.lock").write_text(
        json.dumps({"port": port, "authToken": "tok", **extra}))


def test_a_stub_with_no_roster_record_still_carries_its_folder():
    """The bug: an interactive session has no `claude agents --json` record, so
    the stub's cwd was `""` and the workspace ladder never ran."""
    d = BobDaemon()
    d._session_states["s1"] = {
        "state": "idle", "last_event": 0, "project": "host",
        "cwd": "/x/proj/host",
    }
    stub = d._collect_agent_stubs()[0]
    assert stub["cwd"] == "/x/proj/host"


def test_the_roster_record_still_wins_over_the_stored_folder():
    d = _daemon_with_records(AgentRecord("s1", cwd="/x/roster/repo"))
    d._session_states["s1"] = {
        "state": "idle", "last_event": 0, "cwd": "/x/proj/host",
    }
    assert d._collect_agent_stubs()[0]["cwd"] == "/x/roster/repo"


def test_a_subdirectory_session_files_under_its_window(tmp_path, monkeypatch):
    """End to end: the row's project is the VS Code window's name, not the
    subfolder's basename."""
    ws = _ide_dir(tmp_path, monkeypatch)
    repo = "/Users/bob/Code/work/dark-army"
    _write_lock(tmp_path, 19101, workspaceFolders=[repo],
                workspaceFsPath=f"{repo}.code-workspace")
    try:
        d = BobDaemon()
        d._session_states["00000000-0000-0000-0000-000000000000"] = {
            "state": "idle", "last_event": 0, "project": "host",
            "cwd": f"{repo}/host",
        }
        rows = d._enrich_agent_stubs(d._collect_agent_stubs())
        row = [r for group in rows.values() for r in group][0]
        assert row["project"] == "dark-army"
    finally:
        ws.invalidate()


def test_the_transcript_backs_up_a_row_with_no_stored_folder(tmp_path,
                                                             monkeypatch):
    """Rows whose state predates the hook-carried cwd, or came back from an old
    sessions.json, still resolve — from `stats.cwd`."""
    from dark_army_daemon import session_stats as ss

    ws = _ide_dir(tmp_path, monkeypatch)
    repo = "/Users/bob/Code/work/dark-army"
    _write_lock(tmp_path, 19102, workspaceFolders=[repo],
                workspaceFsPath=f"{repo}.code-workspace")
    try:
        d = BobDaemon()
        stats = ss.SessionStats()
        stats.cwd = f"{repo}/host"

        class _Cache:
            def get(self, _path):
                return stats

        d.__dict__["_stats_cache"] = _Cache()
        stub = {"session_id": "00000000-0000-0000-0000-000000000000",
                "project": "host", "state": "idle", "cwd": "",
                "subagents": 0, "subagent_ids": [], "_category": "sleeping"}
        entry = d._enrich_agent_stubs([stub])["sleeping"][0]
        assert entry["cwd"] == f"{repo}/host"
        assert entry["project"] == "dark-army"
    finally:
        ws.invalidate()


def test_session_place_falls_back_to_the_stored_folder():
    """The repaired card-root path: a session absent from the snapshot still
    names its folder."""
    d = BobDaemon()
    d._session_states["s1"] = {
        "state": "idle", "last_event": 0, "project": "host",
        "cwd": "/x/proj/host",
    }
    cwd, project = d._session_place("s1")
    assert cwd == "/x/proj/host"
    assert project == "host"


def _navigation_daemon(tmp_path, monkeypatch):
    from tests.test_codex_rollouts import _navigation_fixture, _title_roots
    from dark_army_daemon import codex_rollouts
    from types import MappingProxyType
    roots, processes = _navigation_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: roots)
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in roots}
    daemon._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(_title_roots(roots)))
    return daemon, roots, processes


@pytest.mark.asyncio
async def test_codex_navigation_only_publishes_jump_and_reveals_exact_pid(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from dark_army_daemon import vscode_reveal
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    reveal = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(vscode_reveal, "reveal", reveal)
    for root, proc in zip(roots, processes):
        stub = next(s for s in daemon._collect_agent_stubs() if s["session_id"] == root.session_id)
        assert stub["pid"] is None
        assert stub["can_jump"] and stub["can_hide"]
        assert not stub["can_stop"] and not stub["can_resume"]
        assert all(key not in stub for key in ("journal", "proof", "executable", "process_identity", "_codex_navigation"))
        assert await daemon.reveal_in_vscode(root.session_id) == (True, "")
        reveal.assert_called_with(proc.pid)
    assert reveal.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["pid_reused", "hidden", "child", "finished", "replaced", "map_changed"])
async def test_codex_navigation_stale_proof_never_reveals(tmp_path, monkeypatch, fault):
    from unittest.mock import AsyncMock
    from dataclasses import replace
    from types import MappingProxyType
    from dark_army_daemon import vscode_reveal, codex_rollouts
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    root = roots[0]
    reveal = AsyncMock()
    monkeypatch.setattr(vscode_reveal, "reveal", reveal)
    if fault == "pid_reused":
        processes[0].fresh["create_time"] += 100
    elif fault == "hidden":
        daemon._hide_codex_revision(root.session_id, root)
    elif fault == "child":
        root.parent_thread_id = "parent"
    elif fault == "finished":
        daemon._codex_records.pop(root.session_id)
    else:
        original = codex_rollouts.navigation_targets
        def changed(*args):
            result = original(*args)
            if fault == "replaced":
                daemon._codex_records[root.session_id] = replace(root)
            else:
                daemon._codex_navigation = MappingProxyType({})
            return result
        monkeypatch.setattr(codex_rollouts, "navigation_targets", changed)
    assert not (await daemon.reveal_in_vscode(root.session_id))[0]
    reveal.assert_not_called()
