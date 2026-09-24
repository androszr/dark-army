# host/tests/test_board_plan_approval.py
"""Approving the plan version that will run.

Three things, and the middle one is the dangerous one. The store's
`approve_plan` writes down *which wording* a person read, guarded in the
UPDATE's own WHERE clause like every other single-writer verb. The plan gate
grows a second rung that fires when that wording has since changed — and
**never** on a card that was simply never approved, which is every card on
every board that existed before schema 15. And the sealed card read serves the
whole prompt and the plan document together, stating an unavailable plan's
reason rather than leaving a client to guess.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import daemon_board, dispatch
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import SCHEMA_VERSION, SINGLE_WRITER, BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _project(tmp_path, text="# A plan\n\nDo the thing.\n"):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True, exist_ok=True)
    plan = root / "plans" / "p.md"
    plan.write_text(text)
    return root, plan


def _planned(store, tmp_path, **kw):
    root, plan = _project(tmp_path)
    fields = {"title": "t", "project": "proj", "root": str(root),
              "prompt": "go", "tool": "claude", "summary": "s"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    got, detail = store.attach_plan(card["id"], str(plan), "sess-1")
    assert got is not None, detail
    return got, plan


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- the store verb ------------------------------------------------------------


def test_approve_plan_writes_both_columns(daemon, tmp_path):
    _d, store = daemon
    card, plan = _planned(store, tmp_path)
    mark = _digest(plan.read_text())
    got, detail = store.approve_plan(card["id"], card["plan_path"], mark, 1234.0)
    assert got is not None, detail
    assert got["plan_approved"] == mark
    assert got["plan_approved_at"] == 1234.0


def test_approve_plan_refuses_a_path_that_is_no_longer_the_cards(daemon, tmp_path):
    """The guard rides in the WHERE clause: a card whose plan is not the one
    being approved is `rowcount == 0`, not a silent write."""
    _d, store = daemon
    card, _plan = _planned(store, tmp_path)
    got, detail = store.approve_plan(card["id"], "/somewhere/else.md", "a" * 64, 1.0)
    assert got is None
    assert detail == "that card's plan changed — read it again"
    assert store.get(card["id"])["plan_approved"] == ""


def test_approve_plan_refuses_in_words_before_it_writes(daemon, tmp_path):
    _d, store = daemon
    card, plan = _planned(store, tmp_path)
    assert store.approve_plan("no-such-card", str(plan), "a" * 64, 1.0) \
        == (None, "no such card")
    got, detail = store.approve_plan(card["id"], "", "a" * 64, 1.0)
    assert got is None and detail == "a plan needs a path"
    got, detail = store.approve_plan(card["id"], str(plan), "", 1.0)
    assert got is None and detail == "an approval needs the plan it approves"
    plain, detail = store.create({"title": "u", "project": "proj",
                                  "root": str(tmp_path), "tool": "claude"})
    assert plain is not None, detail
    got, detail = store.approve_plan(plain["id"], str(plan), "a" * 64, 1.0)
    assert got is None and detail == "that card has no plan to approve"


# --- the single-writer ring ----------------------------------------------------


def test_the_approval_pair_has_exactly_one_writer():
    assert SINGLE_WRITER["plan_approved"] == "approve_plan"
    assert SINGLE_WRITER["plan_approved_at"] == "approve_plan"
    assert not ({"plan_approved", "plan_approved_at"} & BoardStore._WRITABLE)
    assert not ({"plan_approved", "plan_approved_at"}
                & set(ApiServer._BOARD_FIELDS))


def test_a_generic_update_carrying_the_approval_is_ignored(daemon, tmp_path):
    """Ignored rather than raising: a surface a version ahead must not be able
    to crash a write, and must not be able to forge an approval either."""
    _d, store = daemon
    card, _plan = _planned(store, tmp_path)
    got, _detail = store.update(card["id"], {"title": "renamed",
                                             "plan_approved": "f" * 64})
    assert got is not None
    assert got["title"] == "renamed"
    assert got["plan_approved"] == ""


# --- the schema ----------------------------------------------------------------


def test_the_schema_moved_and_an_older_file_gains_both_columns(tmp_path):
    """The approval pair arrived at v15. The assertion is `>=` rather than
    `==` because the marker is forward-only and later columns land behind it;
    what this pins is that the pair is *in* the shipped schema and that a file
    written without them still opens, migrates and INSERTs."""
    assert SCHEMA_VERSION >= 15
    path = tmp_path / "old.db"
    store = BoardStore(path)
    store.connect()
    store._conn.execute("ALTER TABLE cards DROP COLUMN plan_approved")
    store._conn.execute("ALTER TABLE cards DROP COLUMN plan_approved_at")
    store._conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', '14') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    store._conn.commit()
    store.close()

    reopened = BoardStore(path)
    reopened.connect()
    try:
        names = {row["name"] for row in
                 reopened._conn.execute("PRAGMA table_info(cards)")}
        assert {"plan_approved", "plan_approved_at"} <= names
        card, detail = reopened.create({"title": "t", "project": "p",
                                        "root": "/tmp", "tool": "claude"})
        assert card is not None, detail
        row = reopened.get(card["id"])
        assert row["plan_approved"] == ""
        assert row["plan_approved_at"] is None
    finally:
        reopened.close()


# --- the gate's second rung ----------------------------------------------------


@pytest.mark.asyncio
async def test_no_approval_never_gates(daemon, tmp_path):
    """**The upgrade case, and the single most damaging bug available here.**
    Every card on every board that exists carries `plan_approved == ''`; a
    rung that fired on absence would make the whole board unstartable."""
    d, store = daemon
    card, _plan = _planned(store, tmp_path)
    assert await d._plan_gate_refusal(store.get(card["id"])) == ""


@pytest.mark.asyncio
async def test_a_matching_approval_never_gates(daemon, tmp_path):
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    store.approve_plan(card["id"], card["plan_path"],
                       _digest(plan.read_text()), 1.0)
    assert await d._plan_gate_refusal(store.get(card["id"])) == ""


@pytest.mark.asyncio
async def test_a_stale_approval_gates_in_its_own_words(daemon, tmp_path):
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    store.approve_plan(card["id"], card["plan_path"],
                       _digest(plan.read_text()), 1.0)
    plan.write_text("# A plan\n\nDo something else entirely.\n")
    detail = await d._plan_gate_refusal(store.get(card["id"]))
    assert detail == daemon_mod.PLAN_CHANGED_REFUSAL
    assert detail != daemon_mod.PLAN_GATE_REFUSAL


@pytest.mark.asyncio
async def test_a_deleted_plan_still_gets_the_first_rung(daemon, tmp_path):
    """A file that is gone is *worse* than a changed one — the card claims a
    stage that cannot be read at all — so it keeps the unplanned refusal."""
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    store.approve_plan(card["id"], card["plan_path"],
                       _digest(plan.read_text()), 1.0)
    os.unlink(plan)
    assert await d._plan_gate_refusal(store.get(card["id"])) \
        == daemon_mod.PLAN_GATE_REFUSAL


@pytest.mark.asyncio
async def test_a_bound_card_is_exempt_from_both_rungs(daemon, tmp_path):
    """A card whose assistant already ran is *tracking*, not admitting
    unplanned work — the exemption at the top short-circuits both rungs."""
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    store.approve_plan(card["id"], card["plan_path"],
                       _digest(plan.read_text()), 1.0)
    plan.write_text("moved under it\n")
    row = dict(store.get(card["id"]), session_id="sess-9")
    assert await d._plan_gate_refusal(row) == ""


@pytest.mark.asyncio
async def test_a_confirmed_press_starts_a_stale_approval_card(
        daemon, tmp_path, monkeypatch):
    """The gate is a confirmation, not a wall: `skip_plan_gate` is the escape
    hatch for the second rung exactly as it is for the first."""
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    store.approve_plan(card["id"], card["plan_path"],
                       _digest(plan.read_text()), 1.0)
    plan.write_text("moved under it\n")
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(tmp_path / "proj"))})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: f"/bin/{tool}")

    async def accept(root, argv, name, **_kw):
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    # The baseline `git rev-parse` a successful dispatch books in the
    # background outlives this test's event loop; nothing here is about it.
    monkeypatch.setattr(d, "_schedule_work_baseline",
                        lambda *a, **k: None)
    ok, detail = await d.dispatch_card(card["id"])
    assert not ok and detail == daemon_mod.PLAN_CHANGED_REFUSAL
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail


# --- the daemon verb -----------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_card_plan_refuses_a_digest_it_did_not_just_send(
        daemon, tmp_path):
    d, store = daemon
    card, _plan = _planned(store, tmp_path)
    got, detail = await d.approve_card_plan(card["id"], card["plan_path"],
                                            "0" * 64)
    assert got is None
    assert "changed while you were reading it" in detail
    assert store.get(card["id"])["plan_approved"] == ""


@pytest.mark.asyncio
async def test_approve_card_plan_records_the_version_on_disk(daemon, tmp_path):
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    mark = _digest(plan.read_text())
    got, detail = await d.approve_card_plan(card["id"], card["plan_path"], mark)
    assert got is not None, detail
    assert got["plan_approved"] == mark
    assert got["plan_approved_at"] > 0


@pytest.mark.asyncio
async def test_approve_card_plan_refuses_a_card_with_no_plan(daemon, tmp_path):
    d, store = daemon
    card, detail = store.create({"title": "t", "project": "p",
                                 "root": str(tmp_path), "tool": "claude"})
    assert card is not None, detail
    got, detail = await d.approve_card_plan(card["id"], "x.md", "a" * 64)
    assert got is None and detail == "that card has no plan to approve"
    assert await d.approve_card_plan("nope", "x.md", "a" * 64) \
        == (None, "no such card")


def test_the_digest_helper_fails_closed(tmp_path):
    """An unreadable or oversize plan digests as `""`, which can never equal a
    stored approval — so failure means *the gate fires*, never that it is
    skipped."""
    assert daemon_board._plan_digest(str(tmp_path / "nope.md")) == ""
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (64 * 1024 + 1))
    assert daemon_board._plan_digest(str(big)) == ""
    real = tmp_path / "real.md"
    real.write_text("hello\n")
    assert daemon_board._plan_digest(str(real)) == _digest("hello\n")


# --- the card read -------------------------------------------------------------


def _collect(daemon_obj, card_id, *, with_plan):
    srv = ApiServer(daemon_obj, port=0)
    return srv._card_collect(card_id, with_plan=with_plan)


def test_card_collect_serves_the_whole_prompt_and_the_plan(daemon, tmp_path):
    d, store = daemon
    card, plan = _planned(store, tmp_path, prompt="x" * 900)
    report = _collect(d, card["id"], with_plan=True)
    assert report["available"] is True
    assert report["cards"][0]["prompt"] == "x" * 900
    assert "messages" not in report["cards"][0]
    assert report["plan"] == {
        "available": True, "path": card["plan_path"],
        "text": plan.read_text(), "digest": _digest(plan.read_text()),
        "reason": ""}


@pytest.mark.parametrize("break_it,reason", [
    ("outside", "the plan must be a file inside the card's own project"),
    ("not_md", "a plan is a Markdown (.md) file"),
    ("missing", "there is no file at that path"),
    ("oversize", "that file is too large to be a plan"),
])
def test_an_unusable_plan_states_its_reason(daemon, tmp_path, break_it, reason):
    d, store = daemon
    card, plan = _planned(store, tmp_path)
    if break_it == "outside":
        other = tmp_path / "elsewhere.md"
        other.write_text("# nope\n")
        repoint = str(other)
    elif break_it == "not_md":
        other = plan.with_suffix(".txt")
        other.write_text("# nope\n")
        repoint = str(other)
    elif break_it == "missing":
        os.unlink(plan)
        repoint = str(plan)
    else:
        plan.write_bytes(b"x" * (64 * 1024 + 1))
        repoint = str(plan)
    store._conn.execute("UPDATE cards SET plan_path = ? WHERE id = ?",
                        (repoint, card["id"]))
    store._conn.commit()
    report = _collect(d, card["id"], with_plan=True)
    assert report["plan"]["available"] is False
    assert report["plan"]["reason"] == reason
    assert report["plan"]["digest"] == ""


def test_a_card_with_no_plan_says_so(daemon, tmp_path):
    d, store = daemon
    card, detail = store.create({"title": "t", "project": "p",
                                 "root": str(tmp_path), "tool": "claude"})
    assert card is not None, detail
    report = _collect(d, card["id"], with_plan=True)
    assert report["plan"] == {"available": False, "path": "", "text": "",
                              "digest": "", "reason": "this card has no plan"}


def test_the_loopback_shape_is_unchanged(daemon, tmp_path):
    """`with_plan=False` is byte-for-byte today's `GET /api/board?card=`
    payload: the card with its messages, and no plan key at all."""
    d, store = daemon
    card, _plan = _planned(store, tmp_path)
    report = _collect(d, card["id"], with_plan=False)
    assert set(report) == {"available", "range", "generated_at", "cards"}
    assert report["cards"][0]["messages"] == []
