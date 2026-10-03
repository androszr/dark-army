# host/tests/test_board.py
"""The Kanban store: CRUD, the column vocabulary, the bounds, and the promise
that a file written by another build still opens."""

import os
import sqlite3
import time

import pytest

from dark_army_daemon import board as board_mod
from dark_army_daemon.board import ALREADY_CREATED, BoardStore, COLUMNS, \
    MAX_BLOCKERS, MAX_CARDS, MAX_CLOSE_NOTE_CHARS, MAX_MANUAL_STEPS_CHARS, \
    MAX_MESSAGE_CHARS, MAX_PROMPT_CHARS, MAX_SUMMARY_CHARS, \
    MAX_THREAD_MESSAGES, SCHEMA_VERSION, WAL_SIZE_LIMIT_BYTES, parse_ids, \
    with_ready_alias


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    try:
        yield s
    finally:
        s.close()


def _card(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp/bob"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


# --- the vocabulary -----------------------------------------------------------


def test_the_columns_are_prep_backlog_in_progress_done():
    """Not configurable. Every surface — the board window, the queued count on
    the menu bar, the reconcile that moves a dispatched card — is written
    against these four names, so a fifth is a change to all of them.

    `ready` stays gone: it and Backlog said the same thing, and the dispatch
    gate it really existed to be is now the drop into In progress. `prep` is
    not Ready returned — it has its own verb (Refine) and its own exit
    condition (`attach_plan` writes the plan and moves the card out), which
    is the difference `board.COLUMNS`' comment argues."""
    assert COLUMNS == ("prep", "backlog", "in_progress", "done")
    assert "ready" not in COLUMNS


def test_an_unknown_column_is_refused_rather_than_stored(store):
    card, detail = store.create({"title": "x", "column_name": "someday"})
    assert card is None
    assert "someday" in detail


def test_an_unknown_tool_is_refused(store):
    card, detail = store.create({"title": "x", "tool": "eliza"})
    assert card is None
    assert "eliza" in detail


# --- CRUD ---------------------------------------------------------------------


def test_a_card_round_trips_and_defaults_into_prep(store):
    """A new card is "written down, not yet turned into a plan" — the Prep
    column's one-line meaning — so that is where `create` files it."""
    made = _card(store, prompt="rewrite the sprite pipeline", tool="claude")
    got = store.get(made["id"])
    assert got["title"] == "do the thing"
    assert got["prompt"] == "rewrite the sprite pipeline"
    assert got["tool"] == "claude"
    assert got["column_name"] == "prep"
    assert got["author"] == "user"
    assert got["plan_path"] == ""
    assert got["refine_state"] == "" and got["refine_session_id"] == ""


def test_an_explicit_column_is_still_honoured(store):
    """Only the *default* moved when Prep arrived: a caller that names a
    column — a restore, a test, the daemon's own writes — gets that column."""
    made = _card(store, column_name="backlog")
    assert store.get(made["id"])["column_name"] == "backlog"


def test_a_card_needs_a_title(store):
    card, detail = store.create({"title": "   "})
    assert card is None
    assert "title" in detail


def test_update_only_writes_the_fields_it_was_given(store):
    """The forward-compatibility half of the contract: a build that does not know
    about a column must not be able to blank it by writing a whole row."""
    made = _card(store, prompt="keep me", tool="codex")
    store.update(made["id"], {"title": "renamed"})
    got = store.get(made["id"])
    assert got["title"] == "renamed"
    assert got["prompt"] == "keep me"
    assert got["tool"] == "codex"


def test_moving_to_done_stamps_it_and_leaving_clears_it(store):
    made = _card(store)
    store.move(made["id"], "done")
    assert store.get(made["id"])["done_at"] is not None
    store.move(made["id"], "backlog")
    assert store.get(made["id"])["done_at"] is None


def test_delete_removes_the_card(store):
    made = _card(store)
    ok, _ = store.delete(made["id"])
    assert ok
    assert store.get(made["id"]) is None
    ok, detail = store.delete(made["id"])
    assert not ok and "no such card" in detail


def test_clear_done_deletes_every_done_row_in_one_statement_and_only_done(store):
    hand_done = _card(store, title="closed by hand", column_name="done")
    agent_done = _card(store, title="closed by agent", tool="claude",
                       column_name="in_progress")
    store.bind_session(agent_done["id"], "sess-1")
    closed, detail = store.declare_done(
        agent_done["id"], "sess-1", "verified")
    assert closed is not None, detail
    backlog = _card(store, title="keep backlog", summary="unchanged",
                    prompt="keep this", column_name="backlog")
    active = _card(store, title="keep active", tool="codex",
                   column_name="in_progress")
    prep = _card(store, title="keep prep", summary="also unchanged")
    before = {card["id"]: store.get(card["id"])
              for card in (prep, backlog, active)}
    expected_count, expected_token = store.done_scope()

    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        ok, deleted_count, detail = store.clear_done(
            expected_count, expected_token)
    finally:
        store._conn.set_trace_callback(None)

    assert ok is True and deleted_count == 2 and detail == "deleted"
    assert store.get(hand_done["id"]) is None
    assert store.get(agent_done["id"]) is None
    assert {card_id: store.get(card_id) for card_id in before} == before
    destructive = [sql for sql in statements
                   if sql.lstrip().upper().startswith("DELETE FROM CARDS")]
    assert destructive == ["DELETE FROM cards WHERE column_name = 'done'"]


def test_clear_done_refuses_a_stale_count_without_deleting_anything(store):
    first = _card(store, title="first", column_name="done")
    second = _card(store, title="second", column_name="done")

    _, token = store.done_scope()
    ok, deleted_count, detail = store.clear_done(1, token)

    assert ok is False and deleted_count == 0
    assert "changed" in detail and "nothing was cleared" in detail
    assert store.get(first["id"]) is not None
    assert store.get(second["id"]) is not None


def test_clear_done_zero_is_idempotent_and_preserves_other_columns(store):
    prep = _card(store, title="prep")
    backlog = _card(store, title="backlog", column_name="backlog")
    active = _card(store, title="active", column_name="in_progress")
    before = {card["id"]: store.get(card["id"])
              for card in (prep, backlog, active)}

    count, token = store.done_scope()
    assert store.clear_done(count, token) == (True, 0, "deleted")
    assert {card_id: store.get(card_id) for card_id in before} == before


def test_done_scope_is_deterministic_and_ignores_edits_outside_done(store):
    done = _card(store, title="done", column_name="done")
    backlog = _card(store, title="backlog", column_name="backlog")
    first = store.done_scope()

    store.update(backlog["id"], {"title": "renamed outside Done"})

    assert store.done_scope() == first
    store.move(done["id"], "backlog")
    assert store.done_scope() != first


def test_clear_done_refuses_an_equal_count_membership_swap(store):
    first = _card(store, title="first", column_name="done")
    second = _card(store, title="second", column_name="done")
    incoming = _card(store, title="incoming", column_name="backlog")
    expected_count, expected_token = store.done_scope()

    store.move(first["id"], "backlog")
    store.move(incoming["id"], "done")
    actual_count, actual_token = store.done_scope()
    assert actual_count == expected_count
    assert actual_token != expected_token

    ok, deleted_count, detail = store.clear_done(
        expected_count, expected_token)

    assert (ok, deleted_count) == (False, 0)
    assert "nothing was cleared" in detail
    assert store.get(second["id"]) is not None
    assert store.get(incoming["id"]) is not None
    assert store.get(first["id"])["column_name"] == "backlog"


def test_clear_done_rolls_back_when_the_delete_raises(store):
    done = _card(store, title="done", column_name="done")
    expected_count, expected_token = store.done_scope()
    store._conn.execute("""
        CREATE TRIGGER refuse_done_clear BEFORE DELETE ON cards
        WHEN OLD.column_name = 'done'
        BEGIN SELECT RAISE(ABORT, 'refuse clear'); END
    """)
    store._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="refuse clear"):
        store.clear_done(expected_count, expected_token)

    assert store.get(done["id"]) is not None
    store._conn.execute("DROP TRIGGER refuse_done_clear")
    store._conn.commit()
    assert store.clear_done(*store.done_scope()) == (True, 1, "deleted")


def test_counts_match_the_rows(store):
    _card(store)
    _card(store, column_name="in_progress")
    _card(store, column_name="in_progress")
    _card(store, column_name="done")
    assert store.counts() == {"prep": 1, "backlog": 0,
                              "in_progress": 2, "done": 1}


def test_counts_name_every_column_even_at_zero(store):
    """A surface reading `counts["backlog"]` must never have to handle a
    KeyError — the menu-bar strip does exactly that."""
    assert set(store.counts()) == set(COLUMNS)


def test_with_ready_alias_copies_backlog_and_does_not_invent_a_column():
    """`ready` is a snapshot alias of `backlog`, not a fourth store column."""
    src = {"backlog": 3, "in_progress": 1, "done": 0}
    out = with_ready_alias(src)
    assert out["ready"] == 3
    assert out["backlog"] == 3
    assert out["in_progress"] == 1
    assert out["done"] == 0
    assert "ready" not in src
    assert "ready" not in COLUMNS

    empty = with_ready_alias({})
    assert empty["ready"] == 0

    missing = with_ready_alias({"in_progress": 1, "done": 2})
    assert missing["ready"] == 0
    assert missing["in_progress"] == 1


# --- the link to a session ----------------------------------------------------


def test_binding_a_session_moves_the_card_across(store):
    """A bound card sitting in Ready is a state no surface has a way to draw."""
    made = _card(store, column_name="backlog", tool="claude")
    store.bind_session(made["id"], "sess-1")
    got = store.get(made["id"])
    assert got["session_id"] == "sess-1"
    assert got["link_state"] == "live"
    assert got["column_name"] == "in_progress"


def test_binding_a_session_onto_a_done_card_keeps_it_done(store):
    """A late attach must not bounce a finished card back to In progress
    or wipe the close signature. session_id + live are recorded so Delete
    can still close the terminal."""
    made = _card(store, column_name="backlog", tool="claude")
    store.bind_session(made["id"], "sess-1")
    closed, detail = store.declare_done(made["id"], "sess-1", "tests pass")
    assert closed is not None, detail
    done_at = closed["done_at"]
    store.bind_session(made["id"], "sess-2")
    got = store.get(made["id"])
    assert got["column_name"] == "done"
    assert got["closed_by"] == "sess-1"
    assert got["done_at"] == done_at
    assert got["close_note"] == "tests pass"
    assert got["session_id"] == "sess-2"
    assert got["link_state"] == "live"


def test_marking_ended_never_moves_a_card_to_done(store):
    """A session ending says nothing about whether the work was finished, and Dark Army
    deciding otherwise is the one thing a board must not do."""
    made = _card(store, column_name="backlog", tool="claude")
    store.bind_session(made["id"], "sess-1")
    store.mark_ended(made["id"])
    got = store.get(made["id"])
    assert got["link_state"] == "ended"
    assert got["column_name"] == "in_progress"
    assert got["done_at"] is None


def test_no_session_category_is_ever_written_into_the_store(store):
    """`running`/`waiting`/`sleeping` belong to the agents snapshot. Writing one
    here is how the board would start disagreeing with the menu-bar strip."""
    made = _card(store)
    card, detail = store.update(made["id"], {"link_state": "running"})
    assert card is None
    assert "running" in detail


# --- the bounds ---------------------------------------------------------------


def test_a_long_prompt_is_refused_rather_than_truncated(store):
    """Silently keeping the first 8000 characters of somebody's instructions and
    handing *that* to an agent is worse than saying it was too long."""
    card, detail = store.create({"title": "x", "prompt": "y" * (MAX_PROMPT_CHARS + 1)})
    assert card is None
    assert "longer than" in detail
    assert store.total() == 0


def test_a_long_prompt_is_refused_on_update_too(store):
    made = _card(store, prompt="short")
    card, detail = store.update(made["id"], {"prompt": "y" * (MAX_PROMPT_CHARS + 1)})
    assert card is None
    assert store.get(made["id"])["prompt"] == "short"


def test_the_board_refuses_to_grow_past_max_cards(store, monkeypatch):
    monkeypatch.setattr(board_mod, "MAX_CARDS", 3)
    for _ in range(3):
        _card(store)
    card, detail = store.create({"title": "one too many"})
    assert card is None
    assert "full" in detail


# --- the plain-language line --------------------------------------------------


def test_the_summary_is_stored_and_editable(store):
    """The half of the card written for a reader rather than for a CLI. Its own
    column, not a convention inside the prompt, because every surface has to be
    able to show it *without* showing the instructions."""
    made = _card(store, summary="Customers cannot check out on mobile.")
    assert store.get(made["id"])["summary"].startswith("Customers cannot")
    store.update(made["id"], {"summary": "Checkout is broken on phones."})
    assert store.get(made["id"])["summary"] == "Checkout is broken on phones."


def test_a_card_with_no_summary_is_perfectly_legal(store):
    """Most cards will not have one, and an empty string is what the panel tests
    for. It must never be a reason to refuse a card."""
    assert _card(store)["summary"] == ""


def test_the_summary_ceiling_is_a_paragraph(store):
    """2000 characters: room for a paragraph of plain words. The card face's
    three-line cap keeps the column glanceable; the snapshot trim keeps the
    frame bounded. The store itself accepts the whole thing."""
    assert MAX_SUMMARY_CHARS == 2000
    made = _card(store, summary="n" * MAX_SUMMARY_CHARS)
    assert len(made["summary"]) == MAX_SUMMARY_CHARS


def test_an_over_long_summary_is_refused_rather_than_truncated(store):
    """Refused, in the house style: silently keeping the first N characters of
    somebody's sentence and showing *that* on the card is worse than saying so."""
    card, detail = store.create({"title": "x", "summary": "n" * (MAX_SUMMARY_CHARS + 1)})
    assert card is None
    assert "summary" in detail
    made = _card(store)
    got, detail = store.update(made["id"], {"summary": "n" * (MAX_SUMMARY_CHARS + 1)})
    assert got is None
    assert "summary" in detail


# --- forward compatibility ----------------------------------------------------


def test_a_schema_1_file_has_its_ready_cards_folded_into_backlog(tmp_path):
    """The upgrade to schema 2. Backlog rather than In progress, and it is not a
    close call: a card in Ready had never been started, and In progress now
    means an assistant is working — putting them there would have the board
    assert a run that never happened."""
    path = tmp_path / "board.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO schema_meta VALUES ('version', '1')")
    raw.execute("CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
                " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
                " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
                " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
                " session_id TEXT NOT NULL DEFAULT '',"
                " link_state TEXT NOT NULL DEFAULT '',"
                " dispatch_error TEXT NOT NULL DEFAULT '',"
                " dispatched_at REAL, session_ended_at REAL,"
                " author TEXT NOT NULL DEFAULT 'user', created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL, done_at REAL)")
    for cid, column in (("a", "ready"), ("b", "backlog"), ("c", "done")):
        raw.execute("INSERT INTO cards (id, project, title, column_name,"
                    " created_at, updated_at) VALUES (?, 'bob', ?, ?, 1, 1)",
                    (cid, cid, column))
    raw.commit()
    raw.close()

    store = BoardStore(path)
    store.connect()
    try:
        by_id = {c["id"]: c for c in store.cards()}
        assert by_id["a"]["column_name"] == "backlog"
        assert by_id["b"]["column_name"] == "backlog"
        assert by_id["c"]["column_name"] == "done"
        # And the column added in schema 2 arrived by ALTER, not by CREATE.
        assert by_id["a"]["summary"] == ""
    finally:
        store.close()


def test_the_ready_fold_does_not_touch_a_column_this_build_never_retired(tmp_path):
    """`_retire_ready` names one column on purpose. A blanket "rewrite anything
    I do not recognise" would be a downgraded build silently destroying a newer
    one's data, which is exactly what the forward-compatibility rule forbids."""
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    made = _card(store)
    store.close()

    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE cards SET column_name = 'blocked' WHERE id = ?",
                (made["id"],))
    raw.execute("UPDATE schema_meta SET value = '1' WHERE key = 'version'")
    raw.commit()
    raw.close()

    reopened = BoardStore(path)
    reopened.connect()
    try:
        assert reopened.get(made["id"])["column_name"] == "blocked"
    finally:
        reopened.close()


