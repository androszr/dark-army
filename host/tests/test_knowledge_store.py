# host/tests/test_knowledge_store.py
"""The per-project knowledge notes: the bounds, the upsert, and the one
property the whole channel surface rests on — a root cannot see another
root's rows."""

import sqlite3
from pathlib import Path

import pytest

from dark_army_daemon import knowledge_store as ks
from dark_army_daemon.board import BoardStore, SCHEMA_VERSION


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    try:
        yield s
    finally:
        s.close()


ROOT_A = "/tmp/project-a"
ROOT_B = "/tmp/project-b"


# --- the shape ----------------------------------------------------------------


def test_schema_version_is_twenty_two():
    assert SCHEMA_VERSION == 30


def test_a_fresh_store_has_no_notes(store):
    assert store.knowledge_for(ROOT_A) == []
    assert store.knowledge_count(ROOT_A) == 0


# --- upsert -------------------------------------------------------------------


def test_re_answering_keeps_created_at_and_moves_updated_at(store):
    ok, detail = store.knowledge_put(ROOT_A, "purpose", "What for?", "A tool")
    assert ok, detail
    first = store.knowledge_for(ROOT_A)[0]

    ok, detail = store.knowledge_put(
        ROOT_A, "purpose", "What for?", "A tool, and a service")
    assert ok, detail
    rows = store.knowledge_for(ROOT_A)
    assert len(rows) == 1, "re-answering must replace, never add a second row"
    second = rows[0]
    assert second["answer"] == "A tool, and a service"
    assert second["created_at"] == first["created_at"], (
        "created_at records when the project first answered; re-answering is "
        "not a new fact appearing")
    assert second["updated_at"] >= first["updated_at"]


def test_the_author_is_recorded_and_replaced_on_re_answer(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "a", "sess-1")
    assert store.knowledge_for(ROOT_A)[0]["author"] == "sess-1"
    store.knowledge_put(ROOT_A, "purpose", "q", "b", "sess-2")
    assert store.knowledge_for(ROOT_A)[0]["author"] == "sess-2"


def test_rows_come_back_ordered_by_key(store):
    for key in ("usage", "audience", "purpose"):
        store.knowledge_put(ROOT_A, key, "q", "a")
    assert [r["key"] for r in store.knowledge_for(ROOT_A)] == [
        "audience", "purpose", "usage"]


# --- isolation, the whole security property ------------------------------------


def test_isolation_a_second_roots_rows_are_invisible_to_the_first(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "answer for A")
    store.knowledge_put(ROOT_B, "purpose", "q", "answer for B")

    rows_a = store.knowledge_for(ROOT_A)
    rows_b = store.knowledge_for(ROOT_B)
    assert [r["answer"] for r in rows_a] == ["answer for A"]
    assert [r["answer"] for r in rows_b] == ["answer for B"]


def test_there_is_no_all_roots_read(store):
    """A method that could return every project's rows would put the scoping
    `if` in the daemon rather than in the shape of the API."""
    store.knowledge_put(ROOT_A, "purpose", "q", "a")
    assert store.knowledge_for("") == []
    assert store.knowledge_for(None) == []
    assert store.knowledge_count("") == 0


# --- the bounds ---------------------------------------------------------------


def test_question_and_answer_are_clamped_not_refused(store):
    ok, detail = store.knowledge_put(
        ROOT_A, "purpose",
        "q" * (ks.MAX_QUESTION_CHARS + 500),
        "a" * (ks.MAX_ANSWER_CHARS + 500))
    assert ok, detail
    row = store.knowledge_for(ROOT_A)[0]
    assert len(row["question"]) == ks.MAX_QUESTION_CHARS
    assert len(row["answer"]) == ks.MAX_ANSWER_CHARS


def test_an_empty_answer_is_refused_in_words(store):
    ok, detail = store.knowledge_put(ROOT_A, "purpose", "q", "   ")
    assert not ok
    assert "answer" in detail
    assert store.knowledge_for(ROOT_A) == []


def test_an_empty_root_is_refused_in_words(store):
    ok, detail = store.knowledge_put("", "purpose", "q", "a")
    assert not ok
    assert "project" in detail


def test_a_key_that_normalises_to_nothing_is_refused(store):
    ok, detail = store.knowledge_put(ROOT_A, "!!! ???", "q", "a")
    assert not ok
    assert "key" in detail
    assert store.knowledge_for(ROOT_A) == []


