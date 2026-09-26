# host/tests/test_card_revision.py
"""The card's change number: the column, the ring, the bump set, the guard.

`revision` is the store's own counter — `create_token`'s ring, written by
`BoardStore` and named by no verb — and it exists so two people editing one
card can never silently overwrite each other. Two mirror risks are what these
tests hold down: a revision that moves too **often** makes the phone
re-download the board continuously and refuses a save because a session bound
itself while somebody was typing; one that moves too **rarely** is the silent
overwrite the column exists to prevent.
"""

import json
import sqlite3

import pytest

from dark_army_daemon import board as board_mod
from dark_army_daemon.board import (CARD_CHANGED_REFUSAL, REVISED_COLUMNS,
                                        SCHEMA_VERSION, BoardStore)
from dark_army_daemon.api_server import ApiServer


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


# --- the column ---------------------------------------------------------------


def test_the_create_and_alter_spellings_agree_on_revision():
    """`_add_missing_columns` is PRAGMA-driven and only ever sees the list, so
    the two paths have to agree exactly or an older file gains a column the
    fresh schema spells differently."""
    assert ("revision", "INTEGER NOT NULL DEFAULT 0") \
        in BoardStore._ADDED_COLUMNS["cards"]
    assert "revision INTEGER NOT NULL DEFAULT 0" in board_mod._SCHEMA
    assert SCHEMA_VERSION == 29


def test_a_v15_shaped_file_gains_the_column_and_keeps_its_cards(tmp_path):
    """`outcome_revision`'s precedent: `INTEGER NOT NULL DEFAULT 0`, so a
    build that has never heard of the column goes on INSERTing without naming
    it and an existing row simply reads 0."""
    path = tmp_path / "v15.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,"
                 " value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '15')")
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
        " create_token TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " done_at REAL DEFAULT NULL)")
    conn.execute(
        "INSERT INTO cards (id, project, title, column_name, created_at,"
        " updated_at) VALUES ('old', 'bob', 'from before', 'backlog', 1, 1)")
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.connect()
    try:
        row = store.get("old")
        assert row is not None
        assert row["revision"] == 0
        meta = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        assert int(meta["value"]) == SCHEMA_VERSION == 29
    finally:
        store.close()


# --- the ring -----------------------------------------------------------------


def test_revision_is_in_neither_writable_ring():
    """`create_token`'s ring exactly: the store writes it, no verb is named
    for it, and a surface that could set it could walk a stale save straight
    past the guard the column exists to arm."""
    assert "revision" not in BoardStore._WRITABLE
    assert "revision" not in board_mod.SINGLE_WRITER
    assert "revision" not in ApiServer._BOARD_FIELDS


def test_every_api_writable_field_is_counted_by_the_revision():
    """The silent-overwrite mirror: a field a surface can change and the
    guard cannot see is a field two people can overwrite each other on. The
    outcome names are excluded because they have `outcome_revision` of their
    own, and `author` because `_board_fields` pops it."""
    outcome = {"beneficiary", "intended_benefit", "success_criterion",
               "outcome_check_on"}
    assert set(ApiServer._BOARD_FIELDS) - outcome - {"author"} \
        <= REVISED_COLUMNS


def test_bobs_bookkeeping_is_deliberately_not_counted():
    """The re-download mirror: the reconcile writes several of these every
    few seconds on a busy board."""
    bookkeeping = {
        "session_id", "link_state", "dispatch_error", "dispatched_at",
        "session_ended_at", "queue_state", "queued_at", "queue_rank",
        "agent_trail", "position", "done_at", "updated_at", "reviewed_at",
        "outcome_revision", "outcome_status", "create_token",
        "plan_approved", "plan_approved_at",
    }
    assert REVISED_COLUMNS.isdisjoint(bookkeeping)


# --- the bump -----------------------------------------------------------------


def test_a_title_write_bumps_by_one(store):
    card = _card(store)
    assert card["revision"] == 0
    after, _ = store.update(card["id"], {"title": "renamed"})
    assert after["revision"] == 1


def test_the_same_title_written_twice_bumps_once(store):
    """A no-op save is not a revision — otherwise a card sheet that saves
    every field on every edit would move the number on a press that changed
    nothing, and the phone would re-download it."""
    card = _card(store)
    first, _ = store.update(card["id"], {"title": "renamed"})
    assert first["revision"] == 1
    again, _ = store.update(card["id"], {"title": "renamed"})
    assert again["revision"] == 1


def test_a_link_state_write_bumps_nothing(store):
    """The reconcile's own churn. This is the whole re-download argument."""
    card = _card(store)
    after, _ = store.update(card["id"], {"link_state": "live",
                                         "session_id": "abc"})
    assert after["revision"] == 0


