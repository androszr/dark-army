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
async def test_a_codex_flag_spends_the_fresh_proof_and_nothing_weaker(
        tmp_path, monkeypatch):
    """Open to Codex on the close verb's terms: the flag releases the card's
    place in the queue, so a Codex channel is attributed by the fresh
    native-holder proof alone. The weaker announced-id reading naming the
    session is not enough, and a refused proof writes nothing."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "a card", "project": "bob"})
        store.bind_session(card["id"], "s1")
        daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 4242,
                                   "cwd": "/tmp"}
        monkeypatch.setattr(daemon, "_board_request_session",
                            lambda port: "s1")
        proof = {"sid": None}

        async def fresh(port):
            return proof["sid"]

        monkeypatch.setattr(daemon, "_board_request_session_fresh", fresh)
        reply = await daemon._handle_board_manual_request(
            {"type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert store.get(card["id"])["manual_steps"] == ""

        proof["sid"] = "s1"
        reply = await daemon._handle_board_manual_request(
            {"type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is True and reply["card_id"] == card["id"]
        assert store.get(card["id"])["manual_steps"] == _STEPS
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
async def test_a_card_already_done_can_still_be_flagged(tmp_path):
    """A card with an open check goes to Done, so the order an agent picks —
    flag then close, or close then flag — must not leave a check
    unrecorded: the session's one Done card *it closed itself* takes the
    flag, and stays in Done."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        store.declare_done(card["id"], "s1", "checked")
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is True, reply
        assert reply["card_id"] == card["id"]
        flagged = store.get(card["id"])
        assert flagged["manual_steps"] == _STEPS
        assert flagged["column_name"] == "done"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_done_card_somebody_else_closed_is_still_refused(tmp_path):
    """The fallback is `closed_by == session_id`, never "a Done card of this
    session": a card a person dragged to Done keeps the old refusal."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        store.update(card["id"], {"column_name": "done"})
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert "already done" in reply["detail"]
        assert store.get(card["id"])["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_that_closed_two_cards_is_ambiguous(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        ids = []
        for title in ("one", "two"):
            card, _ = store.create({"title": title, "project": "bob"})
            store.bind_session(card["id"], "s1")
            store.declare_done(card["id"], "s1", "done")
            ids.append(card["id"])
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply["ok"] is False
        assert "more than one card" in reply["detail"]
        assert all(store.get(i)["manual_steps"] == "" for i in ids)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_closing_a_card_that_carries_an_open_check_succeeds(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        closed, detail = await daemon.close_card_by_session("s1", "done")
        assert closed is not None, detail
        assert closed["column_name"] == "done"
        assert closed["manual_steps"] == _STEPS
    finally:
        store.close()


# --- the check file the flag names ------------------------------------------

_CHECK = """# The strip fits

- **Card:** mine
- **Project:** bob
- **Check:** the strip fits
- **Created:** 2026-09-25T14:32:00+02:00
- **Status:** open
- **Outcome:** none
- **Checked at:** none

## Steps

1. Open the menu bar.

## Why not automated

