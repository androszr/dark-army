# host/tests/test_manual_check.py
"""The outstanding hand-check, end to end: the channel verb, the session-scoped
resolution on the daemon, and the person's press that clears it.

The store's own guards live in `test_board.py` and the tool schema in
`test_channel.py`; what is tested here is the join — that the card a session
reaches is only ever its own, and that the only route back out is a human's.
"""
import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


def _daemon():
    return BobDaemon(headless=True)


def _board_daemon(tmp_path):
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


def _attach(daemon, port=51000, pid=4242, session_id=""):
    return daemon._handle_channel_message(
        {"type": "channel_attach", "port": port, "pid": pid, "cwd": "/tmp",
         "session_id": session_id, "is_channel": True, "secret": "",
         "host": cs.HOST_CLAUDE})


def _bind(daemon, store, session_id="s1", port=51000, pid=4242):
    _attach(daemon, port=port, pid=pid, session_id=session_id)
    daemon._session_states[session_id] = {"pid": pid, "last_event": 0}


_STEPS = ("1. Open Dark Army's panel and look at the board.\n"
          "2. Expect the flagged card to read MANUAL CHECK NEEDED.\n"
          "Why not automated: it is a look at a real screen.")


@pytest.mark.asyncio
async def test_a_session_flags_the_one_card_it_is_bound_to(tmp_path):
    """The scope is the whole argument: the message carries no card id, and
    the card is found solely by asking which session is on the other end of
    the port."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        mine, _ = store.create({"title": "mine", "project": "bob"})
        theirs, _ = store.create({"title": "theirs", "project": "bob"})
        store.bind_session(mine["id"], "s1")
        store.bind_session(theirs["id"], "s2")
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is True
        assert reply["card_id"] == mine["id"]
        flagged = store.get(mine["id"])
        assert flagged["manual_steps"] == _STEPS
        # And it moved nothing: In progress goes on meaning an assistant is
        # working, and the badge is what says a person is.
        assert flagged["column_name"] == "in_progress"
        assert store.get(theirs["id"])["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_flag_bob_cannot_attribute_is_refused(tmp_path):
    """The forgery case. An unattributable request writes nothing at all."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "a card", "project": "bob"})
        store.bind_session(card["id"], "s1")
        reply = await daemon._handle_board_manual_request(
            {"type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert store.get(card["id"])["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_on_no_card_is_refused_in_words(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        # An unbound card on the board, which is what `by_session("")` would
        # have matched without its guard.
        store.create({"title": "nobody's", "project": "bob"})
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert "no card" in reply["detail"]
        assert store.cards()[0]["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_on_two_live_cards_fails_closed(tmp_path):
    """Ambiguity is refused rather than guessed at — `close_card_by_session`'s
    rule, and deliberately the same one: these two verbs are the same session
    saying opposite things about the same card, so a difference in which card
    they reach would be a bug with no argument behind it."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        a, _ = store.create({"title": "one", "project": "bob"})
        b, _ = store.create({"title": "two", "project": "bob"})
        store.bind_session(a["id"], "s1")
        store.bind_session(b["id"], "s1")
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert "more than one" in reply["detail"]
        assert store.get(a["id"])["manual_steps"] == ""
        assert store.get(b["id"])["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_card_already_done_cannot_be_flagged(tmp_path):
    """A check outstanding on work somebody has already accepted is a
    contradiction, and the honest route there is Reopen."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        store.declare_done(card["id"], "s1", "checked")
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert "already done" in reply["detail"]
        assert store.get(card["id"])["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_person_clears_it_and_the_card_stays_where_it_is(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        ok, detail = await daemon.clear_manual_check(card["id"])
        assert ok is True, detail
        cleared = store.get(card["id"])
        assert cleared["manual_steps"] == ""
        assert cleared["column_name"] == "in_progress"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_person_clearing_a_check_settles_the_ended_run_under_it(tmp_path):
    """20 Sep 2026: a card whose run had ended also carried a hand-check. The
    Needs you row was the ended run's (`card_kind_and_fp` ranks it first);
    Mark checked cleared the steps and the row stayed, because the press had
    touched the check alone. The press now acknowledges what the card is
    still live under, so the item the person just handled does not come
    back under the same word."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        # The session finished its turn and went quiet until the staleness
        # sweep evicted it: out of the hook-stream set, not mid-turn, its
        # card still In progress with a live link.
        daemon._session_states.pop("s1")
        daemon._agents_snapshot_cache = _running()
        daemon._refresh_board_state()
        row = [c for c in daemon._board_state["cards"]
               if c["id"] == card["id"]][0]
        assert row["needs_you"] is True and row["manual_check_due"] is True
        assert daemon.inbox_snapshot()["acks"] == []
        ok, detail = await daemon.clear_manual_check(card["id"])
        assert ok is True, detail
        acks = daemon.inbox_snapshot()["acks"]
        assert [(a["key"], a["kind"]) for a in acks] == [
            ("c:" + card["id"], "ended_work")]
        # The same ack Dismiss would have written: it matches what the card
        # is live under now, so the row is hidden until that changes.
        assert daemon._inbox_live("c:" + card["id"]) == (
            "ended_work", acks[0]["fp"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_clearing_a_check_on_a_working_card_hides_nothing(tmp_path):
    """The assistant is still busy: the card is live under no Needs you kind
    once the check is gone, and nothing is written to the hide list — a
    later ended run still asks for a person."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        daemon._agents_snapshot_cache = _running("s1")
        daemon._session_states["s1"]["last_event"] = __import__("time").time()
        ok, detail = await daemon.clear_manual_check(card["id"])
        assert ok is True, detail
        assert daemon.inbox_snapshot()["acks"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_clearing_a_card_with_nothing_outstanding_is_refused(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        ok, detail = await daemon.clear_manual_check(card["id"])
        assert ok is False
        assert "no manual check" in detail
        assert (await daemon.clear_manual_check("nope"))[0] is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_flag_rides_the_board_snapshot(tmp_path):
    """The steps reach the panel on the frame the card already rides — bounded
    at the store, inside the budget `_trim_card_for_snapshot` leaves, so no
    frame arithmetic changes."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        state = daemon._build_board_state()
        rows = [c for c in state["cards"] if c["id"] == card["id"]]
        assert rows and rows[0]["manual_steps"] == _STEPS
    finally:
        store.close()


# --- The badge waits its turn (2026-08-30) --------------------------------
#
# The note is stored the moment an assistant says it; the amber line is drawn
# only once that assistant has stopped. `_card_session_working` is the one
# predicate behind both, and these are its corners.


def _running(*session_ids):
    """An agents snapshot whose `running` bucket holds these sessions."""
    return {"running": [{"session_id": sid} for sid in session_ids],
            "waiting": [], "sleeping": [], "finished": []}


def test_the_running_bucket_is_read_defensively():
    """`_board_running_ids`' sibling shape: dict rows carrying an id only, and
    the other three buckets are not `running` and say nothing here."""
    snapshot = {"running": [{"session_id": "s1"}, {"session_id": ""},
                            "not a dict", {"pid": 3}],
                "waiting": [{"session_id": "s9"}]}
    assert BobDaemon._board_running_ids(snapshot) == {"s1"}
    assert BobDaemon._board_running_ids({}) == set()


def test_a_dispatching_card_counts_as_working():
    """A re-Start on a flagged card is the assistant resuming. Hiding through
    the bind window is what stops the badge flashing while the terminal
    opens; a failed bind clears the link and the badge returns on its own."""
    card = {"link_state": "dispatching", "session_id": ""}
    assert BobDaemon._card_session_working(card, set(), set()) is True


def test_a_live_session_in_the_running_bucket_is_working():
    card = {"link_state": "live", "session_id": "s1"}
    assert BobDaemon._card_session_working(card, {"s1"}, {"s1"}) is True


def test_a_live_session_that_is_waiting_is_not_working():
    """The badge's whole point: a turn that ended on a question is exactly
    when somebody should do the hand-check."""
    card = {"link_state": "live", "session_id": "s1"}
    assert BobDaemon._card_session_working(card, set(), {"s1"}) is False


def test_a_roster_stub_is_quiet_and_therefore_badged():
    """The documented wedge, decided in the badge's favour: a session evicted
    from the hook stream still shows in the `running` bucket for as long as
    its terminal stays open, and `claude agents --json` calls it busy. Absent
    from `_claiming_session_ids()` it is quiet, so the note is due."""
    card = {"link_state": "live", "session_id": "s1"}
    assert BobDaemon._card_session_working(card, {"s1"}, set()) is False


def test_an_ended_or_unlinked_card_is_not_working():
    assert BobDaemon._card_session_working(
        {"link_state": "ended", "session_id": "s1"}, {"s1"}, {"s1"}) is False
    assert BobDaemon._card_session_working(
        {"link_state": "", "session_id": ""}, set(), set()) is False
    # Live, but no session id to be working under.
    assert BobDaemon._card_session_working(
        {"link_state": "live", "session_id": ""}, set(), set()) is False


@pytest.mark.asyncio
async def test_a_working_card_publishes_no_badge(tmp_path):
    """The decoration end to end: the steps still ride the frame (the sheet
    draws them regardless), and only the drawn bit is withheld."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        daemon._agents_snapshot_cache = _running("s1")
        row = [c for c in daemon._build_board_state()["cards"]
               if c["id"] == card["id"]][0]
        assert row["manual_steps"] == _STEPS
        assert row["manual_check_due"] is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_stopped_card_publishes_the_badge(tmp_path):
    """The same card once its session leaves the running bucket."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        daemon._agents_snapshot_cache = _running()
        row = [c for c in daemon._build_board_state()["cards"]
               if c["id"] == card["id"]][0]
        assert row["manual_check_due"] is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_bob_cannot_hear_publishes_the_badge(tmp_path):
    """The roster stub through the decoration: in the bucket, out of the
    active set."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        daemon._agents_snapshot_cache = _running("s1")
        daemon._session_states.pop("s1", None)
        row = [c for c in daemon._build_board_state()["cards"]
               if c["id"] == card["id"]][0]
        assert row["manual_check_due"] is True
    finally:
        store.close()


def test_an_unflagged_card_never_carries_the_badge(tmp_path):
    """The invariant: non-empty steps *is* the flag, so a card with none can
    never publish a due bit, whatever its session is doing."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        daemon._agents_snapshot_cache = _running()
        row = [c for c in daemon._build_board_state()["cards"]
               if c["id"] == card["id"]][0]
        assert row["manual_check_due"] is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_category_flip_alone_buys_a_frame(tmp_path):
    """A session's category flipping writes no board row, so without this the
    badge would freeze at whatever the last board *write* published."""
    daemon, store = _board_daemon(tmp_path)
    try:
        # The cost-and-time line's own drift term is a sibling with its own
        # tests (`test_run_figures.py`); hold it still so this asserts on
        # the manual-check bit alone.
        daemon._run_figures_drifted = lambda: False
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        working = _running("s1")
        daemon._reconcile_board(working)
        assert daemon._reconcile_board(working) is False
        # The session stops; nothing on the board moved, and the reconcile
        # still reports a change so `_publish_board` fires.
        assert daemon._reconcile_board(_running()) is True
    finally:
        store.close()


def test_a_quiet_board_still_buys_no_frame(tmp_path):
    """The drift map is restricted to flagged cards and compared on the
    decided bit, or every unflagged card's session churn would republish the
    board on every agents push."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        daemon._reconcile_board(_running("s1"))
        assert daemon._reconcile_board(_running()) is False
    finally:
        store.close()
