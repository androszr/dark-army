# host/tests/test_event_log_hooks.py
"""The daemon writing its diary (`event_log`) at each seam the plan names.

Seam: a `BobDaemon` with `_event_log = EventLog(tmp_path / ...)` — the same
construction `test_permission_broker.py` and `test_board_refine.py` use for
`_board`. Two threads are exercised on purpose: a hook reached from inside a
running loop hops to the executor (`_settle` waits for it), and one reached
from the executor or from plain sync code appends directly.

The one property every test here checks besides its own: **no entry anywhere
carries a forbidden key** — `claim` is the broker's secret, `port` the
channel's plumbing — because a row copied whole would have leaked one.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon.agents_poll import AgentRecord
from dark_army_daemon import dispatch
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.event_log import FORBIDDEN_KEYS, EventLog


# ── seams ─────────────────────────────────────────────────────────────────────

def _daemon(tmp_path) -> BobDaemon:
    d = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    d._event_log = log
    return d


async def _settle(log: EventLog, count: int, timeout: float = 3.0) -> None:
    """Wait for executor-hopped appends to land."""
    deadline = time.monotonic() + timeout
    while len(log) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def _no_secrets(entries) -> None:
    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert k not in FORBIDDEN_KEYS, (k, entries)
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(list(entries))


def _kinds(log: EventLog) -> list:
    return [e["kind"] for e in reversed(log.recent())]


def _known(daemon, sid="s1", pid=4242):
    daemon._session_states[sid] = {
        "state": "working", "last_event": time.time(), "pid": pid,
        "provider": "claude", "project": "bob",
    }
    return sid


def _stub(sid="s1", category="running", **kw):
    row = {"session_id": sid, "pid": 4242, "_category": category, "cwd": "",
           "project": "bob", "provider": "claude", "kind": "interactive"}
    row.update(kw)
    return row


# ── sessions ──────────────────────────────────────────────────────────────────

def test_first_live_appearance_logs_one_start_and_a_second_snapshot_none(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    d._enrich_agent_stubs([_stub()])
    assert _kinds(d._event_log) == ["session_start"]
    entry = d._event_log.recent()[0]
    assert entry["session_id"] == "s1"
    assert entry["project"] == "bob"
    assert entry["nickname"]                       # assigned by the enrich
    assert entry["text"] == f"{entry['nickname']} started in bob"
    assert entry["detail"]["provider"] == "claude"
    d._enrich_agent_stubs([_stub()])
    assert _kinds(d._event_log) == ["session_start"]
    _no_secrets(d._event_log.recent())


def test_tombstones_and_abandoned_rows_are_not_starts(tmp_path):
    d = _daemon(tmp_path)
    d._enrich_agent_stubs([_stub(category="finished", finished_at=time.time(),
                                 end_reason="ended", idle_seconds=1.0),
                           _stub(sid="s2", category="abandoned")])
    assert _kinds(d._event_log) == []


def test_forget_with_a_reason_logs_a_finish_with_the_reason(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    d._enrich_agent_stubs([_stub(started_at=time.time() - 300)])
    d._forget_session("s1", "ended")
    assert _kinds(d._event_log) == ["session_start", "session_end"]
    end = d._event_log.recent()[0]
    assert end["detail"]["end_reason"] == "ended"
    assert end["project"] == "bob"
    assert 290 <= end["detail"]["duration_seconds"] <= 320
    assert " finished in bob after " in end["text"]
    # A returning id logs a fresh start.
    _known(d)
    d._enrich_agent_stubs([_stub()])
    assert _kinds(d._event_log) == ["session_start", "session_end", "session_start"]


def test_forget_without_a_reason_logs_nothing(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    d._enrich_agent_stubs([_stub()])
    d._forget_session("s1", "")
    assert _kinds(d._event_log) == ["session_start"]


def test_a_session_never_seen_live_gets_no_finish(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    d._forget_session("s1", "ended")
    assert _kinds(d._event_log) == []


def test_a_roster_only_row_that_vanishes_logs_one_start_and_one_end(tmp_path):
    """A background agent or a Codex/Grok roster row never passes through
    `_forget_session`; its finish is `_record_finished` straight from
    `_on_agent_records`. The diary's finish rides that seam, so it pairs the
    start the enrich loop wrote — exactly once, and the id is released."""
    d = _daemon(tmp_path)
    rec = AgentRecord("bg1", name="reviewer", kind="background",
                      activity="running", pid=4243, cwd="")
    d._on_agent_records([rec])
    d._enrich_agent_stubs([_stub(sid="bg1", kind="background",
                                 started_at=time.time() - 30)])
    assert _kinds(d._event_log) == ["session_start"]
    assert "bg1" in d._logged_starts
    d._on_agent_records([])                    # gone from `claude agents`
    assert _kinds(d._event_log) == ["session_start", "session_end"]
    end = d._event_log.recent()[0]
    assert end["session_id"] == "bg1"
    assert end["detail"]["end_reason"] == "completed"
    assert "bg1" not in d._logged_starts
    # The tombstone is now in `_finished`; a second roster tick tombstones
    # nothing again and the diary holds exactly one finish.
    d._on_agent_records([])
    assert _kinds(d._event_log) == ["session_start", "session_end"]
    _no_secrets(d._event_log.recent())


def test_a_hook_stream_finish_logs_exactly_one_end(tmp_path):
    """`_forget_session` routes through `_record_finished`, where the line is
    now written; the two seams must not each write one."""
    d = _daemon(tmp_path)
    _known(d)
    d._enrich_agent_stubs([_stub()])
    d._forget_session("s1", "ended")
    assert _kinds(d._event_log) == ["session_start", "session_end"]
    # And a direct `_record_finished` for the same id afterwards (the roster
    # lagging the hook) has nothing left to log.
    d._record_finished("s1", {"state": "idle"}, "completed")
    assert _kinds(d._event_log) == ["session_start", "session_end"]


def test_a_restart_writes_no_second_start_for_a_session_still_live(tmp_path):
    """`_logged_starts` is empty after `run()`, and the first enriched
    snapshot used to log a start for every restored or roster row. The set
    is seeded from the diary instead."""
    d = _daemon(tmp_path)
    _known(d)
    d._enrich_agent_stubs([_stub(started_at=time.time() - 600)])
    assert _kinds(d._event_log) == ["session_start"]
    d._event_log.close()

    again = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    again._event_log = log
    again._seed_logged_starts()
    assert again._logged_starts == {"s1"}
    _known(again)
    again._enrich_agent_stubs([_stub(started_at=time.time() - 600)])
    assert _kinds(again._event_log) == ["session_start"]
    # The finish after the restart still pairs the start from before it.
    again._forget_session("s1", "ended")
    assert _kinds(again._event_log) == ["session_start", "session_end"]


def test_the_seed_reads_the_newest_start_or_end_per_session(tmp_path):
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    now = time.time()
    log.append("session_start", session_id="a", ts=now - 50)
    log.append("session_end", session_id="a", ts=now - 40,
               detail={"end_reason": "ended"})
    log.append("session_start", session_id="b", ts=now - 30)
    log.append("session_start", session_id="c", ts=now - 20)
    log.append("session_end", session_id="c", ts=now - 10,
               detail={"end_reason": "ended"})
    log.append("session_start", session_id="c", ts=now - 5)
    log.append("card_done", card_id="k", title="t", ts=now - 1)
    d = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    d._event_log = log
    d._seed_logged_starts()
    assert d._logged_starts == {"b", "c"}


def test_a_returning_stub_older_than_its_own_logged_end_is_not_a_new_start(tmp_path):
    """A stale-evicted session that comes back under the same id — or a
    roster stub returning after its run was tombstoned — carries a
    `started_at` before the finish already on record. That is the same run,
    not a second one."""
    d = _daemon(tmp_path)
    started = time.time() - 900
    _known(d)
    d._enrich_agent_stubs([_stub(started_at=started)])
    d._forget_session("s1", "stale")
    assert _kinds(d._event_log) == ["session_start", "session_end"]
    # Back under the same id, same start time: no new start.
    _known(d)
    d._enrich_agent_stubs([_stub(started_at=started)])
    assert _kinds(d._event_log) == ["session_start", "session_end"]
    assert "s1" not in d._logged_starts
    # A genuinely new run — a start after the finish — logs again.
    d._enrich_agent_stubs([_stub(started_at=time.time())])
    assert _kinds(d._event_log) == ["session_start", "session_end", "session_start"]


def test_two_stopfailures_log_one_error(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    for _ in range(2):
        asyncio.run(d._handle_message(
            {"event": "add", "hook": "StopFailure", "session_id": "s1",
             "project": "bob", "message": "API error"}))
    assert _kinds(d._event_log) == ["session_error"]
    assert d._event_log.recent()[0]["text"] == "claude hit an API error in bob"


# ── permissions ───────────────────────────────────────────────────────────────

def _ask(daemon, *, sid="s1", request_id="hook-abc", claim="cl41m"):
    return asyncio.run(daemon._handle_message({
        "event": "permission_ask", "session_id": sid, "cwd": "/x/proj",
        "request_id": request_id, "claim": claim, "tool_name": "Read",
        "description": "/etc/hosts",
        "input_preview": '{"file_path": "/etc/hosts"}',
    }))


def test_broker_ask_then_allow_logs_ask_then_allowed_and_no_secret(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    d._nicknames_shown = {"s1": "Hex"}
    assert _ask(d)["hold"] > 0
    ok, _ = asyncio.run(d.answer_permission("hook-abc", "allow"))
    assert ok
    assert _kinds(d._event_log) == ["permission_ask", "permission_resolved"]
    ask, allowed = reversed(d._event_log.recent())
    assert ask["text"] == "Hex wants to run Read: /etc/hosts"
    assert ask["detail"]["via"] == "hook"
    assert ask["detail"]["request_id"] == "hook-abc"
    assert allowed["text"] == "Hex's Read was allowed"
    assert allowed["detail"]["outcome"] == "allow"
    _no_secrets(d._event_log.recent())
    # The poll that collects the staged verdict is not a second site.
    asyncio.run(d._handle_message({
        "event": "permission_poll", "session_id": "s1", "cwd": "/x/proj",
        "request_id": "hook-abc", "claim": "cl41m"}))
    assert _kinds(d._event_log) == ["permission_ask", "permission_resolved"]
    # And the reap finds nothing to lapse.
    d._reap_permissions()
    assert len(d._event_log) == 2


def test_channel_ask_then_channel_gone_logs_lapsed_with_why(tmp_path):
    d = _daemon(tmp_path)
    d._session_states["s1"] = {"pid": 4242, "last_event": 0}
    d._handle_channel_message(
        {"type": "channel_attach", "port": 51000, "pid": 4242, "cwd": "/tmp",
         "session_id": "s1", "is_channel": True, "secret": "",
         "host": cs.HOST_CLAUDE})
    d._handle_channel_message({
        "type": "channel_permission_request", "port": 51000, "pid": 4242,
        "request_id": "abcde", "tool_name": "Bash",
        "description": "Delete the build directory",
        "input_preview": '{"command": "rm -rf .build"}'})
    assert _kinds(d._event_log) == ["permission_ask"]
    assert d._event_log.recent()[0]["detail"]["via"] == "channel"
    d._channels.clear()
    d._reap_permissions()
    assert _kinds(d._event_log) == ["permission_ask", "permission_resolved"]
    lapsed = d._event_log.recent()[0]
    assert lapsed["detail"]["outcome"] == "lapsed"
    assert lapsed["detail"]["why"] == "its channel went away"
    assert lapsed["text"].endswith("Bash lapsed — its channel went away")
    _no_secrets(d._event_log.recent())


def test_a_session_ending_lapses_its_open_prompt(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    _ask(d)
    d._forget_session("s1", "ended")
    kinds = _kinds(d._event_log)
    assert kinds.count("permission_resolved") == 1
    lapsed = [e for e in d._event_log.recent()
              if e["kind"] == "permission_resolved"][0]
    assert lapsed["detail"]["why"] == "the session ended"


# ── cards ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def board(tmp_path):
    d = _daemon(tmp_path)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _make(store, **kw):
    fields = {"title": "Add the diary", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude", "summary": "diary"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _arm_spawn(d, monkeypatch):
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/bin/{tool}")

    async def accept(root, argv, name, **_kw):
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)


@pytest.mark.asyncio
async def test_a_dispatch_logs_card_dispatched(board, monkeypatch):
    d, store = board
    card = _make(store)
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    await _settle(d._event_log, 1)
    entry = d._event_log.recent()[0]
    assert entry["kind"] == "card_dispatched"
    assert entry["card_id"] == card["id"]
    assert entry["text"] == "Add the diary started with claude in bob"
    assert entry["detail"] == {"tool": "claude", "root": "/tmp"}


def test_the_bind_window_expiry_logs_the_exact_dispatch_error(board):
    d, store = board
    card = _make(store, column_name="backlog")
    stale = time.time() - dispatch.DISPATCH_BIND_WINDOW - 1
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": stale})
    assert d._reconcile_board({"running": []}) is True
    got = store.get(card["id"])
    assert got["dispatch_error"] == "no session appeared — check the terminal that opened"
    assert _kinds(d._event_log) == ["card_dispatch_failed"]
    entry = d._event_log.recent()[0]
    assert entry["detail"]["error"] == got["dispatch_error"]
    assert entry["text"] == ("Add the diary could not start — no session "
                             "appeared — check the terminal that opened")


@pytest.mark.asyncio
async def test_an_agent_close_logs_exactly_one_done_with_its_nickname(board):
    d, store = board
    card = _make(store, column_name="in_progress")
    store.update(card["id"], {"session_id": "s1", "link_state": "live"})
    d._nicknames_shown = {"s1": "Vex"}
    got, detail = await d.close_card_by_session("s1", "All green.")
    assert got is not None, detail
    await _settle(d._event_log, 1)
    await asyncio.sleep(0.05)
    assert _kinds(d._event_log) == ["card_done"]
    entry = d._event_log.recent()[0]
    assert entry["nickname"] == "Vex"
    assert entry["detail"]["closed_by"] == "Vex"
    assert entry["detail"]["note"] == "All green."
    assert entry["text"] == "Vex finished the card Add the diary"


@pytest.mark.asyncio
async def test_a_human_drag_into_done_logs_done_by_user(board):
    d, store = board
    card = _make(store, column_name="backlog")
    got, detail = await d.update_card(card["id"], {"column_name": "done"})
    assert got is not None, detail
    await _settle(d._event_log, 1)
    entry = d._event_log.recent()[0]
    assert entry["kind"] == "card_done"
    assert entry["detail"]["closed_by"] == "user"
    assert entry["text"] == "Add the diary was moved to Done by hand"
    # A second write that keeps it in Done is not a second arrival.
    await d.update_card(card["id"], {"summary": "still done"})
    await asyncio.sleep(0.05)
    assert _kinds(d._event_log) == ["card_done"]


@pytest.mark.asyncio
async def test_flag_manual_logs_once(board):
    d, store = board
    card = _make(store, column_name="in_progress")
    store.update(card["id"], {"session_id": "s1", "link_state": "live"})
    got, detail = await d.flag_manual_by_session("s1", "1. Open it.\n2. Look.")
    assert got is not None, detail
    await _settle(d._event_log, 1)
    assert _kinds(d._event_log) == ["card_manual"]
    entry = d._event_log.recent()[0]
    assert entry["text"] == "Add the diary needs a manual check"
    assert entry["detail"]["steps"].startswith("1. Open it.")


@pytest.mark.asyncio
async def test_attach_plan_logs_once_with_the_basename(board, tmp_path):
    d, store = board
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "the-plan.md"
    plan.write_text("# a plan\n")
    card = _make(store, root=str(root))
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    d._nicknames_shown = {"planner-1": "Mira"}
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    await _settle(d._event_log, 1)
    assert _kinds(d._event_log) == ["card_plan_attached"]
    entry = d._event_log.recent()[0]
    assert entry["detail"]["plan_path"] == "the-plan.md"
    assert os.sep not in entry["detail"]["plan_path"]
    assert entry["text"] == "Mira attached a plan to Add the diary"


def test_every_hook_is_a_no_op_without_a_diary(tmp_path):
    d = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    assert d._event_log is None
    _known(d)
    d._enrich_agent_stubs([_stub()])
    d._forget_session("s1", "ended")
    d._log_permission({"request_id": "x", "tool_name": "Bash"}, "permission_ask")
    d._log_card_event({"id": "c", "title": "t"}, "card_manual")


# --- the auto-start reuses the two existing kinds and adds none ---------------


def test_the_diarys_kind_tuple_is_unchanged_by_the_auto_start():
    """The auto-start is `dispatch_card` and a `dispatch_error` write, both of
    which already have a kind. A new one would be a second name for the same
    event."""
    from dark_army_daemon import event_log as el
    assert el.KINDS == (
        "session_start", "session_end", "session_error",
        "permission_ask", "permission_resolved",
        "card_dispatched", "card_done", "card_manual",
        # A person's Passed / Failed on a manual check file.
        "card_manual_outcome",
        "card_dispatch_failed", "card_plan_attached",
        "card_plan_approved", "card_work_recorded",
        # The phone doors' burst alert (access_log.py) — its own event, not
        # a second name for the auto-start's.
        "access_burst",
        # Review and merge (3 Oct 2026): a landed merge, one that stopped
        # and a review's verdict, words only.
        "card_merged", "card_merge_blocked", "card_review_verdict",
        # A step interrupted by a restart and not repeated
        # (`docs/action-journal.md`).
        "action_interrupted",
    )


@pytest.mark.asyncio
async def test_an_auto_start_logs_card_dispatched(board, monkeypatch, tmp_path):
    d, store = board
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "p.md"
    plan.write_text("# a plan\n")
    _arm_spawn(d, monkeypatch)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    card = _make(store, root=str(root), column_name="prep",
                 start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    await asyncio.gather(*list(d._auto_start_tasks))
    await _settle(d._event_log, 2)
    assert _kinds(d._event_log) == ["card_plan_attached", "card_dispatched"]


@pytest.mark.asyncio
async def test_a_refused_auto_start_logs_card_dispatch_failed(board,
                                                              monkeypatch,
                                                              tmp_path):
    d, store = board
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "p.md"
    plan.write_text("# a plan\n")
    _arm_spawn(d, monkeypatch)
    d.board_dispatch_enabled = False
    card = _make(store, root=str(root), column_name="prep",
                 start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    await asyncio.gather(*list(d._auto_start_tasks))
    await _settle(d._event_log, 2)
    assert _kinds(d._event_log) == ["card_plan_attached", "card_dispatch_failed"]
    entry = d._event_log.recent()[0]
    assert "not allowed to start sessions" in entry["detail"]["error"]