A real screen.
"""


def _project(tmp_path, text=_CHECK, folder="2026-09-25-strip"):
    import os
    root = tmp_path / "proj"
    target = root / "manual-check" / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / "check.md").write_text(text)
    return os.path.realpath(root), os.path.realpath(target / "check.md")


def _flag(daemon, path):
    return daemon._handle_board_manual_request({
        "type": "board_manual_request", "port": 51000, "steps": _STEPS,
        "path": path})


@pytest.mark.asyncio
async def test_path_is_stored_as_its_realpath(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root, check = _project(tmp_path)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        # A relative path resolves inside the card's own root.
        reply = await _flag(daemon, "manual-check/2026-09-25-strip/check.md")
        assert reply["ok"] is True, reply
        flagged = store.get(card["id"])
        assert flagged["manual_check_path"] == check
        assert flagged["manual_steps"] == _STEPS
        assert [c["id"] for c in store.by_manual_check_path(check)] \
            == [card["id"]]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_check_in_a_card_side_folder_is_kept_at_the_main_checkout(
        tmp_path, monkeypatch):
    """Inside a card worktree "the project root" is the worktree, so the
    file lands there; the flag copies it to the main checkout's place,
    which outlives the folder, and stores that path."""
    import os
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root = tmp_path / "proj"
        side = root / ".worktrees" / "card-1234" / "manual-check" / "2026-09-25-strip"
        side.mkdir(parents=True)
        (side / "check.md").write_text(_CHECK)
        root = os.path.realpath(root)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        reply = await _flag(daemon, str(side / "check.md"))
        assert reply["ok"] is True, reply
        kept = os.path.join(root, "manual-check", "2026-09-25-strip", "check.md")
        assert store.get(card["id"])["manual_check_path"] == kept
        with open(kept) as fh:
            assert fh.read() == _CHECK
        # A different check already at that place is refused, never replaced.
        (side / "check.md").write_text(_CHECK.replace("Open the menu bar",
                                                      "Open the panel"))
        reply = await _flag(daemon, str(side / "check.md"))
        assert reply["ok"] is False
        assert "different check" in daemon._manual_check_hoist(
            str(side / "check.md"), kept)
        with open(kept) as fh:
            assert fh.read() == _CHECK
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_check_outside_the_folder_is_refused_in_words(
        tmp_path, monkeypatch):
    from dark_army_daemon import board
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root, _check = _project(tmp_path)
        _enrolled(monkeypatch, root)
        stray = tmp_path / "proj" / "docs" / "check.md"
        stray.parent.mkdir()
        stray.write_text(_CHECK)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        reply = await _flag(daemon, str(stray))
        assert reply["ok"] is False
        assert reply["detail"] == board.MANUAL_CHECK_PLACE_REFUSAL
        outside = tmp_path / "elsewhere.md"
        outside.write_text(_CHECK)
        reply = await _flag(daemon, str(outside))
        assert reply["ok"] is False
        assert reply["detail"] == board.MANUAL_CHECK_PLACE_REFUSAL
        after = store.get(card["id"])
        assert after["manual_steps"] == "" and after["manual_check_path"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_malformed_check_is_refused_naming_the_problem(
        tmp_path, monkeypatch):
    from dark_army_daemon import board
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root, check = _project(
            tmp_path, _CHECK.replace("- **Status:** open\n", ""))
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        reply = await _flag(daemon, check)
        assert reply["ok"] is False
        assert reply["detail"].startswith(board.MANUAL_CHECK_MALFORMED_REFUSAL)
        assert "missing Status" in reply["detail"]
        assert store.get(card["id"])["manual_steps"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_no_path_flags_exactly_as_before(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS})
        assert reply == {"ok": True, "detail": "manual check flagged",
                         "card_id": card["id"], "column": "in_progress",
                         "title": "mine"}
        flagged = store.get(card["id"])
        assert flagged["manual_steps"] == _STEPS
        assert flagged["manual_check_path"] == ""
    finally:
        store.close()


# --- recording the outcome ---------------------------------------------------


def _enrolled(monkeypatch, *roots):
    import os
    from dark_army_daemon import enrollment
    members = {os.path.realpath(r) for r in roots}
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(members))


@pytest.mark.asyncio
async def test_recording_clears_the_linked_card_and_writes_the_file(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root, check = _project(tmp_path)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        assert (await _flag(daemon, check))["ok"] is True
        store.declare_done(card["id"], "s1", "done")
        ok, detail = await daemon.record_manual_outcome(check, "failed", "no")
        assert ok is True, detail
        after = store.get(card["id"])
        assert after["manual_steps"] == ""
        # Failed moves no card: Reopen is the person's.
        assert after["column_name"] == "done"
        text = open(check).read()
        assert "- **Status:** failed" in text
        assert "- **Outcome:** no" in text
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_check_with_no_card_records_and_clears_nothing(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path)
    try:
        root, check = _project(tmp_path)
        _enrolled(monkeypatch, root)
        other, _ = store.create({"title": "other", "project": "bob"})
        store.bind_session(other["id"], "s2")
        store.flag_manual(other["id"], "s2", _STEPS)
        ok, detail = await daemon.record_manual_outcome(check, "passed", "")
        assert ok is True, detail
        assert "- **Status:** passed" in open(check).read()
        assert store.get(other["id"])["manual_steps"] == _STEPS
    finally:
        store.close()


@pytest.mark.asyncio
async def test_recording_outside_an_enrolled_root_writes_nothing(
        tmp_path, monkeypatch):
    from dark_army_daemon import board
    daemon, store = _board_daemon(tmp_path)
    try:
        _root, check = _project(tmp_path)
        _enrolled(monkeypatch, str(tmp_path / "somewhere-else"))
        ok, detail = await daemon.record_manual_outcome(check, "passed", "")
        assert ok is False
        assert detail == board.MANUAL_CHECK_PLACE_REFUSAL
        assert open(check).read() == _CHECK
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


# --- flag, list and press agree on one place (audit, 25 Sep 2026) ----------


@pytest.mark.asyncio
async def test_a_differently_cased_folder_is_refused_at_the_flag(
        tmp_path, monkeypatch):
    """A realpath keeps the case it was typed in, so a folder spelled
    `Manual-Check` would be stored under a path the list never joins to:
    the flag refuses it, exactly as the press does."""
    import os
    from dark_army_daemon import board
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root = tmp_path / "proj"
        (root / "Manual-Check" / "2026-09-25-strip").mkdir(parents=True)
        (root / "Manual-Check" / "2026-09-25-strip" / "check.md").write_text(_CHECK)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": os.path.realpath(root)})
        store.bind_session(card["id"], "s1")
        reply = await _flag(
            daemon, os.path.realpath(root) + "/Manual-Check/2026-09-25-strip/check.md")
        assert reply["ok"] is False
        assert reply["detail"] == board.MANUAL_CHECK_PLACE_REFUSAL
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_card_in_a_subfolder_flags_the_enrolled_roots_check(
        tmp_path, monkeypatch):
    """A card whose root is a subfolder of the enrolled project flags the
    project's own check; a `manual-check/` under the subfolder is not a
    place the list or the press reach, so the flag refuses it too."""
    import os
    from dark_army_daemon import board
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root, check = _project(tmp_path)
        sub = os.path.join(root, "host")
        os.makedirs(os.path.join(sub, "manual-check", "x"))
        with open(os.path.join(sub, "manual-check", "x", "check.md"), "w") as f:
            f.write(_CHECK)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob", "root": sub})
        store.bind_session(card["id"], "s1")
        reply = await _flag(daemon, os.path.join(sub, "manual-check", "x", "check.md"))
        assert reply["ok"] is False
        assert reply["detail"] == board.MANUAL_CHECK_PLACE_REFUSAL
        reply = await _flag(daemon, check)
        assert reply["ok"] is True, reply
        assert store.get(card["id"])["manual_check_path"] == check
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_already_recorded_file_still_settles_its_card(
        tmp_path, monkeypatch):
    """A file somebody recorded by hand (or from an older phone that only
    cleared another card) must not leave a card wearing the badge for ever
    with Mark checked hidden: the press is refused in words and the card
    is cleared anyway."""
    from dark_army_daemon import board
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        root, check = _project(tmp_path)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        assert (await _flag(daemon, check))["ok"] is True
        edited = open(check).read().replace("- **Status:** open",
                                            "- **Status:** passed")
        open(check, "w").write(edited)
        ok, detail = await daemon.record_manual_outcome(check, "failed", "")
        assert ok is False
        assert detail == board.MANUAL_OUTCOME_RECORDED_REFUSAL
        assert store.get(card["id"])["manual_steps"] == ""
        assert open(check).read() == edited
    finally:
        store.close()


def test_two_concurrent_presses_write_exactly_once(tmp_path, monkeypatch):
    """Two presses racing past the `Status: open` re-check: the daemon holds
    one lock across place check, re-read and write, so exactly one lands and
    the other is refused in words."""
    import threading
    from dark_army_daemon import board, manual_check
    daemon, store = _board_daemon(tmp_path)
    try:
        root, check = _project(tmp_path)
        _enrolled(monkeypatch, root)
        barrier = threading.Barrier(2)
        real_read = manual_check.read_header

        def slow_read(path):
            header = real_read(path)
            try:
                barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            return header

        events = []
        real_write = manual_check.write_outcome

        def logged_read(path):
            events.append("read")
            return slow_read(path)

        def logged_write(*args, **kwargs):
            events.append("write-start")
            try:
                return real_write(*args, **kwargs)
            finally:
                events.append("write-end")

        monkeypatch.setattr(manual_check, "read_header", logged_read)
        monkeypatch.setattr(manual_check, "write_outcome", logged_write)
        results = []

        def press(status):
            results.append(daemon._record_manual_outcome_sync(
                check, status, "")[2])

        threads = [threading.Thread(target=press, args=(s,))
                   for s in ("passed", "failed")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert sorted(results) == sorted(
            ["", board.MANUAL_OUTCOME_RECORDED_REFUSAL])
        # The second re-read starts only after the first write finished.
        assert events == ["read", "write-start", "write-end", "read"], events
        text = open(check).read()
        assert text.count("- **Status:** passed") + \
            text.count("- **Status:** failed") == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_wrong_cased_dated_folder_is_stored_as_the_disk_spells_it(
        tmp_path, monkeypatch):
    """A flag naming `2026-09-25-STRIP` for the folder the disk calls
    `2026-09-25-strip` is stored in the disk's spelling, so the list's path
    joins to the card and a press from the Checks window clears it."""
    import os
    root, check = _project(tmp_path)
    shouted = check.replace("2026-09-25-strip", "2026-09-25-STRIP")
    if not os.path.exists(shouted):
        pytest.skip("needs a case-insensitive volume")
    daemon, store = _board_daemon(tmp_path)
    try:
        _bind(daemon, store)
        _enrolled(monkeypatch, root)
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        reply = await _flag(daemon, shouted)
        assert reply["ok"] is True, reply
        assert store.get(card["id"])["manual_check_path"] == check
        listed = await daemon.manual_checks_report()
        assert [c["path"] for c in listed["checks"]] == [check]
        assert listed["checks"][0]["card_id"] == card["id"]
        ok, detail = await daemon.record_manual_outcome(
            listed["checks"][0]["path"], "passed", "fine")
        assert ok is True, detail
        assert store.get(card["id"])["manual_steps"] == ""
        # A press naming the file in yet another spelling reaches it too.
        assert daemon._canonical_path(shouted) == check
    finally:
        store.close()


# --- a person's hand on a finished card clears Needs you, like Dismiss -------


async def _done_card_with_check(daemon, store):
    """25 Sep 2026's shape: the agent flagged a hand-check, closed its own
    card into Done and finished its turn — so Needs you holds the card's
    check and the agent's plain finished wait."""
    _bind(daemon, store)
    card, _ = store.create({"title": "mine", "project": "bob"})
    store.bind_session(card["id"], "s1")
    await daemon._handle_board_manual_request({
        "type": "board_manual_request", "port": 51000, "steps": _STEPS})
    closed, detail = await daemon.close_card_by_session("s1", "tests pass")
    assert closed is not None, detail
    daemon._session_states["s1"].update(
        {"state": "idle", "last_event_monotonic": 1.0})
    daemon._active_notifications["s1"] = {"session_id": "s1", "hook": "Stop"}
    daemon._refresh_board_state()
    assert daemon._inbox_live("c:" + card["id"])[0] == "manual_check"
    assert daemon._inbox_live("s:s1")[0] == "waiting"
    return card


