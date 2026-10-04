"""The daemon's own verbs and bookkeeping, driven directly: cards per
session, the one removal funnel, and the rest of `BobDaemon`'s surface."""

import asyncio
import json
import os
import time

import pytest
from unittest.mock import AsyncMock, call, patch
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import dispatch
from dark_army_daemon.daemon import BobDaemon


class FakeObserver:
    """Keeps the last card list and every length it was handed."""

    def __init__(self):
        self.notification_changes = []
        self.notifications = []

    def on_notification_change(self, notifications: list) -> None:
        self.notifications = notifications
        self.notification_changes.append(len(notifications))


async def _deliver(daemon, message):
    await daemon._handle_message(message)


def _fresh(state):
    return {"state": state, "last_event": time.time()}


def _card_for(sid):
    return {"event": "add", "session_id": sid, "project": "shop", "message": "ready"}


@pytest.mark.parametrize("sid", ["", "one", "a-session-id-of-some-length"])
@pytest.mark.asyncio
async def test_a_card_is_keyed_by_its_session_even_an_empty_one(sid):
    daemon = BobDaemon()
    await daemon._handle_message(_card_for(sid))
    assert list(daemon._active_notifications) == [sid]
    await daemon._handle_message({"event": "dismiss", "session_id": sid})
    assert daemon._active_notifications == {}


@pytest.mark.asyncio
async def test_dismissing_one_card_leaves_the_others():
    daemon = BobDaemon()
    for sid in ("left", "middle", "right"):
        await daemon._handle_message(_card_for(sid))
    await daemon._handle_message({"event": "dismiss", "session_id": "middle"})
    assert sorted(daemon._active_notifications) == ["left", "right"]


@pytest.mark.asyncio
async def test_forget_session_leaves_no_per_session_entries_behind():
    """Every dict keyed by session id has to be emptied by the one removal funnel,
    or a daemon running for weeks keeps a row per session it has ever seen."""
    daemon = BobDaemon()
    sid = "sess-abc"
    daemon._session_states[sid] = {"state": "working", "project": "p",
                                   "last_event": 0.0}
    daemon._active_notifications[sid] = {"event": "add"}
    daemon._ai_title_cache[sid] = (1.0, "a title")
    daemon._last_metric_sample[sid] = 123.0

    daemon._forget_session(sid)

    assert sid not in daemon._session_states
    assert sid not in daemon._active_notifications
    assert sid not in daemon._ai_title_cache
    assert sid not in daemon._last_metric_sample


@pytest.mark.asyncio
async def test_notification_snapshot_content_and_order():
    daemon = BobDaemon()
    await _deliver(daemon,
        {"event": "add", "hook": "Stop", "session_id": "s1abcdef00", "project": "proj1",
         "message": "Waiting for input"}
    )
    await _deliver(daemon,
        {"event": "add", "hook": "StopFailure", "session_id": "s2xyz", "project": "proj2",
         "message": "API error"}
    )
    # The waiting hysteresis withholds a just-arrived Stop card until its
    # session has been quiet for WAITING_HYSTERESIS_SECONDS; this test is about
    # content and order, not the window, so age both sessions past it.
    for s in daemon._session_states.values():
        s["last_event_monotonic"] -= 10.0
    snap = daemon._notification_snapshot()
    assert [e["session_id"] for e in snap] == ["s1abcdef00", "s2xyz"]  # arrival order
    # No transcript -> daemon sets session_title to session_id[:8], used as title.
    assert snap[0]["title"] == "s1abcdef"
    assert snap[0]["message"] == "Waiting for input"
    assert snap[1]["hook"] == "StopFailure"
    assert snap[1]["message"] == "API error"


def test_notification_snapshot_title_fallbacks():
    """title prefers session_title, then project, then a literal 'session'."""
    daemon = BobDaemon()
    daemon._active_notifications = {
        "a": {"session_title": "Fixing parser", "project": "proj", "message": "m", "hook": "Stop"},
        "b": {"project": "proj-b", "message": "m", "hook": "Stop"},          # no title
        "c": {"message": "m", "hook": "Stop"},                               # no title/project
    }
    snap = daemon._notification_snapshot()
    titles = {e["session_id"]: e["title"] for e in snap}
    assert titles == {"a": "Fixing parser", "b": "proj-b", "c": "session"}


# --- Debounced / coalesced notification chime -------------------------------

@pytest.mark.asyncio
async def test_new_card_chimes_once():
    """A genuinely new card plays exactly one chime, after the debounce."""
    daemon = BobDaemon()
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.02):
        await daemon._handle_message({"event": "add", "session_id": "s1", "message": "hi"})
        assert daemon._sound_task is not None
        await daemon._sound_task
    play.assert_called_once()


@pytest.mark.asyncio
async def test_re_added_card_does_not_rechime():
    """Stop then Notification for the same session is the same card visually —
    the second add must not re-arm the chime."""
    daemon = BobDaemon()
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.02):
        await _deliver(daemon,
            {"event": "add", "session_id": "s1", "hook": "Stop", "message": "Waiting"})
        task = daemon._sound_task
        # Re-add for the same session while the card is still active.
        await _deliver(daemon,
            {"event": "add", "session_id": "s1", "hook": "Notification", "message": "Waiting"})
        await task
    play.assert_called_once()


@pytest.mark.asyncio
async def test_transient_card_dismissed_before_debounce_is_silent():
    """A card dismissed before the debounce fires (the ~70ms UserPromptSubmit
    phantom) must never chime."""
    daemon = BobDaemon()
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.05):
        await daemon._handle_message({"event": "add", "session_id": "s1", "message": "hi"})
        task = daemon._sound_task
        await _deliver(daemon, {"event": "dismiss", "session_id": "s1"})
        await task
    play.assert_not_called()


@pytest.mark.asyncio
async def test_burst_of_new_cards_coalesces_to_one_chime():
    """Several concurrent sessions surfacing at once collapse to a single chime."""
    daemon = BobDaemon()
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.05):
        for sid in ("s1", "s2", "s3"):
            await daemon._handle_message({"event": "add", "session_id": sid, "message": "hi"})
        task = daemon._sound_task
        await task
    play.assert_called_once()


@pytest.mark.asyncio
async def test_muted_card_is_silent():
    """With the sound toggled off, a real card still surfaces but stays silent."""
    daemon = BobDaemon()
    daemon.notification_sound_enabled = False
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.02):
        await daemon._handle_message({"event": "add", "session_id": "s1", "message": "hi"})
        await daemon._sound_task
    play.assert_not_called()
    assert "s1" in daemon._active_notifications   # muting must not drop the card


@pytest.mark.asyncio
async def test_muting_while_a_chime_is_armed_silences_it():
    """The mute is read when the timer fires, so toggling it off mid-debounce
    cancels the pending chime rather than letting it slip through."""
    daemon = BobDaemon()
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.05):
        await daemon._handle_message({"event": "add", "session_id": "s1", "message": "hi"})
        task = daemon._sound_task
        daemon.notification_sound_enabled = False   # user hits the toggle
        await task
    play.assert_not_called()


@pytest.mark.asyncio
async def test_unmuting_restores_the_chime():
    """Toggling back on chimes again — the mute is not sticky."""
    daemon = BobDaemon()
    daemon.notification_sound_enabled = False
    with patch("dark_army_daemon.daemon._play_notification_sound") as play, \
         patch("dark_army_daemon.daemon.NOTIFICATION_SOUND_DEBOUNCE_SECONDS", 0.02):
        await daemon._handle_message({"event": "add", "session_id": "s1", "message": "hi"})
        await daemon._sound_task
        play.assert_not_called()

        daemon.notification_sound_enabled = True
        await daemon._handle_message({"event": "add", "session_id": "s2", "message": "hi"})
        await daemon._sound_task
    play.assert_called_once()


# ── the push floor ───────────────────────────────────────────────────────────
#
# Every agents push rebuilds the panel's whole view graph, and a click whose
# mouse-down and mouse-up straddle one is cancelled by SwiftUI — measured, three
# full payloads inside 34ms, and rows that could not be opened while it lasted.

def test_the_first_push_is_never_delayed():
    daemon = BobDaemon()
    assert daemon._agents_push_delay(1000.0) == 0.0


def test_a_push_treading_on_the_last_one_waits_out_the_floor():
    daemon = BobDaemon()
    daemon._last_agents_push = 1000.0
    delay = daemon._agents_push_delay(1000.0 + 0.05)
    assert delay == pytest.approx(daemon.AGENTS_PUSH_MIN_INTERVAL_SECONDS - 0.05)


def test_a_push_after_any_quiet_moment_goes_straight_out():
    daemon = BobDaemon()
    daemon._last_agents_push = 1000.0
    assert daemon._agents_push_delay(1000.0 + 5.0) == 0.0


class _AgentsProbe:
    """Counts pushes and keeps what each one carried, stamped off the loop
    clock so the tests can reason about spacing."""

    def __init__(self):
        self.pushes: list = []
        self.times: list = []

    def on_agents_change(self, snapshot) -> None:
        self.pushes.append(snapshot)
        self.times.append(asyncio.get_event_loop().time())


def _floored_daemon(probe, enrich_delay=0.0):
    """A daemon whose push pipeline runs for real, minus its I/O: enrichment
    normally parses transcripts and the title writer opens ttys — neither
    belongs in a timing test. Stubs pass through so a push's content can be
    checked."""
    daemon = BobDaemon(observer=probe)

    def enrich(stubs):
        if enrich_delay:
            time.sleep(enrich_delay)      # executor thread: blocking is the point
        return {"stubs": stubs}

    daemon._enrich_agent_stubs = enrich
    daemon._titles.apply = lambda snapshot: None
    return daemon


@pytest.mark.asyncio
async def test_a_burst_of_schedule_calls_collapses_onto_the_floor():
    """~0.5s of machine-gunned schedule calls lands at most three pushes, and
    no two of them closer together than the floor."""
    probe = _AgentsProbe()
    daemon = _floored_daemon(probe)
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < 0.5:
        daemon._schedule_agents_push()
        await asyncio.sleep(0.01)
    # Let the trailing push drain before counting.
    await asyncio.sleep(daemon.AGENTS_PUSH_MIN_INTERVAL_SECONDS + 0.1)

    floor = daemon.AGENTS_PUSH_MIN_INTERVAL_SECONDS
    in_window = [t for t in probe.times if t - start <= 0.5]
    assert len(in_window) <= 3
    gaps = [b - a for a, b in zip(probe.times, probe.times[1:])]
    # A hair of slop: the floor is slept before the stamp, and two clock reads
    # bracket it.
    assert all(gap >= floor - 0.05 for gap in gaps), gaps


@pytest.mark.asyncio
async def test_a_change_landing_mid_push_still_lands_in_a_trailing_push():
    probe = _AgentsProbe()
    daemon = _floored_daemon(probe, enrich_delay=0.1)
    daemon._session_states["s1"] = {"state": "working", "last_event": time.time()}
    daemon._schedule_agents_push()
    await asyncio.sleep(0.05)             # push 1 is inside its slow enrich
    daemon._session_states["s2"] = {"state": "working", "last_event": time.time()}
    daemon._schedule_agents_push()        # cannot be covered by push 1's stubs
    await asyncio.sleep(daemon.AGENTS_PUSH_MIN_INTERVAL_SECONDS + 0.5)

    assert len(probe.pushes) == 2
    first = {s["session_id"] for s in probe.pushes[0]["stubs"]}
    last = {s["session_id"] for s in probe.pushes[-1]["stubs"]}
    assert first == {"s1"}
    assert last == {"s1", "s2"}


@pytest.mark.asyncio
async def test_the_last_push_reflects_the_final_state():
    """Coalescing merges, it must never drop: whatever the burst did last is
    what the last push says."""
    probe = _AgentsProbe()
    daemon = _floored_daemon(probe)
    for n in range(6):
        daemon._session_states[f"s{n}"] = {
            **_fresh("working"),
        }
        daemon._schedule_agents_push()
        await asyncio.sleep(0.02)
    await asyncio.sleep(daemon.AGENTS_PUSH_MIN_INTERVAL_SECONDS + 0.2)

    last = {s["session_id"] for s in probe.pushes[-1]["stubs"]}
    assert last == {f"s{n}" for n in range(6)}


# --- the board outlives the fleet -------------------------------------------


def _board_daemon(tmp_path):
    from dark_army_daemon.board import BoardStore
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


@pytest.mark.asyncio
async def test_clear_done_uses_one_store_call_and_one_publication():
    daemon = BobDaemon()
    daemon._board = object()
    daemon._board_call = AsyncMock(side_effect=[
        [],  # Done cards, read so their attachment folders can go with them
        (True, 3, "deleted"),
    ])
    daemon._publish_board = AsyncMock()
    daemon._after_board_write = AsyncMock()
    daemon.delete_card = AsyncMock()
    daemon.update_card = AsyncMock()
    daemon.reorder_card = AsyncMock()
    daemon._wrap_up_for_done = AsyncMock()

    token = "a" * 64
    result = await daemon.clear_done_cards(3, token)

    assert result == (True, 3, "deleted")
    assert daemon._board_call.await_args_list == [
        call("cards", ["done"]),
        call("clear_done", 3, token),
    ]
    daemon._publish_board.assert_awaited_once_with()
    daemon._after_board_write.assert_not_awaited()
    daemon.delete_card.assert_not_awaited()
    daemon.update_card.assert_not_awaited()
    daemon.reorder_card.assert_not_awaited()
    daemon._wrap_up_for_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_done_stale_count_does_not_publish():
    daemon = BobDaemon()
    daemon._board = object()
    daemon._board_call = AsyncMock(side_effect=[
        [],
        (False, 0, "Done changed; nothing was cleared"),
    ])
    daemon._publish_board = AsyncMock()

    token = "b" * 64
    result = await daemon.clear_done_cards(2, token)

    assert result == (False, 0, "Done changed; nothing was cleared")
    assert daemon._board_call.await_args_list == [
        call("cards", ["done"]),
        call("clear_done", 2, token),
    ]
    daemon._publish_board.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_done_refuses_when_the_board_is_closed_without_store_work():
    daemon = BobDaemon()
    daemon._board = None
    daemon._board_call = AsyncMock()
    daemon._publish_board = AsyncMock()

    result = await daemon.clear_done_cards(4, "c" * 64)

    assert result == (False, 0, "the board is not open")
    daemon._board_call.assert_not_awaited()
    daemon._publish_board.assert_not_awaited()