def test_the_key_is_normalised_to_the_stored_form(store):
    ok, detail = store.knowledge_put(ROOT_A, "  Purpose Of It!  ", "q", "a")
    assert ok, detail
    assert store.knowledge_for(ROOT_A)[0]["key"] == "purposeofit"
    assert ks.normalise_key("A.B_c-1") == "a.b_c-1"


def test_the_entry_cap_refuses_a_new_key_but_never_an_update(store):
    for i in range(ks.MAX_ENTRIES_PER_ROOT):
        ok, detail = store.knowledge_put(ROOT_A, f"k{i:03d}", "q", "a")
        assert ok, detail
    ok, detail = store.knowledge_put(ROOT_A, "one-too-many", "q", "a")
    assert not ok
    assert str(ks.MAX_ENTRIES_PER_ROOT) in detail

    # A project at its limit must still be able to correct what it said.
    ok, detail = store.knowledge_put(ROOT_A, "k000", "q", "corrected")
    assert ok, detail
    assert store.knowledge_for(ROOT_A)[0]["answer"] == "corrected"
    assert store.knowledge_count(ROOT_A) == ks.MAX_ENTRIES_PER_ROOT

    # And the cap is per root, not machine-wide.
    ok, detail = store.knowledge_put(ROOT_B, "purpose", "q", "a")
    assert ok, detail


# --- forward compatibility -----------------------------------------------------


def test_the_rows_survive_a_reopen(tmp_path):
    path = tmp_path / "board.db"
    first = BoardStore(path)
    first.connect()
    first.knowledge_put(ROOT_A, "purpose", "q", "a")
    first.close()

    second = BoardStore(path)
    second.connect()
    try:
        assert [r["answer"] for r in second.knowledge_for(ROOT_A)] == ["a"]
    finally:
        second.close()


def test_a_file_stamped_at_nineteen_gains_the_table(tmp_path):
    path = tmp_path / "board.db"
    first = BoardStore(path)
    first.connect()
    first.close()
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE knowledge_entries")
    conn.execute("UPDATE schema_meta SET value = '19' WHERE key = 'version'")
    conn.commit()
    conn.close()

    second = BoardStore(path)
    second.connect()
    try:
        ok, detail = second.knowledge_put(ROOT_A, "purpose", "q", "a")
        assert ok, detail
    finally:
        second.close()


def test_cards_are_untouched_by_knowledge_rows(store):
    """`card_runs`' property: no `cards` column, so an older build's tolerant
    `SELECT * FROM cards` cannot see this table or be blanked by it."""
    card, detail = store.create({"title": "do the thing", "project": "bob",
                                 "root": ROOT_A})
    assert card is not None, detail
    store.knowledge_put(ROOT_A, "purpose", "q", "a")

    rows = store._conn.execute("SELECT * FROM cards").fetchall()
    assert len(rows) == 1
    assert "knowledge" not in " ".join(rows[0].keys())
    assert store.get(card["id"])["title"] == "do the thing"


def test_deleting_the_card_leaves_the_project_notes_alone(store):
    """Not card-scoped, on purpose: the notes outlive every card that happened
    to be open when somebody wrote them down."""
    card, _ = store.create({"title": "do the thing", "project": "bob",
                            "root": ROOT_A})
    store.knowledge_put(ROOT_A, "purpose", "q", "a")
    store.delete(card["id"])
    assert [r["answer"] for r in store.knowledge_for(ROOT_A)] == ["a"]


# --- provenance, confirm, stale, person edit -----------------------------------


def test_new_columns_are_present_after_connect(store):
    names = {row["name"] for row in store._conn.execute(
        "PRAGMA table_info(knowledge_entries)").fetchall()}
    assert {"last_confirmed", "stale", "source"} <= names


