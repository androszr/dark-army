"""The action journal's store and its pure declarations (`docs/action-journal.md`)."""
import re
import sqlite3
import time

import pytest

from dark_army_daemon import action_journal as aj
from dark_army_daemon import board as board_mod
from dark_army_daemon import board_journal_store
from dark_army_daemon.board import SCHEMA_VERSION, BoardStore


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _no_crash_hook():
    board_journal_store.CRASH_HOOK = None
    yield
    board_journal_store.CRASH_HOOK = None


def _intent(store, kind="card_start", subject="c1", step="spawn", payload=None,
            action=None):
    action = action or store.journal_begin(kind, subject)
    return action, store.journal_intent(action, kind, subject, step, payload or {})


# --- the store ---------------------------------------------------------------

def test_intent_returns_a_32_hex_id_and_snapshots_replay(store):
    _a, iid = _intent(store, step="record")
    assert re.fullmatch(r"[0-9a-f]{32}", iid)
    row = store._conn.execute("SELECT replay, step FROM action_intents WHERE id=?",
                              (iid,)).fetchone()
    assert (row["replay"], row["step"]) == (aj.SAFE, "record")
    _a, spawn = _intent(store, step="spawn")
    assert store._conn.execute("SELECT replay FROM action_intents WHERE id=?",
                               (spawn,)).fetchone()[0] == aj.NEVER


def test_a_second_result_under_one_id_is_ignored(store):
    _a, iid = _intent(store)
    assert store.journal_result(iid, "spawned") is True
    assert store.journal_result(iid, "refused") is False
    row = store._conn.execute("SELECT outcome FROM action_results WHERE intent_id=?",
                              (iid,)).fetchone()
    assert row["outcome"] == "spawned"


def test_open_lists_an_unresolved_action_and_omits_a_resolved_one(store):
    a1, i1 = _intent(store, subject="c1")
    a2, i2 = _intent(store, subject="c2")
    store.journal_result(i2, "refused")
    open_ = store.journal_open()
    assert [a["action_id"] for a in open_] == [a1]
    assert open_[0]["steps"][0]["result"] is None
    assert open_[0]["steps"][0]["payload"] == {}


def test_an_action_stopped_between_a_result_and_the_next_intent_is_open(store):
    a, spawn = _intent(store, subject="c1")
    store.journal_result(spawn, "spawned", "", {"now": 5.0})
    open_ = store.journal_open()
    assert [x["action_id"] for x in open_] == [a]
    # Once the next step is written the continuation no longer applies.
    _i, rec = _intent(store, subject="c1", step="record", action=a)
    store.journal_result(rec, "completed")
    assert store.journal_open() == []
    # A refused spawn is a finished action, not a stopped one.
    _a2, s2 = _intent(store, subject="c2")
    store.journal_result(s2, "refused")
    assert store.journal_open() == []


def test_attempts_step_per_action_and_survive_a_reopen(tmp_path):
    path = tmp_path / "board.db"
    s = BoardStore(path)
    s.connect()
    a, _i = _intent(s, subject="c1")
    s.journal_intent(a, "card_start", "c1", "record", {})
    assert s.journal_attempts("card_start", "c1") == 1
    _intent(s, subject="c1")
    assert s.journal_attempts("card_start", "c1") == 2
    s.close()
    again = BoardStore(path)
    again.connect()
    try:
        assert again.journal_attempts("card_start", "c1") == 2
        assert again.journal_attempts("card_start", "other") == 0
    finally:
        again.close()


def test_attempts_start_over_after_a_quiet_day(store):
    _intent(store, subject="c1")
    _intent(store, subject="c1")
    store._conn.execute("UPDATE action_attempts SET last_at = ?",
                        (time.time() - aj.ATTEMPT_DECAY_SECONDS - 5,))
    store._conn.commit()
    _intent(store, subject="c1")
    assert store.journal_attempts("card_start", "c1") == 1