def test_board_state_done_scope_is_store_wide_beyond_the_recent_preview(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, detail = store.create({
            "title": "old Done", "column_name": "done"})
        assert card is not None, detail
        old = time.time() - daemon.BOARD_DONE_WINDOW - 60
        store._conn.execute(
            "UPDATE cards SET done_at = ?, updated_at = ? WHERE id = ?",
            (old, old, card["id"]))
        store._conn.commit()
        expected_count, expected_token = store.done_scope()

        snapshot = daemon._build_board_state()

        assert [c for c in snapshot["cards"]
                if c["column_name"] == "done"] == []
        assert snapshot["counts"]["done"] == expected_count == 1
        assert snapshot["done_clear_token"] == expected_token
    finally:
        store.close()


def test_a_card_survives_its_session_being_forgotten(tmp_path):
    """`_forget_session` pops the session and the `_finished` tombstone expires
    half an hour later. The card is a row in another database and neither path
    touches it — that is the whole construction, and it is what makes "a card
    outlives its session" true rather than merely intended."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "backlog"})
        store.bind_session(card["id"], "s1")
        daemon._session_states["s1"] = {"state": "working",
                                        "last_event": time.time()}

        daemon._forget_session("s1", "ended")
        daemon._finished.clear()                    # and the tombstone expires

        got = store.get(card["id"])
        assert got is not None
        assert got["session_id"] == "s1"
        assert got["column_name"] == "in_progress"
    finally:
        store.close()


def test_a_missing_session_is_marked_ended_only_after_the_grace(tmp_path):
    """Above the hysteresis flicker and above a daemon restart's reload of
    sessions.json, so a session that blinks out of one snapshot does not put a
    card into mourning."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "backlog"})
        store.bind_session(card["id"], "s1")

        # First observation publishes the card's Needs you state; that is
        # snapshot news, independent of the persisted session-end grace.
        assert daemon._reconcile_board({"running": []}) is True
        assert store.get(card["id"])["link_state"] == "live"
        assert daemon._reconcile_board({"running": []}) is False
        assert store.get(card["id"])["link_state"] == "live"

        daemon._board_missing_since[card["id"]] -= daemon.BOARD_SESSION_GRACE + 1
        assert daemon._reconcile_board({"running": []}) is True
        got = store.get(card["id"])
        assert got["link_state"] == "ended"
        assert got["session_ended_at"] is not None
    finally:
        store.close()


def test_reconcile_never_moves_a_card_to_done(tmp_path):
    """A session ending says nothing about whether the work was finished, and
    Dark Army deciding otherwise is the one thing a board must not do."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "backlog"})
        store.bind_session(card["id"], "s1")
        daemon._board_missing_since[card["id"]] = 0.0
        daemon._reconcile_board({"running": []})
        assert store.get(card["id"])["column_name"] == "in_progress"
        assert store.counts()["done"] == 0
    finally:
        store.close()


def test_a_session_that_comes_back_clears_the_ended_mark(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "backlog"})
        store.bind_session(card["id"], "s1")
        daemon._board_missing_since[card["id"]] = 0.0
        daemon._reconcile_board({"running": []})
        assert store.get(card["id"])["link_state"] == "ended"

        daemon._reconcile_board({"running": [{"session_id": "s1"}]})
        got = store.get(card["id"])
        assert got["link_state"] == "live"
        assert got["session_ended_at"] is None
    finally:
        store.close()


def test_board_state_aliases_ready_to_backlog(tmp_path):
    """The snapshot copies Backlog onto `ready` so a schema-1 surface still
    sees a number. `ready` is not a column."""
    from dark_army_daemon.board import COLUMNS
    daemon, store = _board_daemon(tmp_path)
    try:
        for i in range(3):
            card, detail = store.create({
                "title": f"todo {i}", "project": "bob", "root": "/tmp",
                "tool": "claude", "column_name": "backlog"})
            assert card is not None, detail
        card, detail = store.create({
            "title": "working", "project": "bob", "root": "/tmp",
            "tool": "claude", "column_name": "in_progress"})
        assert card is not None, detail
        counts = daemon._build_board_state()["counts"]
        assert counts["backlog"] == 3
        assert counts["ready"] == 3
        assert counts["in_progress"] == 1
        assert "ready" not in COLUMNS
    finally:
        store.close()


def test_empty_board_state_still_publishes_the_ready_alias(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        counts = daemon._build_board_state()["counts"]
        assert counts["backlog"] == 0
        assert counts["ready"] == 0
    finally:
        store.close()


def test_no_session_category_is_ever_copied_into_the_board(tmp_path):
    """The board renders `Agent.category` off the agents snapshot and stores
    none of it. If a category ever landed in board.db the two surfaces could
    disagree, which is the failure the whole arrangement exists to prevent."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "backlog"})
        store.bind_session(card["id"], "s1")
        daemon._reconcile_board({"waiting": [{"session_id": "s1"}]})
        stored = json.dumps(store.get(card["id"]), default=str)
        for category in ("running", "waiting", "sleeping", "abandoned"):
            assert category not in stored, category
    finally:
        store.close()


# --- only pipeline mains bind or claim ----------------------------------------


def _dispatching_card(store, tool="claude", when=None):
    card, detail = store.create({"title": "the work", "project": "bob",
                                 "root": "/tmp", "tool": tool,
                                 "column_name": "backlog"})
    assert card is not None, detail
    store.update(card["id"], {"link_state": "dispatching",
                              "dispatched_at": when or time.time()})
    return store.get(card["id"])


def test_a_background_row_never_binds_a_card(tmp_path):
    """A background agent — including one the dispatched session itself
    spawned — is never the session a Start opened, and pairing with it also
    made the queue wait on work that was not Dark Army's."""
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, when=now)
        background = {"session_id": "bg-1", "provider": "claude",
                      "kind": "background", "project": "bob",
                      "cwd": "/tmp", "started_at": now + 1}
        assert daemon._reconcile_board({"running": [background]}) is False
        assert store.get(card["id"])["session_id"] == ""

        interactive = dict(background, session_id="real-1",
                           kind="interactive", started_at=now + 2)
        assert daemon._reconcile_board(
            {"running": [background, interactive]}) is True
        got = store.get(card["id"])
        assert got["session_id"] == "real-1"
        assert got["link_state"] == "live"
    finally:
        store.close()


def test_a_grok_child_row_never_binds_a_card(tmp_path):
    """A Grok child fires hooks under its own id and can transiently be a
    top-level interactive row; `_parent_of_child` is the discriminator that
    keeps it from being paired as if it were a session in its own right."""
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, tool="grok", when=now)
        daemon._session_states["parent-1"] = {
            "state": "working", "last_event": now,
            "subagents": {"child-1"}}
        child = {"session_id": "child-1", "provider": "grok",
                 "kind": "interactive", "project": "bob",
                 "cwd": "/tmp", "started_at": now + 1}
        assert daemon._reconcile_board({"running": [child]}) is False
        assert store.get(card["id"])["session_id"] == ""
    finally:
        store.close()


def test_bind_requires_descent_from_the_spawned_terminal(tmp_path):
    """With a receipt, elimination becomes proof: only a session whose pid
    descends from the terminal Dark Army opened may bind. A row with the wrong pid
    is skipped, not bound."""
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, when=now)
        daemon._spawn_shell_pids[card["id"]] = os.getpid()
        stranger = {"session_id": "stranger", "provider": "claude",
                    "kind": "interactive", "project": "bob",
                    "cwd": "/tmp", "started_at": now + 1, "pid": 1}
        assert daemon._reconcile_board({"running": [stranger]}) is False
        assert store.get(card["id"])["session_id"] == ""

        ours = dict(stranger, session_id="ours", pid=os.getpid(),
                    started_at=now + 2)
        assert daemon._reconcile_board({"running": [stranger, ours]}) is True
        assert store.get(card["id"])["session_id"] == "ours"
        assert card["id"] not in daemon._spawn_shell_pids
    finally:
        store.close()


class _VscodeCodexProcess:
    def __init__(self, pid, cwd, created):
        self.pid = pid
        self.info = {"pid": pid, "name": "codex", "exe": "/opt/codex",
                     "cwd": cwd, "create_time": created, "cmdline": ["codex"]}
        self.created = created

    def exe(self):
        return self.info["exe"]

    def cwd(self):
        return self.info["cwd"]

    def create_time(self):
        return self.created

    def cmdline(self):
        return list(self.info["cmdline"])


@pytest.mark.parametrize("receipt", ["shell", "pty"])
def test_vscode_attribution_binds_only_the_spawned_terminal(tmp_path, monkeypatch, receipt):
    from dark_army_daemon import codex_rollouts

    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, tool="codex", when=now)
        cwd = "/tmp/bob"
        record = codex_rollouts.CodexRecord(
            session_id="codex:launched", thread_id="launched",
            path=tmp_path / "launched.jsonl", cwd=cwd,
            originator="codex-tui", source_kind="vscode", thread_source="user",
            started_at=now + 0.799, last_event=now, activity="working",
        )
        process = _VscodeCodexProcess(60576, cwd, now)
        codex_rollouts.attach_process_ids([record], [process])
        assert record.pid == 60576
        daemon._codex_records[record.session_id] = record
        monkeypatch.setattr(daemon, "_refresh_codex_records", lambda: None)
        monkeypatch.setattr(daemon, "_refresh_grok_records", lambda: None)
        if receipt == "shell":
            daemon._spawn_shell_pids[card["id"]] = 4321
        else:
            daemon._spawn_pty_pids[card["id"]] = 4321
        checked = []

        def ancestry(pid, parent):
            checked.append((pid, parent))
            return (pid, parent) == (60576, 4321)

        monkeypatch.setattr(daemon_mod, "_pid_descends", ancestry)
        stub = next(row for row in daemon._collect_agent_stubs()
                    if row["session_id"] == record.session_id)
        assert stub["pid"] == 60576
        assert daemon._reconcile_board({"running": [stub]}) is True
        linked = store.get(card["id"])
        assert linked["session_id"] == record.session_id
        assert linked["link_state"] == "live"
        assert linked["dispatch_error"] == ""
        assert checked == [(60576, 4321)]
        assert card["id"] not in daemon._spawn_shell_pids
        assert card["id"] not in daemon._spawn_pty_pids
    finally:
        store.close()


@pytest.mark.parametrize("ambiguous,foreign", [(False, True), (True, False)])
def test_vscode_attribution_refuses_unproved_board_bind(
        tmp_path, monkeypatch, ambiguous, foreign):
    from dark_army_daemon import codex_rollouts

    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, tool="codex", when=now)
        cwd = "/tmp/bob"
        record = codex_rollouts.CodexRecord(
            session_id="codex:launched", thread_id="launched",
            path=tmp_path / "launched.jsonl", cwd=cwd,
            originator="codex-tui", source_kind="vscode", thread_source="user",
            started_at=now + 0.799, last_event=now, activity="working",
        )
        records = [record]
        if ambiguous:
            records.append(codex_rollouts.CodexRecord(
                session_id="codex:other", thread_id="other",
                path=tmp_path / "other.jsonl", cwd=cwd))
        codex_rollouts.attach_process_ids(
            records, [_VscodeCodexProcess(60576, cwd, now)])
        daemon._codex_records[record.session_id] = record
        monkeypatch.setattr(daemon, "_refresh_codex_records", lambda: None)
        monkeypatch.setattr(daemon, "_refresh_grok_records", lambda: None)
        daemon._spawn_shell_pids[card["id"]] = 4321
        checked = []

        def ancestry(pid, parent):
            checked.append((pid, parent))
            return not foreign and (pid, parent) == (60576, 4321)

        monkeypatch.setattr(daemon_mod, "_pid_descends", ancestry)
        stub = next(row for row in daemon._collect_agent_stubs()
                    if row["session_id"] == record.session_id)
        assert stub["pid"] == (None if ambiguous else 60576)
        assert daemon._reconcile_board({"running": [stub]}) is False
        assert store.get(card["id"])["session_id"] == ""
        assert checked == ([(60576, 4321)] if foreign else [])
        store.update(card["id"], {
            "dispatched_at": now - dispatch.DISPATCH_BIND_WINDOW - 1})
        assert daemon._reconcile_board({"running": [stub]}) is True
        expired = store.get(card["id"])
        assert expired["session_id"] == ""
        assert expired["link_state"] == ""
        assert "could not prove" in expired["dispatch_error"]
        assert card["id"] not in daemon._spawn_shell_pids
        # A late observation cannot revive the expired dispatch.
        assert daemon._reconcile_board({"running": [stub]}) is False
        assert store.get(card["id"])["session_id"] == ""
    finally:
        store.close()


def test_bind_without_a_receipt_falls_back(tmp_path):
    """No receipt — an older extension window, or Dark Army restarted mid-window —
    and the predicate alone decides, exactly as before. This is where the
    hand-started residual deliberately survives."""
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, when=now)
        row = {"session_id": "unproven", "provider": "claude",
               "kind": "interactive", "project": "bob",
               "cwd": "/tmp", "started_at": now + 1}
        assert daemon._reconcile_board({"running": [row]}) is True
        assert store.get(card["id"])["session_id"] == "unproven"
    finally:
        store.close()


def test_an_unproven_session_writes_its_own_give_up_reason(tmp_path):
    """A window that closes with only unprovable candidates is tellable from a
    dispatch that produced nothing: the orange line names the tightened match."""
    daemon, store = _board_daemon(tmp_path)
    try:
        stale = time.time() - dispatch.DISPATCH_BIND_WINDOW - 1
        card = _dispatching_card(store, when=stale)
        daemon._spawn_shell_pids[card["id"]] = os.getpid()
        stranger = {"session_id": "stranger", "provider": "claude",
                    "kind": "interactive", "project": "bob",
                    "cwd": "/tmp", "started_at": stale + 1, "pid": 1}
        assert daemon._reconcile_board({"running": [stranger]}) is True
        got = store.get(card["id"])
        assert got["session_id"] == ""
        assert got["link_state"] == ""
        assert got["column_name"] == "prep"      # no plan, so back to Prep
        assert "could not prove" in got["dispatch_error"]
        assert card["id"] not in daemon._spawn_shell_pids
    finally:
        store.close()