def test_record_agents_and_mark_reviewed_bump_nothing(store):
    card = _card(store, tool="claude")
    store.update(card["id"], {"session_id": "s1", "link_state": "live"})
    store.record_agents(card["id"], ["bc-implementer"])
    assert store.get(card["id"])["revision"] == 0

    store.declare_done(card["id"], "s1", "done, here is why")
    before = store.get(card["id"])["revision"]
    store.mark_reviewed(card["id"])
    assert store.get(card["id"])["revision"] == before


def test_the_single_writer_verbs_each_bump_once(store, tmp_path):
    card = _card(store, tool="claude")
    store.update(card["id"], {"session_id": "s1", "link_state": "live"})
    start = store.get(card["id"])["revision"]

    store.flag_manual(card["id"], "s1", "1. look at it")
    assert store.get(card["id"])["revision"] == start + 1
    store.clear_manual(card["id"])
    assert store.get(card["id"])["revision"] == start + 2
    store.declare_done(card["id"], "s1", "finished")
    assert store.get(card["id"])["revision"] == start + 3


def test_attach_plan_bumps_once_and_a_refused_one_bumps_nothing(store):
    card = _card(store, tool="claude")
    ok, _ = store.attach_plan(card["id"], "plans/x.md", "s1")
    assert ok is not None
    first = store.get(card["id"])["revision"]
    assert first == 1
    # The card is out of Prep now, so the second attach is refused and the
    # bump inherits the WHERE clause's own guard.
    again, detail = store.attach_plan(card["id"], "plans/y.md", "s1")
    assert again is None, detail
    assert store.get(card["id"])["revision"] == first


def test_a_refused_close_bumps_nothing(store):
    card = _card(store, tool="claude")
    store.update(card["id"], {"session_id": "s1", "link_state": "live"})
    before = store.get(card["id"])["revision"]
    refused, detail = store.declare_done(card["id"], "somebody-else", "nope")
    assert refused is None, detail
    assert store.get(card["id"])["revision"] == before


# --- the guard ----------------------------------------------------------------


def test_a_stale_expected_revision_refuses_and_writes_nothing(store):
    card = _card(store)
    store.update(card["id"], {"title": "renamed"})
    at = store.get(card["id"])
    assert at["revision"] == 1

    refused, detail = store.update(
        card["id"], {"title": "mine", "expected_revision": 0})
    assert refused is None
    assert detail == CARD_CHANGED_REFUSAL
    after = store.get(card["id"])
    assert after["title"] == "renamed"
    assert after["revision"] == 1
    assert after["updated_at"] == at["updated_at"]


def test_the_matching_expected_revision_writes(store):
    card = _card(store)
    after, detail = store.update(
        card["id"], {"title": "mine", "expected_revision": 0})
    assert after is not None, detail
    assert after["title"] == "mine"
    assert after["revision"] == 1


def test_an_absent_expected_revision_writes_at_any_revision(store):
    """**Absent means no guard**, and it is load-bearing: the channel's card
    verbs, the reconcile and the auto-filer all send none, and the board must
    go on moving."""
    card = _card(store)
    for n in range(5):
        store.update(card["id"], {"title": f"pass {n}"})
    assert store.get(card["id"])["revision"] == 5
    after, detail = store.update(card["id"], {"title": "no guard at all"})
    assert after is not None, detail
    assert after["revision"] == 6


def test_a_non_integer_expected_revision_refuses(store):
    card = _card(store)
    for bad in ("0", 0.0, True, [0]):
        refused, detail = store.update(
            card["id"], {"title": "mine", "expected_revision": bad})
        assert refused is None, bad
        assert detail == CARD_CHANGED_REFUSAL


def test_the_refusal_opens_with_the_words_both_surfaces_match(store):
    """Recognised by prefix on both surfaces, `PLAN_GATE_REFUSAL`'s rule, so
    the sentence may only ever be extended at its end."""
    assert CARD_CHANGED_REFUSAL.startswith("this card changed on the Mac")


# --- the two bodies -----------------------------------------------------------


class _FakeDaemon:
    def __init__(self, store):
        self._board = store
        self.card = None
        self.detail = ""

    async def update_card(self, card_id, fields, allow_unplanned=False):
        return self.card, self.detail

    async def card_stated_fields(self, card_id):
        row = self._board.get(card_id) or {}
        return {"revision": int(row.get("revision") or 0),
                "title": str(row.get("title") or ""),
                "summary": str(row.get("summary") or ""),
                "prompt": str(row.get("prompt") or "")}


@pytest.mark.asyncio
async def test_the_200_body_carries_the_new_revision(store):
    card = _card(store)
    daemon = _FakeDaemon(store)
    daemon.card, daemon.detail = store.update(card["id"], {"title": "renamed"})
    srv = ApiServer(daemon, port=0)
    status, _ctype, body = await srv._board_action(
        "board_update", {"card_id": card["id"], "title": "renamed"})
    assert status == 200
    assert json.loads(body)["revision"] == 1