def test_a_forbidden_key_two_levels_down_raises_and_writes_nothing(store):
    action = store.journal_begin("card_start", "c1")
    with pytest.raises(ValueError):
        store.journal_intent(action, "card_start", "c1", "spawn",
                             {"a": {"b": [{"token": "x"}]}})
    assert store.journal_counts() == {"intents": 0, "results": 0, "attempts": 0}
    _a, iid = _intent(store)
    with pytest.raises(ValueError):
        store.journal_result(iid, "spawned", "", {"deep": {"secret": 1}})
    assert store.journal_counts()["results"] == 0


def test_prune_removes_old_resolved_actions_and_keeps_every_open_intent(store):
    old_a, old_i = _intent(store, subject="old")
    store.journal_result(old_i, "refused")
    open_a, _open_i = _intent(store, subject="stuck")
    young_a, young_i = _intent(store, subject="young")
    store.journal_result(young_i, "refused")
    long_ago = time.time() - 40 * 86400
    store._conn.execute("UPDATE action_intents SET created_at=? WHERE action_id IN (?,?)",
                        (long_ago, old_a, open_a))
    store._conn.commit()
    assert store.journal_prune() == 1
    left = {r[0] for r in store._conn.execute("SELECT action_id FROM action_intents")}
    assert left == {open_a, young_a}
    assert store.journal_counts()["results"] == 1


def test_prune_trims_the_oldest_resolved_rows_past_the_cap(store, monkeypatch):
    monkeypatch.setattr(aj, "MAX_JOURNAL_ROWS", 3)
    ids = []
    for n in range(5):
        a, i = _intent(store, subject=f"c{n}")
        store.journal_result(i, "refused")
        store._conn.execute("UPDATE action_intents SET created_at=? WHERE id=?",
                            (time.time() - 100 + n, i))
        ids.append(a)
    store._conn.commit()
    store.journal_prune()
    left = [r[0] for r in store._conn.execute(
        "SELECT action_id FROM action_intents ORDER BY created_at")]
    assert left == ids[2:]


def test_recent_filters_by_kind_subject_and_time(store):
    _intent(store, kind="autocompact", subject="s1", step="type")
    _intent(store, kind="autocompact", subject="s2", step="type")
    _intent(store, kind="answer_burst", subject="s1", step="type")
    assert len(store.journal_recent("autocompact")) == 2
    assert len(store.journal_recent("autocompact", "s1")) == 1
    assert store.journal_recent("autocompact", "s1", time.time() + 5) == []


def test_the_crash_hook_fires_after_each_commit(store):
    seen = []
    board_journal_store.CRASH_HOOK = lambda *a: seen.append(a)
    _a, iid = _intent(store)
    store.journal_result(iid, "spawned")
    assert [s[0] for s in seen] == ["intent", "result"]
    assert seen[0][1:3] == ("card_start", "spawn") and seen[0][3] == iid
    # The row is already there when the hook runs.
    board_journal_store.CRASH_HOOK = lambda *a: (_ for _ in ()).throw(RuntimeError)
    with pytest.raises(RuntimeError):
        _intent(store, subject="c9")
    assert store.journal_counts()["intents"] == 2


# --- the file --------------------------------------------------------------

def test_a_v30_file_without_the_tables_opens_and_gains_them(tmp_path):
    path = tmp_path / "old.db"
    s = BoardStore(path)
    s.connect()
    s.close()
    conn = sqlite3.connect(path)
    for table in ("action_intents", "action_results", "action_attempts"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("UPDATE schema_meta SET value='30' WHERE key='version'")
    conn.commit()
    conn.close()
    s = BoardStore(path)
    s.connect()
    try:
        assert s.journal_counts() == {"intents": 0, "results": 0, "attempts": 0}
        assert int(s._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]) \
            == SCHEMA_VERSION
    finally:
        s.close()


def test_an_older_build_reads_a_journalled_file_and_leaves_the_rows(
        tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    s = BoardStore(path)
    s.connect()
    card, _ = s.create({"title": "t", "project": "bob", "root": "/tmp/bob"})
    _intent(s, subject=card["id"])
    s.close()
    monkeypatch.setattr(board_mod, "SCHEMA_VERSION", SCHEMA_VERSION - 1)
    older = BoardStore(path)
    older.connect()
    try:
        assert [c["id"] for c in older.cards()] == [card["id"]]
        assert older.journal_counts()["intents"] == 1
        assert int(older._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]) \
            == SCHEMA_VERSION
    finally:
        older.close()