def test_a_done_dispatching_card_stays_done_when_its_session_binds(tmp_path):
    """A Grok card dragged to Done while still starting must not reappear
    in In progress as a copy when the assistant finally attaches."""
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card = _dispatching_card(store, when=now)
        store.update(card["id"], {"session_id": "old-sess"})
        closed, detail = store.declare_done(
            card["id"], "old-sess", "tests pass")
        assert closed is not None, detail
        assert closed["column_name"] == "done"
        assert closed["link_state"] == "dispatching"
        done_at = closed["done_at"]
        row = {"session_id": "real-1", "provider": "claude",
               "kind": "interactive", "project": "bob",
               "cwd": "/tmp", "started_at": now + 1}
        assert daemon._reconcile_board({"running": [row]}) is True
        got = store.get(card["id"])
        assert got["column_name"] == "done"
        assert got["link_state"] == "live"
        assert got["session_id"] == "real-1"
        assert got["closed_by"] == "old-sess"
        assert got["done_at"] == done_at
        assert got["close_note"] == "tests pass"
    finally:
        store.close()


def test_a_done_dispatching_card_stays_done_when_the_bind_times_out(tmp_path):
    """Give-up must not bounce a finished card to Prep/Backlog or write
    an orange line about a bind that no longer matters."""
    daemon, store = _board_daemon(tmp_path)
    try:
        stale = time.time() - dispatch.DISPATCH_BIND_WINDOW - 1
        card = _dispatching_card(store, when=stale)
        store.update(card["id"], {"column_name": "done"})
        assert daemon._reconcile_board({"running": []}) is True
        got = store.get(card["id"])
        assert got["column_name"] == "done"
        assert got["link_state"] == ""
        assert got["column_name"] != "prep"
        assert got["column_name"] != "backlog"
        assert got["dispatch_error"] == ""
        assert got["dispatched_at"] is None
    finally:
        store.close()


def test_claiming_ids_hold_only_pipeline_mains(tmp_path):
    """The waiting line only ever counts Dark Army's own pipeline work: no
    background agents, no codex children, no stray Grok child rows — while a
    hook-stream parent quietly supervising its own subagents keeps its claim,
    and Grok roots keep theirs."""
    from types import SimpleNamespace
    from dark_army_daemon.agents_poll import AgentRecord
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        daemon._session_states["parent-1"] = {
            "state": "working", "last_event": now,
            "subagents": {"child-1"}}
        daemon._session_states["child-1"] = {
            "state": "working", "last_event": now}
        daemon._grok_records["grok-root"] = SimpleNamespace()
        daemon._codex_records["codex-root"] = SimpleNamespace(
            parent_thread_id="")
        daemon._codex_records["codex-child"] = SimpleNamespace(
            parent_thread_id="codex-root")
        daemon._agent_records["bg-1"] = AgentRecord(
            "bg-1", kind="background", activity="running")

        ids = daemon._claiming_session_ids()

        assert "parent-1" in ids
        assert "grok-root" in ids
        assert "codex-root" in ids
        assert "child-1" not in ids
        assert "codex-child" not in ids
        assert "bg-1" not in ids
    finally:
        store.close()


def test_a_wrongly_bound_background_card_releases_its_claim(tmp_path):
    """The pre-fix board.db case: a card bound to a background-agent id loads
    fine and simply stops claiming — that is the fix, not a migration."""
    from dark_army_daemon.agents_poll import AgentRecord
    from dark_army_daemon import board_queue
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "backlog"})
        store.bind_session(card["id"], "bg-1")
        daemon._agent_records["bg-1"] = AgentRecord(
            "bg-1", kind="background", activity="running")

        assert not board_queue.holds(store.get(card["id"]),
                                     daemon._claiming_session_ids())
        drawn = daemon._decorate_card_for_snapshot(store.get(card["id"]), {})
        assert drawn["work_active"] is False
        # The band's own answer agrees: a background session is not in
        # `_claiming_session_ids`, so nothing is *working* this card either.
        assert drawn["run_active"] is False

        # And it spends no place in the project's pipeline either: the slot
        # gate counts `claims`, the same predicate, so the next card goes.
        other, _ = store.create({"title": "queued behind it",
                                 "project": "bob", "root": "/tmp",
                                 "tool": "claude", "column_name": "backlog"})
        assert daemon._slot_refusal(store.get(other["id"]),
                                    store.cards()) is False
    finally:
        store.close()


def test_refine_bind_applies_the_same_predicate(tmp_path):
    """A background claude row never becomes `refine_session_id` either — the
    refine bind runs the same candidate predicate as the dispatch bind."""
    daemon, store = _board_daemon(tmp_path)
    try:
        now = time.time()
        card, detail = store.create({"title": "the idea", "project": "bob",
                                     "root": "/tmp", "tool": "claude",
                                     "column_name": "prep"})
        assert card is not None, detail
        store.update(card["id"], {"refine_state": "dispatching",
                                  "dispatched_at": now})
        daemon._refine_baseline[card["id"]] = set()
        background = {"session_id": "bg-1", "provider": "claude",
                      "kind": "background", "project": "bob",
                      "cwd": "/tmp", "started_at": now + 1}
        assert daemon._reconcile_board({"running": [background]}) is False
        assert store.get(card["id"])["refine_session_id"] == ""
    finally:
        store.close()


# --- the stage track ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["arpg-web", "dark-army", "finance-demo"])
async def test_create_card_resolves_standard_workflow_for_each_project(
        tmp_path, project):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / project
    root.mkdir()
    daemon._known_project_roots = lambda: {str(root.resolve())}
    try:
        card, detail = await daemon.create_card({
            "title": f"ship in {project}",
            "project": project,
            "root": str(root),
            "prompt": "Implement the accepted plan at plans/work.md",
            "tool": "claude",
        })
        assert card is not None, detail
        assert card["workflow"] == \
            "bc-implementer\nbc-verifier\nbc-bug-auditor"
        assert card["agent_trail"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_create_card_keeps_an_explicit_workflow_authoritative(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "dark-army"
    root.mkdir()
    daemon._known_project_roots = lambda: {str(root.resolve())}
    try:
        card, detail = await daemon.create_card({
            "title": "explicit",
            "root": str(root),
            "prompt": "/ship implement plans/work.md",
            "workflow": ["only-this", "then-that"],
        })
        assert card is not None, detail
        assert card["workflow"] == "only-this\nthen-that"
    finally:
        store.close()


def test_startup_backfill_repairs_all_projects_before_the_first_snapshot(tmp_path):
    from dark_army_daemon.board import parse_stages

    daemon, store = _board_daemon(tmp_path)
    roots = []
    made = {}
    try:
        for index, project in enumerate(
                ("arpg-web", "dark-army", "finance-demo")):
            root = tmp_path / project
            root.mkdir()
            roots.append(str(root.resolve()))
            plan = root / "plan.md"
            plan.write_text(
                "# Work\n\n- **Stages:** bc-planner | bc-implementer "
                "| bc-verifier | bc-bug-auditor\n\n## Work\n",
                encoding="utf-8",
            )
            card, detail = store.create({
                "title": project,
                "project": project,
                "root": str(root),
                "prompt": f"Plan: {plan}",
                "tool": "claude",
                "column_name": ("backlog", "in_progress", "done")[index],
            })
            assert card is not None, detail
            made[project] = card["id"]

        generic, detail = store.create({
            "title": "S0 Grok playtest",
            "project": "finance-demo",
            "root": roots[-1],
            "prompt": "Try Grok; mention /ship only as a contingency later.",
            "tool": "grok",
            "column_name": "in_progress",
        })
        assert generic is not None, detail
        daemon._known_project_roots = lambda: set(roots)

        assert daemon._backfill_board_workflows() == 3
        first_snapshot = daemon._refresh_board_state()
        by_id = {card["id"]: card for card in first_snapshot["cards"]}
        for project, card_id in made.items():
            assert parse_stages(by_id[card_id]["workflow"]) == [
                "bc-implementer", "bc-verifier", "bc-bug-auditor",
            ], project
            assert by_id[card_id]["agent_trail"] == ""
        assert by_id[generic["id"]]["workflow"] == ""
        assert by_id[generic["id"]]["agent_trail"] == ""

        # Repeating startup is idempotent, and actual observation remains the
        # only way the generic card gains history.
        assert daemon._backfill_board_workflows() == 0
        store.record_agents(generic["id"], ["grok-specialist"])
        observed = daemon._refresh_board_state()
        generic_now = next(c for c in observed["cards"] if c["id"] == generic["id"])
        assert generic_now["workflow"] == ""
        assert generic_now["agent_trail"] == "grok-specialist"
    finally:
        store.close()


def test_subagents_seen_accumulates_and_never_shrinks(tmp_path):
    """The append-only companion to the live `subagents` set, and the reason it
    exists: a stage that starts and finishes between two agents snapshots is
    never in the live set when the reconcile looks, so a track built from that
    set would have holes in it with no way to tell a missed stage from one that
    never ran."""
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._update_session_state("session_start", "SessionStart", "s1")
    daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                 agent_id="bc-planner")
    daemon._update_session_state("subagent_stop", "SubagentStop", "s1",
                                 agent_id="bc-planner")
    daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                 agent_id="bc-implementer")
    state = daemon._session_states["s1"]
    assert state["subagents_seen"] == ["bc-planner", "bc-implementer"]
    # The live set is a different fact and has moved on.
    assert "bc-planner" not in state.get("subagents", set())


def test_subagents_seen_prefers_the_type_and_dedupes(tmp_path):
    """A pip says "implementer". `agent_id` is already the type for Claude Code
    while Grok and Codex put it in `subagent_type`."""
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._update_session_state("session_start", "SessionStart", "s1")
    for _ in range(3):
        daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                     agent_id="abc123", subagent_type="Explore")
    assert daemon._session_states["s1"]["subagents_seen"] == ["Explore"]


def test_subagents_seen_is_bounded(tmp_path):
    from dark_army_daemon.daemon import MAX_SEEN_SUBAGENTS
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._update_session_state("session_start", "SessionStart", "s1")
    for i in range(MAX_SEEN_SUBAGENTS + 20):
        daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                     agent_id=f"stage-{i}")
    assert len(daemon._session_states["s1"]["subagents_seen"]) == \
        MAX_SEEN_SUBAGENTS


def test_a_session_id_is_never_drawn_as_a_stage():
    """`_parent_of_child` states the discriminator: Claude Code's agent ids are
    types, which no session id can equal, while a Grok child is addressed by its
    own session id and folded onto the parent as a subagent. Left alone that
    child's uuid becomes a marker reading `a3f1c2d4…`, which says nothing."""
    ok = BobDaemon._stage_name_ok
    assert ok("bc-implementer") is True
    assert ok("Codex agent") is True
    assert ok("code-reviewer") is True
    assert ok("") is False
    assert ok("3f2a1b4c5d6e7f8091a2b3c4d5e6f708") is False
    assert ok("3f2a1b4c-5d6e-7f80-91a2-b3c4d5e6f708") is False
    # The 17-character ids the harness actually mints. These are *shorter*
    # than a uuid, so the original 32-char floor passed every one of them and
    # the live board drew five cards' trails as nothing but hex.
    assert ok("a5ef36af63236e492") is False
    assert ok("aae779cea484b68fb") is False
    # ...without taking a short name that merely happens to spell in hex.
    assert ok("added") is True
    assert ok("facade") is True
    assert ok("deadbeef") is True


def test_the_reconcile_writes_a_live_cards_stages(tmp_path):
    # The cost-and-time line's own drift term is a sibling with its own
    # tests (`test_run_figures.py`); hold it still so this asserts on the
    # stage write alone.
    daemon, store = _board_daemon(tmp_path)
    daemon._run_figures_drifted = lambda: False
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "in_progress"})
        store.bind_session(card["id"], "s1")
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagents_seen": ["bc-planner", "3f2a1b4c5d6e7f8091a2b3c4d5e6f708"],
        }
        snapshot = {"running": [{"session_id": "s1"}], "waiting": [],
                    "sleeping": [], "finished": [], "abandoned": []}
        assert daemon._reconcile_board(snapshot) is True
        from dark_army_daemon.board import parse_stages
        assert parse_stages(store.get(card["id"])["agent_trail"]) == ["bc-planner"]
        # Nothing new to say, so the board earns no frame.
        assert daemon._reconcile_board(snapshot) is False
    finally:
        store.close()


# ── the crew: who took each observed stage ──────────────────────────────────


def _crew_of(store, card_id):
    from dark_army_daemon.board import parse_crew

    return parse_crew(store.get(card_id)["crew_trail"])


def test_a_stage_gets_a_character_from_its_own_pool(tmp_path):
    from dark_army_daemon import crew

    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "in_progress"})
        store.bind_session(card["id"], "s1")
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagents_seen": ["bc-implementer"],
        }
        snapshot = {"running": [{"session_id": "s1"}], "waiting": [],
                    "sleeping": [], "finished": [], "abandoned": []}
        assert daemon._reconcile_board(snapshot) is True
        faces = _crew_of(store, card["id"])
        assert faces == {}
        assert store.get(card["id"])["agent_trail"] == "bc-implementer"
    finally:
        store.close()


def test_two_cards_running_one_stage_draw_two_people(tmp_path):
    """`_crew_busy` is computed once per pass and each allocation joins it, so
    two stages recorded in the same reconcile cannot draw the same character."""
    daemon, store = _board_daemon(tmp_path)
    try:
        cards = []
        rows = []
        for i in (1, 2):
            card, _ = store.create({"title": f"work {i}", "project": "bob",
                                    "root": "/tmp", "tool": "claude",
                                    "column_name": "in_progress"})
            store.bind_session(card["id"], f"s{i}")
            daemon._session_states[f"s{i}"] = {
                **_fresh("working"),
                "subagents_seen": ["bc-verifier"],
            }
            cards.append(card)
            rows.append({"session_id": f"s{i}",
                         "subagent_rows": [{"subagent_type": "bc-verifier"}]})
        snapshot = {"running": rows, "waiting": [], "sleeping": [],
                    "finished": [], "abandoned": []}
        # Two passes: the first records both, the second sees the first card's
        # character as busy — which is the reading `_crew_busy` exists for.
        daemon._reconcile_board(snapshot)
        assert all(_crew_of(store, c["id"]) == {} for c in cards)
        assert all(store.get(c["id"])["agent_trail"] == "bc-verifier" for c in cards)
    finally:
        store.close()


