"""One published answer for cards whose assistant has gone, on both clients."""
from pathlib import Path

import pytest

from dark_army_daemon import board_queue
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


def _board_daemon(tmp_path):
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


def _bind(daemon, store, session_id="s1"):
    daemon._session_states[session_id] = {"pid": 4242, "last_event": 0}


def _running(*session_ids):
    return {"running": [{"session_id": sid} for sid in session_ids],
            "waiting": [], "sleeping": [], "finished": []}


@pytest.mark.parametrize("fields, active, expected", [
    ({"link_state": "ended"}, None, True),
    ({"link_state": "live"}, {"s1"}, False),
    ({"link_state": "live"}, set(), True),
    ({"link_state": "live"}, None, False),
    ({"link_state": "dispatching"}, set(), False),
    ({"session_id": ""}, set(), False),
    ({"column_name": "backlog"}, set(), False),
    ({"column_name": "prep"}, set(), False),
    ({"closed_by": "s1"}, set(), False),
    ({"column_name": "done"}, set(), False),
    ({"link_state": ""}, set(), False),
])
def test_predicate(fields, active, expected):
    card = {"column_name": "in_progress", "session_id": "s1",
            "link_state": "ended", "closed_by": ""}
    card.update(fields)
    assert board_queue.needs_you(card, active) is expected


def test_no_card_needs_nobody():
    assert board_queue.needs_you(None, set()) is False


@pytest.mark.parametrize("mid_turn, expected", [
    (None, True), (set(), True), ({"other"}, True), ({"s1"}, False),
])
def test_a_session_lost_mid_turn_is_working_not_lost(mid_turn, expected):
    """Silence inside a turn is a long tool call, not an abandoned card."""
    card = {"column_name": "in_progress", "session_id": "s1",
            "link_state": "live", "closed_by": ""}
    assert board_queue.needs_you(card, set(), mid_turn) is expected


def test_an_ended_link_asks_for_a_person_whatever_the_last_state_was():
    """The mid-turn reading softens silence alone; a real end still counts."""
    card = {"column_name": "in_progress", "session_id": "s1",
            "link_state": "ended", "closed_by": ""}
    assert board_queue.needs_you(card, set(), {"s1"}) is True


def _tombstone(daemon, sid, reason, state):
    daemon._finished[sid] = {"end_reason": reason, "stub": {"state": state},
                             "finished_at": 0.0, "finished_mono": 0.0}


@pytest.mark.parametrize("reason, state, expected", [
    # Wall-clock eviction mid-turn: the terminal, the process and the work
    # are all where they were, so the card is not the person's problem yet.
    ("evicted", "working", False),
    ("evicted", "thinking", False),
    # Quiet because the turn ended — the case the predicate was built for.
    ("evicted", "idle", True),
    ("evicted", "", True),
    # A dead process is a real end, never merely our bookkeeping.
    ("no process", "working", True),
])
def test_the_snapshot_reads_the_tombstone(tmp_path, reason, state, expected):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        _bind(daemon, store)
        daemon._agents_snapshot_cache = _running("s1")
        daemon._session_states.pop("s1")
        _tombstone(daemon, "s1", reason, state)
        row = next(c for c in daemon._build_board_state()["cards"]
                   if c["id"] == card["id"])
        assert row["needs_you"] is expected
    finally:
        store.close()


def test_a_session_evicted_mid_turn_buys_no_needs_you_frame(tmp_path):
    """The drift check reads the same tombstone the snapshot does."""
    daemon, store = _board_daemon(tmp_path)
    try:
        # The cost-and-time line's own drift term is a sibling with its own
        # tests (`test_run_figures.py`); hold it still so this asserts on
        # the needs-you bit alone.
        daemon._run_figures_drifted = lambda: False
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        _bind(daemon, store)
        daemon._reconcile_board(_running("s1"))
        daemon._session_states.pop("s1")
        _tombstone(daemon, "s1", "evicted", "working")
        assert daemon._reconcile_board(_running("s1")) is False
        assert daemon._needs_you_seen == {card["id"]: False}
        # The tombstone expiring is a change the person must see.
        daemon._finished.pop("s1")
        assert daemon._reconcile_board(_running("s1")) is True
        assert daemon._needs_you_seen == {card["id"]: True}
    finally:
        store.close()


@pytest.mark.parametrize("state, active, expected", [
    ("ended", False, True), ("live", True, False), ("live", False, True),
])
def test_snapshot_publishes_the_decision(tmp_path, state, active, expected):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        _bind(daemon, store)
        daemon._agents_snapshot_cache = _running("s1")
        if not active:
            daemon._session_states.pop("s1")
        if state == "ended":
            store.mark_ended(card["id"])
        row = next(c for c in daemon._build_board_state()["cards"]
                   if c["id"] == card["id"])
        assert row["needs_you"] is expected
        assert "needs_you" not in store.get(card["id"])
    finally:
        store.close()


def test_a_waiting_session_is_still_present(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        _bind(daemon, store)
        daemon._session_states["s1"]["state"] = "waiting"
        daemon._agents_snapshot_cache = {"waiting": [{"session_id": "s1"}]}
        assert daemon._build_board_state()["cards"][0]["needs_you"] is False
    finally:
        store.close()


def test_a_live_card_going_quiet_alone_buys_a_frame(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        # The cost-and-time line's own drift term is a sibling with its own
        # tests (`test_run_figures.py`); hold it still so this asserts on
        # the needs-you bit alone.
        daemon._run_figures_drifted = lambda: False
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        _bind(daemon, store)
        daemon._reconcile_board(_running("s1"))
        assert daemon._reconcile_board(_running("s1")) is False
        daemon._session_states.pop("s1")
        assert daemon._reconcile_board(_running("s1")) is True
        assert daemon._needs_you_seen == {card["id"]: True}
        assert daemon._reconcile_board(_running("s1")) is False
        _bind(daemon, store)
        assert daemon._reconcile_board(_running("s1")) is True
        assert daemon._needs_you_seen == {card["id"]: False}
    finally:
        store.close()


def test_prep_cards_do_not_enter_the_drift_map(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        store.mark_ended(card["id"])
        store.update(card["id"], {"column_name": "prep"})
        assert daemon._needs_you_drifted(store.cards(), active=set()) is False
        assert daemon._needs_you_seen == {}
    finally:
        store.close()


def test_existing_verbs_are_chosen_on_both_phone_doors():
    for verb in ("board_update", "board_reset"):
        assert verb in ApiServer.LAN_ACTIONS
        assert verb in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_send_back_rule_matches_dispatch_expiry():
    root = Path(__file__).resolve().parents[2]
    panel = (root / "panel/Sources/BobPanel/BoardModels.swift").read_text()
    daemon = (root / "host/dark_army_daemon/daemon_board.py").read_text()
    assert "var sendBackColumn: BoardColumn { planPath.isEmpty ? .prep : .backlog }" in panel
    assert '"backlog" if str(card.get("plan_path")' in daemon