def test_the_journal_lives_in_board_db_and_adds_no_private_file():
    from dark_army_daemon import paths
    text = repr(paths._PRIVATE_FILES)
    for name in ("action_intents", "action_results", "action_attempts"):
        assert name not in text


# --- the pure module --------------------------------------------------------

@pytest.mark.parametrize("snapshot,kind,step,attempts,expected", [
    (aj.SAFE, "card_start", "record", 1, True),
    (aj.SAFE, "card_start", "record", aj.MAX_SAFE_REPLAYS, True),
    (aj.SAFE, "card_start", "record", aj.MAX_SAFE_REPLAYS + 1, False),
    (aj.NEVER, "card_start", "record", 1, False),       # snapshot says never
    (aj.SAFE, "card_start", "spawn", 1, False),         # declaration says never
    (aj.SAFE, "nope", "record", 1, False),
    (aj.SAFE, "card_start", "nope", 1, False),
    (aj.SAFE, "card_start", "record", "x", False),
])
def test_may_replay_truth_table(snapshot, kind, step, attempts, expected):
    assert aj.may_replay(snapshot, kind, step, attempts) is expected


def _step(name, replay, attempt=1, outcome=None):
    return {"step": name, "replay": replay, "attempt": attempt,
            "result": {"outcome": outcome} if outcome else None}


def test_decide_never_replays_over_an_open_spawn():
    action = {"kind": "card_start", "steps": [
        _step("spawn", aj.NEVER), _step("record", aj.SAFE)]}
    assert aj.decide(action) == [("spawn", aj.INTERRUPTED),
                                 ("record", aj.INTERRUPTED)]


def test_decide_replays_a_safe_record_after_a_finished_spawn():
    action = {"kind": "card_start", "steps": [
        _step("spawn", aj.NEVER, outcome="spawned"), _step("record", aj.SAFE)]}
    assert aj.decide(action) == [("record", aj.REPLAYED)]
    action["steps"][1]["attempt"] = aj.MAX_SAFE_REPLAYS + 1
    assert aj.decide(action) == [("record", aj.INTERRUPTED)]


def test_decide_leaves_a_step_this_build_does_not_declare_interrupted():
    action = {"kind": "card_start", "steps": [_step("teleport", aj.SAFE)]}
    assert aj.decide(action) == [("teleport", aj.INTERRUPTED)]
    assert aj.decide({"kind": "unknown", "steps": [_step("x", aj.SAFE)]}) \
        == [("x", aj.INTERRUPTED)]


def test_forbidden_payload_finds_a_key_at_any_depth():
    assert aj.forbidden_payload({"a": 1}) == ""
    assert aj.forbidden_payload({"a": {"b": [{"claim": 1}]}}) == "claim"
    assert aj.forbidden_payload({"port": 1}) == "port"


def test_every_action_declares_only_known_replay_words():
    for kind, steps in aj.ACTIONS.items():
        assert kind in aj.INTERRUPTED_WORDS
        for replay in steps.values():
            assert replay in (aj.SAFE, aj.NEVER)


def test_prune_over_the_cap_counts_rows_already_removed_for_age(store, monkeypatch):
    monkeypatch.setattr(aj, "MAX_JOURNAL_ROWS", 4)
    ids = []
    for n in range(5):
        a, i = _intent(store, subject=f"c{n}")
        store.journal_result(i, "refused")
        ids.append(a)
    now = time.time()
    # Two are old enough to go for age; 3 remain, under the cap of 4.
    for n, a in enumerate(ids):
        at = now - 40 * 86400 if n < 2 else now - 10 + n
        store._conn.execute("UPDATE action_intents SET created_at=? WHERE action_id=?", (at, a))
    store._conn.commit()
    store.journal_prune()
    left = {r[0] for r in store._conn.execute("SELECT action_id FROM action_intents")}
    assert left == set(ids[2:])