def test_a_full_pool_repeats_rather_than_inventing_somebody(tmp_path):
    """One more card than the pool can hold repeats a real member."""
    from dark_army_daemon import crew

    pool = {"hex", "franio", "forge", "ptys"}
    daemon, store = _board_daemon(tmp_path)
    try:
        rows = []
        cards = []
        for i in range(len(pool) + 1):
            card, _ = store.create({"title": f"work {i}", "project": "bob",
                                    "root": "/tmp", "tool": "claude",
                                    "column_name": "in_progress"})
            store.bind_session(card["id"], f"s{i}")
            daemon._session_states[f"s{i}"] = {
                **_fresh("working"),
                "subagents_seen": ["bc-bug-auditor"],
            }
            cards.append(card)
            rows.append({"session_id": f"s{i}",
                         "subagent_rows": [{"subagent_type": "bc-bug-auditor"}]})
        snapshot = {"running": rows, "waiting": [], "sleeping": [],
                    "finished": [], "abandoned": []}
        for _ in range(4):
            daemon._reconcile_board(snapshot)
        assert all(_crew_of(store, c["id"]) == {} for c in cards)
        assert all(store.get(c["id"])["agent_trail"] == "bc-bug-auditor" for c in cards)
    finally:
        store.close()


def test_an_already_recorded_stage_is_never_reallocated(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "in_progress"})
        store.bind_session(card["id"], "s1")
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagents_seen": ["bc-verifier"],
        }
        snapshot = {"running": [{"session_id": "s1"}], "waiting": [],
                    "sleeping": [], "finished": [], "abandoned": []}
        store.record_agents(card["id"], ["bc-verifier"], {"bc-verifier": "zosia"})
        daemon._reconcile_board(snapshot)
        first = _crew_of(store, card["id"])["bc-verifier"]
        for _ in range(3):
            daemon._reconcile_board(snapshot)
        assert _crew_of(store, card["id"])["bc-verifier"] == first
    finally:
        store.close()


def test_a_refining_card_records_its_planner(tmp_path):
    """The "who planned it" half. A refinement's stages were recorded nowhere
    before this call site existed."""
    from dark_army_daemon import crew

    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the idea", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "prep"})
        store.update(card["id"], {"refine_session_id": "r1",
                                  "refine_state": "live"})
        daemon._session_states["r1"] = {
            **_fresh("working"),
            "subagents_seen": ["bc-planner"],
        }
        snapshot = {"running": [{"session_id": "r1"}], "waiting": [],
                    "sleeping": [], "finished": [], "abandoned": []}
        assert daemon._reconcile_board(snapshot) is True
        from dark_army_daemon.board import parse_stages
        assert parse_stages(store.get(card["id"])["agent_trail"]) == ["bc-planner"]
        assert _crew_of(store, card["id"]) == {}
        # The card stays in Prep and keeps its refinement link: recording a
        # stage is an observation, never a move.
        assert store.get(card["id"])["column_name"] == "prep"
        assert store.get(card["id"])["session_id"] == ""
    finally:
        store.close()


def test_the_snapshot_omits_crew_entirely_on_a_card_with_none(tmp_path):
    """`work_record`'s rule: a card nobody has worked and a card whose crew is
    empty must not become the same fact on the wire."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "fresh", "project": "bob",
                                "root": "/tmp", "column_name": "backlog"})
        drawn = daemon._decorate_card_for_snapshot(store.get(card["id"]), {})
        assert "crew" not in drawn

        store.record_agents(card["id"], ["bc-planner"],
                            {"bc-planner": "overwatch"})
        drawn = daemon._decorate_card_for_snapshot(store.get(card["id"]), {})
        assert drawn["crew"] == {"bc-planner": "overwatch"}
    finally:
        store.close()


def test_an_old_card_shows_the_renamed_crew_and_keeps_its_record(tmp_path):
    """A card that recorded a character the 22 Sep 2026 cast rebrand retired
    publishes that index's new callsign, while `crew_trail` itself is never
    rewritten. An unknown value passes through."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "old", "project": "bob",
                                "root": "/tmp", "column_name": "backlog"})
        store.record_agents(
            card["id"], ["bc-planner", "bc-implementer", "bc-verifier"],
            {"bc-planner": "mrrobot", "bc-implementer": "elliot",
             "bc-verifier": "somebody-else"})
        drawn = daemon._decorate_card_for_snapshot(store.get(card["id"]), {})
        assert drawn["crew"] == {"bc-planner": "overwatch",
                                 "bc-implementer": "cipher",
                                 "bc-verifier": "somebody-else"}
        trail = store.get(card["id"])["crew_trail"]
        assert "bc-planner\tmrrobot" in trail
        assert "bc-implementer\telliot" in trail
    finally:
        store.close()



def test_a_card_prompt_is_a_preview_in_the_snapshot(tmp_path):
    """Every card's *full* prompt used to ride `/api/state`, and therefore every
    SSE frame — 20 cards at the store's limit is 165 KB against a limiter tuned
    around a ~19 KB snapshot. `GET /api/board` still serves the whole thing."""
    daemon, store = _board_daemon(tmp_path)
    try:
        long_prompt = "x" * 5000
        card, _ = store.create({"title": "big", "project": "bob", "root": "/tmp",
                                "prompt": long_prompt, "column_name": "backlog"})
        short, _ = store.create({"title": "small", "project": "bob",
                                 "root": "/tmp", "prompt": "brief",
                                 "column_name": "backlog"})
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert len(by_id[card["id"]]["prompt"]) == \
            BobDaemon.BOARD_SNAPSHOT_PROMPT_CHARS
        assert by_id[card["id"]]["prompt_truncated"] is True
        assert by_id[short["id"]]["prompt"] == "brief"
        assert "prompt_truncated" not in by_id[short["id"]]
        # The store still holds all of it — the truncation is a view, and a copy.
        assert store.get(card["id"])["prompt"] == long_prompt
    finally:
        store.close()


def test_a_card_summary_is_a_preview_in_the_snapshot(tmp_path):
    """A paragraph of description used to ride `/api/state` whole. The live
    board draws three lines; the editor fetches the rest from `GET /api/board`
    before Save is allowed. An untruncated card carries no flag key."""
    daemon, store = _board_daemon(tmp_path)
    try:
        long_summary = "s" * 800
        card, _ = store.create({"title": "big", "project": "bob", "root": "/tmp",
                                "summary": long_summary, "column_name": "backlog"})
        short, _ = store.create({"title": "small", "project": "bob",
                                 "root": "/tmp", "summary": "brief",
                                 "column_name": "backlog"})
        bound = "e" * BobDaemon.BOARD_SNAPSHOT_SUMMARY_CHARS
        exact, _ = store.create({"title": "exact", "project": "bob",
                                 "root": "/tmp", "summary": bound,
                                 "column_name": "backlog"})
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert len(by_id[card["id"]]["summary"]) == \
            BobDaemon.BOARD_SNAPSHOT_SUMMARY_CHARS
        assert by_id[card["id"]]["summary_truncated"] is True
        assert "prompt_truncated" not in by_id[card["id"]]
        assert by_id[short["id"]]["summary"] == "brief"
        assert "summary_truncated" not in by_id[short["id"]]
        assert by_id[exact["id"]]["summary"] == bound
        assert "summary_truncated" not in by_id[exact["id"]]
        assert store.get(card["id"])["summary"] == long_summary
    finally:
        store.close()


def test_a_card_says_who_wrote_it_in_the_snapshot(tmp_path):
    """The author sentinel and session id survive the snapshot view, including
    when that view trims a long prompt for SSE."""
    daemon, store = _board_daemon(tmp_path)
    try:
        human, _ = store.create({
            "title": "from me", "project": "bob", "root": "/tmp",
            "column_name": "backlog",
        })
        agent, _ = store.create({
            "title": "from an agent", "project": "bob", "root": "/tmp",
            "author": "s1", "column_name": "backlog",
        })
        long, _ = store.create({
            "title": "long from an agent", "project": "bob", "root": "/tmp",
            "prompt": "x" * 5000, "author": "s1",
            "column_name": "backlog",
        })

        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[human["id"]]["author"] == "user"
        assert by_id[agent["id"]]["author"] == "s1"
        assert by_id[long["id"]]["author"] == "s1"
        assert by_id[long["id"]]["prompt_truncated"] is True
    finally:
        store.close()


def test_snapshot_decorates_author_name_from_peek(tmp_path, monkeypatch):
    from dark_army_daemon.identity import NAMES, proposed_index

    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json",
                       identities_path=tmp_path / "identities.json")
    from dark_army_daemon.board import BoardStore
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        sid = "sess-live"
        daemon._identities._sessions[sid] = "Vex"
        named, _ = store.create({
            "title": "from gil", "project": "bob", "root": "/tmp",
            "author": sid, "column_name": "backlog"})
        human, _ = store.create({
            "title": "from me", "project": "bob", "root": "/tmp",
            "author": "user", "column_name": "backlog"})
        missing_sid = "sess-gone"
        missing, _ = store.create({
            "title": "from a ghost", "project": "bob", "root": "/tmp",
            "author": missing_sid, "column_name": "backlog"})
        def boom(*a, **k):
            raise AssertionError("name_for must not run on a board read")
        monkeypatch.setattr(daemon._identities, "name_for", boom)
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[named["id"]]["author_name"] == "Vex"
        assert by_id[human["id"]]["author_name"] == ""
        assert by_id[missing["id"]]["author_name"] == NAMES[proposed_index(missing_sid)]
    finally:
        store.close()


def test_snapshot_carries_the_dependencies_both_ways(tmp_path):
    """Cards wait on cards again (`docs/card-dependencies.md`): the waiter
    publishes `dependencies` and its line, the card it waits on publishes
    `dependents` and its line, and a card with no links carries none of the
    four keys — `work_record`'s absent-where-empty rule."""
    daemon, store = _board_daemon(tmp_path)
    try:
        blocker, _ = store.create({
            "title": "the blocker", "project": "bob", "root": "/tmp",
            "column_name": "backlog"})
        waiter, _ = store.create({
            "title": "the waiter", "project": "bob", "root": "/tmp",
            "column_name": "backlog"})
        loner, _ = store.create({
            "title": "the loner", "project": "bob", "root": "/tmp",
            "column_name": "backlog"})
        store.update(waiter["id"], {"blocked_by": blocker["id"]})
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        got = by_id[waiter["id"]]
        assert got["dependencies"] == [{
            "id": blocker["id"], "title": "the blocker",
            "column_name": "backlog", "met": False}]
        assert got["dependency_line"] == 'Waits on: "the blocker" (not yet)'
        assert "dependents" not in got and "dependents_line" not in got
        assert by_id[blocker["id"]]["dependents"] == [
            {"id": waiter["id"], "title": "the waiter"}]
        assert by_id[blocker["id"]]["dependents_line"] == 'Unblocks: "the waiter"'
        assert "dependencies" not in by_id[blocker["id"]]
        for key in ("dependencies", "dependents", "dependency_line",
                    "dependents_line"):
            assert key not in by_id[loner["id"]], key
        # Not queued: no dependency sentence rides `queue_reason`.
        assert "queue_reason" not in got
        # Done flips the word and the bit on the next frame.
        store.update(blocker["id"], {"column_name": "done"})
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[waiter["id"]]["dependencies"][0]["met"] is True
        assert by_id[waiter["id"]]["dependency_line"] == (
            'Waits on: "the blocker" (done)')
    finally:
        store.close()


def test_a_dependency_outside_the_frame_is_resolved_not_read_as_missing(tmp_path):
    """A dependency finished long ago is outside the Done preview; one
    `cards_by_id` read resolves it, so it reads as done by rule rather than
    as missing by accident — and a deleted one is simply not drawn."""
    daemon, store = _board_daemon(tmp_path)
    try:
        old, _ = store.create({"title": "long done", "project": "bob",
                               "root": "/tmp", "column_name": "backlog"})
        gone, _ = store.create({"title": "deleted", "project": "bob",
                                "root": "/tmp", "column_name": "backlog"})
        waiter, _ = store.create({"title": "waiter", "project": "bob",
                                  "root": "/tmp", "column_name": "backlog"})
        store.update(waiter["id"], {"blocked_by": [old["id"], gone["id"]]})
        store.update(old["id"], {"column_name": "done"})
        # Finished long ago and closed by a person: outside the 24 h preview
        # and outside the awaiting-review read, so not in the frame at all.
        store._conn.execute("UPDATE cards SET done_at = 1, updated_at = 1"
                            " WHERE id = ?", (old["id"],))
        store._conn.commit()
        store.delete(gone["id"])
        state = daemon._build_board_state()
        assert old["id"] not in {c["id"] for c in state["cards"]}
        got = {c["id"]: c for c in state["cards"]}[waiter["id"]]
        assert got["dependencies"] == [{
            "id": old["id"], "title": "long done", "column_name": "done",
            "met": True}]
        assert got["dependency_line"] == 'Waits on: "long done" (done)'
    finally:
        store.close()


def test_a_poisoned_card_cannot_take_the_whole_board_down(tmp_path):
    """One card's text must not cost every project its board.

    `_backfill_board_workflows` runs inside `run`'s board `try`, which answers
    a raise by setting `self._board = None` for the rest of the process — the
    board gone for the whole run, and the offending card undeletable because
    the only surface that could delete it is the board.
    """
    from dark_army_daemon import board_workflow
    from dark_army_daemon.board import parse_stages

    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = root / "plan.md"
    plan.write_text(
        "# Work\n\n- **Stages:** bc-implementer | bc-verifier\n\n## Work\n",
        encoding="utf-8",
    )
    original = board_workflow.resolve

    def exploding(card, known_roots=(), roster=()):
        if card.get("title") == "poison":
            raise RuntimeError("Could not determine home directory")
        return original(card, known_roots, roster)

    try:
        poison, detail = store.create({
            "title": "poison",
            "project": "dark-army",
            "root": str(root),
            "prompt": "Plan: ~nobody42/plans/x.md\n\n/ship implement x",
            "tool": "claude",
        })
        assert poison is not None, detail
        good, detail = store.create({
            "title": "good",
            "project": "dark-army",
            "root": str(root),
            "prompt": f"Plan: {plan}",
            "tool": "claude",
        })
        assert good is not None, detail
        daemon._known_project_roots = lambda: {str(root.resolve())}

        with patch.object(board_workflow, "resolve", exploding):
            assert daemon._backfill_board_workflows() == 1

        by_id = {c["id"]: c for c in daemon._refresh_board_state()["cards"]}
        assert parse_stages(by_id[good["id"]]["workflow"]) == [
            "bc-implementer", "bc-verifier"]
        assert by_id[poison["id"]]["workflow"] == ""
    finally:
        store.close()


