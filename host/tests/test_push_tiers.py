"""The two tiers of `_push_agents_snapshot`.

The heavy half of a push — the roster re-read, navigation proofs, the
checklist, collaboration, terminal titles and the board reconcile — runs only
when `_structural_key` moved or `FULL_PUSH_INTERVAL_SECONDS` lapsed. Every
push still notifies the observers, so the panel's numbers keep moving.
These tests count each heavy step through its own seam.
"""
import asyncio
import time
from collections import Counter

import pytest

from dark_army_daemon import collaboration
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


class _Probe:
    def __init__(self):
        self.pushes = 0

    def on_agents_change(self, snapshot) -> None:
        self.pushes += 1


def _tiered_daemon(monkeypatch, probe, *, real_reconcile=False, during=None):
    """A daemon whose push pipeline runs for real, with each heavy step
    replaced by a counter and every I/O seam stubbed."""
    d = BobDaemon(observer=probe)
    d.AGENTS_PUSH_MIN_INTERVAL_SECONDS = 0
    counts = Counter()

    d._enrich_agent_stubs = lambda stubs: {"stubs": stubs}
    d._titles.apply = lambda *a, **k: None

    def titles(snapshot, roots):
        counts["titles"] += 1
    d._apply_terminal_titles = titles

    def checklist(snapshot):
        counts["checklist"] += 1
        return {}
    d._observe_checklist = checklist

    def build(snapshot, cards=()):
        counts["collaboration"] += 1
        return collaboration.unavailable()
    monkeypatch.setattr(collaboration, "build", build)

    real = d._reconcile_board

    def reconcile(snapshot):
        counts["reconcile"] += 1
        if during is not None:
            during()
        if real_reconcile:
            real(snapshot)
        return False
    d._reconcile_board = reconcile

    def load_rosters():
        counts["rosters"] += 1
        return None, None
    d._load_rosters = load_rosters
    return d, counts


async def _settle(d):
    """Until no push and no roster refresh is in flight or pending."""
    quiet = 0
    for _ in range(500):
        task = d.__dict__.get("_agents_push_task")
        roster = d._roster_refresh_task
        busy = ((task is not None and not task.done())
                or d._agents_push_pending
                or (roster is not None and not roster.done()))
        quiet = 0 if busy else quiet + 1
        if quiet >= 3:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pushes never settled")


def _working(sid="s1"):
    return {"state": "working", "last_event": time.time(), "project": "p",
            "cwd": "/tmp/p"}


@pytest.mark.asyncio
async def test_twenty_wakes_without_a_change_reconcile_once(monkeypatch):
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    d._session_states["s1"] = _working()
    for _ in range(20):
        await d._wake_surfaces()
        await asyncio.sleep(0)
    await _settle(d)
    assert probe.pushes >= 1
    assert counts["reconcile"] == 1
    assert counts["titles"] == 1
    assert counts["collaboration"] == 1
    assert counts["checklist"] == 1


@pytest.mark.asyncio
async def test_the_light_tier_still_notifies_observers_on_every_push(monkeypatch):
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    d._session_states["s1"] = _working()
    for _ in range(5):
        await d._push_agents_snapshot()
    assert probe.pushes == 5
    assert counts["reconcile"] == 1


@pytest.mark.asyncio
async def test_a_state_change_runs_the_full_tier_once_more(monkeypatch):
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    d._session_states["s1"] = _working()
    await d._push_agents_snapshot()
    await d._push_agents_snapshot()
    assert counts["reconcile"] == 1
    d._session_states["s1"]["state"] = "idle"
    await d._push_agents_snapshot()
    await d._push_agents_snapshot()
    assert counts["reconcile"] == 2
    assert counts["titles"] == 2


@pytest.mark.asyncio
async def test_the_floor_runs_the_full_tier_once_more(monkeypatch):
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    d._session_states["s1"] = _working()
    await d._push_agents_snapshot()
    await d._push_agents_snapshot()
    assert counts["reconcile"] == 1
    d._last_full_push_at -= d.FULL_PUSH_INTERVAL_SECONDS
    await d._push_agents_snapshot()
    await d._push_agents_snapshot()
    assert counts["reconcile"] == 2


