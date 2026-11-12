"""The steering queue's table in board.db (`docs/steering-queue.md`)."""
import json
import re
import sqlite3
import time

import pytest

from dark_army_daemon import board as board_mod
from dark_army_daemon import board_journal_store, steering
from dark_army_daemon.board import SCHEMA_VERSION, BoardStore


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    yield s
    s.close()


def _entry(**kw):
    e = {"session_id": "s1", "kind": "reply", "source": "mac", "dedupe": "",
         "payload": {"text": "hello"}, "action_id": "a1"}
    e.update(kw)
    return e


def test_enqueue_mints_a_32_hex_id_and_keeps_the_words(store):
    row = store.steer_enqueue(_entry())
    assert re.fullmatch(r"[0-9a-f]{32}", row["id"])
    assert row["payload"] == {"text": "hello"}
    assert row["state"] == "queued"


def test_a_forbidden_key_two_levels_down_raises_and_writes_nothing(store):
    with pytest.raises(ValueError):
        store.steer_enqueue(_entry(payload={"a": {"b": {"token": "x"}}}))
    assert store.steer_counts() == {}


def test_resolve_blanks_the_words_and_a_second_resolve_is_refused(store):
    row = store.steer_enqueue(_entry())
    assert store.steer_resolve(row["id"], "typed", True, "") is True
    got = store.steer_get(row["id"])
    assert got["payload"] == {} and got["state"] == "typed" and got["ok"]
    raw = store._conn.execute(
        "SELECT payload FROM steer_queue WHERE id=?", (row["id"],)).fetchone()
    assert raw[0] == ""
    assert store.steer_resolve(row["id"], "refused", False, "x") is False
    assert store.steer_get(row["id"])["state"] == "typed"


def test_queued_is_oldest_first(store):
    now = time.time()
    b = store.steer_enqueue(_entry(created_at=now - 1, payload={"text": "b"}))
    a = store.steer_enqueue(_entry(created_at=now - 5, payload={"text": "a"}))
    assert [r["id"] for r in store.steer_queued()] == [a["id"], b["id"]]


def test_by_dedupe_finds_a_row_inside_the_window_only(store):
    now = time.time()
    store.steer_enqueue(_entry(dedupe="d" * 32, created_at=now - 100))
    assert store.steer_by_dedupe("d" * 32, now - 900)["dedupe"] == "d" * 32
    assert store.steer_by_dedupe("d" * 32, now - 50) is None
    assert store.steer_by_dedupe("", 0) is None
    assert store.steer_by_dedupe("e" * 32, 0) is None


def test_prune_drops_old_resolved_rows_and_keeps_every_queued_one(store):
    now = time.time()
    old = store.steer_enqueue(_entry(created_at=now - 5000))
    store.steer_resolve(old["id"], "typed", True)
    store._conn.execute("UPDATE steer_queue SET resolved_at=? WHERE id=?",
                        (now - 1000, old["id"]))
    store._conn.commit()
    fresh = store.steer_enqueue(_entry())
    store.steer_resolve(fresh["id"], "typed", True)
    ancient = store.steer_enqueue(_entry(created_at=now - 99999))
    assert store.steer_prune(now) == 1
    assert store.steer_get(old["id"]) is None
    assert store.steer_get(fresh["id"]) is not None
    assert store.steer_get(ancient["id"])["state"] == "queued"


def test_prune_caps_resolved_rows(store, monkeypatch):
    monkeypatch.setattr(steering, "MAX_STEER_ROWS", 3)
    ids = []
    for i in range(6):
        r = store.steer_enqueue(_entry())
        store.steer_resolve(r["id"], "typed", True)
        ids.append(r["id"])
    queued = store.steer_enqueue(_entry())
    store.steer_prune()
    counts = store.steer_counts()
    assert counts["queued"] == 1
    assert counts["typed"] == 2
    assert store.steer_get(queued["id"]) is not None


def test_the_crash_hook_fires_after_enqueue_and_resolve(store):
    seen = []
    board_journal_store.CRASH_HOOK = lambda *a: seen.append(a)
    try:
        row = store.steer_enqueue(_entry())
        store.steer_resolve(row["id"], "typed", True)
        store.steer_resolve(row["id"], "typed", True)
    finally:
        board_journal_store.CRASH_HOOK = None
    assert seen == [("queued", "steer", "enqueue", row["id"]),
                    ("resolved", "steer", "resolve", row["id"])]


def test_a_v33_file_opens_and_gains_the_table(tmp_path):
    path = tmp_path / "board.db"
    s = BoardStore(path)
    s.connect()
    s.close()
    raw = sqlite3.connect(str(path))
    raw.execute("DROP TABLE steer_queue")
    raw.execute("UPDATE schema_meta SET value='33' WHERE key='version'")
    raw.commit()
    raw.close()
    s = BoardStore(path)
    s.connect()
    try:
        assert s.steer_counts() == {}
        s.steer_enqueue(_entry())
        assert s.steer_counts() == {"queued": 1}
    finally:
        s.close()


def test_an_older_build_ignores_the_table_and_keeps_the_rows(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    s = BoardStore(path)
    s.connect()
    s.steer_enqueue(_entry())
    s.close()
    monkeypatch.setattr(board_mod, "SCHEMA_VERSION", SCHEMA_VERSION - 1)
    older = BoardStore(path)
    older.connect()
    assert older.cards() == []
    older.close()
    raw = sqlite3.connect(str(path))
    assert raw.execute("SELECT COUNT(*) FROM steer_queue").fetchone()[0] == 1
    assert int(raw.execute(
        "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]) \
        == SCHEMA_VERSION
    raw.close()


def test_the_payload_is_json(store):
    row = store.steer_enqueue(_entry(payload={"text": "x", "n": [1, 2]}))
    raw = store._conn.execute(
        "SELECT payload FROM steer_queue WHERE id=?", (row["id"],)).fetchone()
    assert json.loads(raw[0]) == {"n": [1, 2], "text": "x"}