# --- the folders (initiatives) are gone from the daemon -----------------------


def test_the_board_snapshot_carries_no_folder_list(tmp_path):
    """Retired whole at schema 22: no `initiatives` key on a live board, none
    on a shut one, and no filer left to queue a card for."""
    daemon, store = _board_daemon(tmp_path)
    try:
        store.create({"title": "the work", "project": "bob",
                      "column_name": "backlog"})
        state = daemon._build_board_state()
        assert "initiatives" not in state
        assert all("initiative_id" not in c for c in state["cards"])
    finally:
        store.close()
    shut = BobDaemon(sessions_path=tmp_path / "sessions.json")
    shut._board = None
    assert "initiatives" not in shut._build_board_state()
    for name in ("_consider_filing", "_flush_card_filings", "_file_card",
                 "file_card_into", "create_initiative", "rename_initiative",
                 "delete_initiative", "_filing_shop", "_filing_queue"):
        assert not hasattr(shut, name), name


# --- the review acknowledgement ----------------------------------------------


@pytest.mark.asyncio
async def test_review_card_publishes_a_board_frame():
    daemon = BobDaemon()
    daemon._board = object()
    daemon._board_call = AsyncMock(return_value=({"id": "c1"}, "reviewed"))
    daemon._publish_board = AsyncMock()
    daemon._after_board_write = AsyncMock()

    card, detail = await daemon.review_card("c1")

    assert card == {"id": "c1"} and detail == "reviewed"
    daemon._board_call.assert_awaited_once_with("mark_reviewed", "c1")
    daemon._publish_board.assert_awaited_once_with()
    # `review_card` moves no column, so the wrap-up leg must never fire.
    daemon._after_board_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_review_does_not_publish():
    daemon = BobDaemon()
    daemon._board = object()
    daemon._board_call = AsyncMock(return_value=(None, "already reviewed"))
    daemon._publish_board = AsyncMock()

    card, detail = await daemon.review_card("c1")

    assert card is None and "already reviewed" in detail
    daemon._publish_board.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_card_refuses_when_the_board_is_closed():
    daemon = BobDaemon()
    daemon._board = None
    daemon._board_call = AsyncMock()

    card, detail = await daemon.review_card("c1")

    assert card is None and "not open" in detail
    daemon._board_call.assert_not_awaited()


def test_a_pending_close_survives_the_done_window(tmp_path):
    """`done_since` is windowed at 24 h, and a close nobody has acknowledged
    must never age out of the live board — a card closed on Friday still wears
    its banner on Monday. A *reviewed* close the same age falls out normally,
    and a fresh close (legitimately in both reads) rides the frame once."""
    daemon, store = _board_daemon(tmp_path)
    try:
        old = time.time() - daemon.BOARD_DONE_WINDOW - 3600

        pending, _ = store.create({"title": "pending", "project": "bob",
                                   "column_name": "in_progress"})
        store.bind_session(pending["id"], "s1")
        store.declare_done(pending["id"], "s1", "done")
        acknowledged, _ = store.create({"title": "acknowledged",
                                        "project": "bob",
                                        "column_name": "in_progress"})
        store.bind_session(acknowledged["id"], "s2")
        store.declare_done(acknowledged["id"], "s2", "done")
        store.mark_reviewed(acknowledged["id"])
        for cid in (pending["id"], acknowledged["id"]):
            store._conn.execute(
                "UPDATE cards SET done_at = ?, updated_at = ? WHERE id = ?",
                (old, old, cid))
        store._conn.commit()
        fresh, _ = store.create({"title": "fresh", "project": "bob",
                                 "column_name": "in_progress"})
        store.bind_session(fresh["id"], "s3")
        store.declare_done(fresh["id"], "s3", "done")

        ids = [c["id"] for c in daemon._build_board_state()["cards"]]

        assert pending["id"] in ids
        assert acknowledged["id"] not in ids
        assert fresh["id"] in ids
        # Deduped: no id rides the frame twice.
        assert len(ids) == len(set(ids))
    finally:
        store.close()


# --- the parallel dial: how many agents may work at once in one project -----
#
# The daemon-level half of the change of 24 Aug 2026. The pure policy
# (`board_queue.slot_head`) and the queue's own daemon cases live in
# `test_board_queue.py`; what is pinned here is the dial itself — the clamp,
# the two numbers on the snapshot, and the sentence a waiting card wears.


def _parallel_project(tmp_path, daemon, monkeypatch, opened):
    """A real project root with `dispatch.spawn` stubbed to record names."""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(daemon, "_known_project_roots",
                        lambda: {str(root.resolve())})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def accept(root_, argv, name, **_kw):
        opened.append(name)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    return root