def test_a_row_carrying_an_unknown_column_still_loads(tmp_path):
    """A database written by a newer build opens in this one, and the fields this
    build does know about are all readable. Same rule sessions.json lives under."""
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    made = _card(store)
    store.close()

    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE cards ADD COLUMN swimlane TEXT")
    raw.execute("UPDATE cards SET swimlane = 'urgent' WHERE id = ?", (made["id"],))
    raw.commit()
    raw.close()

    reopened = BoardStore(path)
    reopened.connect()
    got = reopened.get(made["id"])
    assert got["title"] == "do the thing"
    # And a write that does not name the unknown column leaves it alone.
    reopened.update(made["id"], {"title": "renamed"})
    assert reopened.get(made["id"])["swimlane"] == "urgent"
    reopened.close()


def test_a_newer_schema_version_is_read_anyway(tmp_path, caplog):
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    store.close()
    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE schema_meta SET value = ? WHERE key = 'version'",
                (str(SCHEMA_VERSION + 5),))
    raw.commit()
    raw.close()

    reopened = BoardStore(path)
    reopened.connect()
    assert reopened.counts()["backlog"] == 0
    reopened.close()

    raw = sqlite3.connect(str(path))
    value = raw.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0]
    raw.close()
    assert int(value) == SCHEMA_VERSION + 5


def test_an_older_build_does_not_stamp_the_version_down(tmp_path, monkeypatch):
    """Both the .app and a dev checkout open the same file. Stamping down would
    re-run every upgrade step the next time the newer build opened it."""
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    store.close()

    # `_migrate` reads the module global at call time, so the patch bites.
    monkeypatch.setattr(board_mod, "SCHEMA_VERSION", SCHEMA_VERSION - 1)
    older = BoardStore(path)
    older.connect()
    older.close()

    raw = sqlite3.connect(str(path))
    value = raw.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0]
    raw.close()
    assert int(value) == SCHEMA_VERSION


def test_an_empty_file_migrates_to_the_current_version(tmp_path):
    path = tmp_path / "board.db"
    path.write_bytes(b"")
    store = BoardStore(path)
    store.connect()
    with sqlite3.connect(str(path)) as raw:
        row = raw.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    assert int(row[0]) == SCHEMA_VERSION
    store.close()


def test_connect_is_idempotent(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    _card(store)
    store.connect()
    assert store.total() == 1
    store.close()


# --- the archive --------------------------------------------------------------


def test_done_since_returns_the_recent_ones_newest_first(store):
    old = _card(store, title="old")
    new = _card(store, title="new")
    store.move(old["id"], "done")
    store.update(old["id"], {"done_at": time.time() - 86400 * 3})
    store.move(new["id"], "done")
    recent = store.done_since(time.time() - 3600)
    assert [c["title"] for c in recent] == ["new"]
    assert len(store.done_since(0)) == 2


def test_prune_done_only_ever_collects_done_cards(store):
    live = _card(store, title="live", column_name="in_progress")
    stale = _card(store, title="stale")
    store.move(stale["id"], "done")
    store.update(stale["id"], {"done_at": time.time() - 86400 * 90})
    assert store.prune_done(time.time() - 86400 * 30) == 1
    assert store.get(live["id"]) is not None
    assert store.get(stale["id"]) is None


# --- the stage track ----------------------------------------------------------


def test_workflow_is_declared_and_the_trail_is_not_seeded_from_it(store):
    """The two columns say different things and must never be conflated: one is
    what the card *expects*, the other is what Dark Army *saw*. A new card has
    expectations and no history, and that is what makes a hollow marker
    truthful."""
    card = _card(store, workflow=["bc-planner", "bc-implementer"])
    assert card["workflow"] == "bc-planner\nbc-implementer"
    assert card["agent_trail"] == ""


def test_workflow_accepts_the_shapes_its_three_callers_send(store):
    """The panel sends newlines, the channel tool sends a list, a person types
    commas. Normalised rather than refused — dropping a blank or a repeat is not
    a mistake worth an error message."""
    card = _card(store, workflow="a, b , a,,c")
    assert card["workflow"] == "a\nb\nc"
    updated, _ = store.update(card["id"], {"workflow": ["x", "y"]})
    assert updated["workflow"] == "x\ny"


def test_workflow_is_bounded_at_the_store(store):
    card = _card(store, workflow=[f"s{i}" for i in range(board_mod.MAX_STAGES + 5)])
    assert len(board_mod.parse_stages(card["workflow"])) == board_mod.MAX_STAGES
    long = _card(store, workflow=["x" * (board_mod.MAX_STAGE_CHARS + 20)])
    assert len(board_mod.parse_stages(long["workflow"])[0]) == \
        board_mod.MAX_STAGE_CHARS


def test_record_agents_appends_in_order_and_never_reorders(store):
    card = _card(store)
    store.record_agents(card["id"], ["bc-planner"])
    updated, changed = store.record_agents(
        card["id"], ["bc-planner", "bc-implementer"])
    assert changed is True
    assert board_mod.parse_stages(updated["agent_trail"]) == \
        ["bc-planner", "bc-implementer"]
    # An already-recorded stage arriving again is not news, and the caller folds
    # this into whether the board earns an SSE frame.
    _, changed = store.record_agents(card["id"], ["bc-planner", "bc-implementer"])
    assert changed is False


def test_record_agents_is_bounded_and_survives_a_missing_card(store):
    card = _card(store)
    store.record_agents(card["id"],
                        [f"s{i}" for i in range(board_mod.MAX_STAGES + 10)])
    trail = board_mod.parse_stages(store.get(card["id"])["agent_trail"])
    assert len(trail) == board_mod.MAX_STAGES
    assert store.record_agents("no-such-card", ["a"]) == (None, False)


def test_the_trail_is_not_writable_through_update(store):
    """`agent_trail` is Dark Army's record of what ran. A surface that could set it
    could claim a stage had happened that never did — the same argument that
    keeps `session_id` and `link_state` out of the writable set."""
    card = _card(store)
    assert "agent_trail" not in BoardStore._WRITABLE
    assert "workflow" in BoardStore._WRITABLE
    result, detail = store.update(card["id"], {"agent_trail": "invented"})
    assert result is None
    assert "writable" in detail
    assert store.get(card["id"])["agent_trail"] == ""


# ── the crew: who was on each observed stage ────────────────────────────────


def test_parse_crew_is_tolerant_of_a_ragged_value(store):
    """Read on every snapshot frame, so a ragged value degrades to "no crew" —
    which draws the roles' anchor faces — never to a board that will not
    decorate."""
    raw = ("bc-planner\toverwatch\n"
           "no-tab-at-all\n"
           "\tnobody\n"
           "empty-half\t\n"
           "\n"
           "bc-verifier\tledger")
    assert board_mod.parse_crew(raw) == {"bc-planner": "overwatch",
                                         "bc-verifier": "ledger"}
    assert board_mod.parse_crew(None) == {}
    assert board_mod.parse_crew("") == {}
    assert board_mod.parse_crew({"a": "b"}) == {"a": "b"}
    big = {f"s{i}": f"c{i}" for i in range(board_mod.MAX_STAGES + 10)}
    assert len(board_mod.parse_crew(big)) == board_mod.MAX_STAGES


def test_join_crew_round_trips_and_is_bounded():
    faces = {"bc-planner": "overwatch", "bc-implementer": "relay"}
    assert board_mod.parse_crew(board_mod.join_crew(faces)) == faces
    assert board_mod.join_crew({}) == ""


def test_record_agents_writes_both_columns(store):
    card = _card(store)
    updated, changed = store.record_agents(
        card["id"], ["bc-planner", "bc-implementer"],
        {"bc-planner": "overwatch", "bc-implementer": "relay"})
    assert changed is True
    assert board_mod.parse_crew(updated["crew_trail"]) == {
        "bc-planner": "overwatch", "bc-implementer": "relay"}
    assert board_mod.parse_stages(updated["agent_trail"]) == \
        ["bc-planner", "bc-implementer"]


def test_a_recorded_stage_never_has_its_character_rewritten(store):
    """The whole memory: a finished part of the job keeps the face that did it
    even after that character has been allocated to somebody else's card."""
    card = _card(store)
    store.record_agents(card["id"], ["bc-verifier"], {"bc-verifier": "ledger"})
    updated, changed = store.record_agents(
        card["id"], ["bc-verifier"], {"bc-verifier": "zosia"})
    assert changed is False
    assert board_mod.parse_crew(updated["crew_trail"]) == {"bc-verifier": "ledger"}


def test_a_character_for_a_stage_that_is_not_in_the_trail_is_dropped(store):
    """`crew_trail` never names a stage `agent_trail` does not."""
    card = _card(store)
    updated, _ = store.record_agents(card["id"], ["bc-planner"],
                                     {"bc-planner": "overwatch",
                                      "bc-verifier": "ledger"})
    assert board_mod.parse_crew(updated["crew_trail"]) == {"bc-planner": "overwatch"}


def test_record_agents_without_a_crew_writes_no_faces(store):
    card = _card(store)
    updated, changed = store.record_agents(card["id"], ["bc-planner"])
    assert changed is True
    assert updated["crew_trail"] == ""


def test_the_crew_is_not_writable_through_update(store):
    """A surface that could write it could put a face on a stage that never
    ran — `agent_trail`'s argument, one rung on."""
    card = _card(store)
    assert "crew_trail" not in BoardStore._WRITABLE
    assert "crew_trail" not in board_mod.REVISED_COLUMNS
    assert board_mod.SINGLE_WRITER["crew_trail"] == "record_agents"
    result, detail = store.update(card["id"], {"crew_trail": "bc-planner\tinvented"})
    assert result is None
    assert "writable" in detail
    assert store.get(card["id"])["crew_trail"] == ""


def test_recording_a_crew_does_not_move_the_revision(store):
    """Dark Army observing is not somebody editing: a revision that moved here would
    make the phone re-download the board continuously."""
    card = _card(store)
    before = store.get(card["id"])["revision"]
    store.record_agents(card["id"], ["bc-planner"], {"bc-planner": "overwatch"})
    assert store.get(card["id"])["revision"] == before


def test_a_v18_file_gains_crew_trail_by_alter(tmp_path):
    """`_ADDED_COLUMNS`' rule at v19: the column carries a DEFAULT, so a v18
    build opening this file goes on INSERTing into a table that has it and
    reads `''` as "no crew"."""
    path = tmp_path / "v18.db"
    # The v18 table is today's, minus the one column this migration adds —
    # built from `_SCHEMA` itself so the fixture cannot drift from the DDL.
    ddl = "\n".join(line for line in board_mod._SCHEMA.splitlines()
                    if "crew_trail" not in line
                    and not line.strip().startswith("--"))
    conn = sqlite3.connect(path)
    conn.executescript(ddl[:ddl.find("CREATE UNIQUE INDEX IF NOT EXISTS")])
    conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', '18')")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, agent_trail,"
        " created_at, updated_at) VALUES ('c1', 'bob', 'old card',"
        " 'in_progress', 'bc-planner', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "crew_trail" in names
        card = store.get("c1")
        assert card["crew_trail"] == ""
        assert card["agent_trail"] == "bc-planner"
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_the_create_and_alter_spellings_of_crew_trail_match_exactly():
    assert ("crew_trail", "TEXT NOT NULL DEFAULT ''") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "crew_trail      TEXT NOT NULL DEFAULT ''," in board_mod._SCHEMA


def test_a_new_card_is_born_with_no_crew(store):
    card = _card(store)
    assert card["crew_trail"] == ""


def test_record_agents_does_not_stamp_updated_at(store):
    """Dark Army observing a stage is not somebody editing the card, and the Done
    archive sorts on that column."""
    card = _card(store)
    before = store.get(card["id"])["updated_at"]
    store.record_agents(card["id"], ["bc-planner"])
    assert store.get(card["id"])["updated_at"] == before


def test_fill_workflow_if_empty_is_conditional_and_preserves_history(store):
    card = _card(store)
    store.record_agents(card["id"], ["observed-first"])
    before = store.get(card["id"])

    filled, changed = store.fill_workflow_if_empty(
        card["id"], ["bc-implementer", "bc-verifier", "bc-implementer"])
    assert changed is True
    assert filled["workflow"] == "bc-implementer\nbc-verifier"
    assert filled["agent_trail"] == before["agent_trail"]
    assert filled["updated_at"] == before["updated_at"]

    kept, changed = store.fill_workflow_if_empty(card["id"], ["invented"])
    assert changed is False
    assert kept["workflow"] == filled["workflow"]
    assert kept["agent_trail"] == before["agent_trail"]
    assert kept["updated_at"] == before["updated_at"]


def test_fill_workflow_if_empty_refuses_empty_or_missing_cards(store):
    card = _card(store)
    before = store.get(card["id"])
    same, changed = store.fill_workflow_if_empty(card["id"], [])
    assert changed is False and same == before
    assert store.fill_workflow_if_empty("missing", ["bc-implementer"]) == \
        (None, False)


def test_a_database_from_before_the_stage_track_gains_both_columns(tmp_path):
    """The `_ADDED_COLUMNS` promise, and its first real use. Both carry a
    DEFAULT, so a build that predates them goes on INSERTing successfully into a
    table that has them."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user', created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "workflow" in names and "agent_trail" in names
        card = store.get("c1")
        assert card["workflow"] == "" and card["agent_trail"] == ""
        assert store.record_agents("c1", ["bc-planner"])[1] is True
    finally:
        store.close()


# --- the close signature ------------------------------------------------------


def test_declare_done_moves_the_card_and_records_who_said_so(store):
    """The whole verb. Dark Army established nothing here — it recorded a statement,
    which is why the row keeps the speaker as well as the words."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    closed, detail = store.declare_done(card["id"], "sess-1", "tests pass")
    assert closed is not None, detail
    assert closed["column_name"] == "done"
    assert closed["done_at"] is not None
    assert closed["closed_by"] == "sess-1"
    assert closed["close_note"] == "tests pass"


def test_declare_done_never_passes_through_update(store, monkeypatch):
    """Its own single UPDATE, not a call into `update()` — which is what makes
    the daemon's card-arrives-in-Done wrap-up structurally unreachable from an
    agent closing its own card, rather than a flag somebody has to remember."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")

    def boom(*a, **k):
        raise AssertionError("declare_done went through update()")

    monkeypatch.setattr(store, "update", boom)
    closed, detail = store.declare_done(card["id"], "sess-1", "tests pass")
    assert closed is not None, detail
    assert closed["column_name"] == "done"


def test_a_session_cannot_close_a_card_that_is_not_its_own(store):
    """The scope restriction, at the store. The daemon resolves the card from
    the caller rather than taking a card id, and this is the second guard: even
    a caller that reached here with somebody else's id closes nothing."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    closed, detail = store.declare_done(card["id"], "sess-2", "I finished it")
    assert closed is None
    assert "not this session's" in detail
    assert store.get(card["id"])["column_name"] == "in_progress"


def test_an_unbound_card_cannot_be_closed_by_anybody(store):
    """`session_id` defaults to `''`, so an empty owner matching an empty
    caller would let any unattributable request close the backlog. Refused
    here as well as guarded in `by_session`, because the failure is silent."""
    card = _card(store)
    closed, detail = store.declare_done(card["id"], "", "done I think")
    assert closed is None
    assert store.get(card["id"])["column_name"] == "prep"
    assert store.get(card["id"])["closed_by"] == ""


def test_a_card_already_in_done_cannot_be_closed_again(store):
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    assert store.declare_done(card["id"], "sess-1", "first")[0] is not None
    closed, detail = store.declare_done(card["id"], "sess-1", "second")
    assert closed is None
    assert "already done" in detail
    assert store.get(card["id"])["close_note"] == "first"


def test_declare_done_on_a_card_that_does_not_exist(store):
    assert store.declare_done("nope", "sess-1", "done")[0] is None


def test_a_close_with_no_reason_is_refused(store):
    """A signature with nothing under it tells a reader of the Done column
    nothing at all, which is the one thing this pair of columns is for."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    for note in ("", "   ", "\n\t "):
        closed, detail = store.declare_done(card["id"], "sess-1", note)
        assert closed is None, note
        assert "a reason" in detail
    assert store.get(card["id"])["column_name"] == "in_progress"


def test_an_over_long_note_is_clamped_rather_than_refused(store):
    """Dark Army's own record of somebody's sentence, not instructions about to be
    handed to an agent — so `MAX_PROMPT_CHARS`' refuse-don't-truncate argument
    does not apply, and a long note must not cost somebody their close."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    closed, detail = store.declare_done(card["id"], "sess-1",
                                        "x" * (MAX_CLOSE_NOTE_CHARS + 500))
    assert closed is not None, detail
    assert len(closed["close_note"]) == MAX_CLOSE_NOTE_CHARS