@pytest.mark.asyncio
async def test_statusline_messages_push_light(monkeypatch):
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    d._session_states["s1"] = _working()
    await d._push_agents_snapshot()
    await _settle(d)
    assert counts["reconcile"] == 1
    before = probe.pushes
    d._last_metrics_push = 0.0
    for n in range(10):
        d._handle_statusline({"session_id": "s1", "data": {
            "session_id": "s1", "cost": {"total_cost_usd": 0.01 * n}}})
        await asyncio.sleep(0)
    await _settle(d)
    # The first message buys one push (METRICS_PUSH_INTERVAL); it is light.
    assert probe.pushes == before + 1
    assert counts["reconcile"] == 1


@pytest.mark.asyncio
async def test_a_board_write_moves_the_key(tmp_path, monkeypatch):
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        d._session_states["s1"] = _working()
        await d._push_agents_snapshot()
        await d._push_agents_snapshot()
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 1
        card, reason = store.create({"title": "New", "root": str(tmp_path),
                                     "tool": "claude"})
        assert card, reason
        await d._push_agents_snapshot()
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_reconciles_own_writes_do_not_make_the_next_push_full(
        tmp_path, monkeypatch):
    """A live card's observation writes its ledgers on the reconcile; those
    writes move the store's counter, and must not count as structural."""
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe, real_reconcile=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        card, reason = store.create({"title": "Run", "root": str(tmp_path),
                                     "tool": "claude"})
        assert card, reason
        d._session_states["s1"] = _working()
        before = store.change_counter()
        rows_before = store._conn.total_changes
        await d._push_agents_snapshot()
        # The first pass wrote the card's first checkpoints, and the store's
        # counter leaves exactly those writes out.
        assert store._conn.total_changes > rows_before
        assert store.change_counter() == before
        await d._push_agents_snapshot()
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_lapsed_roster_window_on_the_loop_schedules_not_refreshes(
        monkeypatch):
    d = BobDaemon()
    calls = []
    loads = []

    def load_rosters():
        loads.append(1)
        return ["grok roster"], ["codex roster"]
    d._load_rosters = load_rosters
    monkeypatch.setattr(d, "_refresh_grok_records",
                        lambda recs=None: calls.append(("grok", recs)))
    monkeypatch.setattr(d, "_refresh_codex_records",
                        lambda records=None: calls.append(("codex", records)))
    d._roster_refreshed_at = (time.monotonic()
                              - daemon_mod.ROSTER_REFRESH_INTERVAL - 1)

    categories = d._reconciled_categories()
    # Answered from the last rosters, synchronously: nothing loaded inline.
    assert categories == {}
    assert loads == [] and calls == []
    # A second lapsed ask while the first is in flight starts nothing new.
    d._roster_refreshed_at -= daemon_mod.ROSTER_REFRESH_INTERVAL
    d._reconciled_categories()
    await _settle(d)
    assert loads == [1]
    assert calls == [("grok", ["grok roster"]), ("codex", ["codex roster"])]


@pytest.mark.asyncio
async def test_a_board_write_during_the_reconcile_still_makes_the_next_push_full(
        tmp_path, monkeypatch):
    """A person's write that commits while the reconcile runs is news; only
    the pass's own ledger writes are left out of the store's counter."""
    probe = _Probe()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    card, reason = store.create({"title": "Run", "root": str(tmp_path),
                                 "tool": "claude"})
    assert card, reason
    writes = []

    def person_writes():
        if not writes:
            writes.append(store.update(card["id"], {"title": "Renamed"}))
    d, counts = _tiered_daemon(monkeypatch, probe, real_reconcile=True,
                               during=person_writes)
    d._board = store
    try:
        d._session_states["s1"] = _working()
        await d._push_agents_snapshot()
        assert writes and writes[0][0] is not None
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 2
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_overlapping_roster_triggers_run_one_load(monkeypatch):
    """The full tier and a statusline's lapsed window share one refresh:
    two reads in flight could apply in the wrong order."""
    import threading
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    release = threading.Event()

    def slow_load():
        counts["rosters"] += 1
        release.wait(2.0)
        return None, None
    d._load_rosters = slow_load
    d._session_states["s1"] = _working()
    push = asyncio.ensure_future(d._push_agents_snapshot())
    for _ in range(100):
        if counts["rosters"]:
            break
        await asyncio.sleep(0.01)
    assert counts["rosters"] == 1
    # A statusline message lands while the push's read is in flight.
    d._roster_refreshed_at -= daemon_mod.ROSTER_REFRESH_INTERVAL
    d._reconciled_categories()
    await asyncio.sleep(0.05)
    release.set()
    await push
    await _settle(d)
    assert counts["rosters"] == 1