def _hidden(daemon):
    return {a["key"]: a["kind"] for a in daemon.inbox_snapshot()["acks"]}


@pytest.mark.asyncio
async def test_mark_reviewed_clears_the_card_and_its_agent_from_needs_you(
        tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card = await _done_card_with_check(daemon, store)
        reviewed, detail = await daemon.review_card(card["id"])
        assert reviewed is not None, detail
        assert _hidden(daemon) == {"c:" + card["id"]: "manual_check",
                                   "s:s1": "waiting"}
        assert "s1" not in daemon._active_notifications
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mark_checked_also_clears_the_agents_finished_wait(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        card = await _done_card_with_check(daemon, store)
        ok, detail = await daemon.clear_manual_check(card["id"])
        assert ok is True, detail
        assert _hidden(daemon).get("s:s1") == "waiting"
        assert "s1" not in daemon._active_notifications
    finally:
        store.close()


@pytest.mark.asyncio
async def test_acknowledge_and_close_clears_a_card_already_in_done(
        tmp_path, monkeypatch):
    """The agent closed its own card, so the person's close finishes
    nothing — and still takes the card's entry off Needs you."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card = await _done_card_with_check(daemon, store)

        async def closed(_sid):
            return True, ""
        monkeypatch.setattr(daemon, "_close_session_terminal", closed)
        ok, detail = await daemon.close_session_terminal("s1", by_person=True)
        assert ok is True, detail
        assert _hidden(daemon).get("c:" + card["id"]) == "manual_check"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_review_leaves_a_live_question_on_needs_you(tmp_path):
    """Reviewing the work answers nothing the agent is asking."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card = await _done_card_with_check(daemon, store)
        daemon._active_notifications.pop("s1", None)
        daemon._agents_snapshot_cache = {
            "running": [], "sleeping": [], "abandoned": [], "finished": [],
            "waiting": [{"session_id": "s1",
                         "questions": [{"id": "t1", "text": "ok?"}]}]}
        assert daemon._inbox_live("s:s1")[0] == "question"
        reviewed, detail = await daemon.review_card(card["id"])
        assert reviewed is not None, detail
        assert "s:s1" not in _hidden(daemon)
    finally:
        store.close()