def _parallel_card(store, root, title, **kw):
    fields = {"title": title, "project": "proj", "root": str(root),
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _parallel_hear(daemon, *sids):
    for sid in sids:
        daemon._session_states[sid] = {"state": "working",
                                       "last_event": time.time()}


@pytest.mark.asyncio
async def test_one_parallel_place_queues_the_second_card(tmp_path, monkeypatch):
    """The default dial, and the one place it is **stricter** than the file
    gate it replaced: two cards whose plans declare nothing in common used to
    run together, because the old gate asked about files. It asks about
    places now, so the second waits."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        running = _parallel_card(store, root, "running")
        store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                     "column_name": "in_progress"})
        _parallel_hear(daemon, "s1")
        second = _parallel_card(store, root, "second")

        ok, detail = await daemon.dispatch_card(second["id"],
                                                allow_unplanned=True)

        assert not ok
        assert opened == []
        assert detail == ("Queued — Dark Army will start it when the agent working "
                          "in this project finishes")
        assert store.get(second["id"])["queue_state"] == "queued"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_two_parallel_places_start_the_second_and_queue_the_third(
        tmp_path, monkeypatch):
    """The dial's whole promise: at two, the second card in a busy project
    opens a terminal and the third waits — and the third counts the agents
    rather than naming one, because neither of them is what it waits on."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        daemon.set_board_parallel(2)
        first = _parallel_card(store, root, "first")
        store.update(first["id"], {"link_state": "live", "session_id": "s1",
                                   "column_name": "in_progress"})
        _parallel_hear(daemon, "s1")

        second = _parallel_card(store, root, "second")
        ok, detail = await daemon.dispatch_card(second["id"],
                                                allow_unplanned=True)
        assert ok, detail
        assert opened == ["second"]
        store.update(second["id"], {"link_state": "live", "session_id": "s2"})
        _parallel_hear(daemon, "s2")

        third = _parallel_card(store, root, "third")
        ok, detail = await daemon.dispatch_card(third["id"],
                                                allow_unplanned=True)
        assert not ok
        assert opened == ["second"]
        assert detail == ("Queued — Dark Army will start it when one of the 2 "
                          "agents working in this project finishes")
        assert store.get(third["id"])["queue_state"] == "queued"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_finished_row_spends_no_parallel_place(tmp_path, monkeypatch):
    """Rows still on screen with the work over do not hold a place: a card in
    Done, and a `live` card whose session Dark Army can no longer hear. Both come
    from `board_queue.holds` — the same predicate the snapshot publishes —
    rather than a second count filtering `link_state`, which is exactly the
    wedge that held four cards for an hour."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        done = _parallel_card(store, root, "done")
        store.update(done["id"], {"link_state": "live", "session_id": "s1",
                                  "column_name": "done"})
        _parallel_hear(daemon, "s1")
        quiet = _parallel_card(store, root, "quiet")
        store.update(quiet["id"], {"link_state": "live", "session_id": "gone",
                                   "column_name": "in_progress"})

        waiting = _parallel_card(store, root, "waiting")
        store.update(waiting["id"], {"queue_state": "queued",
                                     "queued_at": 1.0})
        daemon._decide_queue_dispatches(store.cards())

        assert daemon._queue_candidates == [waiting["id"]]
    finally:
        store.close()


def test_the_parallel_dial_is_clamped_at_both_ends(tmp_path):
    """The daemon is where an out-of-range number is refused, so a hand-edited
    preferences.json runs at a legal figure — and because the snapshot
    publishes the clamped attribute, the readout can never show a limit Dark Army
    is not obeying."""
    daemon, store = _board_daemon(tmp_path)
    try:
        daemon.set_board_parallel(0)
        assert daemon.board_parallel_limit == 1
        daemon.set_board_parallel(99)
        assert daemon.board_parallel_limit == 4
        daemon.set_board_parallel("not a number")
        assert daemon.board_parallel_limit == 1
        daemon.set_board_parallel(3)
        assert daemon.board_parallel_limit == 3
        assert daemon._build_board_state()["parallel_limit"] == 3
    finally:
        store.close()


def test_the_parallel_limit_rides_an_open_and_a_shut_board(tmp_path):
    """`counts`' rule applied to the number: a surface never handles a missing
    key, and a limit of zero is not a state the readout may draw."""
    daemon, store = _board_daemon(tmp_path)
    try:
        assert daemon._build_board_state()["parallel_limit"] == 1
    finally:
        store.close()
    shut = BobDaemon(sessions_path=tmp_path / "sessions2.json")
    shut._board = None
    state = shut._build_board_state()
    assert state["available"] is False
    assert state["parallel_limit"] == 1


@pytest.mark.asyncio
async def test_a_parallel_queued_card_carries_a_reason_and_no_holder(
        tmp_path, monkeypatch):
    """The sentence reaches the card, and it is the daemon's own words: no
    holder is named because under the slot rule none exists — the project is
    full, not a particular card in the way. Composed at decoration time, so
    switching the drain off rewords every queued card on the next frame."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        running = _parallel_card(store, root, "running")
        store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                     "column_name": "in_progress"})
        _parallel_hear(daemon, "s1")
        waiting = _parallel_card(store, root, "waiting")
        await daemon.dispatch_card(waiting["id"], allow_unplanned=True)

        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[waiting["id"]]["queue_reason"] == (
            "Queued — Dark Army will start it when the agent working in this "
            "project finishes")
        assert "queue_holder" not in by_id[waiting["id"]]
        # The retired file gate's published switch went with it.
        assert "queue_enabled" not in daemon._build_board_state()
        assert "queue_reason" not in by_id[running["id"]]
        # Never stored: a remembered reason is a decision the drain may not act
        # on, and the count behind it changes every time a session ends.
        assert "queue_reason" not in store.get(waiting["id"])

        daemon.board_autostart_enabled = False
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[waiting["id"]]["queue_reason"] == (
            "Queued — press Start when the agent working in this project "
            "finishes")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_second_press_on_a_queued_card_says_its_place(
        tmp_path, monkeypatch):
    """A person repeating the gesture gets an answer, not a status number —
    and the answer names the card's place in the drain's own ordering. The
    branch is a pure read: a re-stamp of `queued_at` here would move a card to
    the back of a queue it is near the front of."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        running = _parallel_card(store, root, "running")
        store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                     "column_name": "in_progress"})
        _parallel_hear(daemon, "s1")
        waiting = _parallel_card(store, root, "waiting")
        await daemon.dispatch_card(waiting["id"], allow_unplanned=True)
        assert store.get(waiting["id"])["queue_state"] == "queued"

        before = store.get(waiting["id"])
        ok, detail = await daemon.dispatch_card(waiting["id"],
                                                allow_unplanned=True)

        assert not ok
        assert opened == []
        assert detail == ("Already queued — first in line for this project; "
                          "Dark Army will start it when a place frees up")
        # Nothing written: the card keeps its exact place, however many times
        # the gesture is repeated.
        assert store.get(waiting["id"]) == before

        daemon.board_autostart_enabled = False
        ok, detail = await daemon.dispatch_card(waiting["id"],
                                                allow_unplanned=True)
        assert not ok
        assert detail == ("Already queued — first in line for this project; "
                          "press Start when a place frees up")
        assert store.get(waiting["id"]) == before
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_second_press_counts_the_cards_queued_ahead(
        tmp_path, monkeypatch):
    """The position is `board_queue.queue_key`'s ordering — the one the drain
    will act on — so the number a person is told is the number that decides
    who starts next."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        running = _parallel_card(store, root, "running")
        store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                     "column_name": "in_progress"})
        _parallel_hear(daemon, "s1")
        first = _parallel_card(store, root, "first")
        await daemon.dispatch_card(first["id"], allow_unplanned=True)
        second = _parallel_card(store, root, "second")
        await daemon.dispatch_card(second["id"], allow_unplanned=True)
        assert store.get(second["id"])["queue_state"] == "queued"

        ok, detail = await daemon.dispatch_card(second["id"],
                                                allow_unplanned=True)

        assert not ok
        assert detail == ("Already queued — second in line for this project; "
                          "Dark Army will start it when a place frees up")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_drains_replay_stays_silent(tmp_path, monkeypatch):
    """The `queued_replay` test comes first in the split for a reason: a
    replayed card is always queued, so the other ordering would send the
    person's sentence down the drain's own path and add a board read to every
    pass."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    root = _parallel_project(tmp_path, daemon, monkeypatch, opened)
    try:
        running = _parallel_card(store, root, "running")
        store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                     "column_name": "in_progress"})
        _parallel_hear(daemon, "s1")
        waiting = _parallel_card(store, root, "waiting")
        await daemon.dispatch_card(waiting["id"], allow_unplanned=True)
        before = store.get(waiting["id"])

        ok, detail = await daemon.dispatch_card(waiting["id"],
                                               allow_unplanned=True,
                                               queued_replay=True)

        assert not ok
        assert detail == ""
        assert store.get(waiting["id"]) == before
    finally:
        store.close()


# --- the per-project dial: the same number, settable one project at a time --


def _parallel_two_projects(tmp_path, daemon, monkeypatch, opened):
    """Two real project roots, `dispatch.spawn` stubbed. Returns both."""
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setattr(daemon, "_known_project_roots",
                        lambda: {str(one.resolve()), str(two.resolve())})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def accept(root_, argv, name, **_kw):
        opened.append(name)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    return one, two


@pytest.mark.asyncio
async def test_a_projects_own_parallel_dial_wins_at_the_gate(tmp_path, monkeypatch):
    """The whole point: a project set to 2 starts a second card while its
    neighbour, left on the shared default of 1, still queues one."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    one, two = _parallel_two_projects(tmp_path, daemon, monkeypatch, opened)
    try:
        daemon.set_board_parallel_override(str(one), 2)

        busy_one = _parallel_card(store, one, "busy one", project="one")
        store.update(busy_one["id"], {"link_state": "live",
                                      "session_id": "a1",
                                      "column_name": "in_progress"})
        busy_two = _parallel_card(store, two, "busy two", project="two")
        store.update(busy_two["id"], {"link_state": "live",
                                      "session_id": "b1",
                                      "column_name": "in_progress"})
        _parallel_hear(daemon, "a1", "b1")

        second_one = _parallel_card(store, one, "second one", project="one")
        ok_one, _ = await daemon.dispatch_card(second_one["id"],
                                               allow_unplanned=True)
        second_two = _parallel_card(store, two, "second two", project="two")
        ok_two, detail_two = await daemon.dispatch_card(second_two["id"],
                                                        allow_unplanned=True)

        assert ok_one is True
        assert ok_two is False
        assert detail_two.startswith("Queued")
        assert store.get(second_two["id"])["queue_state"] == "queued"
        assert store.get(second_one["id"])["queue_state"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_projects_own_parallel_dial_wins_at_the_drain(tmp_path, monkeypatch):
    """The drain resolves from the sorted head's **own root**, so a project
    with room replays its queue while its neighbour's stays put."""
    daemon, store = _board_daemon(tmp_path)
    opened: list = []
    one, two = _parallel_two_projects(tmp_path, daemon, monkeypatch, opened)
    try:
        busy_one = _parallel_card(store, one, "busy one", project="one")
        store.update(busy_one["id"], {"link_state": "live",
                                      "session_id": "a1",
                                      "column_name": "in_progress"})
        busy_two = _parallel_card(store, two, "busy two", project="two")
        store.update(busy_two["id"], {"link_state": "live",
                                      "session_id": "b1",
                                      "column_name": "in_progress"})
        _parallel_hear(daemon, "a1", "b1")

        wait_one = _parallel_card(store, one, "wait one", project="one")
        await daemon.dispatch_card(wait_one["id"], allow_unplanned=True)
        wait_two = _parallel_card(store, two, "wait two", project="two")
        await daemon.dispatch_card(wait_two["id"], allow_unplanned=True)
        assert store.get(wait_one["id"])["queue_state"] == "queued"
        assert store.get(wait_two["id"])["queue_state"] == "queued"

        daemon._decide_queue_dispatches(store.cards())
        assert daemon._queue_candidates == []

        daemon.set_board_parallel_override(str(one), 2)
        daemon._decide_queue_dispatches(store.cards())

        assert daemon._queue_candidates == [wait_one["id"]]
    finally:
        store.close()


def test_the_per_project_parallel_dial_is_clamped_and_cleared(tmp_path):
    """One bound, at the daemon — and 0 is the unambiguous "back to the shared
    default", because 0 is never a legal limit."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    key = dispatch.normalise_root(str(root))
    try:
        daemon.set_board_parallel_override(str(root), 99)
        assert daemon.board_parallel_overrides == {key: 4}
        daemon.set_board_parallel_override(str(root), 3)
        assert daemon._parallel_limit_for(str(root)) == 3

        daemon.set_board_parallel_override(str(root), 0)
        assert daemon.board_parallel_overrides == {}
        assert daemon._parallel_limit_for(str(root)) == 1

        daemon.set_board_parallel_override(str(root), 2)
        daemon.set_board_parallel_override(str(root), None)
        assert daemon.board_parallel_overrides == {}

        daemon.set_board_parallel_override(str(root), "not a number")
        assert daemon.board_parallel_overrides == {}
        daemon.set_board_parallel_override("", 3)
        assert daemon.board_parallel_overrides == {}

        # The machine dial is what an unset project gets, and it moves.
        daemon.set_board_parallel(3)
        assert daemon._parallel_limit_for(str(root)) == 3
        daemon.set_board_parallel_override(str(root), 1)
        assert daemon._parallel_limit_for(str(root)) == 1
    finally:
        store.close()


def test_the_bulk_parallel_setter_clamps_every_entry(tmp_path):
    """The startup feed: an unusable entry is dropped rather than failing the
    lot, and every survivor comes through the one clamp."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    key = dispatch.normalise_root(str(root))
    try:
        daemon.set_board_parallel_overrides({
            str(root): 50, "": 2, "/nope/nowhere": 0,
            "/nope/elsewhere": "three",
        })
        assert daemon.board_parallel_overrides == {key: 4}
        daemon.set_board_parallel_overrides({})
        assert daemon.board_parallel_overrides == {}
    finally:
        store.close()


def test_the_parallel_resolution_rides_every_snapshot_shape(tmp_path):
    """Published, never joined in Swift: the resolved figure per card, the
    override map for the picker's tick, the scalar unchanged as the default."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    key = dispatch.normalise_root(str(root))
    try:
        daemon.set_board_parallel(2)
        daemon.set_board_parallel_override(str(root), 4)
        card = _parallel_card(store, root, "a card")

        state = daemon._build_board_state()

        assert state["parallel_limit"] == 2
        assert state["parallel_overrides"] == {key: 4}
        row = [c for c in state["cards"] if c["id"] == card["id"]][0]
        assert row["parallel_limit"] == 4
    finally:
        store.close()

    shut = BobDaemon(sessions_path=tmp_path / "sessions3.json")
    shut._board = None
    shut.set_board_parallel_override(str(root), 3)
    state = shut._build_board_state()
    assert state["available"] is False
    assert state["parallel_limit"] == 1
    assert state["parallel_overrides"] == {key: 3}


def test_the_parallel_map_rides_the_exception_fallback(tmp_path):
    """The third shape. A board that cannot be read still hands the panel
    every key it has always been sent."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    key = dispatch.normalise_root(str(root))
    try:
        daemon.set_board_parallel_override(str(root), 2)
        daemon._board_state = {}

        class Boom:
            def cards(self, *a, **k):
                raise RuntimeError("no board")

        daemon._board = Boom()
        state = daemon._build_board_state()

        assert state["available"] is False
        assert state["parallel_limit"] == 1
        assert state["parallel_overrides"] == {key: 2}
    finally:
        store.close()


def test_the_already_queued_sentence_cannot_collide_with_the_plan_gate():
    """`ActionResult.isPlanGateRefusal` is prefix-based, so a wording drift
    that started either sentence with the gate's opening words would route a
    queue answer into the "Start unplanned?" confirmation."""
    for autostart in (True, False):
        for position in (1, 2, 8, 12):
            line = daemon_mod._already_queued_reason(position, autostart)
            assert not line.startswith(daemon_mod.PLAN_GATE_REFUSAL[:28])
            assert line.startswith("Already queued — ")
    assert daemon_mod._already_queued_reason(0, True).startswith(
        "Already queued — first in line")
    assert "12th in line" in daemon_mod._already_queued_reason(12, True)


# --- card dependencies: the three sentences both clients draw verbatim ------


def test_the_dependency_sentence_names_the_cards_and_keeps_the_promise():
    reason = daemon_mod._dependency_reason
    assert reason(["Build it"], True) == (
        'Queued — Dark Army will start it once "Build it" is done')
    assert reason(["Build it", "Ship it"], True) == (
        'Queued — Dark Army will start it once "Build it" and "Ship it" are done')
    assert reason(["A", "B", "C"], True) == (
        'Queued — Dark Army will start it once "A", "B" and "C" are done')
    # With the drain off nobody is coming: the promise head says so.
    assert reason(["Build it"], False) == (
        'Queued — press Start once "Build it" is done')
    assert reason([], True) == (
        "Queued — Dark Army will start it once the cards it waits on are done")
    assert reason([""], True).endswith('once "untitled" is done')


def test_the_dependency_sentence_never_collides_with_a_prefix_both_clients_match():
    """Both clients match refusals by prefix (`ActionResult`,
    `PhoneActions.planGatePrefix` …), and `_queue_reason`'s exact strings are
    pinned elsewhere: the dependency sentence is none of them."""
    for autostart in (True, False):
        for titles in (["x"], ["x", "y"], []):
            line = daemon_mod._dependency_reason(titles, autostart)
            for prefix in (daemon_mod.PLAN_GATE_REFUSAL[:28],
                           daemon_mod.PLAN_CHANGED_REFUSAL[:28],
                           daemon_mod.board.CARD_CHANGED_REFUSAL[:24],
                           "this card has no plan yet",
                           "this card's plan has changed",
                           "this card changed on the Mac"):
                assert not line.startswith(prefix), (line, prefix)
            for count in (0, 1, 2, 3):
                assert line != daemon_mod._queue_reason(count, autostart)


def test_the_two_dependency_lines():
    line = daemon_mod._dependency_line
    assert line([]) == ""
    assert line([{"title": "A", "column_name": "done", "met": True},
                 {"title": "B", "column_name": "in_progress", "met": True},
                 {"title": "C", "column_name": "backlog", "met": False}]) == (
        'Waits on: "A" (done) \u00b7 "B" (check pending) \u00b7 "C" (not yet)')
    assert daemon_mod._dependents_line([]) == ""
    assert daemon_mod._dependents_line(["A", "B"]) == 'Unblocks: "A" \u00b7 "B"'


def test_a_held_queued_card_says_what_it_waits_for_and_a_slot_held_one_does_not(
        tmp_path):
    """The decoration's `queue_reason`: the dependency sentence while the
    card waits on an unfinished card, `_queue_reason`'s own once it waits
    only for a place."""
    daemon, store = _board_daemon(tmp_path)
    try:
        dep, _ = store.create({"title": "the foundation", "project": "bob",
                               "root": "/tmp", "column_name": "backlog"})
        held, _ = store.create({"title": "held", "project": "bob",
                                "root": "/tmp", "column_name": "backlog"})
        slot, _ = store.create({"title": "slot", "project": "bob",
                                "root": "/tmp", "column_name": "backlog"})
        store.update(held["id"], {"blocked_by": dep["id"],
                                  "queue_state": "queued", "queued_at": 1.0})
        store.update(slot["id"], {"queue_state": "queued", "queued_at": 2.0})
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[held["id"]]["queue_reason"] == (
            'Queued — Dark Army will start it once "the foundation" is done')
        assert by_id[slot["id"]]["queue_reason"] == daemon_mod._queue_reason(
            0, True)
        # Met: the same card now waits only for a place, and says so.
        store.update(dep["id"], {"column_name": "done"})
        by_id = {c["id"]: c for c in daemon._build_board_state()["cards"]}
        assert by_id[held["id"]]["queue_reason"] == daemon_mod._queue_reason(
            0, True)
    finally:
        store.close()


def test_the_pipeline_markers_say_dependencies_are_supported(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        assert daemon._build_board_state()["dependencies_supported"] is True
        assert daemon._pipeline_writable()["dependencies_supported"] is True
    finally:
        store.close()


# ── the enrolment gate ────────────────────────────────────────────────────────
#
# Dark Army only watches projects the user enrolled. Every message on the hook socket
# carries the key its project holds; a message with no key, or a wrong one, is
# turned away above every branch of `_handle_message`. These ask for
# `enforce_enrolment`, which puts the real module back in place of conftest's
# open-door fixture.


def _enrol(tmp_path, name="proj"):
    """Enrol a real folder and return `(root, key)`."""
    from dark_army_daemon import enrollment
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    ok, _ = enrollment.enroll(str(root))
    assert ok
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    return os.path.realpath(str(root)), key


@pytest.mark.asyncio
async def test_an_unkeyed_hook_event_creates_no_session(enforce_enrolment,
                                                        tmp_path):
    _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "ghost",
                             "cwd": str(tmp_path / "proj")})
    assert "ghost" not in d._session_states


@pytest.mark.asyncio
async def test_a_wrong_key_creates_no_session(enforce_enrolment, tmp_path):
    from dark_army_daemon import enrollment
    _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "ghost",
                             "key": enrollment.mint()})
    assert "ghost" not in d._session_states


@pytest.mark.asyncio
async def test_the_right_key_is_admitted(enforce_enrolment, tmp_path):
    _root, key = _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "real",
                             "key": key})
    assert "real" in d._session_states


@pytest.mark.asyncio
async def test_every_board_verb_is_refused_with_a_reply(enforce_enrolment,
                                                        tmp_path):
    """An unanswered `tools/call` id is a session waiting for ever, which is
    worse than the thing being prevented."""
    from dark_army_daemon.daemon import ENROLLMENT_REFUSAL
    _enrol(tmp_path)
    d = BobDaemon()
    for kind in ("board_card_request", "board_close_request",
                 "board_attach_request", "board_manual_request",
                 "board_answer_request"):
        reply = await d._handle_message({"type": kind, "port": 1})
        assert reply == {"ok": False, "error": ENROLLMENT_REFUSAL}


@pytest.mark.asyncio
async def test_an_unkeyed_statusline_learns_nothing(enforce_enrolment,
                                                    tmp_path):
    """`others_waiting` must not cross into a project Dark Army was never told to
    watch — that is a cross-project leak, not merely a refusal."""
    _enrol(tmp_path)
    d = BobDaemon()
    reply = await d._handle_message({"event": "statusline", "session_id": "x",
                                     "data": {"session_id": "x"}})
    assert reply == {}


@pytest.mark.asyncio
async def test_an_unkeyed_channel_attach_registers_nothing(enforce_enrolment,
                                                           tmp_path):
    _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"type": "channel_attach", "port": 5555,
                             "pid": os.getpid(), "session_id": "s",
                             "cwd": str(tmp_path / "proj")})
    assert 5555 not in d._channels