@pytest.mark.asyncio
async def test_a_read_older_than_an_inline_refresh_is_not_applied(monkeypatch):
    """An inline refresh (the liveness sweep) that lands while a scheduled
    read is in flight is fresher; the older read must not be applied over
    it, or it could readmit a root the sweep just retired."""
    import threading
    d = BobDaemon()
    applied = []
    release = threading.Event()

    def slow_load():
        release.wait(2.0)
        return ["old grok"], ["old codex"]
    d._load_rosters = slow_load
    real_codex = d._refresh_codex_records

    def codex(records=None):
        applied.append(records)
        real_codex([] if records is None else [])
    monkeypatch.setattr(d, "_refresh_codex_records", codex)
    monkeypatch.setattr(d, "_refresh_grok_records",
                        lambda recs=None: applied.append(recs))
    task = d._schedule_roster_refresh()
    await asyncio.sleep(0.02)
    d._refresh_codex_records()           # the inline sweep, mid-read
    release.set()
    await task
    assert applied == [None]


@pytest.mark.asyncio
async def test_a_session_shared_by_two_cards_keeps_the_next_push_light(
        tmp_path, monkeypatch):
    """A refinement session bound to two cards is a standing conflict the
    pass re-sees every time; it must neither move the key nor rewrite rows."""
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe, real_reconcile=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        for title in ("One", "Two"):
            card, reason = store.create({"title": title, "root": str(tmp_path),
                                         "tool": "claude"})
            assert card, reason
            updated, reason = store.update(card["id"], {
                "refine_session_id": "shared", "refine_state": "live"})
            assert updated, reason
        d._session_states["s1"] = _working()
        await d._push_agents_snapshot()
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 1
        # And a pass forced by the floor rewrites nothing either.
        rows = store._conn.total_changes
        d._last_full_push_at -= d.FULL_PUSH_INTERVAL_SECONDS
        await d._push_agents_snapshot()
        assert counts["reconcile"] == 2
        assert store._conn.total_changes == rows
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_tool_call_alone_keeps_the_push_light(monkeypatch):
    """Measured on the live fleet, `current_tool` was nearly every key move:
    each tool call of each working session made the push a full one. Nothing
    in the full tier reads it, so N pushes in which only the tool moves run
    the full tier once per `FULL_PUSH_INTERVAL_SECONDS` — and the observers
    still get every tool on every push."""
    probe = _Probe()
    d, counts = _tiered_daemon(monkeypatch, probe)
    seen = []
    d._enrich_agent_stubs = lambda stubs: (
        seen.append([s.get("current_tool") for s in stubs]) or {"stubs": stubs})
    d._session_states["s1"] = _working()
    d._session_states["s2"] = _working()
    tools = ["Bash", "Read", "Edit", "Grep", "Write"]
    for n in range(20):
        d._session_states["s1"]["tool_name"] = tools[n % len(tools)]
        d._session_states["s2"]["tool_name"] = tools[(n + 2) % len(tools)]
        await d._push_agents_snapshot()
    assert probe.pushes == 20
    assert counts["reconcile"] == 1
    assert [row[0] for row in seen[-5:]] == ["Bash", "Read", "Edit", "Grep", "Write"]
    d._last_full_push_at -= d.FULL_PUSH_INTERVAL_SECONDS
    d._session_states["s1"]["tool_name"] = "Glob"
    await d._push_agents_snapshot()
    assert counts["reconcile"] == 2