def test_a_file_without_the_new_columns_gains_them(tmp_path):
    path = tmp_path / "board.db"
    first = BoardStore(path)
    first.connect()
    first.close()
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE knowledge_entries")
    conn.execute(
        "CREATE TABLE knowledge_entries ("
        "root TEXT NOT NULL, key TEXT NOT NULL,"
        " question TEXT NOT NULL DEFAULT '',"
        " answer TEXT NOT NULL DEFAULT '',"
        " author TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " PRIMARY KEY (root, key))")
    conn.execute(
        "INSERT INTO knowledge_entries"
        "(root, key, question, answer, author, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (ROOT_A, "purpose", "q", "legacy", "", 1.0, 1.0))
    conn.commit()
    conn.close()

    second = BoardStore(path)
    second.connect()
    try:
        names = {row["name"] for row in second._conn.execute(
            "PRAGMA table_info(knowledge_entries)").fetchall()}
        assert {"last_confirmed", "stale", "source"} <= names
        row = second.knowledge_for(ROOT_A)[0]
        assert row["answer"] == "legacy"
        assert row["last_confirmed"] == 0
        assert row["stale"] == ""
        assert row["source"] == ""
    finally:
        second.close()


def test_knowledge_put_sets_source_agent_and_leaves_last_confirmed_zero(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "a")
    row = store.knowledge_for(ROOT_A)[0]
    assert row["source"] == "agent"
    assert row["last_confirmed"] == 0
    assert row["stale"] == ""


def test_knowledge_put_does_not_clear_stale(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "old")
    ok, detail = store.knowledge_mark_stale(ROOT_A, "purpose")
    assert ok, detail
    before = store.knowledge_for(ROOT_A)[0]
    assert before["stale"] == "1"
    confirmed = before["last_confirmed"]
    ok, detail = store.knowledge_put(ROOT_A, "purpose", "q", "rewritten")
    assert ok, detail
    row = store.knowledge_for(ROOT_A)[0]
    assert row["answer"] == "rewritten"
    assert row["source"] == "agent"
    assert row["stale"] == "1", "an agent rewrite must not clear a stale mark"
    assert row["last_confirmed"] == confirmed


def test_confirm_sets_last_confirmed_source_person_and_clears_stale(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "a")
    store.knowledge_mark_stale(ROOT_A, "purpose")
    ok, detail = store.knowledge_confirm(ROOT_A, "purpose")
    assert ok, detail
    row = store.knowledge_for(ROOT_A)[0]
    assert row["last_confirmed"] > 0
    assert row["source"] == "person"
    assert row["stale"] == ""


def test_mark_stale_does_not_move_last_confirmed(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "a")
    store.knowledge_confirm(ROOT_A, "purpose")
    confirmed = store.knowledge_for(ROOT_A)[0]["last_confirmed"]
    ok, detail = store.knowledge_mark_stale(ROOT_A, "purpose")
    assert ok, detail
    row = store.knowledge_for(ROOT_A)[0]
    assert row["stale"] == "1"
    assert row["last_confirmed"] == confirmed
    assert row["answer"] == "a"


def test_edit_updates_answer_sets_person_does_not_confirm_or_un_stale(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "old")
    store.knowledge_mark_stale(ROOT_A, "purpose")
    before = store.knowledge_for(ROOT_A)[0]
    ok, detail = store.knowledge_edit(ROOT_A, "purpose", "new q", "new a")
    assert ok, detail
    row = store.knowledge_for(ROOT_A)[0]
    assert row["question"] == "new q"
    assert row["answer"] == "new a"
    assert row["source"] == "person"
    assert row["author"] == ""
    assert row["stale"] == "1"
    assert row["last_confirmed"] == before["last_confirmed"] == 0


def test_unknown_key_refuses_in_words(store):
    for verb in (store.knowledge_confirm, store.knowledge_mark_stale):
        ok, detail = verb(ROOT_A, "missing")
        assert not ok
        assert "note" in detail
    ok, detail = store.knowledge_edit(ROOT_A, "missing", "q", "a")
    assert not ok
    assert "note" in detail


def test_edit_clamps_prose_and_refuses_empty_answer(store):
    store.knowledge_put(ROOT_A, "purpose", "q", "a")
    ok, detail = store.knowledge_edit(
        ROOT_A, "purpose",
        "q" * (ks.MAX_QUESTION_CHARS + 50),
        "a" * (ks.MAX_ANSWER_CHARS + 50))
    assert ok, detail
    row = store.knowledge_for(ROOT_A)[0]
    assert len(row["question"]) == ks.MAX_QUESTION_CHARS
    assert len(row["answer"]) == ks.MAX_ANSWER_CHARS
    ok, detail = store.knowledge_edit(ROOT_A, "purpose", "q", "   ")
    assert not ok
    assert "answer" in detail


def test_there_is_no_delete_verb():
    assert not hasattr(BoardStore, "knowledge_delete")
    text = Path(__file__).resolve().parents[1].joinpath(
        "dark_army_daemon", "knowledge_store.py").read_text()
    assert "DELETE FROM knowledge_entries" not in text