@pytest.mark.asyncio
async def test_a_session_is_forgotten_when_its_project_is_unenrolled(
        enforce_enrolment, tmp_path):
    """Un-enrolment bites on the next thing the project says, not at the end of
    a run that may last hours."""
    from dark_army_daemon import enrollment
    root, key = _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "live",
                             "key": key})
    assert "live" in d._session_states
    # The door asks `_session_place`, not the arriving message, so the snapshot
    # has to know the folder — `_session_states` carries no cwd at all.
    d._agents_snapshot_cache = {"running": [{"session_id": "live",
                                             "cwd": root}]}
    enrollment.unenroll(root)
    await d._handle_message({"event": "tool_use", "session_id": "live",
                             "key": key, "tool_name": "Bash"})
    assert "live" not in d._session_states


@pytest.mark.asyncio
async def test_an_unkeyed_channel_attach_does_not_forget_an_enrolled_session(
        enforce_enrolment, tmp_path):
    """A channel copy launched before enrolment shipped sends no key and
    heartbeats every 30s. It is refused — but it only *names* the session, and
    that session's project is still enrolled, so the row stays."""
    root, key = _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "live",
                             "key": key})
    d._agents_snapshot_cache = {"running": [{"session_id": "live",
                                             "cwd": root}]}
    await d._handle_message({"type": "channel_attach", "port": 5555,
                             "pid": os.getpid(), "session_id": "live",
                             "cwd": root})
    assert "live" in d._session_states
    assert 5555 not in d._channels


@pytest.mark.asyncio
async def test_an_unkeyed_message_naming_an_enrolled_session_does_not_forget_it(
        enforce_enrolment, tmp_path):
    """The hook socket authenticates nothing and `/api/state` is ungated, so a
    message can name any live id and claim any folder. The session's own folder
    decides."""
    root, key = _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "live",
                             "key": key})
    d._agents_snapshot_cache = {"running": [{"session_id": "live",
                                             "cwd": root}]}
    await d._handle_message({"event": "tool_use", "session_id": "live",
                             "cwd": str(tmp_path / "elsewhere"),
                             "tool_name": "Bash"})
    assert "live" in d._session_states


def test_note_unenrolled_only_keeps_a_corroborated_folder(enforce_enrolment,
                                                          tmp_path,
                                                          monkeypatch):
    """The claimed cwd arrived on a socket that authenticates nothing. Without
    corroboration a forged message could put an arbitrary path in front of
    somebody, with an Enrol button beside it that writes a key into it."""
    from dark_army_daemon import daemon as dmod
    real = tmp_path / "open-window"
    real.mkdir()
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (
                            dmod.workspace.Window("w", (str(real),)),))
    d = BobDaemon()
    d._note_unenrolled({"cwd": "/etc/definitely-not-open"})
    assert d._unenrolled == {}
    assert d._unenrolled_other == 1

    d._note_unenrolled({"cwd": str(real / "src")})
    assert list(d._unenrolled) == [os.path.realpath(str(real))]
    assert d._unenrolled[os.path.realpath(str(real))]["count"] == 1


def test_note_unenrolled_is_bounded(enforce_enrolment, tmp_path, monkeypatch):
    from dark_army_daemon import daemon as dmod
    folders = []
    for i in range(dmod.MAX_UNENROLLED + 4):
        f = tmp_path / f"w{i}"
        f.mkdir()
        folders.append(str(f))
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (
                            dmod.workspace.Window("w", tuple(folders)),))
    d = BobDaemon()
    for i, folder in enumerate(folders):
        d._note_unenrolled({"cwd": folder})
    assert len(d._unenrolled) == dmod.MAX_UNENROLLED


def test_the_enrolment_snapshot_carries_no_key_or_digest(enforce_enrolment,
                                                         tmp_path):
    from dark_army_daemon import enrollment
    _root, key = _enrol(tmp_path)
    d = BobDaemon()
    blob = json.dumps(d.enrollment_snapshot())
    assert key not in blob
    assert enrollment.digest(key) not in blob
    assert "digest" not in blob
    assert d.enrollment_snapshot()["available"] is True


def test_the_codex_roster_drops_an_unenrolled_record(enforce_enrolment,
                                                     tmp_path, monkeypatch):
    from dark_army_daemon import codex_rollouts
    from dark_army_daemon import daemon as dmod
    root, _key = _enrol(tmp_path)
    inside = codex_rollouts.CodexRecord(
        session_id="codex:in", thread_id="t1", path=tmp_path / "a.jsonl",
        cwd=root)
    outside = codex_rollouts.CodexRecord(
        session_id="codex:out", thread_id="t2", path=tmp_path / "b.jsonl",
        cwd=str(tmp_path / "elsewhere"))
    monkeypatch.setattr(dmod.codex_rollouts, "load_recent",
                        lambda *a, **kw: [inside, outside])
    d = BobDaemon()
    d._refresh_codex_records()
    assert set(d._codex_records) == {"codex:in"}


def test_the_grok_roster_drops_an_unenrolled_record(enforce_enrolment,
                                                    tmp_path, monkeypatch):
    from dark_army_daemon import grok_roster
    from dark_army_daemon import daemon as dmod
    root, _key = _enrol(tmp_path)
    inside = grok_roster.GrokRecord(session_id="g-in", pid=os.getpid(),
                                    cwd=root, opened_at=time.time())
    outside = grok_roster.GrokRecord(session_id="g-out", pid=os.getpid(),
                                     cwd=str(tmp_path / "elsewhere"),
                                     opened_at=time.time())
    monkeypatch.setattr(dmod.grok_roster, "load_active",
                        lambda *a, **kw: [inside, outside])
    # Liveness is a separate question; this test is about the filter.
    monkeypatch.setattr(BobDaemon, "_live_grok_records",
                        lambda self, recs: {r.session_id: r for r in recs})
    d = BobDaemon()
    d._refresh_grok_records()
    assert set(d._grok_records) == {"g-in"}


def test_the_stub_sweep_keeps_a_row_with_no_folder_yet(enforce_enrolment,
                                                       tmp_path):
    """A stub with an empty cwd already passed the key gate at the door;
    dropping it would delete every hook-stream row whose folder is unresolved."""
    _root, key = _enrol(tmp_path)
    d = BobDaemon()
    d._session_states["nofolder"] = {"state": "idle", "last_event": time.time()}
    ids = {s["session_id"] for s in d._collect_agent_stubs()}
    assert "nofolder" in ids


def test_an_enrolled_folder_is_never_listed_as_turned_away(enforce_enrolment,
                                                           tmp_path,
                                                           monkeypatch):
    """A refusal from an enrolled folder is a stale client — a channel process
    still running the copy it launched with — not an unenrolled project. Listing
    it would put "Dark Army is ignoring it" beside that project's own live rows, with
    an Enrol button that does nothing."""
    from dark_army_daemon import daemon as dmod
    from dark_army_daemon import enrollment
    root, _key = _enrol(tmp_path)
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (
                            dmod.workspace.Window("w", (root,)),))
    d = BobDaemon()
    d._note_unenrolled({"cwd": root})
    assert d._unenrolled == {}
    assert d._unenrolled_other == 1
    assert d.enrollment_snapshot()["pending"] == []


def test_enrolling_clears_a_folder_from_pending_on_the_next_frame(
        enforce_enrolment, tmp_path, monkeypatch):
    """Recorded before enrolment, gone after it — without waiting for a
    memory-only entry to age out of a list nothing prunes on enrolment."""
    from dark_army_daemon import daemon as dmod
    from dark_army_daemon import enrollment
    later = tmp_path / "later"
    later.mkdir()
    real = os.path.realpath(str(later))
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (
                            dmod.workspace.Window("w", (real,)),))
    d = BobDaemon()
    d._note_unenrolled({"cwd": real})
    assert [p["root"] for p in d.enrollment_snapshot()["pending"]] == [real]
    assert enrollment.enroll(real)[0]
    assert d.enrollment_snapshot()["pending"] == []


# --- The card's model, at v13 -----------------------------------------------


def test_the_board_snapshot_carries_the_model_catalogue(tmp_path):
    """Published rather than duplicated in Swift: the panel's model chooser
    reads this and re-derives nothing."""
    daemon, store = _board_daemon(tmp_path)
    try:
        state = daemon._build_board_state()
        assert state["models"] == {t: list(m)
                                   for t, m in dispatch.MODELS.items()}
    finally:
        store.close()


def test_a_shut_board_still_publishes_the_model_catalogue(tmp_path):
    """`counts`' never-missing-key rule: a surface reading `models` never has
    to handle its absence."""
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._board = None
    assert daemon._build_board_state()["models"] == {
        t: list(m) for t, m in dispatch.MODELS.items()}


def _argv_recording_project(tmp_path, daemon, monkeypatch, seen):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(daemon, "_known_project_roots",
                        lambda: {str(root.resolve())})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/" + tool)

    async def accept(root_, argv, name, **_kw):
        seen.append(list(argv))
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    return root


@pytest.mark.asyncio
async def test_dispatching_a_card_with_a_model_puts_the_flag_on_the_argv(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path)
    seen: list = []
    root = _argv_recording_project(tmp_path, daemon, monkeypatch, seen)
    try:
        card = _parallel_card(store, root, "with a model", model="opus")
        ok, detail = await daemon.dispatch_card(card["id"],
                                                allow_unplanned=True)
        assert ok, detail
        assert len(seen) == 1
        argv = seen[0]
        assert argv[:3] == ["/bin/claude", "--model", "opus"]
        # And the prompt is still last, verbatim.
        assert argv[-1] == "go"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_default_card_dispatches_with_no_model_flag(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path)
    seen: list = []
    root = _argv_recording_project(tmp_path, daemon, monkeypatch, seen)
    try:
        card = _parallel_card(store, root, "default")
        ok, detail = await daemon.dispatch_card(card["id"],
                                                allow_unplanned=True)
        assert ok, detail
        assert seen == [["/bin/claude", "go"]]
    finally:
        store.close()


# --- The session's working folder rides the hook stream ---

@pytest.mark.asyncio
async def test_a_hook_message_stores_the_working_folder():
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "s-cwd",
                             "project": "host", "cwd": "/x/proj/host"})
    assert d._session_states["s-cwd"]["cwd"] == "/x/proj/host"


@pytest.mark.asyncio
async def test_a_later_message_updates_the_stored_folder():
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "s-cwd",
                             "project": "host", "cwd": "/x/proj/host"})
    await d._handle_message({"event": "tool_use", "session_id": "s-cwd",
                             "tool_name": "Bash", "project": "panel",
                             "cwd": "/x/proj/panel"})
    assert d._session_states["s-cwd"]["cwd"] == "/x/proj/panel"


@pytest.mark.asyncio
async def test_a_message_with_no_folder_leaves_the_stored_one_alone():
    """An older installed handler sends no cwd; absence is silence, not a
    retraction."""
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "s-cwd",
                             "project": "host", "cwd": "/x/proj/host"})
    await d._handle_message({"event": "tool_use", "session_id": "s-cwd",
                             "tool_name": "Bash", "project": "host"})
    assert d._session_states["s-cwd"]["cwd"] == "/x/proj/host"


# --- a card follows its enrolled folder's name -------------------------------


def _enrol_in_tests(root):
    """Seed the tmp ledger conftest already points at, and drop the cache."""
    from dark_army_daemon import enrollment, paths
    paths.ENROLLMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.ENROLLMENT_PATH.write_text(json.dumps({
        "version": 1,
        "projects": [{"root": root, "digest": "x", "label": "IGNORED"}],
    }))
    enrollment.invalidate()