def test_reopening_a_card_throws_the_signature_away(store):
    """Reopen is the undo and it has to undo the whole close. A card back in
    Backlog still carrying `closed_by` would, on its next hand-drag into Done,
    claim a verifier had checked it."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    back, _ = store.update(card["id"], {"column_name": "backlog"})
    assert back["column_name"] == "backlog"
    assert back["done_at"] is None
    assert back["closed_by"] == ""
    assert back["close_note"] == ""


def test_no_surface_can_write_a_signature_through_update(store):
    """`closed_by` and `close_note` are outside `_WRITABLE`. A surface that
    could set them could paint a hand-dragged card with a verifier's name."""
    assert "closed_by" not in BoardStore._WRITABLE
    assert "close_note" not in BoardStore._WRITABLE
    card = _card(store)
    updated, detail = store.update(card["id"], {
        "closed_by": "bc-verifier", "close_note": "trust me"})
    assert updated is None
    assert detail == "no writable fields in that update"
    stored = store.get(card["id"])
    assert stored["closed_by"] == "" and stored["close_note"] == ""


def test_a_card_reopened_under_the_close_is_not_closed(store):
    """The window this pair of columns exists to be incapable of. A human
    presses Back or Reopen while the close is in flight; without the guard in
    the statement the write lands anyway and the card is back in Done wearing
    a signature for a close the human explicitly withdrew.

    `update` does not clear `session_id` on the way out of In progress, so the
    identity test alone would not catch this — the column is pinned to the one
    that was observed for exactly that reason.
    """
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    real_get = store.get
    fired = []

    def racing_get(card_id):
        row = real_get(card_id)
        if not fired:
            fired.append(True)
            store.update(card_id, {"column_name": "backlog"})
        return row

    store.get = racing_get
    try:
        closed, detail = store.declare_done(card["id"], "sess-1", "tests pass")
    finally:
        store.get = real_get
    assert closed is None
    assert "nothing was closed" in detail
    stored = store.get(card["id"])
    assert stored["column_name"] == "backlog"
    assert stored["closed_by"] == "" and stored["close_note"] == ""


def test_a_card_deleted_under_the_close_reports_that_nothing_happened(store):
    """The same window swallowed a delete: the UPDATE matched nothing and the
    re-read that used to follow it answered `None, "closed"` — a card that no
    longer exists, reported as successfully closed."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    real_get = store.get
    fired = []

    def racing_get(card_id):
        row = real_get(card_id)
        if not fired:
            fired.append(True)
            store.delete(card_id)
        return row

    store.get = racing_get
    try:
        closed, detail = store.declare_done(card["id"], "sess-1", "tests pass")
    finally:
        store.get = real_get
    assert closed is None
    assert "nothing was closed" in detail
    assert store.get(card["id"]) is None


# --- the outstanding hand-check ----------------------------------------------


def test_flag_manual_records_the_steps_and_moves_nothing(store):
    """The verb. It is the opposite statement to `declare_done` about the same
    card, and the card staying put is half of what it means: In progress goes
    on meaning an assistant is working, and the badge says a person is."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    flagged, detail = store.flag_manual(
        card["id"], "sess-1",
        "1. Open the board.\nWhy not automated: it is a look at a screen.")
    assert flagged is not None, detail
    assert flagged["column_name"] == "in_progress"
    assert flagged["manual_steps"].startswith("1. Open the board.")


def test_manual_steps_is_outside_writable(store):
    """One writer, `closed_by`'s reason: a surface that could set this could
    hang a chore on a card nobody flagged, and the badge's whole value is that
    it appears only where the session that did the work put it."""
    assert "manual_steps" not in BoardStore._WRITABLE
    card = _card(store)
    updated, _ = store.update(card["id"], {"manual_steps": "go and look"})
    assert store.get(card["id"])["manual_steps"] == ""


def test_a_session_cannot_flag_a_card_that_is_not_its_own(store):
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    flagged, detail = store.flag_manual(card["id"], "sess-2", "1. look")
    assert flagged is None
    assert "not this session's" in detail
    assert store.get(card["id"])["manual_steps"] == ""


def test_an_unbound_card_cannot_be_flagged_by_anybody(store):
    """`session_id` defaults to `''`, so an empty owner matching an empty
    caller would let any unattributable request badge the whole backlog."""
    card = _card(store)
    flagged, detail = store.flag_manual(card["id"], "", "1. look")
    assert flagged is None
    assert store.get(card["id"])["manual_steps"] == ""


def test_a_card_in_done_can_be_flagged_by_its_own_session(store):
    """v26: a card with an open check goes to Done, so the flag may follow
    the close. The WHERE still pins the session: another session is refused
    and the card stays in Done either way."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    flagged, detail = store.flag_manual(card["id"], "sess-2", "1. look")
    assert flagged is None
    assert "not this session's" in detail
    flagged, detail = store.flag_manual(card["id"], "sess-1", "1. look")
    assert flagged is not None, detail
    assert flagged["column_name"] == "done"
    assert flagged["manual_steps"] == "1. look"


def test_flag_manual_writes_and_clears_the_check_path(store):
    """The file rides the same UPDATE; a re-flag with no file clears a stale
    link, and `by_manual_check_path` is an exact match that never answers
    for `''`."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    flagged, _ = store.flag_manual(card["id"], "sess-1", "1. look",
                                   "/p/manual-check/a/check.md")
    assert flagged["manual_check_path"] == "/p/manual-check/a/check.md"
    assert [c["id"] for c in store.by_manual_check_path(
        "/p/manual-check/a/check.md")] == [card["id"]]
    assert store.by_manual_check_path("/p/manual-check/b/check.md") == []
    assert store.by_manual_check_path("") == []
    flagged, _ = store.flag_manual(card["id"], "sess-1", "1. look again")
    assert flagged["manual_check_path"] == ""
    assert store.by_manual_check_path("") == []


def test_manual_check_path_has_one_writer_and_moves_the_revision():
    assert board_mod.SINGLE_WRITER["manual_check_path"] == "flag_manual"
    assert "manual_check_path" not in BoardStore._WRITABLE
    assert "manual_check_path" in board_mod.REVISED_COLUMNS


def test_a_schema_25_file_gains_manual_check_path_by_alter(tmp_path):
    """The `_ADDED_COLUMNS` contract at v26: a v25 store opens with the
    column present and `''` on every existing row, and a v25-shaped INSERT
    that does not name it still lands."""
    path = tmp_path / "v25.db"
    store = BoardStore(path)
    store.connect()
    store._conn.execute("ALTER TABLE cards DROP COLUMN manual_check_path")
    store._conn.execute(
        "UPDATE schema_meta SET value = '25' WHERE key = 'version'")
    store._conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    store._conn.commit()
    store.close()
    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "manual_check_path" in names
        assert store.get("c1")["manual_check_path"] == ""
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v25 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["manual_check_path"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_a_flag_with_no_steps_is_refused(store):
    """A badge with nothing behind it is the state this column exists to be
    incapable of showing."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    for steps in ("", "   ", "\n\t "):
        flagged, detail = store.flag_manual(card["id"], "sess-1", steps)
        assert flagged is None, steps
        assert "its steps" in detail


def test_over_long_steps_are_clamped_rather_than_refused(store):
    """`close_note`'s argument: Dark Army is relaying somebody's words to a person,
    so a long one truncated is still readable where a long one refused is a
    check nobody hears about."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    flagged, detail = store.flag_manual(card["id"], "sess-1",
                                        "x" * (MAX_MANUAL_STEPS_CHARS + 500))
    assert flagged is not None, detail
    assert len(flagged["manual_steps"]) == MAX_MANUAL_STEPS_CHARS


def test_a_card_moved_under_the_flag_reports_that_nothing_happened(store):
    """`declare_done`'s race and `declare_done`'s answer: the guard rides in
    the UPDATE's own WHERE clause, so a card dragged in the seconds this spends
    on the executor is a `rowcount == 0` and not a stale stamp."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    real_get = store.get
    fired = []

    def racing_get(card_id):
        row = real_get(card_id)
        if not fired:
            fired.append(True)
            store.update(card_id, {"column_name": "backlog"})
        return row

    store.get = racing_get
    try:
        flagged, detail = store.flag_manual(card["id"], "sess-1", "1. look")
    finally:
        store.get = real_get
    assert flagged is None
    assert "nothing was flagged" in detail
    assert store.get(card["id"])["manual_steps"] == ""


def test_clear_manual_empties_the_field_and_moves_nothing(store):
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.flag_manual(card["id"], "sess-1", "1. look")
    cleared, detail = store.clear_manual(card["id"])
    assert cleared is not None, detail
    assert cleared["manual_steps"] == ""
    assert cleared["column_name"] == "in_progress"


def test_clearing_a_card_with_nothing_outstanding_is_refused(store):
    card = _card(store)
    cleared, detail = store.clear_manual(card["id"])
    assert cleared is None
    assert "no manual check" in detail
    assert store.clear_manual("nope")[0] is None


def test_by_session_finds_a_bound_card(store):
    a = _card(store, title="mine")
    b = _card(store, title="theirs")
    store.bind_session(a["id"], "sess-1")
    store.bind_session(b["id"], "sess-2")
    found = store.by_session("sess-1")
    assert [c["id"] for c in found] == [a["id"]]


def test_by_session_with_an_empty_id_matches_nothing(store):
    """`session_id` defaults to `''`, so without the guard this would return
    the whole backlog — and the one caller closes a card when it sees exactly
    one match."""
    _card(store)
    assert store.by_session("") == []
    assert store.by_session(None) == []


def test_a_schema_3_file_gains_the_close_columns_by_alter(tmp_path):
    """The `_ADDED_COLUMNS` contract again, at v4. Both carry a DEFAULT, so a
    schema-3 build goes on INSERTing into a table that has them, and the
    existing rows are untouched."""
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '3')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, session_id,"
        " created_at, updated_at) VALUES ('c1', 'bob', 'old card',"
        " 'in_progress', 'sess-1', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "closed_by" in names and "close_note" in names
        card = store.get("c1")
        assert card["title"] == "old card"
        assert card["closed_by"] == "" and card["close_note"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
        assert store.declare_done("c1", "sess-1", "checked")[0] is not None
    finally:
        store.close()


# --- waiting-on marks and in-column order ------------------------------------


def test_a_schema_4_file_gains_blocked_by_by_alter(tmp_path):
    """The `_ADDED_COLUMNS` contract at v5. DEFAULT means a v4-shaped INSERT
    still works, and existing rows read as empty."""
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '4')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " closed_by TEXT NOT NULL DEFAULT '',"
        " close_note TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "blocked_by" in names
        card = store.get("c1")
        assert card["blocked_by"] == ""
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v4 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["blocked_by"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_a_schema_24_file_gains_kind_and_report_path_by_alter(tmp_path):
    """The `_ADDED_COLUMNS` contract at v25. DEFAULT means a v24-shaped INSERT
    still works, and existing rows read as empty."""
    path = tmp_path / "v24.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '24')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " start_when_planned TEXT NOT NULL DEFAULT '',"
        " priority TEXT NOT NULL DEFAULT '',"
        " area TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "kind" in names and "report_path" in names
        card = store.get("c1")
        assert card["kind"] == "" and card["report_path"] == ""
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v24 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["kind"] == ""
        assert store.get("c2")["report_path"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_reorder_places_a_card_before_another(store):
    a = _card(store, title="first")
    b = _card(store, title="second")
    c = _card(store, title="last")
    got, detail = store.reorder(c["id"], "prep", a["id"])
    assert got is not None, detail
    ids = [x["id"] for x in store.cards(columns=["prep"])]
    assert ids == [c["id"], a["id"], b["id"]]


def test_reorder_before_itself_is_a_noop(store):
    a = _card(store, title="a")
    b = _card(store, title="b")
    got, detail = store.reorder(a["id"], "prep", a["id"])
    assert got is not None, detail
    ids = [x["id"] for x in store.cards(columns=["prep"])]
    assert ids == [a["id"], b["id"]]
    assert store.get(a["id"])["position"] == got["position"]


def test_reorder_with_empty_before_id_appends(store):
    a = _card(store, title="a")
    b = _card(store, title="b")
    got, detail = store.reorder(a["id"], "prep", "")
    assert got is not None, detail
    ids = [x["id"] for x in store.cards(columns=["prep"])]
    assert ids == [b["id"], a["id"]]


def test_reorder_reindexes_a_tight_gap_and_leaves_other_columns(store):
    a = _card(store, title="a")
    b = _card(store, title="b")
    c = _card(store, title="c")
    other = _card(store, title="elsewhere", column_name="in_progress")
    store.update(other["id"], {"position": 99.0})
    store.update(a["id"], {"position": 1.0})
    store.update(b["id"], {"position": 1.0 + 1e-8})
    store.update(c["id"], {"position": 2.0})
    got, detail = store.reorder(c["id"], "prep", b["id"])
    assert got is not None, detail
    ordered = store.cards(columns=["prep"])
    assert [x["id"] for x in ordered] == [a["id"], c["id"], b["id"]]
    positions = [x["position"] for x in ordered]
    assert positions == [1.0, 1.5, 2.0]
    assert store.get(other["id"])["position"] == 99.0


def test_a_card_cannot_wait_on_itself(store):
    a = _card(store, title="a")
    got, detail = store.update(a["id"], {"blocked_by": a["id"]})
    assert got is None
    assert "cannot wait on itself" in detail


def test_a_cycle_of_waiters_is_refused(store):
    a = _card(store, title="a")
    b = _card(store, title="b")
    got, detail = store.update(a["id"], {"blocked_by": b["id"]})
    assert got is not None, detail
    got, detail = store.update(b["id"], {"blocked_by": a["id"]})
    assert got is None
    assert "already wait on each other" in detail
    assert store.get(b["id"])["blocked_by"] == ""


def test_a_card_cannot_wait_on_a_card_in_another_project(store):
    """The queue, the drain and the parallel limit are per project, so a
    hold across projects would stall one queue with nothing on its screen
    saying why. Refused in words, and the card is left exactly as it was."""
    a = _card(store, title="here", root="/tmp/bob")
    b = _card(store, title="elsewhere", root="/tmp/other")
    before = store.get(a["id"])
    got, detail = store.update(a["id"], {"blocked_by": b["id"]})
    assert got is None
    assert detail == "a card can only wait on a card in its own project"
    after = store.get(a["id"])
    assert after["blocked_by"] == "" and after["revision"] == before["revision"]
    # The same folder spelt through a symlink-free trailing slash is the
    # same project: the check canonicalises both sides.
    c = _card(store, title="same place", root="/tmp/bob/")
    got, detail = store.update(a["id"], {"blocked_by": c["id"]})
    assert got is not None, detail
    # An id naming no card is not refused — it can hold nothing.
    got, detail = store.update(a["id"], {"blocked_by": "gone0000"})
    assert got is not None, detail


def test_cards_by_id_returns_only_the_ids_that_exist(store):
    a = _card(store, title="a")
    b = _card(store, title="b")
    got = store.cards_by_id([a["id"], "missing", b["id"], a["id"], ""])
    assert set(got) == {a["id"], b["id"]}
    assert got[b["id"]]["title"] == "b"
    assert store.cards_by_id([]) == {}
    assert store.cards_by_id(None) == {}


def test_fill_dependencies_if_empty_fills_only_an_empty_list(store):
    a = _card(store, title="waiter")
    b = _card(store, title="first")
    c = _card(store, title="second")
    card, detail = store.fill_dependencies_if_empty(a["id"], [b["id"]])
    assert detail == "filled"
    assert parse_ids(card["blocked_by"]) == [b["id"]]
    assert card["revision"] > a["revision"]
    # A list somebody already set is theirs: nothing moves.
    card, detail = store.fill_dependencies_if_empty(a["id"], [c["id"]])
    assert detail == "already set"
    assert parse_ids(store.get(a["id"])["blocked_by"]) == [b["id"]]
    # Nothing to fill is not a write.
    card, detail = store.fill_dependencies_if_empty(c["id"], [])
    assert detail == "nothing to fill" and card["blocked_by"] == ""
    assert store.fill_dependencies_if_empty("no-such", [b["id"]]) == (
        None, "no such card")


def test_fill_dependencies_if_empty_surfaces_the_stores_refusals(store):
    """A seed is not the one writer that skips the rules: a cycle, a
    self-wait and another project refuse it in the store's own words."""
    a = _card(store, title="a")
    b = _card(store, title="b")
    store.update(a["id"], {"blocked_by": b["id"]})
    got, detail = store.fill_dependencies_if_empty(b["id"], [a["id"]])
    assert got is None and detail == "those cards already wait on each other"
    assert store.get(b["id"])["blocked_by"] == ""
    got, detail = store.fill_dependencies_if_empty(b["id"], [b["id"]])
    assert got is None and detail == "a card cannot wait on itself"
    far = _card(store, title="far", root="/tmp/far")
    got, detail = store.fill_dependencies_if_empty(b["id"], [far["id"]])
    assert got is None
    assert detail == "a card can only wait on a card in its own project"


