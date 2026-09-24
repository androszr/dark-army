"""A finished card whose manual check is still open stays on the live board.

23 Sep 2026: the phone listed "Rewrite the helper banners…" under Needs you,
and neither the swipe nor Dismiss all could clear it. The card was reviewed
and more than a day in Done, so it had fallen out of the daemon's live board
(the 24-hour preview) while the phone, which holds the whole Done archive,
still read its open manual steps. Every dismiss was answered "that item is
not waiting on you" for a card the daemon no longer carried, and the
acknowledgement pruner would have dropped any ack for it anyway.
"""
import asyncio
import time

import pytest

from dark_army_daemon import inbox_ack
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    d._inbox_acks = inbox_ack.InboxAckStore(tmp_path / "inbox-acks.json")
    try:
        yield d, store
    finally:
        store.close()


def _old_reviewed_done(store, title, steps):
    card, detail = store.create({"title": title, "project": "p", "root": "/tmp/p",
                                 "column_name": "in_progress"})
    assert card is not None, detail
    store.bind_session(card["id"], "sess-" + title)
    store.declare_done(card["id"], "sess-" + title, "done")
    if steps:
        store._conn.execute("UPDATE cards SET manual_steps = ? WHERE id = ?",
                            (steps, card["id"]))
    store.mark_reviewed(card["id"])
    old = time.time() - 86400 * 3
    store._conn.execute("UPDATE cards SET done_at = ?, updated_at = ? WHERE id = ?",
                        (old, old, card["id"]))
    store._conn.commit()
    return card


def test_the_store_read_returns_only_done_cards_with_steps(daemon):
    _d, store = daemon
    checking = _old_reviewed_done(store, "checking", "1. Open it.")
    _old_reviewed_done(store, "clean", "")
    blank = _old_reviewed_done(store, "blank", "   \n")
    got = [c["id"] for c in store.done_manual_check_pending()]
    assert got == [checking["id"]]
    assert blank["id"] not in got


def test_an_old_reviewed_card_with_open_steps_rides_the_frame(daemon):
    d, store = daemon
    checking = _old_reviewed_done(store, "checking", "1. Open it.")
    clean = _old_reviewed_done(store, "clean", "")
    by_id = {c["id"]: c for c in d._build_board_state()["cards"]}
    assert checking["id"] in by_id
    assert by_id[checking["id"]]["manual_check_due"] is True
    # Not stamped as preview: a `?done=review` client keeps receiving it.
    assert not by_id[checking["id"]].get("done_preview")
    assert clean["id"] not in by_id


def test_its_needs_you_entry_can_be_dismissed_and_stays_dismissed(daemon):
    d, store = daemon
    checking = _old_reviewed_done(store, "checking", "1. Open it.")
    d._board_state = d._build_board_state()
    d._agents_snapshot_cache = {}
    key = "c:" + checking["id"]
    fp = inbox_ack.fingerprint("manual_check", "1. Open it.")

    async def wake():
        return None
    d._wake_surfaces = wake
    ok, detail = asyncio.run(d.ack_inbox(key, "manual_check", fp))
    assert ok, detail
    # The pruner keeps it, because the card is still a live subject.
    acks = d.inbox_snapshot()["acks"]
    assert {"key": key, "kind": "manual_check", "fp": fp} in acks