def test_reconcile_relabels_a_card_to_its_enrolled_folder(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        root = os.path.realpath(str(tmp_path / "finance-demo"))
        os.makedirs(root, exist_ok=True)
        _enrol_in_tests(root)
        card, detail = store.create({"title": "the work", "project": "Ledgerly",
                                     "root": root, "tool": "claude",
                                     "column_name": "backlog"})
        assert card is not None, detail
        assert daemon._reconcile_board({"running": []}) is True
        assert store.get(card["id"])["project"] == "finance-demo"
        # Steady state: the second pass has nothing to say.
        assert daemon._reconcile_board({"running": []}) is False
    finally:
        store.close()


def test_a_pre_rename_card_still_binds_the_session_it_starts(tmp_path):
    """The board joins card to session by *label*, so without the relabel a
    card written before the rename could never bind and would time out with an
    orange line for ever."""
    daemon, store = _board_daemon(tmp_path)
    try:
        root = os.path.realpath(str(tmp_path / "finance-demo"))
        os.makedirs(root, exist_ok=True)
        _enrol_in_tests(root)
        now = time.time()
        card, detail = store.create({"title": "the work", "project": "Ledgerly",
                                     "root": root, "tool": "claude",
                                     "column_name": "backlog"})
        assert card is not None, detail
        store.update(card["id"], {"link_state": "dispatching",
                                  "dispatched_at": now})
        row = {"session_id": "fresh", "provider": "claude",
               "kind": "interactive", "project": "finance-demo",
               "cwd": root, "started_at": now + 1}
        assert daemon._reconcile_board({"running": [row]}) is True
        after = store.get(card["id"])
        assert after["project"] == "finance-demo"
        assert after["session_id"] == "fresh"
    finally:
        store.close()


# --- `request_preference`: the phone's two dials, handed over ---------------


class _PreferenceObserver:
    """Everything the daemon needs of the menu-bar app for this seam."""

    def __init__(self, raises=False):
        self.seen: list = []
        self.raises = raises

    def on_preference_request(self, key, value):
        self.seen.append((key, value))
        if self.raises:
            raise RuntimeError("boom")


def test_request_preference_refuses_an_unknown_key_by_name():
    daemon = BobDaemon()
    daemon.add_observer(_PreferenceObserver())
    ok, detail = daemon.request_preference("board_dispatch", True)
    assert ok is False
    assert "board_dispatch" in detail


def test_request_preference_refuses_when_nobody_is_listening():
    """`preferences.json` is the menu-bar app's file. With no app there is
    nothing to hand the press to, and a switch reporting success while
    changing nothing is worse than one that says it cannot."""
    from dark_army_daemon.daemon import PREFERENCE_UNREACHABLE_REFUSAL
    daemon = BobDaemon()
    ok, detail = daemon.request_preference("board_autostart", False)
    assert ok is False
    assert detail == PREFERENCE_UNREACHABLE_REFUSAL


def test_request_preference_hands_over_and_writes_nothing_itself():
    daemon = BobDaemon()
    before_overrides = dict(daemon.board_parallel_overrides)
    before_autostart = daemon.board_autostart_enabled
    observer = _PreferenceObserver()
    daemon.add_observer(observer)

    ok, _ = daemon.request_preference(
        "board_parallel_root", {"root": "/x", "limit": 0})

    assert ok is True
    assert observer.seen == [("board_parallel_root", {"root": "/x", "limit": 0})]
    # One writer, and it is the app: an applied-but-unstored value is a
    # setting that dies at the next launch.
    assert daemon.board_parallel_overrides == before_overrides
    assert daemon.board_autostart_enabled is before_autostart


def test_an_observer_that_raises_does_not_take_the_caller_down():
    daemon = BobDaemon()
    daemon.add_observer(_PreferenceObserver(raises=True))
    ok, _ = daemon.request_preference("board_autostart", True)
    assert ok is True


# --- what Dark Army observed a run do ---------------------------------------------


def _ended_card(daemon, store, **fields):
    """A card bound to `s1` whose grace has already run out, so the next
    reconcile is the one that stamps it `ended`."""
    base = {"title": "the work", "project": "bob", "root": "/tmp",
            "tool": "claude", "column_name": "backlog"}
    base.update(fields)
    card, _ = store.create(base)
    store.bind_session(card["id"], "s1")
    store.update(card["id"], {"dispatched_at": 100.0})
    daemon._board_missing_since[card["id"]] = 0.0
    return card


def test_every_way_a_run_can_end_queues_exactly_one_record(tmp_path):
    """The `mark_ended` seam fires for all three shapes the interview named:
    the assistant closed the card, it left a hand-check, or it went quiet."""
    from dark_army_daemon import work_record
    for fields, verdict in (
            ({}, work_record.VERDICT_QUIET),
            ({"manual_steps": "1. look at it"}, work_record.VERDICT_MANUAL),
            ({"column_name": "done", "closed_by": "s1"},
             work_record.VERDICT_CLOSED)):
        daemon, store = _board_daemon(tmp_path / str(hash(str(fields))))
        try:
            card = _ended_card(daemon, store)
            if fields:
                if "manual_steps" in fields:
                    store.flag_manual(card["id"], "s1", fields["manual_steps"])
                else:
                    store.declare_done(card["id"], "s1", "done it")
            daemon._reconcile_board({"running": [], "finished": [
                {"session_id": "s1", "last_text": "## Work done\nall of it",
                 "last_summary": "did the thing"}]})
            assert len(daemon._work_record_queue) == 1
            item = daemon._work_record_queue[0]
            assert item[0] == card["id"] and item[1] == "s1"
            assert item[4] == verdict
            assert item[5] == "## Work done\nall of it"
            assert item[6] == "did the thing"
        finally:
            store.close()


def test_a_refining_card_is_given_no_record(tmp_path):
    """A refinement's exit condition is the plan it attaches, not a diff."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "plan it", "project": "bob",
                                "root": "/tmp", "tool": "claude",
                                "column_name": "prep"})
        store.update(card["id"], {"refine_session_id": "r1",
                                  "refine_state": "live"})
        daemon._refine_missing_since[card["id"]] = 0.0
        daemon._reconcile_board({"running": []})
        assert daemon._work_record_queue == []
    finally:
        store.close()


def test_a_record_already_written_for_this_run_is_not_queued_again(tmp_path):
    """A restart re-runs the grace; the stored record's own `run_at` is what
    stops the same run being collected twice."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card = _ended_card(daemon, store)
        store.open_run(card["id"], 100.0, "/tmp", "abc")
        store.close_run(card["id"], 100.0, "s1", "quiet", "", "", [], False,
                        "no baseline")
        daemon._reconcile_board({"running": []})
        assert daemon._work_record_queue == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_flush_starts_at_most_one_collection_per_cycle(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        started = []

        async def fake(*item):
            started.append(item[0])
            await asyncio.sleep(0.05)

        daemon._collect_work_record = fake
        daemon._work_record_queue = [
            ("c1", "s1", "/tmp", 1.0, "quiet", "", ""),
            ("c2", "s2", "/tmp", 1.0, "quiet", "", ""),
        ]
        await daemon._flush_work_records()
        await asyncio.sleep(0)          # let the detached task get going
        await daemon._flush_work_records()
        assert started == ["c1"]
        assert len(daemon._work_record_queue) == 1
        await daemon._work_record_task
        await daemon._flush_work_records()
        await daemon._work_record_task
        assert started == ["c1", "c2"]
        assert daemon._work_record_queue == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_git_failure_still_writes_a_record_saying_why(tmp_path):
    """The failure paths are the point: a run that ended with no record is
    the hole this exists to fill, so every one of them still writes one."""
    from dark_army_daemon import enrollment, work_record
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude"})
        store.open_run(card["id"], 100.0, "/tmp", "abc")

        async def failing(argv, root, *, truncate=False):
            return False, b"", work_record.GIT_TIMEOUT_REASON

        daemon._run_git = failing
        daemon._publish_board = AsyncMock()
        with patch.object(enrollment, "root_enrolled", lambda cwd: "/tmp"):
            await daemon._collect_work_record(
                card["id"], "s1", "/tmp", 100.0, "quiet", "went quiet", "")
        record = store.run_for(card["id"])
        assert record["recorded_at"] is not None
        assert record["files_available"] is False
        assert record["files_reason"] == work_record.GIT_TIMEOUT_REASON
        assert record["report"] == "went quiet"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_un_enrolled_project_is_not_read_at_all(tmp_path):
    from dark_army_daemon import enrollment, work_record
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude"})
        store.open_run(card["id"], 100.0, "/tmp", "abc")
        calls = []

        async def counting(argv, root, *, truncate=False):
            calls.append(argv)
            return True, b"", ""

        daemon._run_git = counting
        daemon._publish_board = AsyncMock()
        with patch.object(enrollment, "root_enrolled", lambda cwd: ""):
            await daemon._collect_work_record(
                card["id"], "s1", "/tmp", 100.0, "quiet", "", "")
        assert calls == []
        record = store.run_for(card["id"])
        assert record["files_reason"] == work_record.NOT_ENROLLED_REASON
    finally:
        store.close()


def test_the_headline_rides_the_snapshot_and_absence_means_no_record(tmp_path):
    """Eight scalars per card and **absent** where there is no record: a card
    nobody ran and a run that changed nothing are different facts."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "the work", "project": "bob",
                                "root": "/tmp", "tool": "claude"})
        state = daemon._build_board_state()
        assert "work_record" not in state["cards"][0]
        store.open_run(card["id"], 100.0, "/tmp", "abc")
        store.close_run(card["id"], 100.0, "s1", "closed", "r", "",
                        [{"path": "a.py", "added": 2, "removed": 1,
                          "binary": False, "new": False}], True)
        head = daemon._build_board_state()["cards"][0]["work_record"]
        assert head["verdict"] == "closed"
        assert head["files"] == 1 and head["added"] == 2 and head["removed"] == 1
        assert head["report"] is True and head["files_available"] is True
        # The text itself never rides the frame.
        assert "report_text" not in head
        assert all(not isinstance(v, str) or len(v) < 40
                   for v in head.values())
    finally:
        store.close()


# ── the timed spawn log and the window a card's trail is cut to ─────────────


def _start(daemon, sid, role, at):
    daemon._update_session_state("subagent_start", "SubagentStart", sid,
                                 agent_id=role)
    daemon._session_states[sid]["subagent_spawns"][-1][1] = at


def test_subagent_spawns_keeps_every_start_with_its_time(tmp_path):
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._update_session_state("session_start", "SessionStart", "s1")
    for _ in range(3):
        daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                     agent_id="bc-implementer")
    state = daemon._session_states["s1"]
    log = state["subagent_spawns"]
    assert [r for r, _ in log] == ["bc-implementer"] * 3
    assert all(isinstance(t, float) for _, t in log)
    assert [t for _, t in log] == sorted(t for _, t in log)
    assert state["subagents_seen"] == ["bc-implementer"]


def test_subagent_spawns_is_bounded_and_keeps_the_newest(tmp_path):
    from dark_army_daemon.daemon import MAX_SUBAGENT_SPAWNS
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._update_session_state("session_start", "SessionStart", "s1")
    for n in range(MAX_SUBAGENT_SPAWNS + 5):
        daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                     agent_id=f"role-{n}")
    log = daemon._session_states["s1"]["subagent_spawns"]
    assert len(log) == MAX_SUBAGENT_SPAWNS
    assert log[-1][0] == f"role-{MAX_SUBAGENT_SPAWNS + 4}"
    assert log[0][0] == "role-5"


def test_subagent_spawns_survives_a_prompt_and_a_round_trip(tmp_path):
    from dark_army_daemon.session_store import load_sessions, save_sessions
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._update_session_state("session_start", "SessionStart", "s1")
    daemon._update_session_state("subagent_start", "SubagentStart", "s1",
                                 agent_id="bc-implementer")
    daemon._update_session_state("user_prompt", "UserPromptSubmit", "s1")
    assert len(daemon._session_states["s1"]["subagent_spawns"]) == 1
    path = tmp_path / "round.json"
    save_sessions(daemon._session_states, path)
    assert load_sessions(path)["s1"]["subagent_spawns"][0][0] == "bc-implementer"


def _windowed(tmp_path, **card_fields):
    daemon, store = _board_daemon(tmp_path)
    daemon._run_figures_drifted = lambda: False
    card, _ = store.create({"title": "the work", "project": "bob",
                            "root": "/tmp", "tool": "claude",
                            "column_name": "in_progress"})
    store.bind_session(card["id"], "s1")
    if card_fields:
        store.update(card["id"], card_fields, bump=False)
    return daemon, store, card["id"]


_SNAP = {"running": [{"session_id": "s1"}], "waiting": [],
         "sleeping": [], "finished": [], "abandoned": []}


def test_the_reconcile_records_only_the_stages_inside_the_window(tmp_path):
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path, dispatched_at=100.0,
                                   batch_id="b1", batch_rank="1")
    try:
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagent_spawns": [["bc-planner", 50.0], ["bc-implementer", 120.0],
                                ["bc-verifier", 130.0]],
        }
        assert daemon._reconcile_board(_SNAP) is True
        assert parse_stages(store.get(cid)["agent_trail"]) == [
            "bc-implementer", "bc-verifier"]
        assert daemon._reconcile_board(_SNAP) is False
    finally:
        store.close()


def test_a_done_card_stops_at_its_close(tmp_path):
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path, dispatched_at=100.0,
                                   batch_id="b1", batch_rank="1")
    try:
        store.update(cid, {"column_name": "done", "done_at": 125.0}, bump=False)
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagent_spawns": [["bc-implementer", 120.0],
                                ["bc-verifier", 130.0]],
        }
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == ["bc-implementer"]
        daemon._session_states["s1"]["subagent_spawns"].append(
            ["bc-bug-auditor", 140.0])
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == ["bc-implementer"]
    finally:
        store.close()


def test_a_restored_state_without_the_log_reads_the_seen_list_for_one_card_and_nothing_for_two(
        tmp_path):
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path)
    try:
        daemon._session_states["s1"] = {**_fresh("working"),
                                        "subagents_seen": ["bc-planner"]}
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == ["bc-planner"]
        other, _ = store.create({"title": "second", "project": "bob",
                                 "root": "/tmp", "tool": "claude",
                                 "column_name": "in_progress"})
        store.bind_session(other["id"], "s1")
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(other["id"])["agent_trail"]) == []
    finally:
        store.close()


def test_a_codex_session_on_a_second_card_records_no_trail(tmp_path):
    from types import SimpleNamespace
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path)
    try:
        other, _ = store.create({"title": "second", "project": "bob",
                                 "root": "/tmp", "tool": "codex",
                                 "column_name": "in_progress"})
        store.bind_session(other["id"], "s1")
        daemon._codex_records["s1"] = SimpleNamespace(
            observed_roles=["bc-implementer"])
        assert daemon._record_card_stages(cid, "s1", shared=True) is False
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == []
        assert parse_stages(store.get(other["id"])["agent_trail"]) == []
        # Alone, a Codex card reads as before.
        assert daemon._record_card_stages(cid, "s1") is True
    finally:
        store.close()


def test_a_single_card_reads_the_whole_session_whatever_its_column(tmp_path):
    """No window for a card on its own: a card dragged to Done while its
    session lives, and a spawn stamped before its `dispatched_at`, still
    read as they did before batches."""
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path, dispatched_at=100.0)
    try:
        store.update(cid, {"column_name": "done", "done_at": 125.0}, bump=False)
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagents_seen": ["bc-planner", "bc-implementer", "bc-verifier"],
            "subagent_spawns": [["bc-planner", 0.0], ["bc-implementer", 120.0],
                                ["bc-verifier", 130.0]],
        }
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == [
            "bc-planner", "bc-implementer", "bc-verifier"]
    finally:
        store.close()


def test_a_batch_member_with_no_dispatched_at_yet_records_nothing(tmp_path):
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path, batch_id="b1", batch_rank="2")
    try:
        daemon._session_states["s1"] = {
            **_fresh("working"),
            "subagents_seen": ["bc-implementer"],
            "subagent_spawns": [["bc-implementer", 120.0]],
        }
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == []
    finally:
        store.close()


def test_a_codex_batch_head_alone_records_its_trail(tmp_path):
    """Every batch card carries `batch_id` from the press, but only a session
    with several cards bound is shared: the head alone still gets its roles."""
    from types import SimpleNamespace
    from dark_army_daemon.board import parse_stages
    daemon, store, cid = _windowed(tmp_path, dispatched_at=100.0,
                                   batch_id="b1", batch_rank="1", tool="codex")
    try:
        daemon._session_states["s1"] = _fresh("working")
        daemon._codex_records["s1"] = SimpleNamespace(
            observed_roles=["bc-implementer"])
        daemon._reconcile_board(_SNAP)
        assert parse_stages(store.get(cid)["agent_trail"]) == ["bc-implementer"]
    finally:
        store.close()