def test_a_schema_28_file_has_its_leftover_dependencies_cleared_once(tmp_path):
    """v29: `blocked_by` gates Start again, so a value a build from before
    20 Sep 2026 left behind is emptied on the first open — the column kept,
    `_retire_initiatives`' rule — and a list a person writes after the
    upgrade is never touched by a later open (forward-only marker)."""
    path = tmp_path / "v28.db"
    store = BoardStore(path)
    store.connect()
    store._conn.execute(
        "UPDATE schema_meta SET value = '28' WHERE key = 'version'")
    store._conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at, blocked_by) VALUES ('c1', 'bob', 'old card', 'backlog',"
        " 1, 1, 'stale')")
    store._conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c2', 'bob', 'other', 'backlog', 2, 2)")
    store._conn.commit()
    store.close()
    store = BoardStore(path)
    store.connect()
    try:
        assert store.get("c1")["blocked_by"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
        got, detail = store.update("c1", {"blocked_by": "c2"})
        assert got is not None, detail
    finally:
        store.close()
    store = BoardStore(path)
    store.connect()
    try:
        assert parse_ids(store.get("c1")["blocked_by"]) == ["c2"]
    finally:
        store.close()


def test_active_blockers_is_gone_and_the_daemon_resolves_dependencies():
    """One resolver: `daemon_board._dependency_entries`. A second store-side
    reading of "met" would be the two-surfaces-disagree bug."""
    assert not hasattr(BoardStore, "active_blockers")


def test_parse_ids_drops_blanks_dedupes_and_caps():
    assert parse_ids("a\n\nb\na\n") == ["a", "b"]
    assert parse_ids("a, b, a") == ["a", "b"]
    assert parse_ids(["x", "", "x", "y"]) == ["x", "y"]
    many = [f"id{i}" for i in range(MAX_BLOCKERS + 5)]
    assert parse_ids(many) == many[:MAX_BLOCKERS]


# --- the Prep column, the refine fields and attach_plan (schema 6) ------------


def test_a_schema_5_file_gains_the_refine_columns_and_moves_nothing(tmp_path):
    """The `_ADDED_COLUMNS` contract at v6, plus the migration promise the plan
    makes out loud: **no rows move**. A Backlog card written under schema 5 is
    still in Backlog after the upgrade — only newly created cards default into
    Prep."""
    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '5')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " closed_by TEXT NOT NULL DEFAULT '',"
        " close_note TEXT NOT NULL DEFAULT '',"
        " blocked_by TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert {"plan_path", "refine_session_id", "refine_state"} <= names
        card = store.get("c1")
        assert card["column_name"] == "backlog"
        assert card["plan_path"] == ""
        assert card["refine_session_id"] == "" and card["refine_state"] == ""
        # A v5-shaped INSERT still works: every new column carries a DEFAULT.
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v5 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["plan_path"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_plan_path_is_not_store_writable():
    """`plan_path` is the Prep column's exit condition and `attach_plan` is its
    only writer. A surface (or the daemon's own `update`) that could set it
    could stamp a card "planned" and walk it past the plan gate with no plan
    behind it."""
    assert "plan_path" not in BoardStore._WRITABLE
    assert "refine_session_id" in BoardStore._WRITABLE
    assert "refine_state" in BoardStore._WRITABLE


def test_plan_path_cannot_be_written_through_update(store):
    card = _card(store)
    updated, detail = store.update(card["id"], {"plan_path": "/tmp/x.md"})
    assert updated is None
    assert detail == "no writable fields in that update"
    assert store.get(card["id"])["plan_path"] == ""


def test_update_refuses_an_unknown_refine_state(store):
    card = _card(store)
    updated, detail = store.update(card["id"], {"refine_state": "thinking"})
    assert updated is None
    assert "thinking" in detail


def test_by_refine_session_with_an_empty_id_matches_nothing(store):
    """`refine_session_id` defaults to `''` — `by_session`'s documented trap,
    inherited whole: without the guard an empty caller would match the entire
    unrefined board and the single-match ladder would attach a plan to a card
    at random."""
    _card(store)
    assert store.by_refine_session("") == []
    assert store.by_refine_session(None) == []


def test_by_refine_session_finds_the_refining_card(store):
    a = _card(store, title="mine")
    _card(store, title="theirs")
    store.update(a["id"], {"refine_session_id": "sess-1",
                           "refine_state": "live"})
    found = store.by_refine_session("sess-1")
    assert [c["id"] for c in found] == [a["id"]]


def test_attach_plan_lands_the_card_in_backlog_in_one_write(store):
    card = _card(store)
    store.update(card["id"], {"refine_session_id": "sess-1",
                              "refine_state": "live"})
    attached, detail = store.attach_plan(
        card["id"], "/tmp/bob/plans/x.md", "sess-1")
    assert attached is not None, detail
    assert attached["column_name"] == "backlog"
    assert attached["plan_path"] == "/tmp/bob/plans/x.md"
    assert attached["refine_state"] == ""
    assert attached["refine_session_id"] == "sess-1"


def test_attach_plan_refuses_a_card_not_in_prep(store):
    card = _card(store, column_name="backlog")
    attached, detail = store.attach_plan(card["id"], "/tmp/bob/p.md", "s1")
    assert attached is None
    assert "not in Prep" in detail


def test_attach_plan_refuses_a_card_that_already_has_a_plan(store):
    """Re-refinement is deliberately out of scope, and the refusal is the
    record of that decision."""
    card = _card(store)
    assert store.attach_plan(card["id"], "/tmp/bob/a.md", "s1")[0] is not None
    # Back to prep by hand; the plan survives, so a second attach is refused.
    store.move(card["id"], "prep")
    attached, detail = store.attach_plan(card["id"], "/tmp/bob/b.md", "s1")
    assert attached is None
    assert "already has a plan" in detail
    assert store.get(card["id"])["plan_path"] == "/tmp/bob/a.md"


def test_attach_plan_answers_moved_when_the_card_shifts_under_it(store):
    """`declare_done`'s race, `declare_done`'s answer: the guard is the
    UPDATE's own WHERE clause, so a card moved between the read and the write
    matches nothing and the caller is told the truth."""
    card = _card(store)
    real_get = store.get
    fired = []

    def racing_get(card_id):
        row = real_get(card_id)
        if not fired:
            fired.append(True)
            store.update(card_id, {"column_name": "in_progress"})
        return row

    store.get = racing_get
    try:
        attached, detail = store.attach_plan(card["id"], "/tmp/bob/p.md", "s1")
    finally:
        store.get = real_get
    assert attached is None
    assert "moved" in detail
    assert store.get(card["id"])["plan_path"] == ""


def test_reorder_into_prep_with_the_unlink_extra_clears_the_session(store):
    """The daemon sends the unlink extra on a cross-column drop into any
    startable column; the store's `reorder` has to carry it in the same
    write, or a card lands in Prep still wearing a dead session id and
    `dispatch.guard` refuses it forever."""
    card = _card(store, column_name="backlog", tool="claude")
    store.bind_session(card["id"], "sess-1")
    got, detail = store.reorder(card["id"], "prep", "", {
        "session_id": "", "link_state": "", "dispatch_error": "",
        "dispatched_at": None, "session_ended_at": None,
    })
    assert got is not None, detail
    assert got["column_name"] == "prep"
    assert got["session_id"] == "" and got["link_state"] == ""


# --- the folders (initiatives) are retired at schema 22 ----------------------


def test_an_older_file_with_folders_keeps_the_shape_and_empties_them(tmp_path):
    """A schema-21 file keeps the `initiatives` table and the two membership
    columns so a downgraded build can still open the file. This build empties
    both and never reads them: cards stay where they are."""
    import sqlite3
    path = tmp_path / "board.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(board_mod._SCHEMA)
    conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', '21')")
    conn.execute(
        "INSERT INTO initiatives VALUES ('f1', 'bob', 'Board polish', 1, 1, 1)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, initiative_id,"
        " initiative_by, created_at, updated_at)"
        " VALUES ('c1', 'bob', 'filed card', 'backlog', 'f1', 'user', 1, 1)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c2', 'bob', 'loose card', 'prep', 2, 2)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        tables = {r["name"] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "initiatives" in tables
        names = {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(cards)")}
        assert {"initiative_id", "initiative_by"} <= names
        remaining = store._conn.execute(
            "SELECT COUNT(*) AS n FROM initiatives").fetchone()["n"]
        assert remaining == 0
        stored = store._conn.execute(
            "SELECT initiative_id, initiative_by FROM cards WHERE id = 'c1'"
        ).fetchone()
        assert stored["initiative_id"] == "" and stored["initiative_by"] == ""
        filed = store.get("c1")
        assert filed["column_name"] == "backlog" and filed["title"] == "filed card"
        assert "initiative_id" not in filed and "initiative_by" not in filed
        loose = store.get("c2")
        assert loose["column_name"] == "prep" and loose["project"] == "bob"
        version = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(version["value"]) == board_mod.SCHEMA_VERSION
        made = _card(store)
        assert "initiative_id" not in made and "initiative_by" not in made
        # An older build's INSERT still names the retired column.
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, initiative_id,"
            " created_at, updated_at) VALUES ('c3', 'bob', 'downgrade write',"
            " 'prep', 'f1', 3, 3)")
        store._conn.commit()
        assert store.get("c3")["title"] == "downgrade write"
        assert "initiative_id" not in store.get("c3")
    finally:
        store.close()


def test_a_file_that_already_dropped_folders_gets_the_shape_back(tmp_path):
    """A schema-22 file written by the drop-the-table build still opens: the
    table and columns come back empty, and no card is rewritten."""
    import sqlite3
    path = tmp_path / "board.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(board_mod._SCHEMA)
    conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', '22')")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'kept card', 'backlog', 1, 1)")
    conn.execute("DROP TABLE IF EXISTS initiatives")
    have = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    for column in ("initiative_id", "initiative_by"):
        if column in have:
            try:
                conn.execute(f"ALTER TABLE cards DROP COLUMN {column}")
            except sqlite3.OperationalError:
                pass
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        tables = {r["name"] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "initiatives" in tables
        names = {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(cards)")}
        assert {"initiative_id", "initiative_by"} <= names
        assert store.get("c1")["title"] == "kept card"
        assert "initiative_id" not in store.get("c1")
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, initiative_id,"
            " created_at, updated_at) VALUES ('c2', 'bob', 'older insert',"
            " 'prep', '', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["title"] == "older insert"
    finally:
        store.close()


def test_a_new_card_carries_no_folder_field(store):
    card = _card(store)
    assert "initiative_id" not in card
    assert "initiative_by" not in card
    assert "initiative_id" not in BoardStore._WRITABLE
    assert "initiative_by" not in board_mod.SINGLE_WRITER
    assert "initiative_id" not in board_mod.REVISED_COLUMNS
    for verb in ("initiatives", "create_initiative", "rename_initiative",
                 "delete_initiative", "file_card"):
        assert not hasattr(store, verb), verb


# --- the per-project work queue (schema 8) ---------------------------------


def test_queue_fields_default_empty_and_take_only_known_states(store):
    """`queue_state` is an enum at the store, like `link_state`.

    Refused here rather than at a surface because every route inherits the
    store — including a future one nobody has written yet.
    """
    card = _card(store)
    assert card["queue_state"] == "" and card["queued_at"] is None
    assert card["queue_rank"] is None
    bad, detail = store.update(card["id"], {"queue_state": "waiting"})
    assert bad is None and "queue state" in detail
    worse, detail = store.update(card["id"], {"queued_at": "soonish"})
    assert worse is None and "queued_at" in detail
    ok, _ = store.update(card["id"], {"queue_state": "queued",
                                      "queued_at": 1234.5})
    assert ok["queue_state"] == "queued" and ok["queued_at"] == 1234.5


def test_moving_a_queued_card_by_hand_clears_its_queue_slot(store):
    """A hand column-move is the person overriding the queue.

    Both directions, which is why the clear sits outside `update`'s
    done/not-done branch: dragging a queued card *anywhere* is the override,
    not only dragging it into Done.
    """
    card = _card(store)
    store.update(card["id"], {"queue_state": "queued", "queued_at": 10.0})
    moved, _ = store.update(card["id"], {"column_name": "in_progress"})
    assert moved["queue_state"] == "" and moved["queued_at"] is None
    # And an edit that does *not* touch the column leaves the slot alone —
    # otherwise every card sheet save would silently unqueue the card.
    store.update(card["id"], {"queue_state": "queued", "queued_at": 11.0})
    edited, _ = store.update(card["id"], {"title": "renamed"})
    assert edited["queue_state"] == "queued" and edited["queued_at"] == 11.0


def test_the_queue_is_bounded_per_project(store):
    """`MAX_QUEUED_PER_PROJECT`, refused at the store like `MAX_CARDS`.

    And the bound counts *joining*: a card already in the queue can be
    re-stamped without being refused by the count it is itself part of.
    """
    ids = []
    for n in range(board_mod.MAX_QUEUED_PER_PROJECT):
        c = _card(store, title=f"card {n}")
        assert store.update(c["id"], {"queue_state": "queued",
                                      "queued_at": float(n)})[0] is not None
        ids.append(c["id"])
    over = _card(store, title="one too many")
    refused, detail = store.update(over["id"], {"queue_state": "queued",
                                                "queued_at": 99.0})
    assert refused is None and "queued" in detail
    # Another project is unaffected: pipelines are independent.
    other = _card(store, title="elsewhere", project="other", root="/tmp/other")
    assert store.update(other["id"], {"queue_state": "queued",
                                      "queued_at": 1.0})[0] is not None
    # And a re-stamp of a card already in the full queue is not a join.
    again, _ = store.update(ids[0], {"queue_state": "queued", "queued_at": 5.0})
    assert again is not None and again["queued_at"] == 5.0


def test_queued_cards_are_read_back_in_drain_order(store):
    """`queued_at` then id. The id breaks the tie because two enqueues inside
    one clock tick must still have *an* order — an unordered queue drains
    differently on every pass, which is indistinguishable from a bug."""
    a = _card(store, title="second")
    b = _card(store, title="first")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 10.0})
    assert [c["title"] for c in store.queued_cards("bob")] == ["first", "second"]
    assert store.queued_count("bob") == 2
    assert store.queued_cards("nobody") == []
    # No project named reads every queue in one pass — what the drain does.
    assert len(store.queued_cards()) == 2


def test_a_schema_7_file_gains_the_queue_pair_by_alter(tmp_path):
    """The forward-compatibility promise at v8: a v7 file opens, keeps its
    cards, and goes on accepting a v7-shaped INSERT because both new columns
    carry a DEFAULT (`queued_at` nullable, `dispatched_at`'s precedent)."""
    path = tmp_path / "board.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '7')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " closed_by TEXT NOT NULL DEFAULT '',"
        " close_note TEXT NOT NULL DEFAULT '',"
        " blocked_by TEXT NOT NULL DEFAULT '',"
        " plan_path TEXT NOT NULL DEFAULT '',"
        " refine_session_id TEXT NOT NULL DEFAULT '',"
        " refine_state TEXT NOT NULL DEFAULT '',"
        " initiative_id TEXT NOT NULL DEFAULT '',"
        " initiative_by TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert {"queue_state", "queued_at"} <= names
        card = store.get("c1")
        assert card["title"] == "old card"
        assert card["queue_state"] == "" and card["queued_at"] is None
        # A v7-shaped INSERT still works: both new columns carry a DEFAULT.
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v7 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["queue_state"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_a_schema_8_file_gains_manual_steps_by_alter(tmp_path):
    """The forward-compatibility promise at v9: a v8 file opens, keeps its
    cards with `manual_steps` empty, and goes on accepting a v8-shaped INSERT
    because the new column carries a DEFAULT — which is what lets a downgraded
    build go on writing to a table it has never heard of this column in."""
    path = tmp_path / "board.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '8')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " closed_by TEXT NOT NULL DEFAULT '',"
        " close_note TEXT NOT NULL DEFAULT '',"
        " blocked_by TEXT NOT NULL DEFAULT '',"
        " plan_path TEXT NOT NULL DEFAULT '',"
        " refine_session_id TEXT NOT NULL DEFAULT '',"
        " refine_state TEXT NOT NULL DEFAULT '',"
        " initiative_id TEXT NOT NULL DEFAULT '',"
        " initiative_by TEXT NOT NULL DEFAULT '',"
        " queue_state TEXT NOT NULL DEFAULT '',"
        " queued_at REAL DEFAULT NULL,"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "manual_steps" in names
        card = store.get("c1")
        assert card["title"] == "old card"
        assert card["manual_steps"] == ""
        # A v8-shaped INSERT still works: the new column carries a DEFAULT.
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v8 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["manual_steps"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_the_create_path_and_the_alter_path_agree_on_manual_steps():
    """`_add_missing_columns` is PRAGMA-driven and only ever sees
    `_ADDED_COLUMNS`, so a column spelled one way in `_SCHEMA` and another
    here would mean an existing database never gains it — silently, and for
    good."""
    assert ("manual_steps", "TEXT NOT NULL DEFAULT ''") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "manual_steps    TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA


# --- the review acknowledgement ----------------------------------------------
# `reviewed_at`, at v10: an assistant's close waits at the top of Done wearing
# a "FINISHED · REVIEW" banner until a person presses Reviewed. NULL means
# nobody has looked yet; the store is the only writer.


def test_mark_reviewed_stamps_an_agent_closed_done_card(store):
    """The verb. `declare_done` records the assistant's statement; this
    records that a human has seen it — and `updated_at` is stamped, because a
    person pressing Reviewed is somebody acting on the card, unlike
    `record_agents`' observing."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    before = store.get(card["id"])
    assert before["reviewed_at"] is None
    reviewed, detail = store.mark_reviewed(card["id"])
    assert reviewed is not None, detail
    assert reviewed["reviewed_at"] is not None
    assert reviewed["updated_at"] >= before["updated_at"]
    # The close signature survives: nothing is destroyed by acknowledging it.
    assert reviewed["closed_by"] == "sess-1"
    assert reviewed["close_note"] == "tests pass"
    assert reviewed["column_name"] == "done"


def test_a_card_not_in_done_takes_no_review(store):
    card = _card(store, column_name="in_progress")
    reviewed, detail = store.mark_reviewed(card["id"])
    assert reviewed is None
    assert "not in Done" in detail
    assert store.get(card["id"])["reviewed_at"] is None


def test_a_hand_dragged_done_card_takes_no_review(store):
    """`closed_by` empty means nobody declared anything, so there is nothing
    to acknowledge — the banner never draws on a hand-drag and the verb
    refuses rather than stamping a review of a close that never happened."""
    card = _card(store, column_name="done")
    reviewed, detail = store.mark_reviewed(card["id"])
    assert reviewed is None
    assert "only an assistant's close" in detail
    assert store.get(card["id"])["reviewed_at"] is None


def test_a_second_review_is_refused(store):
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    first, detail = store.mark_reviewed(card["id"])
    assert first is not None, detail
    again, detail = store.mark_reviewed(card["id"])
    assert again is None
    assert "already reviewed" in detail
    assert store.get(card["id"])["reviewed_at"] == first["reviewed_at"]


def test_mark_reviewed_on_a_card_that_does_not_exist(store):
    assert store.mark_reviewed("nope")[0] is None


def test_a_card_reopened_under_the_review_reviews_nothing(store):
    """`declare_done`'s race and `declare_done`'s answer: the guard rides in
    the UPDATE's own WHERE clause, so a Reopen landing between the pre-read
    and the write is a `rowcount == 0` — never a card back in Backlog wearing
    an acknowledgement for a close that was just withdrawn."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    real_get = store.get
    fired = []

    def racing_get(card_id):
        row = real_get(card_id)
        if not fired:
            fired.append(True)
            store.update(card_id, {"column_name": "backlog"})
        return row

    store.get = racing_get
    try:
        reviewed, detail = store.mark_reviewed(card["id"])
    finally:
        store.get = real_get
    assert reviewed is None
    assert "nothing changed" in detail
    assert store.get(card["id"])["reviewed_at"] is None


def test_reopening_clears_the_review_beside_the_signature(store):
    """Reopen undoes the *whole* close — the third field through `update`'s
    documented bypass. A card sent back to Backlog still carrying
    `reviewed_at` would, on its next agent close, skip the banner for a close
    nobody has seen."""
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    store.mark_reviewed(card["id"])
    back, _ = store.update(card["id"], {"column_name": "backlog"})
    assert back["closed_by"] == "" and back["close_note"] == ""
    assert back["reviewed_at"] is None


def test_prune_done_skips_an_unreviewed_close(store):
    """The one automatic deletion must not be the thing that removes a banner
    whose whole point is that only a person clears it. A reviewed close and an
    old hand-drag are still collected — a close a human has acknowledged is
    ordinary archive."""
    old = time.time() - 86400 * 90
    pending = _card(store, title="pending", column_name="in_progress")
    store.bind_session(pending["id"], "sess-1")
    store.declare_done(pending["id"], "sess-1", "done")
    acknowledged = _card(store, title="acknowledged",
                         column_name="in_progress")
    store.bind_session(acknowledged["id"], "sess-2")
    store.declare_done(acknowledged["id"], "sess-2", "done")
    store.mark_reviewed(acknowledged["id"])
    dragged = _card(store, title="dragged")
    store.move(dragged["id"], "done")
    for cid in (pending["id"], acknowledged["id"], dragged["id"]):
        store._conn.execute(
            "UPDATE cards SET done_at = ?, updated_at = ? WHERE id = ?",
            (old, old, cid))
    store._conn.commit()
    assert store.prune_done(time.time() - 86400 * 30) == 2
    assert store.get(pending["id"]) is not None
    assert store.get(acknowledged["id"]) is None
    assert store.get(dragged["id"]) is None


def test_reviewed_at_is_outside_writable(store):
    """The field records a human's acknowledgement, but the card sheet saves
    several fields on every edit — a generically writable review field could
    ride a draft, and a Save must never silently acknowledge a review the
    person did not make. The gesture is the named verb; the field is closed."""
    assert "reviewed_at" not in BoardStore._WRITABLE
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.declare_done(card["id"], "sess-1", "tests pass")
    updated, detail = store.update(card["id"], {"reviewed_at": time.time()})
    assert updated is None
    assert "writable" in detail
    assert store.get(card["id"])["reviewed_at"] is None


def test_schema_10_and_a_v9_shaped_file_gains_reviewed_at_via_the_seam(tmp_path):
    """The `_ADDED_COLUMNS` promise for v10. Nullable is the shape the seam's
    rule explicitly admits (`queued_at` is the precedent), so a v9 build goes
    on INSERTing without naming the column."""
    assert SCHEMA_VERSION == 31
    path = tmp_path / "v9.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " manual_steps TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "reviewed_at" in names
        assert store.get("c1")["reviewed_at"] is None
        # A v9-shaped INSERT still works: the new column is nullable.
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v9 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["reviewed_at"] is None
    finally:
        store.close()


def test_the_create_path_and_the_alter_path_agree_on_reviewed_at():
    """`_add_missing_columns` only ever sees `_ADDED_COLUMNS`, so a column
    spelled one way in `_SCHEMA` and another here would mean an existing
    database never gains it — silently, and for good."""
    assert ("reviewed_at", "REAL DEFAULT NULL") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "reviewed_at REAL DEFAULT NULL" in board_mod._SCHEMA


def test_queue_rank_is_outside_writable(store):
    """`plan_path`'s ring: `move_queued` is the only writer. A surface (or
    the daemon's own `update`) that could set it would be a second writer
    of one order."""
    assert "queue_rank" not in BoardStore._WRITABLE
    card = _card(store)
    updated, detail = store.update(card["id"], {"queue_rank": 1})
    assert updated is None
    assert "writable" in detail
    assert store.get(card["id"])["queue_rank"] is None


def test_the_create_path_and_the_alter_path_agree_on_queue_rank():
    """`_add_missing_columns` only ever sees `_ADDED_COLUMNS`, so a column
    spelled one way in `_SCHEMA` and another here would mean an existing
    database never gains it — silently, and for good."""
    assert ("queue_rank", "REAL DEFAULT NULL") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "queue_rank REAL DEFAULT NULL" in board_mod._SCHEMA


def test_schema_11_and_a_v10_shaped_file_gains_queue_rank(tmp_path):
    """The `_ADDED_COLUMNS` promise for v11. Nullable is the shape the seam's
    rule explicitly admits (`queued_at` is the precedent), so a v10 build
    goes on INSERTing without naming the column."""
    assert SCHEMA_VERSION == 31
    path = tmp_path / "v10.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " manual_steps TEXT NOT NULL DEFAULT '',"
        " reviewed_at REAL DEFAULT NULL, created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "queue_rank" in names
        assert store.get("c1")["queue_rank"] is None
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v10 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["queue_rank"] is None
    finally:
        store.close()


def test_attachments_is_writable(store):
    assert "attachments" in BoardStore._WRITABLE


def test_the_create_path_and_the_alter_path_agree_on_attachments():
    assert ("attachments", "TEXT NOT NULL DEFAULT ''") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "attachments     TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA


def test_schema_11_and_a_v10_shaped_file_gains_attachments(tmp_path):
    """The `_ADDED_COLUMNS` promise for the attachments column at v11. DEFAULT
    `''` means a v10 build goes on INSERTing without naming it."""
    assert SCHEMA_VERSION == 31
    path = tmp_path / "v10-attach.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " manual_steps TEXT NOT NULL DEFAULT '',"
        " reviewed_at REAL DEFAULT NULL, created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "attachments" in names
        assert store.get("c1")["attachments"] == ""
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v10 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["attachments"] == ""
    finally:
        store.close()


def test_create_refuses_a_bad_attachments_value(store):
    from dark_army_daemon import attachments as attach_mod
    bad = "not-a-path.sh"
    card, detail = store.create({
        "title": "t", "project": "bob", "attachments": bad})
    assert card is None
    assert detail == attach_mod.field_refusal(bad)


def test_a_valid_attachments_value_round_trips(store):
    value = "abcd1234-efgh5678-ijkl9012-mnop3456/shot.png"
    card = _card(store, attachments=value)
    assert store.get(card["id"])["attachments"] == value
    updated, detail = store.update(card["id"], {
        "attachments": value + "\n" + "abcd1234-efgh5678-ijkl9012-mnop3456/spec.pdf"})
    assert updated is not None, detail
    assert store.get(card["id"])["attachments"].split("\n") == [
        value, "abcd1234-efgh5678-ijkl9012-mnop3456/spec.pdf"]


def test_done_awaiting_review_returns_only_pending_closes(store):
    """The snapshot read that keeps a pending close on the board past the
    24-hour Done window: agent-closed, unacknowledged, newest first — never a
    reviewed close, a hand-drag or an open card."""
    pending_old = _card(store, title="pending old", column_name="in_progress")
    store.bind_session(pending_old["id"], "sess-1")
    store.declare_done(pending_old["id"], "sess-1", "done")
    store._conn.execute(
        "UPDATE cards SET done_at = ? WHERE id = ?",
        (time.time() - 86400 * 3, pending_old["id"]))
    store._conn.commit()
    pending_new = _card(store, title="pending new", column_name="in_progress")
    store.bind_session(pending_new["id"], "sess-2")
    store.declare_done(pending_new["id"], "sess-2", "done")
    acknowledged = _card(store, title="acknowledged",
                         column_name="in_progress")
    store.bind_session(acknowledged["id"], "sess-3")
    store.declare_done(acknowledged["id"], "sess-3", "done")
    store.mark_reviewed(acknowledged["id"])
    dragged = _card(store, title="dragged", column_name="done")
    open_card = _card(store, title="open", column_name="backlog")

    got = store.done_awaiting_review()
    assert [c["title"] for c in got] == ["pending new", "pending old"]
    assert store.done_awaiting_review(limit=1)[0]["title"] == "pending new"
    assert dragged["id"] not in [c["id"] for c in got]
    assert open_card["id"] not in [c["id"] for c in got]


# --- the per-card thread (schema 12) -----------------------------------------


def test_add_message_round_trips_and_counts(store):
    card = _card(store)
    q, detail = store.add_message(card["id"], "user", "did you take X?",
                                  "question", "")
    assert q is not None, detail
    a, detail = store.add_message(card["id"], "sess-1", "yes",
                                  "answer", "session")
    assert a is not None, detail
    rows = store.messages(card["id"])
    assert [r["kind"] for r in rows] == ["question", "answer"]
    assert [r["text"] for r in rows] == ["did you take X?", "yes"]
    assert store.message_counts() == {card["id"]: 2}
    other = _card(store, title="quiet")
    assert other["id"] not in store.message_counts()


def test_a_question_over_the_limit_is_refused_an_answer_is_clamped(store):
    card = _card(store)
    too_long = "x" * (MAX_MESSAGE_CHARS + 1)
    q, detail = store.add_message(card["id"], "user", too_long, "question")
    assert q is None
    assert str(MAX_MESSAGE_CHARS) in detail
    assert store.messages(card["id"]) == []
    a, detail = store.add_message(card["id"], "sess-1", too_long,
                                  "answer", "session")
    assert a is not None, detail
    assert len(a["text"]) == MAX_MESSAGE_CHARS
    n, detail = store.add_message(card["id"], "bob", too_long, "note")
    assert n is not None, detail
    assert len(n["text"]) == MAX_MESSAGE_CHARS


def test_the_per_card_thread_cap_is_refused_at_the_store(store):
    card = _card(store)
    for i in range(MAX_THREAD_MESSAGES):
        msg, detail = store.add_message(card["id"], "user", f"q{i}", "question")
        assert msg is not None, detail
    extra, detail = store.add_message(card["id"], "user", "one more", "question")
    assert extra is None
    assert str(MAX_THREAD_MESSAGES) in detail
    assert len(store.messages(card["id"])) == MAX_THREAD_MESSAGES


def test_delete_cascades_to_messages(store):
    card = _card(store)
    store.add_message(card["id"], "user", "hello", "question")
    ok, _ = store.delete(card["id"])
    assert ok
    assert store.messages(card["id"]) == []
    leftover = store._conn.execute(
        "SELECT COUNT(*) FROM card_messages").fetchone()[0]
    assert leftover == 0


def test_prune_done_collects_messages(store):
    live = _card(store, title="live", column_name="in_progress")
    store.add_message(live["id"], "user", "keep me", "question")
    stale = _card(store, title="stale")
    store.move(stale["id"], "done")
    store.update(stale["id"], {"done_at": time.time() - 86400 * 90})
    store.add_message(stale["id"], "user", "drop me", "question")
    assert store.prune_done(time.time() - 86400 * 30) == 1
    assert store.get(live["id"]) is not None
    assert store.messages(live["id"])[0]["text"] == "keep me"
    assert store.messages(stale["id"]) == []


def test_clear_done_cascades_to_messages(store):
    done = _card(store, title="done", column_name="done")
    keep = _card(store, title="keep")
    store.add_message(done["id"], "user", "gone", "question")
    store.add_message(keep["id"], "user", "stays", "question")
    count, token = store.done_scope()
    ok, deleted, detail = store.clear_done(count, token)
    assert ok, detail
    assert deleted == 1
    assert store.messages(done["id"]) == []
    assert store.messages(keep["id"])[0]["text"] == "stays"


def test_orphan_messages_are_swept_on_connect(tmp_path):
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    card = _card(store)
    store.add_message(card["id"], "user", "orphan me", "question")
    store._conn.execute("DELETE FROM cards WHERE id = ?", (card["id"],))
    store._conn.commit()
    store.close()

    again = BoardStore(path)
    again.connect()
    try:
        leftover = again._conn.execute(
            "SELECT COUNT(*) FROM card_messages").fetchone()[0]
        assert leftover == 0
    finally:
        again.close()


def test_a_file_without_card_messages_gains_the_table(tmp_path):
    """A v11-shaped file (no card_messages) opens and gains the table via
    CREATE TABLE IF NOT EXISTS in `_SCHEMA`."""
    path = tmp_path / "v11.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '11')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        tables = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "card_messages" in tables
        msg, detail = store.add_message("c1", "user", "hello", "question")
        assert msg is not None, detail
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_add_message_refuses_a_missing_card_and_empty_text(store):
    missing, detail = store.add_message("nope", "user", "hello", "question")
    assert missing is None
    assert "no such card" in detail
    card = _card(store)
    empty, detail = store.add_message(card["id"], "user", "   ", "question")
    assert empty is None
    assert "needs some text" in detail


# --- the card's model, at v13 ------------------------------------------------


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
@pytest.mark.parametrize("on_create", [True, False])
def test_current_codex_selection_survives_create_update_and_reopen(
        tmp_path, on_create, model):
    path = tmp_path / f"{model}.db"
    store = BoardStore(path)
    store.connect()
    try:
        card = _card(store, tool="codex", model=model if on_create else "")
        if not on_create:
            card, detail = store.update(card["id"], {"model": model})
            assert card is not None, detail
        assert card["model"] == model
        card_id = card["id"]
    finally:
        store.close()
    reopened = BoardStore(path)
    reopened.connect()
    try:
        assert reopened.get(card_id)["model"] == model
        after, detail = reopened.update(card_id, {"tool": "claude"})
        assert after is not None, detail
        assert after["model"] == ""
    finally:
        reopened.close()


@pytest.mark.parametrize("tool", ["claude", "grok"])
def test_astra_is_refused_for_other_providers_on_create_and_update(store, tool):
    card, detail = store.create({"title": "x", "tool": tool, "model": "gpt-6-astra"})
    assert card is None
    assert detail == f"this card names a model Dark Army does not offer for {tool}"
    existing = _card(store, tool=tool)
    after, detail = store.update(existing["id"], {"model": "gpt-6-astra"})
    assert after is None
    assert detail == f"this card names a model Dark Army does not offer for {tool}"
    assert store.get(existing["id"])["model"] == ""


def test_grok_4_7_selection_survives_create_update_and_reopen(tmp_path):
    path = tmp_path / "grok47.db"
    store = BoardStore(path)
    store.connect()
    try:
        card = _card(store, tool="grok", model="grok-4.7")
        assert card["model"] == "grok-4.7"
        card_id = card["id"]
        switched, detail = store.update(card_id, {"model": "grok-4.6"})
        assert switched is not None, detail
        assert switched["model"] == "grok-4.6"
        back, detail = store.update(card_id, {"model": "grok-4.7"})
        assert back is not None, detail
    finally:
        store.close()
    reopened = BoardStore(path)
    reopened.connect()
    try:
        assert reopened.get(card_id)["model"] == "grok-4.7"
    finally:
        reopened.close()


def test_create_accepts_a_shipped_model_for_the_named_tool(store):
    card, _ = store.create({"project": "bob", "title": "t", "tool": "claude",
                            "model": "opus"})
    assert card["model"] == "opus"


def test_create_refuses_a_model_from_another_provider(store):
    card, detail = store.create({"project": "bob", "title": "t",
                                 "tool": "claude", "model": "grok-4.6"})
    assert card is None
    assert detail == "this card names a model Dark Army does not offer for claude"


def test_create_refuses_a_model_when_no_assistant_is_named(store):
    """`MODELS.get("", ())` is empty, so a card with nobody on it cannot carry a
    model nothing will ever honour."""
    card, detail = store.create({"project": "bob", "title": "t", "tool": "",
                                 "model": "opus"})
    assert card is None
    assert "does not offer" in detail


def test_default_is_always_legal(store):
    for tool in ("", "claude", "codex", "grok"):
        card, _ = store.create({"project": "bob", "title": "t", "tool": tool,
                                "model": ""})
        assert card is not None and card["model"] == ""


def test_retooling_a_card_clears_its_model(store):
    card, _ = store.create({"project": "bob", "title": "t", "tool": "grok",
                            "model": "grok-4.5"})
    after, detail = store.update(card["id"], {"tool": "claude"})
    assert after["model"] == "", detail
    assert after["tool"] == "claude"


def test_retooling_to_the_same_tool_keeps_the_model(store):
    card, _ = store.create({"project": "bob", "title": "t", "tool": "grok",
                            "model": "grok-4.5"})
    after, _ = store.update(card["id"], {"tool": "grok"})
    assert after["model"] == "grok-4.5"


def test_tool_and_a_matching_model_in_one_write_keep_both(store):
    card, _ = store.create({"project": "bob", "title": "t", "tool": "grok",
                            "model": "grok-4.5"})
    after, detail = store.update(card["id"], {"tool": "claude",
                                              "model": "sonnet"})
    assert after["tool"] == "claude", detail
    assert after["model"] == "sonnet"


def test_update_refuses_a_model_the_new_tool_does_not_offer(store):
    card, _ = store.create({"project": "bob", "title": "t", "tool": "claude"})
    after, detail = store.update(card["id"], {"model": "grok-4.6"})
    assert after is None
    assert detail == "this card names a model Dark Army does not offer for claude"
    assert store.get(card["id"])["model"] == ""


def test_update_may_always_clear_the_model(store):
    card, _ = store.create({"project": "bob", "title": "t", "tool": "claude",
                            "model": "opus"})
    after, _ = store.update(card["id"], {"model": ""})
    assert after["model"] == ""


def test_a_pre_v13_file_gains_the_model_column(tmp_path):
    """`_ADDED_COLUMNS`' contract at v13: the column arrives with `''`, existing
    rows read it back as Default, and a schema-12-shaped INSERT still works
    because the column carries a DEFAULT."""
    path = tmp_path / "v12.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '12')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "model" in names
        assert store.get("c1")["model"] == ""
        # A schema-12-shaped INSERT still works: the new column has a DEFAULT.
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v12 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["model"] == ""
    finally:
        store.close()


def test_the_create_and_alter_spellings_agree():
    """One grep has to be able to prove the two paths declare the same column,
    which is why both are single-spaced and identical."""
    assert ("model", "TEXT NOT NULL DEFAULT ''") in BoardStore._ADDED_COLUMNS["cards"]
    assert "model TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA


# --- create-token idempotency -------------------------------------------------

_TOKEN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_two_creates_with_the_same_token_are_one_row(store):
    first, detail = store.create({"title": "one", "create_token": _TOKEN})
    assert first is not None, detail
    assert detail == "created"
    second, detail = store.create(
        {"title": "two", "create_token": _TOKEN})
    assert second is not None, detail
    assert detail == ALREADY_CREATED
    assert second["id"] == first["id"]
    assert second["title"] == "one"
    assert len(store.cards()) == 1


def test_different_or_missing_tokens_yield_two_rows(store):
    a, _ = store.create({"title": "same title"})
    b, _ = store.create({"title": "same title",
                         "create_token": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"})
    assert a["id"] != b["id"]
    assert len(store.cards()) == 2


def test_missing_empty_and_whitespace_tokens_store_empty(store):
    a, _ = store.create({"title": "none"})
    b, _ = store.create({"title": "empty", "create_token": ""})
    c, _ = store.create({"title": "spaces", "create_token": "   "})
    assert a["create_token"] == ""
    assert b["create_token"] == ""
    assert c["create_token"] == ""
    assert len({a["id"], b["id"], c["id"]}) == 3


def test_invalid_create_token_is_refused(store):
    for bad in ("short", "!!!!!!!!", "a" * 41, "foo/bar/xx", "../secret1"):
        card, detail = store.create({"title": "x", "create_token": bad})
        assert card is None, bad
        assert detail == "that is not a create token", bad
    assert store.total() == 0


def test_replay_does_not_consume_max_cards(store, monkeypatch):
    monkeypatch.setattr(board_mod, "MAX_CARDS", 3)
    first, _ = store.create({"title": "one", "create_token": _TOKEN})
    _card(store, title="two")
    _card(store, title="three")
    assert store.total() == 3
    card, detail = store.create({"title": "replay", "create_token": _TOKEN})
    assert card["id"] == first["id"]
    assert detail == ALREADY_CREATED
    assert store.total() == 3


def test_replay_of_a_moved_card_returns_that_row(store):
    first, _ = store.create({"title": "one", "create_token": _TOKEN})
    store.update(first["id"], {"column_name": "backlog"})
    card, detail = store.create({"title": "two", "create_token": _TOKEN})
    assert card["id"] == first["id"]
    assert card["column_name"] == "backlog"
    assert detail == ALREADY_CREATED
    assert len(store.cards()) == 1


def test_delete_then_create_with_the_same_token_is_a_new_card(store):
    first, _ = store.create({"title": "one", "create_token": _TOKEN})
    store.delete(first["id"])
    second, detail = store.create({"title": "two", "create_token": _TOKEN})
    assert second is not None, detail
    assert detail == "created"
    assert second["id"] != first["id"]
    assert second["create_token"] == _TOKEN
    assert len(store.cards()) == 1


def test_update_cannot_rewrite_create_token(store):
    first, _ = store.create({"title": "one", "create_token": _TOKEN})
    after, detail = store.update(
        first["id"], {"create_token": "anything-at-all-xx", "title": "two"})
    assert after is not None, detail
    assert after["title"] == "two"
    assert after["create_token"] == _TOKEN
    assert "create_token" not in BoardStore._WRITABLE


def test_create_token_is_not_single_writer():
    assert "create_token" not in board_mod.SINGLE_WRITER


def test_the_create_and_alter_spellings_agree_on_create_token():
    assert ("create_token", "TEXT NOT NULL DEFAULT ''") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "create_token TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA
    assert SCHEMA_VERSION == 31


def test_a_pre_v14_file_gains_the_create_token_column(tmp_path):
    """A v13-shaped file gains the column via `_ADDED_COLUMNS` as `''`,
    and an INSERT that does not name it still works. The unique index
    exists after connect even though the recorded version stays 14."""
    path = tmp_path / "v13.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '13')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " workflow TEXT NOT NULL DEFAULT '',"
        " agent_trail TEXT NOT NULL DEFAULT '',"
        " model TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('c1', 'bob', 'old card', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute("PRAGMA table_info(cards)")}
        assert "create_token" in names
        assert store.get("c1")["create_token"] == ""
        store._conn.execute(
            "INSERT INTO cards (id, project, title, column_name, created_at,"
            " updated_at) VALUES ('c2', 'bob', 'v13 write', 'backlog', 2, 2)")
        store._conn.commit()
        assert store.get("c2")["create_token"] == ""
        indexes = {r["name"] for r in store._conn.execute(
            "PRAGMA index_list(cards)")}
        assert "cards_by_create_token" in indexes
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_integrity_error_on_create_token_returns_the_existing_row(
        store, monkeypatch):
    """The lookup-then-insert race: first lookup misses, INSERT hits the
    unique index, re-lookup returns the row that won."""
    first, _ = store.create({"title": "one", "create_token": _TOKEN})
    calls = {"n": 0}
    real = store._card_by_create_token_locked

    def flaky(token):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(token)

    monkeypatch.setattr(store, "_card_by_create_token_locked", flaky)
    card, detail = store.create({"title": "two", "create_token": _TOKEN})
    assert card["id"] == first["id"]
    assert detail == ALREADY_CREATED
    assert calls["n"] == 2


def test_replay_skips_later_validation(store):
    """A retry returns the existing row even if the payload would fail
    title / objective checks."""
    first, _ = store.create({"title": "one", "create_token": _TOKEN})
    card, detail = store.create({"title": "", "create_token": _TOKEN})
    assert card["id"] == first["id"]
    assert detail == ALREADY_CREATED


# --- relabel_root: cards follow their project's name ---------------------------


def test_relabel_root_moves_every_card_of_that_root(store):
    a = _card(store, title="a", project="Ledgerly", root="/tmp/sf")
    b = _card(store, title="b", project="Ledgerly", root="/tmp/sf")
    other = _card(store, title="c", project="bob", root="/tmp/bob")
    assert store.relabel_root("/tmp/sf", "finance-demo") == 2
    assert store.get(a["id"])["project"] == "finance-demo"
    assert store.get(b["id"])["project"] == "finance-demo"
    assert store.get(other["id"])["project"] == "bob"


def test_relabel_root_is_a_no_op_on_empty_arguments_or_no_change(store):
    _card(store, project="bob", root="/tmp/bob")
    assert store.relabel_root("", "x") == 0
    assert store.relabel_root("/tmp/bob", "") == 0
    assert store.relabel_root("/tmp/bob", "bob") == 0
    assert store.relabel_root("/tmp/nothing-here", "x") == 0


# --- the single-writer ring, as a table (2026-09-01) ---------------------------


def test_single_writer_ring_is_disjoint_from_writable():
    """Each column in `SINGLE_WRITER` exists precisely because no surface may
    set it through `update` — one in `_WRITABLE` is a forged signature waiting
    to happen."""
    assert BoardStore._WRITABLE.isdisjoint(board_mod.SINGLE_WRITER)


def test_single_writer_verbs_all_exist_on_the_store():
    for column, verb in board_mod.SINGLE_WRITER.items():
        assert callable(getattr(BoardStore, verb, None)), \
            f"{column}'s one writer {verb!r} is not a BoardStore verb"


def test_api_board_fields_stay_inside_writable():
    """The API's allow-list may only ever name store-writable columns — a field
    there that `update` ignores is a write the panel thinks landed."""
    from dark_army_daemon.api_server import ApiServer
    assert set(ApiServer._BOARD_FIELDS) <= BoardStore._WRITABLE


def test_the_three_ring_assertions_still_hold_with_revision_present():
    """`revision` is `create_token`'s ring: the store writes it, no verb is
    named for it, and no surface may set it. A surface that could would walk
    a stale save straight past the guard the column exists to arm."""
    from dark_army_daemon.api_server import ApiServer
    assert "revision" not in BoardStore._WRITABLE
    assert "revision" not in board_mod.SINGLE_WRITER
    assert "revision" not in ApiServer._BOARD_FIELDS
    # And the ring tests above are unchanged by its arrival.
    assert BoardStore._WRITABLE.isdisjoint(board_mod.SINGLE_WRITER)
    assert set(ApiServer._BOARD_FIELDS) <= BoardStore._WRITABLE


# --- bind_session vs. a concurrent drag (2026-09-01) ---------------------------


def test_bind_session_does_not_undo_a_concurrent_done_move(store, monkeypatch):
    """`bind_session` runs on the executor; a person's Done drag lands on the
    loop. With the read outside the lock, a drag arriving between
    `bind_session`'s read and its write was silently overwritten back to
    In progress. The lock now spans read+write, so the drag waits its turn and
    lands after — the human's move survives."""
    import threading

    card = _card(store, column_name="backlog", tool="claude", prompt="go")
    cid = card["id"]

    orig_get = store.get
    fired = {}

    def racy_get(card_id):
        row = orig_get(card_id)
        if "thread" not in fired:
            mover = threading.Thread(
                target=lambda: store.update(cid, {"column_name": "done"}))
            fired["thread"] = mover
            mover.start()
            # Give the drag every chance to interleave. Under the fix it
            # blocks on the store lock until the bind finishes.
            time.sleep(0.3)
        return row

    monkeypatch.setattr(store, "get", racy_get)
    bound, detail = store.bind_session(cid, "sess-1")
    assert bound is not None, detail
    fired["thread"].join(timeout=5)
    monkeypatch.setattr(store, "get", orig_get)

    assert store.get(cid)["column_name"] == "done", \
        "the human's Done drag was undone by a concurrent bind"


# --- the run record's table (v15) ---------------------------------------------


def test_a_file_without_card_runs_gains_the_table(tmp_path):
    """`card_messages`' precedent, for `card_messages`' reason: the run
    record is a table rather than columns on `cards`, so a build that has
    never heard of it cannot see it in `SELECT * FROM cards` and cannot blank
    it on the next write."""
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/r",
                                "tool": "claude"})
        cid = card["id"]
        # Simulate the older file: drop the table this build creates.
        store._conn.execute("DROP TABLE card_runs")
        store._conn.commit()
    finally:
        store.close()

    again = BoardStore(path)
    again.connect()
    try:
        tables = {r[0] for r in again._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "card_runs" in tables
        assert again.open_run(cid, 1.0, "/r", "abc")[0] is True
        record, detail = again.close_run(cid, 1.0, "s1", "quiet", "r", "",
                                         [], False, "no baseline")
        assert record is not None, detail
    finally:
        again.close()


def test_the_run_table_adds_no_cards_column(tmp_path):
    """A `cards` column for any of this would break the forward-compatibility
    rule the whole file lives under. The table is the answer; the `cards`
    entry of `_ADDED_COLUMNS` must stay clean (the table has its own entry
    since v24, keyed on itself, never on `cards`)."""
    assert "card_runs" not in str(BoardStore._ADDED_COLUMNS["cards"])
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        have = {row["name"] for row in
                store._conn.execute("PRAGMA table_info(cards)")}
        for name in ("run_at", "baseline", "files", "files_available",
                     "lines_added", "lines_removed", "recorded_at"):
            assert name not in have, name
    finally:
        store.close()


def test_no_surface_field_can_write_a_run(tmp_path):
    """The fifth writer ring: the record's own columns are outside
    `_WRITABLE`, so no panel, phone, channel tool or LAN door can write a
    word of it. A record a surface could write is a record that proves
    nothing."""
    for name in ("run_at", "baseline", "files", "files_available",
                 "files_reason", "files_changed", "files_total",
                 "lines_added", "lines_removed", "recorded_at", "verdict"):
        assert name not in BoardStore._WRITABLE, name


# --- "start it when its plan lands" (v17) -------------------------------------


def test_the_create_and_alter_spellings_agree_on_start_when_planned():
    """`_add_missing_columns` is PRAGMA-driven and only ever sees the list, so
    the two paths have to agree exactly or an older file gains a column the
    fresh schema spells differently. `model`'s pinned convention."""
    assert ("start_when_planned", "TEXT NOT NULL DEFAULT ''") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "start_when_planned TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA
    assert SCHEMA_VERSION == 31


def test_start_when_planned_is_in_the_rings_a_person_writes():
    """Ring 1 — a thing a person states, like `tool` and `model`. A surface
    must be able to set it; that is the whole feature. And it is counted by
    the revision, which the pinned mirror rule requires the moment it lands in
    `ApiServer._BOARD_FIELDS`."""
    from dark_army_daemon.api_server import ApiServer
    assert "start_when_planned" in BoardStore._WRITABLE
    assert "start_when_planned" in board_mod.REVISED_COLUMNS
    assert "start_when_planned" not in board_mod.SINGLE_WRITER
    assert "start_when_planned" in ApiServer._BOARD_FIELDS


def test_a_plain_card_is_not_armed(store):
    card, _ = store.create({"title": "t", "project": "p"})
    assert card["start_when_planned"] == ""
    assert store.get(card["id"])["start_when_planned"] == ""


def test_create_normalises_the_tick(store):
    card, _ = store.create({"title": "t", "project": "p",
                            "start_when_planned": "1"})
    assert store.get(card["id"])["start_when_planned"] == "1"


@pytest.mark.parametrize("value", ["1", "true", "True", True, "yes", "on"])
def test_every_on_shape_stores_one(store, value):
    """`ApiServer._board_fields` stringifies every allow-listed key, so a JSON
    `true` arrives as `"True"`. Both shapes have to land on the same value."""
    card, _ = store.create({"title": "t", "project": "p"})
    store.update(card["id"], {"start_when_planned": value})
    assert store.get(card["id"])["start_when_planned"] == "1"


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", None])
def test_every_off_shape_stores_empty(store, value):
    card, _ = store.create({"title": "t", "project": "p",
                            "start_when_planned": "1"})
    store.update(card["id"], {"start_when_planned": value})
    assert store.get(card["id"])["start_when_planned"] == ""


def test_ticking_it_bumps_the_revision_and_bobs_own_clear_does_not(store):
    card, _ = store.create({"title": "t", "project": "p"})
    before = store.get(card["id"])["revision"]
    store.update(card["id"], {"start_when_planned": "1"})
    assert store.get(card["id"])["revision"] == before + 1
    # Dark Army acting on a standing instruction is not somebody editing the card.
    store.update(card["id"], {"start_when_planned": ""}, bump=False)
    assert store.get(card["id"])["revision"] == before + 1
    assert store.get(card["id"])["start_when_planned"] == ""


def test_a_v16_shaped_file_gains_the_column_with_its_default(tmp_path):
    """A schema-16 build opening this file goes on INSERTing without naming
    it, and its surfaces simply never draw the tick."""
    path = tmp_path / "v16.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '16')")
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, project TEXT NOT NULL,"
        " root TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " prompt TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',"
        " column_name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,"
        " session_id TEXT NOT NULL DEFAULT '',"
        " link_state TEXT NOT NULL DEFAULT '',"
        " dispatch_error TEXT NOT NULL DEFAULT '',"
        " dispatched_at REAL DEFAULT NULL, session_ended_at REAL DEFAULT NULL,"
        " author TEXT NOT NULL DEFAULT 'user',"
        " revision INTEGER NOT NULL DEFAULT 0,"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute("INSERT INTO cards (id, project, title, column_name,"
                 " created_at, updated_at) VALUES ('a', 'p', 'old', 'prep',"
                 " 1, 1)")
    conn.commit()
    conn.close()
    store = BoardStore(path)
    store.connect()
    try:
        row = store.get("a")
        assert row["title"] == "old"
        assert row["start_when_planned"] == ""
    finally:
        store.close()


# --- the importance number (v18) ----------------------------------------------
#
# `''` means "nobody has scored this"; `'0'` means "scored, lowest". They are
# distinguishable on a card face and deliberately indistinguishable in the
# sort, which is `CAST(priority AS INTEGER)` — 0 in SQLite for both.


def test_schema_version_names_the_priority_column():
    """Bumped in the same change as the column, or `_migrate`'s forward-only
    marker says 18 while the table is v19."""
    assert SCHEMA_VERSION == 31


def test_the_create_and_alter_spellings_of_priority_match_exactly():
    """`_add_missing_columns` is PRAGMA-driven and only ever sees the list, so
    the two paths have to agree by hand."""
    assert ("priority", "TEXT NOT NULL DEFAULT ''") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "priority TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA


def test_priority_is_a_thing_a_person_states_not_a_single_writer():
    """`model`'s ring: a person edits it, and Dark Army's one suggestion is just
    another writer of the same field going through the ordinary `update`."""
    assert "priority" in BoardStore._WRITABLE
    assert "priority" not in board_mod.SINGLE_WRITER


def test_create_seeds_an_empty_priority_and_may_be_told_one(store):
    assert _card(store)["priority"] == ""
    assert _card(store, priority="90")["priority"] == "90"
    assert _card(store, priority=" 07 ")["priority"] == "7"


def test_update_accepts_a_number_zero_and_a_clearing(store):
    card = _card(store)
    row, detail = store.update(card["id"], {"priority": "80"})
    assert row is not None and row["priority"] == "80", detail
    row, _ = store.update(card["id"], {"priority": "0"})
    assert row["priority"] == "0"
    row, _ = store.update(card["id"], {"priority": ""})
    assert row["priority"] == ""


@pytest.mark.parametrize("bad", ["101", "-1", "7.5", "٧٥", "high", "1e2"])
def test_update_refuses_a_bad_number_in_words_and_stores_nothing(store, bad):
    """Refused rather than clamped: a client that sent 101 has misunderstood
    the field, and silently storing 100 would hide that."""
    card = _card(store, priority="50")
    row, detail = store.update(card["id"], {"priority": bad})
    assert row is None
    assert detail == board_mod.PRIORITY_REFUSAL
    assert store.get(card["id"])["priority"] == "50"


def test_cards_are_ordered_most_important_first_with_drag_as_the_tie_break(store):
    """`CARD_ORDER_SQL`: column, then priority DESC, then position, then
    creation. `''` and `'0'` tie, at the bottom."""
    plain = _card(store, title="unscored")
    zero = _card(store, title="zero", priority="0")
    fifty = _card(store, title="fifty", priority="50")
    ninety = _card(store, title="ninety", priority="90")
    # Interleave the positions so priority is provably the *first* term.
    store.update(ninety["id"], {"position": 400.0})
    store.update(fifty["id"], {"position": 300.0})
    store.update(plain["id"], {"position": 100.0})
    store.update(zero["id"], {"position": 200.0})
    titles = [c["title"] for c in store.cards(["prep"])]
    assert titles == ["ninety", "fifty", "unscored", "zero"]


def test_by_session_and_by_refine_session_use_the_same_clause(store):
    low = _card(store, priority="10")
    high = _card(store, priority="90")
    for cid in (low["id"], high["id"]):
        store.update(cid, {"session_id": "s1", "refine_session_id": "s1"})
    assert [c["priority"] for c in store.by_session("s1")] == ["90", "10"]
    assert [c["priority"] for c in store.by_refine_session("s1")] == ["90", "10"]


def test_the_queue_is_still_ordered_by_when_a_person_pressed(store):
    """The queue is a record of a person's press and their drags. Priority is
    not its business, and the cost is stated in the plan."""
    first = _card(store, priority="10")
    second = _card(store, priority="90")
    store.update(first["id"], {"queue_state": "queued", "queued_at": 100.0})
    store.update(second["id"], {"queue_state": "queued", "queued_at": 200.0})
    queued = store.queued_cards("bob")
    assert [c["priority"] for c in queued] == ["10", "90"]


def test_done_since_is_still_a_time_read(store):
    """`COALESCE(done_at, updated_at) DESC`. Recently finished and the review
    banner are time reads, not importance reads."""
    old = _card(store, column_name="done", priority="90")
    time.sleep(0.01)
    new = _card(store, column_name="done", priority="10")
    ids = [c["id"] for c in store.done_since(0)]
    assert ids.index(new["id"]) < ids.index(old["id"])


# --- the two Done tokens ------------------------------------------------------


def _finished(store, session_id: str):
    """A Done card closed by its own session — `declare_done` guards on the
    binding and the observed column in its own WHERE clause, so there is no
    shortcut past this."""
    card = _card(store)
    store.update(card["id"], {"session_id": session_id,
                              "column_name": "in_progress"})
    closed, detail = store.declare_done(card["id"], session_id, "finished")
    assert closed is not None, detail
    return closed


def test_done_tokens_repeats_done_scope_and_adds_one_field(store):
    """The count and the membership token are `done_scope`'s, unchanged: this
    method adds a field, it does not redefine one."""
    _finished(store, "sess-a")
    _finished(store, "sess-b")
    count, clear_token, view_token = store.done_tokens()
    assert (count, clear_token) == store.done_scope()
    assert len(view_token) == 64
    assert view_token != clear_token


def test_marking_reviewed_moves_only_the_view_token(store):
    """The change this pair exists for: a review moves a card out of the
    always-carried set and touches no id at all."""
    card = _finished(store, "sess-a")
    _, before_clear, before_view = store.done_tokens()
    store.mark_reviewed(card["id"])
    _, after_clear, after_view = store.done_tokens()
    assert after_clear == before_clear
    assert after_view != before_view


def test_a_revised_column_moves_only_the_view_token(store):
    """Retitling a finished card must never be able to refuse a Clear Done —
    that is why the membership token was left byte for byte alone."""
    card = _finished(store, "sess-a")
    _, before_clear, before_view = store.done_tokens()
    store.update(card["id"], {"title": "renamed"})
    _, after_clear, after_view = store.done_tokens()
    assert after_clear == before_clear
    assert after_view != before_view


def test_a_create_and_a_delete_move_both_tokens(store):
    _finished(store, "sess-a")
    _, clear_one, view_one = store.done_tokens()
    extra = _finished(store, "sess-b")
    count, clear_two, view_two = store.done_tokens()
    assert count == 2
    assert clear_two != clear_one and view_two != view_one
    store.delete(extra["id"])
    count, clear_three, view_three = store.done_tokens()
    assert count == 1
    assert clear_three == clear_one and view_three == view_one


def test_review_only_drops_the_stamped_cards_and_nothing_else():
    """One spelling of "the lighter board", and it re-derives no judgment: it
    reads the record of which store read produced each row."""
    state = {"cards": [{"id": "a"},
                       {"id": "b", "done_preview": True},
                       {"id": "c", "done_preview": False}],
             "counts": {"done": 2}, "done_clear_token": "x"}
    out = board_mod.review_only(state)
    assert [c["id"] for c in out["cards"]] == ["a", "c"]
    assert out["counts"] == {"done": 2}
    assert out["done_clear_token"] == "x"
    # And the input is untouched — the full frame is built from it too.
    assert len(state["cards"]) == 3


def test_review_only_survives_a_board_with_no_cards_key():
    assert board_mod.review_only({})["cards"] == []
    assert board_mod.review_only({"cards": None})["cards"] == []


@pytest.mark.parametrize("area", ["invalid", "backbone desk"])
def test_area_create_refuses_off_list(store, area):
    from dark_army_daemon.areas import AREA_REFUSAL
    assert store.create({"title": "x", "area": area}) == (None, AREA_REFUSAL)

def test_area_update_refuses_and_clear_is_legal(store):
    from dark_army_daemon.areas import AREA_REFUSAL
    card = _card(store, area="Backbone")
    assert card["area"] == "backbone"
    assert store.update(card["id"], {"area": "bad"}) == (None, AREA_REFUSAL)
    assert store.get(card["id"])["area"] == "backbone"
    cleared, _ = store.update(card["id"], {"area": ""})
    assert cleared["area"] == ""
    assert cleared["revision"] == card["revision"] + 1

def test_area_seed_is_conditional_and_revised(store):
    card = _card(store)
    seeded, changed = store.fill_area_if_empty(card["id"], "desk")
    assert changed and seeded["area"] == "desk"
    assert seeded["revision"] == card["revision"] + 1
    same, changed = store.fill_area_if_empty(card["id"], "pocket")
    assert not changed and same == seeded

def test_area_seed_never_overwrites_person(store):
    card = _card(store, area="gate")
    seeded, changed = store.fill_area_if_empty(card["id"], "desk")
    assert not changed and seeded["area"] == "gate"

def test_area_version22_file_opens_at23(tmp_path):
    path = tmp_path / "old.db"
    s = BoardStore(path); s.connect(); s.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE cards DROP COLUMN area")
    conn.execute("UPDATE schema_meta SET value = '22' WHERE key = 'version'")
    conn.commit(); conn.close()
    s = BoardStore(path); s.connect()
    assert SCHEMA_VERSION == 31
    card, _ = s.create({"title": "old file"})
    assert card["area"] == ""
    assert s._conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0] == "31"
    s.close()


def test_objective_seed_is_per_field_clamped_and_revised(store):
    from dark_army_daemon.board_outcomes import OBJECTIVE_LIMITS
    card = _card(store, intended_benefit="typed by a person")
    seeded, changed = store.fill_objective_if_empty(card["id"], {
        "beneficiary": "everyone", "intended_benefit": "a plan's words",
        "success_criterion": "x" * (OBJECTIVE_LIMITS["success_criterion"] + 50)})
    assert changed
    assert seeded["beneficiary"] == "everyone"
    assert seeded["intended_benefit"] == "typed by a person"
    assert len(seeded["success_criterion"]) == OBJECTIVE_LIMITS["success_criterion"]
    assert seeded["revision"] == card["revision"] + 1
    assert seeded["outcome_revision"] == card["outcome_revision"] + 1
    same, changed = store.fill_objective_if_empty(card["id"], {"beneficiary": "nobody"})
    assert not changed and same == seeded
    same, changed = store.fill_objective_if_empty(card["id"], {})
    assert not changed and same == seeded
    # The outcome ledger's copy followed the seed.
    ledger = store._conn.execute("SELECT objective FROM outcome_cards WHERE card_id=?",
                                 (card["id"],)).fetchone()
    import json as _json
    assert _json.loads(ledger["objective"])["beneficiary"] == "everyone"


def test_connect_caps_the_write_ahead_log(store):
    """`journal_size_limit` is per connection and unpersisted, so it has
    to be applied on every open; ask the connection back."""
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    got = store._conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    assert got == WAL_SIZE_LIMIT_BYTES == 8 * 1024 * 1024


def test_a_vacuum_sized_wal_is_cut_back_to_the_cap(tmp_path):
    """The 177 MB case: a transaction far bigger than the cap, then the
    coalesce's own VACUUM. The WAL grows for the job and is trimmed at
    the next checkpoint, not left for the daemon's lifetime."""
    path = tmp_path / "board.db"
    s = BoardStore(path)
    s.connect()
    try:
        s._conn.execute("CREATE TABLE wal_probe(x BLOB)")
        with s._conn:
            s._conn.executemany("INSERT INTO wal_probe VALUES(?)",
                                [(os.urandom(1 << 20),)] * 30)
        wal = path.with_name("board.db-wal")
        assert wal.stat().st_size > 3 * WAL_SIZE_LIMIT_BYTES  # peak ~31.7 MB
        with s._conn:
            s._conn.execute("DELETE FROM wal_probe")
        s._conn.execute("VACUUM")
        assert wal.stat().st_size <= WAL_SIZE_LIMIT_BYTES
    finally:
        s.close()


# --- v27: the batch mark ------------------------------------------------------


def test_v27_batch_columns_are_there_with_an_empty_default(tmp_path):
    """Two ring-2 columns, spelled alike in the CREATE and the ALTER list, in
    `_WRITABLE` and in neither `SINGLE_WRITER` nor `REVISED_COLUMNS`."""
    assert SCHEMA_VERSION == 31
    for name in ("batch_id", "batch_rank"):
        assert (name, "TEXT NOT NULL DEFAULT ''") \
            in BoardStore._ADDED_COLUMNS["cards"]
        assert f"{name} TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA
        assert name in BoardStore._WRITABLE
        assert name not in board_mod.SINGLE_WRITER
        assert name not in board_mod.REVISED_COLUMNS
    assert board_mod.MAX_BATCH_CARDS == 8
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp"})
        card = store.get(card["id"])
        assert card["batch_id"] == "" and card["batch_rank"] == ""
        before = card["revision"]
        got, _ = store.update(card["id"], {"batch_id": "tok", "batch_rank": "2"})
        assert got["batch_id"] == "tok" and got["batch_rank"] == "2"
        # Bookkeeping: the card's change number does not move.
        assert got["revision"] == before
    finally:
        store.close()


def test_a_v26_file_gains_the_batch_columns(tmp_path):
    path = tmp_path / "v26.db"
    store = BoardStore(path)
    store.connect()
    card, _ = store.create({"title": "t", "project": "p", "root": "/tmp"})
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '26' WHERE key = 'version'")
    conn.execute("ALTER TABLE cards DROP COLUMN batch_id")
    conn.execute("ALTER TABLE cards DROP COLUMN batch_rank")
    conn.commit()
    conn.close()
    store = BoardStore(path)
    store.connect()
    try:
        got = store.get(card["id"])
        assert got["batch_id"] == "" and got["batch_rank"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row[0]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_v27_file_gains_the_verdict_columns(tmp_path):
    path = tmp_path / "v27.db"
    store = BoardStore(path)
    store.connect()
    card, _ = store.create({"title": "t", "project": "p", "root": "/tmp"})
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '27' WHERE key = 'version'")
    conn.execute("ALTER TABLE cards DROP COLUMN report_verdict")
    conn.execute("ALTER TABLE cards DROP COLUMN report_recommendation")
    conn.commit()
    names = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    assert "report_verdict" not in names
    conn.close()
    store = BoardStore(path)
    store.connect()
    try:
        names = {r[1] for r in store._conn.execute(
            "PRAGMA table_info(cards)").fetchall()}
        assert {"report_verdict", "report_recommendation"} <= names
        got = store.get(card["id"])
        assert got["report_verdict"] == ""
        assert got["report_recommendation"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row[0]) == SCHEMA_VERSION == 31
    finally:
        store.close()

def test_attach_plan_clears_the_batch_mark(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp"})
        store.update(card["id"], {"refine_session_id": "s", "refine_state": "live",
                                  "batch_id": "tok", "batch_rank": "1"})
        got, detail = store.attach_plan(card["id"], "/tmp/plan.md", "s")
        assert got is not None, detail
        assert got["batch_id"] == "" and got["batch_rank"] == ""
        assert got["column_name"] == "backlog"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reset_card_clears_the_batch_mark(tmp_path):
    from dark_army_daemon.daemon import BobDaemon
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp"})
        store.update(card["id"], {"refine_session_id": "s", "refine_state": "live",
                                  "batch_id": "tok", "batch_rank": "3"})
        got, detail = await d.reset_card(card["id"])
        assert got is not None, detail
        assert got["batch_id"] == "" and got["batch_rank"] == ""
        assert got["refine_state"] == ""
    finally:
        store.close()


# --- the batch-implement store verbs -----------------------------------------


def _batch_card(store, title, rank, bid="tok", **kw):
    fields = {"title": title, "project": "bob", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    card, detail = store.update(card["id"], {"batch_id": bid,
                                             "batch_rank": str(rank)},
                                bump=False)
    assert card is not None, detail
    time.sleep(0.002)
    return card


def test_batch_members_is_every_marked_card_in_board_order(store):
    first = _batch_card(store, "first", 1)
    second = _batch_card(store, "second", 2)
    _batch_card(store, "elsewhere", 1, bid="other")
    store.create({"title": "unmarked", "project": "bob",
                  "column_name": "backlog"})
    assert [c["id"] for c in store.batch_members("tok")] == \
        [first["id"], second["id"]]
    # An empty id is every card no batch touched — never returned.
    assert store.batch_members("") == []


def test_release_batch_waiting_touches_only_waiting_members(store):
    bound = _batch_card(store, "bound", 1)
    store.bind_session(bound["id"], "s1")
    done = _batch_card(store, "done", 2)
    store.bind_session(done["id"], "s1")
    store.update(done["id"], {"column_name": "done"})
    waiting = _batch_card(store, "waiting", 3)
    # A waiting member a person dragged out of Backlog: no session, so it
    # loses its mark too, whatever its column.
    dragged = _batch_card(store, "dragged", 4)
    store.update(dragged["id"], {"column_name": "in_progress"})
    starting = _batch_card(store, "starting", 5)
    store.update(starting["id"], {"link_state": "dispatching"}, bump=False)
    other = _batch_card(store, "other batch", 1, bid="other")
    revisions = {c["id"]: store.get(c["id"])["revision"]
                 for c in (bound, done, waiting, dragged, other)}

    assert store.release_batch_waiting("tok", "the batch ended") == 2
    for released in (waiting, dragged):
        row = store.get(released["id"])
        assert (row["batch_id"], row["batch_rank"]) == ("", "")
        assert row["dispatch_error"] == "the batch ended"
    assert store.get(waiting["id"])["column_name"] == "backlog"
    assert store.get(dragged["id"])["column_name"] == "in_progress"
    assert store.get(starting["id"])["batch_id"] == "tok", \
        "a head still binding clears its own mark"
    for kept in (bound, done):
        assert store.get(kept["id"])["batch_id"] == "tok"
    assert store.get(other["id"])["batch_id"] == "other"
    # Dark Army observing, not a person editing: no revision step.
    for cid, rev in revisions.items():
        assert store.get(cid)["revision"] == rev
    # Idempotent, and an empty id is a no-op.
    assert store.release_batch_waiting("tok", "again") == 0
    assert store.release_batch_waiting("", "x") == 0


def test_release_batch_waiting_clamps_the_note(store):
    waiting = _batch_card(store, "waiting", 2)
    store.release_batch_waiting("tok", "x" * 2000)
    assert len(store.get(waiting["id"])["dispatch_error"]) == \
        board_mod.MAX_CLOSE_NOTE_CHARS


# --- card dependencies: the dispatch-4 repairs ---------------------------------


def test_a_dependency_id_longer_than_the_bound_is_refused(store):
    a = _card(store, title="a")
    got, detail = store.update(a["id"], {
        "blocked_by": "x" * (board_mod.MAX_CARD_ID_CHARS + 1)})
    assert got is None
    assert detail == (f"a card id is at most {board_mod.MAX_CARD_ID_CHARS} "
                      "characters")
    got, detail = store.update(a["id"], {"blocked_by": "y" * 200_000})
    assert got is None and store.get(a["id"])["blocked_by"] == ""
    b = _card(store, title="b")
    assert len(b["id"]) <= board_mod.MAX_CARD_ID_CHARS
    got, detail = store.update(a["id"], {"blocked_by": b["id"]})
    assert got is not None, detail


def test_the_flag_records_its_session_and_the_clear_empties_it(store):
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    flagged, _ = store.flag_manual(card["id"], "sess-1", "1. look")
    assert flagged["manual_session_id"] == "sess-1"
    assert board_mod.SINGLE_WRITER["manual_session_id"] == "flag_manual"
    assert "manual_session_id" not in BoardStore._WRITABLE
    cleared, detail = store.clear_manual(card["id"])
    assert cleared is not None, detail
    assert cleared["manual_session_id"] == "" and cleared["manual_steps"] == ""


def test_the_v29_wipe_runs_before_the_marker_moves(tmp_path, monkeypatch):
    """A wipe that fails must leave the file at 28, so the next open tries
    again — never a 29 with the leftover lists still in it."""
    path = tmp_path / "v28.db"
    store = BoardStore(path)
    store.connect()
    store._conn.execute(
        "UPDATE schema_meta SET value = '28' WHERE key = 'version'")
    store._conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at, blocked_by) VALUES ('c1', 'bob', 'old', 'backlog',"
        " 1, 1, 'stale')")
    store._conn.commit()
    store.close()

    def boom(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(BoardStore, "_clear_retired_blocked_by", boom)
    failed = BoardStore(path)
    with pytest.raises(sqlite3.OperationalError):
        failed.connect()
    failed.close()
    monkeypatch.undo()
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT value FROM schema_meta WHERE key = "
                           "'version'").fetchone()[0] == "28"
    finally:
        raw.close()
    store = BoardStore(path)
    store.connect()
    try:
        assert store.get("c1")["blocked_by"] == ""
    finally:
        store.close()


# --- v30: the card's own worktree ---------------------------------------------


def test_v30_worktree_columns_are_bookkeeping_with_one_writer():
    """`record_worktree`'s pair: spelled alike in the CREATE and the ALTER
    list, one writer in `SINGLE_WRITER`, and in none of the three rings a
    surface reaches — `_WRITABLE`, `ApiServer._BOARD_FIELDS`,
    `REVISED_COLUMNS` (`docs/card-worktrees.md`, *The store*)."""
    from dark_army_daemon.api_server import ApiServer
    assert SCHEMA_VERSION == 31
    for name in ("worktree_path", "worktree_branch"):
        assert (name, "TEXT NOT NULL DEFAULT ''") \
            in BoardStore._ADDED_COLUMNS["cards"]
        assert f"{name} TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA
        assert board_mod.SINGLE_WRITER[name] == "record_worktree"
        assert name not in BoardStore._WRITABLE
        assert name not in ApiServer._BOARD_FIELDS
        assert name not in board_mod.REVISED_COLUMNS
    assert callable(BoardStore.record_worktree)
    assert callable(BoardStore.clear_worktree)


def test_a_v29_file_gains_the_worktree_columns_and_keeps_its_cards(tmp_path):
    path = tmp_path / "v29.db"
    store = BoardStore(path)
    store.connect()
    card, _ = store.create({"title": "kept", "project": "p", "root": "/tmp"})
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '29' WHERE key = 'version'")
    conn.execute("ALTER TABLE cards DROP COLUMN worktree_path")
    conn.execute("ALTER TABLE cards DROP COLUMN worktree_branch")
    conn.commit()
    names = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    assert "worktree_path" not in names
    conn.close()
    store = BoardStore(path)
    store.connect()
    try:
        got = store.get(card["id"])
        assert got["title"] == "kept"
        assert got["worktree_path"] == "" and got["worktree_branch"] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row[0]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_record_worktree_is_the_one_writer_and_moves_no_revision(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp"})
        before = store.get(card["id"])["revision"]
        # `update()` ignores the pair: no surface and no generic save sets it.
        store.update(card["id"], {"title": "t2",
                                  "worktree_path": "/tmp/.worktrees/card-x",
                                  "worktree_branch": "card/x"})
        got = store.get(card["id"])
        assert got["worktree_path"] == "" and got["worktree_branch"] == ""
        moved = got["revision"]
        got, detail = store.record_worktree(
            card["id"], "/tmp/.worktrees/card-x", "card/x-t")
        assert detail == "recorded"
        assert got["worktree_path"] == "/tmp/.worktrees/card-x"
        assert got["worktree_branch"] == "card/x-t"
        assert got["revision"] == moved and moved >= before
        # A branch alone is a record (a finished card's memory of it); a
        # path with no branch is not.
        assert store.record_worktree(card["id"], "/p", "")[0] is None
        assert store.record_worktree(card["id"], "/p", "b" * 2000)[0] is None
        assert store.record_worktree("gone", "/p", "b") == (None,
                                                             "that card is gone")
        # The default clear empties the folder only; the branch stays.
        got, detail = store.clear_worktree(card["id"])
        assert detail == "cleared"
        assert got["worktree_path"] == ""
        assert got["worktree_branch"] == "card/x-t"
        assert store.clear_worktree(card["id"])[0] is None
        # `branch=True` (a landed merge that deleted it) empties the name.
        got, detail = store.clear_worktree(card["id"], branch=True)
        assert detail == "cleared" and got["worktree_branch"] == ""
        assert store.clear_worktree(card["id"], branch=True)[0] is None
        got, _ = store.record_worktree(card["id"], "", "card/y")
        assert got["worktree_path"] == "" and got["worktree_branch"] == "card/y"
    finally:
        store.close()


# --- v31: review and merge ------------------------------------------------------


V31_COLUMNS = ("merge_state", "merge_note", "review_verdict", "review_tip")


def test_v31_columns_are_bookkeeping_with_their_own_writers():
    """The four columns: spelled alike in the CREATE and the ALTER list, one
    writer each in `SINGLE_WRITER`, and in none of the three rings a surface
    reaches (`docs/card-worktrees.md`, *Review and merge*)."""
    from dark_army_daemon.api_server import ApiServer
    assert SCHEMA_VERSION == 31
    writers = {"merge_state": "record_merge", "merge_note": "record_merge",
               "review_verdict": "record_review_verdict",
               "review_tip": "record_review_verdict"}
    for name in V31_COLUMNS:
        assert (name, "TEXT NOT NULL DEFAULT ''") \
            in BoardStore._ADDED_COLUMNS["cards"]
        assert f"{name} TEXT NOT NULL DEFAULT ''" in board_mod._SCHEMA
        assert board_mod.SINGLE_WRITER[name] == writers[name]
        assert callable(getattr(BoardStore, writers[name]))
        assert name not in BoardStore._WRITABLE
        assert name not in ApiServer._BOARD_FIELDS
        assert name not in board_mod.REVISED_COLUMNS


def test_a_v30_file_gains_the_merge_columns_and_keeps_its_cards(tmp_path):
    path = tmp_path / "v30.db"
    store = BoardStore(path)
    store.connect()
    card, _ = store.create({"title": "kept", "project": "p", "root": "/tmp"})
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '30' WHERE key = 'version'")
    for name in V31_COLUMNS:
        conn.execute(f"ALTER TABLE cards DROP COLUMN {name}")
    conn.commit()
    names = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    assert not names & set(V31_COLUMNS)
    conn.close()
    store = BoardStore(path)
    store.connect()
    try:
        got = store.get(card["id"])
        assert got["title"] == "kept"
        for name in V31_COLUMNS:
            assert got[name] == ""
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(row[0]) == SCHEMA_VERSION == 31
    finally:
        store.close()


def test_record_merge_and_review_verdict_are_guarded_writers(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp",
                                "column_name": "done"})
        cid = card["id"]
        before = store.get(cid)["revision"]
        got, detail = store.record_merge(cid, "conflict", "needs you")
        assert detail == "recorded"
        assert got["merge_state"] == "conflict"
        assert got["merge_note"] == "needs you"
        assert got["revision"] == before
        # Off-list values and a gone card are refused.
        assert store.record_merge(cid, "exploded", "x")[0] is None
        assert store.record_merge("gone", "merged", "x")[0] is None
        # `''` empties both.
        got, _ = store.record_merge(cid, "", "ignored")
        assert got["merge_state"] == "" and got["merge_note"] == ""
        tip = "a" * 40
        got, detail = store.record_review_verdict(cid, "ship", tip)
        assert detail == "recorded"
        assert got["review_verdict"] == "ship" and got["review_tip"] == tip
        assert got["revision"] == before
        assert store.record_review_verdict(cid, "maybe", tip)[0] is None
        assert store.record_review_verdict(cid, "stop", "not-a-hash")[0] is None
        assert store.record_review_verdict("gone", "stop", tip)[0] is None
        got, _ = store.record_review_verdict(cid, "", tip)
        assert got["review_verdict"] == "" and got["review_tip"] == ""
        # `update()` ignores all four: no surface or generic save sets them.
        store.update(cid, {"title": "t2", "merge_state": "merged",
                           "review_verdict": "ship"})
        got = store.get(cid)
        assert got["merge_state"] == "" and got["review_verdict"] == ""
    finally:
        store.close()


def test_a_card_leaving_done_loses_its_merge_pair_and_keeps_its_review(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp",
                                "column_name": "done"})
        cid = card["id"]
        store.record_merge(cid, "blocked", "words")
        store.record_review_verdict(cid, "stop", "b" * 40)
        store.update(cid, {"column_name": "backlog"})
        got = store.get(cid)
        assert got["merge_state"] == "" and got["merge_note"] == ""
        assert got["review_verdict"] == "stop" and got["review_tip"] == "b" * 40
        # A merge record on a card that is not in Done is refused.
        assert store.record_merge(cid, "merged", "x")[0] is None
    finally:
        store.close()