@pytest.mark.asyncio
async def test_the_409_body_carries_what_the_store_holds(store):
    card = _card(store)
    store.update(card["id"], {"title": "renamed", "summary": "the real one"})
    daemon = _FakeDaemon(store)
    daemon.card, daemon.detail = None, CARD_CHANGED_REFUSAL
    srv = ApiServer(daemon, port=0)
    status, _ctype, body = await srv._board_action(
        "board_update", {"card_id": card["id"], "title": "mine",
                         "expected_revision": "0"})
    assert status == 409
    payload = json.loads(body)
    assert payload["detail"] == CARD_CHANGED_REFUSAL
    assert payload["current"]["title"] == "renamed"
    assert payload["current"]["summary"] == "the real one"
    assert payload["current"]["revision"] == 1


def test_expected_revision_is_only_carried_when_the_key_is_there():
    """A default of 0 would guard every write in the codebase against a value
    none of them holds, and the board would stop moving."""
    srv = ApiServer(object(), port=0)
    assert "expected_revision" not in srv._board_fields({"title": "x"})
    assert srv._board_fields(
        {"title": "x", "expected_revision": "4"})["expected_revision"] == 4
    # And a payload naming only the expectation stays "no fields to change".
    assert srv._board_fields({"expected_revision": "4"}) == {}


def test_a_malformed_expectation_is_not_silently_dropped():
    """The key being present says the caller meant to guard. Dropping an
    unparseable value would turn a malformed guard into no guard at all."""
    srv = ApiServer(object(), port=0)
    out = srv._board_fields({"title": "x", "expected_revision": "banana"})
    assert out["expected_revision"] == "banana"
    assert type(out["expected_revision"]) is not int


# --- Dark Army's own moves do not count (2026-09-06 bug audit) ----------------------


def test_bind_session_moves_the_card_and_bumps_nothing(store):
    """The failure `REVISED_COLUMNS`' docstring names, driven rather than
    asserted from a list of column names: `column_name` **is** counted (a
    person dragging a card is a change a person made), but `bind_session`
    writes it as Dark Army's own bookkeeping. Without `bump=False` a session
    binding itself would refuse the save of whoever was typing into that
    card at the time.

    `bump=False` rather than dropping `column_name` from `REVISED_COLUMNS`,
    deliberately: the mirror invariant — every `_BOARD_FIELDS` name is
    counted — is the *silent overwrite* guard, and weakening it to fix a
    nuisance refusal would trade a correctness property for a convenience.
    """
    card = _card(store, tool="claude")
    at = store.get(card["id"])["revision"]

    bound, detail = store.bind_session(card["id"], "sess-1")
    assert bound is not None, detail
    assert bound["column_name"] == "in_progress"
    assert bound["revision"] == at

    # And a save made against the revision the person was shown still works.
    saved, detail = store.update(
        card["id"], {"title": "typed while it bound",
                     "expected_revision": at})
    assert saved is not None, detail
    assert saved["revision"] == at + 1


def test_a_persons_column_move_still_counts(store):
    """The other half: `bump` defaults to True, so a hand drag is a change
    the phone re-reads and a stale save is refused over."""
    card = _card(store)
    at = store.get(card["id"])["revision"]
    moved, detail = store.update(card["id"], {"column_name": "backlog"})
    assert moved is not None, detail
    assert moved["revision"] == at + 1


def test_bump_false_is_unreachable_from_a_surface():
    """`ApiServer` calls `daemon.update_card(card_id, fields)`, which passes
    no such keyword; the flag is a positional-free, keyword-only argument on
    the store and cannot ride in a fields dict."""
    import inspect
    sig = inspect.signature(BoardStore.update)
    assert sig.parameters["bump"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["bump"].default is True
    src = inspect.getsource(ApiServer._board_action)
    assert "bump" not in src


# --- the importance number (v18) ----------------------------------------------


def test_priority_is_counted_by_the_revision():
    """It is drawn on the card face, so a change to it must move the change
    number or a phone holding a stale copy would never re-fetch it. The
    `_BOARD_FIELDS ⊆ REVISED_COLUMNS` mirror requires it anyway."""
    assert "priority" in REVISED_COLUMNS
    assert "priority" in ApiServer._BOARD_FIELDS


def test_writing_a_priority_moves_the_revision_and_arms_the_guard(store):
    """Dark Army's own scoring write goes through the ordinary `update` with no
    `bump=False`: it happens once per card ever, and it is a change to
    something a person reads."""
    card = _card(store)
    stale = card["revision"]
    row, detail = store.update(card["id"], {"priority": "80"})
    assert row is not None, detail
    assert row["revision"] > stale

    refused, detail = store.update(
        card["id"], {"title": "mine", "expected_revision": stale})
    assert refused is None
    assert detail == CARD_CHANGED_REFUSAL
    assert store.get(card["id"])["title"] == card["title"]
