# host/tests/test_scout_cards.py
"""Scout cards: kind, the waived plan gate, attach_report, and Promote."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon import dispatch
from dark_army_daemon import scout_report
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import (
    KIND_LOCKED_REFUSAL, KIND_REFUSAL, MAX_PROMPT_CHARS, MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    REPORT_MALFORMED_REFUSAL, REPORT_NOT_SCOUT_REFUSAL, REVISED_COLUMNS, SCOUT_PLAN_REFUSAL,
    BoardStore, SINGLE_WRITER,
)
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.daemon_board import BoardVerbsMixin

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    d._board = board
    try:
        yield d, board
    finally:
        board.close()


def _make(store, **kw):
    fields = {"title": "Scout: why X", "project": "bob", "root": "/tmp",
              "prompt": "look into it", "tool": "claude",
              "summary": "why X"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _arm_spawn(daemon_obj, monkeypatch, spawns=None):
    monkeypatch.setattr(daemon_obj, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/bin/{tool}")

    async def accept(root, argv, name, **_kw):
        if spawns is not None:
            spawns.append({"root": root, "argv": argv, "name": name})
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)


def _done_scout(store, report, **kw):
    card = _make(store, kind="scout", **kw)
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1",
        "link_state": "live"}, bump=False)
    attached, detail = store.attach_report(card["id"], report, "sess-1")
    assert attached is not None, detail
    done, detail = store.declare_done(card["id"], "sess-1", "Report: " + report)
    assert done is not None, detail
    return done


# --- kind round-trip ----------------------------------------------------------


def test_create_scout_round_trips_kind(store):
    card = _make(store, kind="scout")
    assert card["kind"] == "scout"
    assert store.get(card["id"])["kind"] == "scout"


def test_create_default_and_ship_store_empty_kind(store):
    assert _make(store)["kind"] == ""
    assert _make(store, kind="ship", title="build it")["kind"] == ""


def test_create_bogus_kind_is_refused(store):
    card, detail = store.create({"title": "nope", "kind": "bogus"})
    assert card is None
    assert detail == KIND_REFUSAL


def test_update_kind_in_prep_bumps_revision(store):
    card = _make(store)
    assert card["revision"] == 0
    after, detail = store.update(card["id"], {"kind": "scout"})
    assert after is not None, detail
    assert after["kind"] == "scout"
    assert after["revision"] == 1


def test_update_kind_outside_prep_is_locked(store):
    card = _make(store)
    store.update(card["id"], {"column_name": "backlog"})
    before = store.get(card["id"])
    after, detail = store.update(card["id"], {"kind": "scout"})
    assert after is None
    assert detail == KIND_LOCKED_REFUSAL
    held = store.get(card["id"])
    assert held["kind"] == ""
    assert held["revision"] == before["revision"]


# --- rings --------------------------------------------------------------------


def test_rings_place_kind_and_report_path():
    assert "report_path" not in BoardStore._WRITABLE
    assert SINGLE_WRITER["report_path"] == "attach_report"
    assert "kind" in ApiServer._BOARD_FIELDS
    assert "report_path" not in ApiServer._BOARD_FIELDS
    assert {"kind", "report_path"} <= REVISED_COLUMNS
    # The attached report's verdict line, at v28: `attach_report`'s ring.
    assert SINGLE_WRITER["report_verdict"] == "attach_report"
    assert SINGLE_WRITER["report_recommendation"] == "attach_report"
    assert {"report_verdict", "report_recommendation"} <= REVISED_COLUMNS
    for field in ("report_verdict", "report_recommendation"):
        assert field not in BoardStore._WRITABLE
        assert field not in ApiServer._BOARD_FIELDS


def test_update_cannot_write_report_verdict(store):
    card = _make(store, kind="scout")
    after, _detail = store.update(card["id"], {"report_verdict": "x",
                                               "report_recommendation": "build"})
    stored = store.get(card["id"])
    assert stored["report_verdict"] == ""
    assert stored["report_recommendation"] == ""
    if after is not None:
        assert after["report_verdict"] == ""


def test_update_cannot_write_report_path(store):
    card = _make(store, kind="scout")
    after, _detail = store.update(card["id"], {"report_path": "x"})
    assert store.get(card["id"])["report_path"] == ""
    if after is not None:
        assert after["report_path"] == ""


# --- attach_report ------------------------------------------------------------


def test_attach_report_on_bound_scout(store):
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    attached, detail = store.attach_report(
        card["id"], "/tmp/docs/research/x.md", "sess-1")
    assert attached is not None, detail
    assert attached["report_path"] == "/tmp/docs/research/x.md"
    assert attached["revision"] == card["revision"] + 1
    assert attached["column_name"] == "in_progress"


def test_attach_report_replaces_on_second_attach(store):
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    store.attach_report(card["id"], "/tmp/a.md", "sess-1")
    attached, detail = store.attach_report(card["id"], "/tmp/b.md", "sess-1")
    assert attached is not None, detail
    assert attached["report_path"] == "/tmp/b.md"


def _bound_scout(store):
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    return card


def test_attach_report_stores_verdict_and_recommendation(store):
    card = _bound_scout(store)
    before = store.get(card["id"])["revision"]
    attached, detail = store.attach_report(
        card["id"], "/tmp/a.md", "sess-1",
        verdict="X waits on a lock nobody releases.", recommendation="build")
    assert attached is not None, detail
    assert attached["report_verdict"] == "X waits on a lock nobody releases."
    assert attached["report_recommendation"] == "build"
    assert attached["revision"] == before + 1


def test_attach_report_clamps_and_collapses_the_verdict(store):
    card = _bound_scout(store)
    long_line = "word " * (MAX_SUMMARY_CHARS // 5 + 50)
    verdict = "first line\n" + long_line + "\n  third\tline"
    attached, detail = store.attach_report(
        card["id"], "/tmp/a.md", "sess-1", verdict=verdict)
    assert attached is not None, detail
    stored = attached["report_verdict"]
    assert "\n" not in stored and "\t" not in stored and "  " not in stored
    assert stored.startswith("first line word")
    assert len(stored) <= MAX_SUMMARY_CHARS
    assert stored == stored.strip()


def test_attach_report_drops_an_unknown_recommendation(store):
    card = _bound_scout(store)
    attached, _ = store.attach_report(
        card["id"], "/tmp/a.md", "sess-1", verdict="v", recommendation="ship it")
    assert attached["report_recommendation"] == ""
    attached, _ = store.attach_report(
        card["id"], "/tmp/a.md", "sess-1", verdict="v", recommendation="BUILD")
    assert attached["report_recommendation"] == "build"


def test_attach_report_replaces_the_verdict_on_second_attach(store):
    card = _bound_scout(store)
    store.attach_report(card["id"], "/tmp/a.md", "sess-1",
                        verdict="first", recommendation="build")
    attached, detail = store.attach_report(card["id"], "/tmp/b.md", "sess-1")
    assert attached is not None, detail
    assert attached["report_verdict"] == ""
    assert attached["report_recommendation"] == ""


def test_attach_report_wrong_session_is_refused(store):
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    before = store.get(card["id"])["revision"]
    attached, _detail = store.attach_report(
        card["id"], "/tmp/docs/research/x.md", "other")
    assert attached is None
    assert store.get(card["id"])["report_path"] == ""
    assert store.get(card["id"])["revision"] == before


def test_attach_report_on_ship_is_refused(store):
    card = _make(store)
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    attached, detail = store.attach_report(
        card["id"], "/tmp/docs/research/x.md", "sess-1")
    assert attached is None
    assert detail == REPORT_NOT_SCOUT_REFUSAL


def test_attach_report_on_done_scout_is_refused(store):
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    store.declare_done(card["id"], "sess-1", "done")
    attached, _detail = store.attach_report(
        card["id"], "/tmp/docs/research/x.md", "sess-1")
    assert attached is None
    assert store.get(card["id"])["report_path"] == ""


def test_attach_plan_on_scout_is_refused(store):
    card = _make(store, kind="scout")
    attached, detail = store.attach_plan(
        card["id"], "/tmp/plans/x.md", "sess-1")
    assert attached is None
    assert detail == SCOUT_PLAN_REFUSAL
    assert store.get(card["id"])["column_name"] == "prep"
    assert store.get(card["id"])["plan_path"] == ""


# --- attach_report_by_session -------------------------------------------------


@pytest.mark.asyncio
async def test_attach_report_by_session_resolves_the_open_card(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = root / "docs" / "research" / "why.md"
    report.parent.mkdir(parents=True)
    report.write_text("# why\n")
    card = _make(store, kind="scout", root=str(root))
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    assert attached["report_path"] == os.path.realpath(str(report))


@pytest.mark.asyncio
async def test_attach_report_by_session_refuses_no_card(daemon):
    d, _store = daemon
    card, detail = await d.attach_report_by_session("nobody", "/tmp/x.md")
    assert card is None
    assert "no card" in detail


@pytest.mark.asyncio
async def test_attach_report_by_session_refuses_two_cards(daemon, tmp_path):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    for title in ("one", "two"):
        card = _make(store, kind="scout", title=title, root=str(root))
        store.update(card["id"], {
            "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    card, detail = await d.attach_report_by_session("sess-1", "/tmp/x.md")
    assert card is None
    assert "more than one" in detail


@pytest.mark.asyncio
async def test_attach_report_by_session_refuses_a_non_scout(daemon, tmp_path):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    card = _make(store, root=str(root))
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    attached, detail = await d.attach_report_by_session("sess-1", "/tmp/x.md")
    assert attached is None
    assert detail == REPORT_NOT_SCOUT_REFUSAL


@pytest.mark.asyncio
async def test_attach_report_by_session_refuses_a_path_outside_the_root(
        daemon, tmp_path):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# no\n")
    card = _make(store, kind="scout", root=str(root))
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    attached, detail = await d.attach_report_by_session("sess-1", str(outside))
    assert attached is None
    assert "inside the card's own project" in detail


@pytest.mark.asyncio
async def test_attach_report_by_session_refuses_empty_session(daemon):
    d, _store = daemon
    card, detail = await d.attach_report_by_session("", "/tmp/x.md")
    assert card is None
    assert "could not tell which session" in detail


@pytest.mark.asyncio
async def test_handle_board_attach_report_request_refuses_unattributable_port(
        daemon, monkeypatch):
    d, _store = daemon

    async def nobody(port):
        return ""

    monkeypatch.setattr(d, "_board_request_session_fresh", nobody)
    reply = await d._handle_board_attach_report_request(
        {"type": "board_attach_report_request", "port": 51000,
         "path": "/tmp/x.md"})
    assert reply["ok"] is False
    assert "could not tell which session" in reply["detail"]


@pytest.mark.asyncio
async def test_handle_board_attach_report_request_answers_ok(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = root / "why.md"
    report.write_text("# why\n")
    card = _make(store, kind="scout", root=str(root))
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)

    async def fresh(port):
        return "sess-1"

    async def publish():
        return None

    monkeypatch.setattr(d, "_board_request_session_fresh", fresh)
    monkeypatch.setattr(d, "_board_author_guard", lambda port: None)
    monkeypatch.setattr(d, "_publish_board", publish)
    reply = await d._handle_board_attach_report_request(
        {"type": "board_attach_report_request", "port": 51000,
         "path": str(report)})
    assert reply["ok"] is True
    assert reply["column"] == "in_progress"
    assert reply["card_id"] == card["id"]


# --- the gate -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_scout_start_is_not_gated(daemon, monkeypatch):
    d, store = daemon
    card = _make(store, kind="scout")
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.dispatch_card(card["id"])
    assert ok, detail
    assert len(spawns) == 1
    prompt = spawns[0]["argv"][-1]
    assert prompt.startswith("/scout ")
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_ship_start_is_still_gated(daemon, monkeypatch):
    from dark_army_daemon import daemon as daemon_mod
    d, store = daemon
    card = _make(store)
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == daemon_mod.PLAN_GATE_REFUSAL
    assert spawns == []
    assert store.get(card["id"])["column_name"] == "prep"


@pytest.mark.asyncio
async def test_update_card_to_in_progress_on_scout_is_not_gated(daemon):
    d, store = daemon
    card = _make(store, kind="scout")
    after, detail = await d.update_card(
        card["id"], {"column_name": "in_progress"})
    assert after is not None, detail
    assert after["column_name"] == "in_progress"


# --- the prompt ---------------------------------------------------------------


def test_scout_prompt_leads_with_the_title_on_a_one_word_summary():
    """A card titled "Before open report" with the summary `O` opened
    `/scout O` — a brief with nothing in it. The title is the brief
    when the summary is under `SCOUT_BRIEF_MIN_WORDS` words; the summary
    still rides, once, on its own line."""
    prompt = dispatch.scout_prompt({
        "kind": "scout", "title": "Before open report", "summary": "O"})
    assert prompt.startswith("/scout Before open report")
    assert "Brief: O" in prompt
    assert "Title:" not in prompt
    # A real summary keeps the lead, and the title follows as before.
    prompt = dispatch.scout_prompt({
        "kind": "scout", "title": "Scout: why X",
        "summary": "why does the strip flicker on wake"})
    assert prompt.startswith("/scout why does the strip flicker on wake")
    assert "Title: Scout: why X" in prompt
    assert "Brief:" not in prompt
    # No summary at all: the title leads and is not repeated.
    prompt = dispatch.scout_prompt({"kind": "scout", "title": "Why X"})
    assert prompt == "/scout Why X"
    # A "Scout:" label on the title is not more brief than the summary.
    prompt = dispatch.scout_prompt({
        "kind": "scout", "title": "Scout: why X", "summary": "why X"})
    assert prompt.startswith("/scout why X\n")
    assert "Title: Scout: why X" in prompt
    # The label is dropped when the title does lead.
    prompt = dispatch.scout_prompt({
        "kind": "scout", "title": "Scout: before open report", "summary": "O"})
    assert prompt.startswith("/scout before open report\n")


def test_scout_prompt_ignores_plan_path():
    prompt = dispatch.start_prompt({
        "kind": "scout", "title": "Scout: why X", "summary": "why X",
        "plan_path": "/tmp/plans/x.md"})
    assert prompt.startswith("/scout why X")
    assert "/ship implement" not in prompt


def test_refine_guard_refuses_a_scout():
    ok, detail = dispatch.refine_guard(
        {"kind": "scout", "column_name": "prep", "title": "Scout: why X",
         "tool": "claude", "root": "/tmp"},
        roots=["/tmp"], in_flight=[], now=0)
    assert ok is False
    assert detail == dispatch.SCOUT_REFINE_REFUSAL


def test_guard_accepts_a_prep_scout(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    card = {
        "kind": "scout", "column_name": "prep", "title": "Scout: why X",
        "summary": "why X", "tool": "claude", "root": str(root),
        "session_id": "", "link_state": "", "refine_state": "",
    }
    ok, detail = dispatch.guard(
        card, roots=[str(root)], in_flight=[], now=0)
    assert ok, detail
    assert dispatch.start_prompt(card).startswith("/scout ")


# --- Promote ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_creates_a_prep_build_card(daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = str(root / "docs" / "research" / "why.md")
    scout = _done_scout(store, report, root=str(root), project="bob",
                        tool="claude", area="backbone",
                        summary="why X")
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    new, detail = await d.promote_card(scout["id"])
    assert new is not None, detail
    assert new["kind"] == ""
    assert new["column_name"] == "prep"
    assert new["title"] == "why X"
    assert new["summary"] == "why X"
    assert new["area"] == "backbone"
    assert new["root"] == str(root)
    assert new["project"] == "bob"
    assert new["tool"] == "claude"
    assert new["prompt"].startswith(
        f"From report: {report}\n\nReport: {report}\n\n")


@pytest.mark.asyncio
async def test_promote_second_press_is_refused(daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = str(root / "why.md")
    scout = _done_scout(store, report, root=str(root))
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    first, detail = await d.promote_card(scout["id"])
    assert first is not None, detail
    second, detail = await d.promote_card(scout["id"])
    assert second is None
    assert detail.startswith("already promoted")


@pytest.mark.asyncio
async def test_promote_is_refused_after_the_promoted_card_moved_on(
        daemon, tmp_path, monkeypatch):
    """The twin scan used to read Prep and Backlog alone, so a promoted
    card that had been started was invisible and a second press wrote a
    second card."""
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = str(root / "why.md")
    scout = _done_scout(store, report, root=str(root))
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    first, detail = await d.promote_card(scout["id"])
    assert first is not None, detail
    moved, detail = store.update(first["id"], {
        "column_name": "in_progress", "session_id": "sess-2"}, bump=False)
    assert moved is not None, detail
    second, detail = await d.promote_card(scout["id"])
    assert second is None
    assert detail.startswith("already promoted")
    assert len(store.cards()) == 2


@pytest.mark.asyncio
async def test_promote_without_report_is_refused(daemon):
    d, store = daemon
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    store.declare_done(card["id"], "sess-1", "finished without a report")
    before = len(store.cards())
    new, detail = await d.promote_card(card["id"])
    assert new is None
    assert "no report" in detail
    assert len(store.cards()) == before


@pytest.mark.asyncio
async def test_promote_in_progress_is_refused(daemon):
    d, store = daemon
    card = _make(store, kind="scout")
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    store.attach_report(card["id"], "/tmp/why.md", "sess-1")
    new, detail = await d.promote_card(card["id"])
    assert new is None
    assert "finish the scout first" in detail


@pytest.mark.asyncio
async def test_promote_done_ship_is_refused(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    store.declare_done(card["id"], "sess-1", "shipped")
    new, detail = await d.promote_card(card["id"])
    assert new is None
    assert "only a scout card" in detail


@pytest.mark.asyncio
async def test_promote_clamps_overlong_prompt(daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = str(root / "why.md")
    blob = "x" * (MAX_PROMPT_CHARS - 10)
    scout = _done_scout(store, report, root=str(root), prompt=blob)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    new, detail = await d.promote_card(scout["id"])
    assert new is not None, detail
    assert len(new["prompt"]) == MAX_PROMPT_CHARS
    assert new["prompt"].startswith("From report: " + report)


@pytest.mark.asyncio
async def test_board_promote_answers_200_with_card_id(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = str(root / "why.md")
    scout = _done_scout(store, report, root=str(root))
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    srv = ApiServer(d, port=0)
    status, _ctype, body = await srv._board_action(
        "board_promote", {"card_id": scout["id"]})
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["card_id"]
    assert payload["card_id"] != scout["id"]


@pytest.mark.asyncio
async def test_board_promote_answers_409_on_refusal(daemon):
    d, store = daemon
    card = _make(store, kind="scout")
    srv = ApiServer(d, port=0)
    status, _ctype, body = await srv._board_action(
        "board_promote", {"card_id": card["id"]})
    assert status == 409
    payload = json.loads(body)
    assert payload["ok"] is False
    assert payload["card_id"] == ""


def test_board_promote_is_a_phone_verb_at_home_and_away():
    """Loopback-only in v1; chosen for the phone on 21 Sep 2026, once the
    phone drew the report it promotes. Each tuple names it on its own line
    (`test_phone_needs_you` slices at the first parenthesis), and the
    board-name set routes it through `_board_action`."""
    assert "board_promote" in ApiServer.BOARD_ACTIONS
    assert "board_promote" in ApiServer.LAN_ACTIONS
    assert "board_promote" in ApiServer.REMOTE_ACTIONS
    assert "board_promote" in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


class _PipelineStub(BoardVerbsMixin):
    """`_pipeline_writable` alone, off a daemon with no observers and no
    projects — the marker is a version fact, not a live one."""

    def _observers_implementing(self, name):
        return False

    def _board_projects(self):
        return []


def test_board_says_promote_supported():
    assert _PipelineStub()._pipeline_writable()["promote_supported"] is True
    models = (ROOT / "ios" / "BobPhone" / "Models.swift").read_text()
    assert 'case promoteSupported = "promote_supported"' in models
    assert "promoteSupported = c.value(.promoteSupported, false)" in models


# --- channel ------------------------------------------------------------------


def test_card_tool_kind_is_optional_enum():
    props = cs.CARD_TOOL["inputSchema"]["properties"]
    assert props["kind"]["enum"] == ["ship", "scout"]
    assert "kind" not in cs.CARD_TOOL["inputSchema"]["required"]


@pytest.mark.asyncio
async def test_handle_board_card_request_writes_a_scout(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    d._handle_channel_message({
        "type": "channel_attach", "port": 51000, "pid": 4242,
        "cwd": str(root), "session_id": "s1", "is_channel": True,
        "secret": "x", "host": cs.HOST_CLAUDE})
    d._session_states["s1"] = {"pid": 4242, "last_event": 0, "project": "bob"}
    d._agents_snapshot_cache = {"running": [
        {"session_id": "s1", "cwd": str(root), "project": "bob"}]}
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    monkeypatch.setattr(d, "_board_projects",
                        lambda: [{"name": "bob",
                                  "root": os.path.realpath(str(root))}])
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)
    reply = await d._handle_board_card_request({
        "type": "board_card_request", "port": 51000,
        "title": "Scout: flicker", "kind": "scout"})
    assert reply["ok"] is True, reply
    card = store.cards()[0]
    assert card["kind"] == "scout"
    assert card["column_name"] == "prep"


@pytest.mark.asyncio
async def test_handle_board_card_request_refuses_bogus_kind(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    d._handle_channel_message({
        "type": "channel_attach", "port": 51000, "pid": 4242,
        "cwd": str(root), "session_id": "s1", "is_channel": True,
        "secret": "x", "host": cs.HOST_CLAUDE})
    d._session_states["s1"] = {"pid": 4242, "last_event": 0, "project": "bob"}
    d._agents_snapshot_cache = {"running": [
        {"session_id": "s1", "cwd": str(root), "project": "bob"}]}
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    monkeypatch.setattr(d, "_board_projects",
                        lambda: [{"name": "bob",
                                  "root": os.path.realpath(str(root))}])
    reply = await d._handle_board_card_request({
        "type": "board_card_request", "port": 51000,
        "title": "nope", "kind": "bogus"})
    assert reply["ok"] is False
    assert reply["detail"] == KIND_REFUSAL
    assert store.total() == 0


def test_report_tool_schema():
    assert set(cs.REPORT_TOOL["inputSchema"]["properties"]) == {"path"}


def test_attach_report_over_stdio_carries_the_port_and_the_path(monkeypatch):
    server = cs.ChannelServer(stdin=__import__("io").StringIO(),
                              stdout=__import__("io").StringIO())
    server.port = 51000
    sent = {}
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.update(msg) or {
                            "ok": True, "column": "in_progress", "title": "t"})
    result = server.call_tool({"name": "dark_army_attach_report",
                               "arguments": {"path": "docs/research/x.md"}})
    assert result["isError"] is False
    assert sent["type"] == "board_attach_report_request"
    assert sent["port"] == 51000
    assert sent["path"] == "docs/research/x.md"
    assert "card_id" not in sent
    assert "stays in progress" in result["content"][0]["text"]


def test_attach_report_empty_path_is_error_without_round_trip(monkeypatch):
    server = cs.ChannelServer(stdin=__import__("io").StringIO(),
                              stdout=__import__("io").StringIO())

    def boom(msg):
        raise AssertionError("empty path must not round-trip")

    monkeypatch.setattr(server, "call_daemon", boom)
    result = server.call_tool({"name": "dark_army_attach_report", "arguments": {}})
    assert result["isError"] is True


# --- grep pins ----------------------------------------------------------------


def _board_card_source(rel: str) -> str:
    """`struct BoardCard` through the next top-level `struct`, not the file.

    Phone `Models.swift` has other `c.value(.kind, "")` decodes (AccessLog,
    enrolment, usage, event log, CardTimeline); those predate this pin.
    """
    text = (ROOT / rel).read_text()
    start = text.find("struct BoardCard")
    assert start >= 0, f"{rel}: no struct BoardCard"
    rest = text[start:]
    nxt = rest.find("\nstruct ", 1)
    assert nxt > 0, f"{rel}: no next struct after BoardCard"
    return rest[:nxt]


def test_both_clients_decode_kind_and_report_path_tolerantly():
    for rel in ("panel/Sources/BobPanel/BoardModels.swift",
                "ios/BobPhone/Models.swift"):
        text = _board_card_source(rel)
        assert text.count('c.value(.kind, "")') == 1
        assert text.count('c.value(.reportPath, "")') == 1
        assert "decode(.kind" not in text
        assert "decode(.reportPath" not in text
        assert text.count('c.value(.reportVerdict, "")') == 1
        assert text.count('c.value(.reportRecommendation, "")') == 1
        assert "decode(.reportVerdict" not in text
        assert "decode(.reportRecommendation" not in text


def test_starts_unplanned_excludes_a_scout():
    """Start on a scout is not asked 'this card has no plan yet'."""
    panel_card = (ROOT / "panel/Sources/BobPanel/BoardCardView.swift").read_text()
    panel_sheet = (ROOT / "panel/Sources/BobPanel/BoardCardSheet.swift").read_text()
    panel_board = (ROOT / "panel/Sources/BobPanel/BoardView.swift").read_text()
    phone = (ROOT / "ios/BobPhone/CardDetailView.swift").read_text()
    assert "card.planPath.isEmpty && !card.isScout" in panel_card
    assert "live.planPath.isEmpty && !live.isScout" in panel_sheet
    assert panel_board.count("card.planPath.isEmpty && !card.isScout") == 2
    assert "card.planPath.isEmpty && !card.isScout" in phone
    assert "live.planPath.isEmpty && !live.isScout" in phone
    # The old planPath-only startsUnplanned / drop-hold forms are gone.
    assert "private var startsUnplanned: Bool { card.planPath.isEmpty }" \
        not in panel_card
    assert "private var startsUnplanned: Bool { live?.planPath.isEmpty ?? true }" \
        not in panel_sheet
    assert "private var startsUnplanned: Bool { card.planPath.isEmpty }" \
        not in phone


def test_phone_can_refine_excludes_a_scout():
    phone = (ROOT / "ios/BobPhone/CardDetailView.swift").read_text()
    start = phone.index("private var canRefine: Bool {")
    body = phone[start:phone.index("\n    }", start)]
    # The phone's Refine terms live in `PhoneRowSelection` since the Board
    # tab's batch refine (the tick and the button share one rule), so the
    # pin follows them there, as the Mac's does below.
    assert 'PhoneRowSelection.tickable("prep", card: card,' in body
    rule = (ROOT / "ios/BobPhone/RowSelection.swift").read_text()
    start = rule.index("static func tickable(")
    tick = rule[start:rule.index("\n    }", start)]
    assert "&& !card.isScout" in tick
    panel = (ROOT / "panel/Sources/BobPanel/BoardCardView.swift").read_text()
    start = panel.index("private var canRefine: Bool {")
    body = panel[start:panel.index("\n    }", start)]
    # The Mac's Refine terms live in `RowSelection` since the Prep row's
    # batch refine (the tick and the button share one rule), so the pin
    # follows them there: `canRefine` delegates, and the `.prep` case
    # excludes a scout.
    assert "RowSelection.tickable(.prep, card: card, chrome: chrome)" in body
    rule = (ROOT / "panel/Sources/BobPanel/RowSelection.swift").read_text()
    start = rule.index("case .prep:")
    prep = rule[start:rule.index("case .backlog", start)]
    assert "&& !card.isScout" in prep


def test_can_promote_requires_done():
    panel_card = (ROOT / "panel/Sources/BobPanel/BoardCardView.swift").read_text()
    panel_sheet = (ROOT / "panel/Sources/BobPanel/BoardCardSheet.swift").read_text()
    assert "card.column == BoardColumn.done.rawValue" in panel_card
    assert "live.column == BoardColumn.done.rawValue" in panel_sheet
    # Promote is not offered on an In-progress scout that already has a report.
    card_start = panel_card.index("private var canPromote: Bool {")
    card_body = panel_card[card_start:panel_card.index("\n    }", card_start)]
    assert "BoardColumn.done.rawValue" in card_body
    sheet_start = panel_sheet.index(
        "private func canPromote(_ live: BoardCard) -> Bool {")
    sheet_body = panel_sheet[sheet_start:panel_sheet.index("\n    }", sheet_start)]
    assert "BoardColumn.done.rawValue" in sheet_body


def test_composer_hides_refine_for_a_scout_draft():
    sheet = (ROOT / "panel/Sources/BobPanel/BoardCardSheet.swift").read_text()
    start = sheet.index("private var footer: some View {")
    end = sheet.index("private var saveHeld: Bool {", start)
    footer = sheet[start:end]
    assert 'state.draft.kind != "scout"' in footer
    assert footer.index('state.draft.kind != "scout"') < footer.index(
        'Button("Add & Refine")')
    assert footer.index('state.draft.kind != "scout"') < footer.index(
        "state.draft.startWhenPlanned.toggle()")


# --- the phone composer -------------------------------------------------------


def test_the_daemon_marks_scout_support_for_the_phone():
    """An older Mac drops `kind` on a phone's `board_create` and writes a
    build card, so the phone draws its Build / Scout control only against a
    Mac that says it takes the key."""
    assert _PipelineStub()._pipeline_writable()["scout_supported"] is True
    models = (ROOT / "ios/BobPhone/Models.swift").read_text()
    assert 'case scoutSupported = "scout_supported"' in models
    assert "scoutSupported = c.value(.scoutSupported, false)" in models


def test_the_phone_composer_offers_scout_and_sends_kind_only_for_a_scout():
    composer = (ROOT / "ios/BobPhone/ComposerView.swift").read_text()
    assert "if board.scoutSupported {\n                    kindControl" in composer
    assert 'kindChip("Build", selected: kind != "scout") { kind = "" }' in composer
    assert 'kindChip("Scout", selected: kind == "scout") { kind = "scout" }' in composer
    start = composer.index("private func save(refine: Bool = false) async {")
    body = composer[start:composer.index("private func priorityAcceptable", start)]
    assert 'if board.scoutSupported && kind == "scout" { fields["kind"] = kind }' in body
    # A scout has no plan: start-when-planned never rides with it, and the
    # Save & Refine button is absent for it.
    assert 'if startWhenPlanned && kind != "scout" {' in body
    assert 'if board.dispatchEnabled && !offline && kind != "scout" {' in composer
    # The draft keeps the choice and the bank carries it.
    assert "_kind = State(initialValue: d?.kind ?? \"\")" in composer
    assert "priority: draftPriority, area: area, kind: kind)" in composer


def test_the_phone_outbox_carries_kind_tolerantly():
    outbox = (ROOT / "ios/BobPhone/Outbox.swift").read_text()
    assert outbox.count('kind = c.value(.kind, "")') == 2, "entry and draft"
    assert 'if entry.kind == "scout" { fields["kind"] = entry.kind }' in outbox


# --- the scout/ folder: shape on attach, header on Promote -----------------------

CHECKED_REPORT = """# Why X stalls

- **Card:** Scout: why X
- **Project:** bob
- **Question:** Why does X stall?
- **Verdict:** X waits on a lock nobody releases.
- **Confidence:** high
- **Recommendation:** build
- **Follow-up:** Release the lock on error — free X's lock in the failure path
- **Follow-up:** Log the holder — name who holds X's lock when it stalls
- **Sources:** host/x.py, the log

## Question
## What was found
## Evidence
## Recommendation
## Open questions
"""


def _scout_in_progress(store, root):
    card = _make(store, kind="scout", root=str(root))
    store.update(card["id"], {
        "column_name": "in_progress", "session_id": "sess-1"}, bump=False)
    return card


def _quiet(d, monkeypatch):
    async def publish():
        return None
    monkeypatch.setattr(d, "_publish_board", publish)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_attach_report_under_scout_refuses_a_malformed_report(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "scout" / "2026-09-24-x" / "report.md",
                    "# why\n\nprose, no block\n")
    card = _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is None
    assert detail.startswith(REPORT_MALFORMED_REFUSAL)
    assert "missing" in detail and "Verdict" in detail
    assert "scout_check.py" in detail
    assert store.get(card["id"])["report_path"] == ""


@pytest.mark.asyncio
async def test_attach_report_under_scout_accepts_a_checked_report(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "scout" / "2026-09-24-x" / "report.md",
                    CHECKED_REPORT)
    _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    assert os.path.isabs(attached["report_path"])
    assert attached["report_path"] == os.path.realpath(str(report))


@pytest.mark.asyncio
async def test_attach_report_outside_scout_is_not_checked(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "docs" / "research" / "why.md", "# why\n")
    # Neither a look-alike folder nor a file named after it is inside scout/.
    lookalike = _write(root / "scouting" / "report.md", "# why\n")
    _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    attached, detail = await d.attach_report_by_session(
        "sess-1", str(lookalike))
    assert attached is not None, detail
    assert d._report_shape_refusal(str(root), str(root / "scout.md")) == ""


@pytest.mark.asyncio
async def test_report_path_is_always_absolute(daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "scout" / "2026-09-24-x" / "report.md",
                    CHECKED_REPORT)
    card = _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session(
        "sess-1", "scout/2026-09-24-x/report.md")
    assert attached is not None, detail
    stored = store.get(card["id"])["report_path"]
    assert os.path.isabs(stored)
    assert stored == os.path.realpath(str(report))


def _promotable(d, store, root, monkeypatch, report, **kw):
    scout = _done_scout(store, report, root=str(root), **kw)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    _quiet(d, monkeypatch)
    return scout


@pytest.mark.asyncio
async def test_promote_reads_the_header(daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = os.path.realpath(str(_write(
        root / "scout" / "2026-09-24-x" / "report.md", CHECKED_REPORT)))
    scout = _promotable(d, store, root, monkeypatch, report)
    new, detail = await d.promote_card(scout["id"])
    assert new is not None, detail
    assert new["title"] == "Release the lock on error"
    assert new["summary"] == "X waits on a lock nobody releases."
    assert new["kind"] == ""
    assert new["column_name"] == "prep"
    assert new["prompt"].startswith(
        f"From report: {report}\n\nRecommendation: build (high confidence)")
    assert "Question: Why does X stall?" in new["prompt"]
    assert "- Log the holder — name who holds X's lock when it stalls" \
        in new["prompt"]
    # The twin check still reads the lead line.
    again, detail = await d.promote_card(scout["id"])
    assert again is None and detail.startswith("already promoted")


@pytest.mark.asyncio
async def test_promote_without_a_header_is_unchanged(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = str(_write(root / "docs" / "research" / "why.md",
                        "# why\n\nprose\n"))
    scout = _promotable(d, store, root, monkeypatch, report, summary="why X")
    new, detail = await d.promote_card(scout["id"])
    assert new is not None, detail
    assert new["title"] == "why X"
    assert new["summary"] == "why X"
    assert new["prompt"].startswith(
        f"From report: {report}\n\nReport: {report}\n\n")
    assert BoardVerbsMixin._promoted_fields(scout, "L", {}) == \
        BoardVerbsMixin._promoted_fields(scout, "L")


@pytest.mark.asyncio
async def test_promote_header_clamp_keeps_the_lead(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    report = os.path.realpath(str(_write(
        root / "scout" / "2026-09-24-x" / "report.md", CHECKED_REPORT)))
    scout = _promotable(d, store, root, monkeypatch, report,
                        prompt="x" * (MAX_PROMPT_CHARS - 10))
    new, detail = await d.promote_card(scout["id"])
    assert new is not None, detail
    assert new["prompt"].startswith("From report: " + report + "\n\n")
    assert len(new["prompt"]) <= MAX_PROMPT_CHARS


def test_promoted_title_from_a_follow_up_is_clamped():
    card = {"title": "Scout: why X", "summary": "why X"}
    header = {"verdict": "v", "follow_ups": [{"title": "t" * 500,
                                              "summary": "s"}]}
    fields = BoardVerbsMixin._promoted_fields(card, "L", header)
    assert len(fields["title"]) == MAX_TITLE_CHARS
    empty = {"verdict": "v", "follow_ups": [{"title": "", "summary": "s"}]}
    assert BoardVerbsMixin._promoted_fields(card, "L", empty)["title"] == "why X"


@pytest.mark.asyncio
async def test_attach_report_under_scout_is_checked_whatever_the_case(
        daemon, tmp_path, monkeypatch):
    """A macOS disk is case-insensitive: `Scout/` is the `scout/` folder,
    so a report written there is checked, not waved through."""
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "Scout" / "2026-09-24-x" / "report.md",
                    "# why\n\nprose, no block\n")
    _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is None
    assert detail.startswith(REPORT_MALFORMED_REFUSAL)
    assert d._report_shape_refusal(
        str(root), os.path.realpath(str(root)) + "/SCOUT/x/report.md") != ""


@pytest.mark.asyncio
async def test_promote_with_a_report_replaced_by_a_fifo_is_headerless(
        daemon, tmp_path, monkeypatch):
    """A FIFO put where the report was must neither hang the press nor
    hold the board's write lock."""
    d, store = daemon
    root = tmp_path / "proj"
    path = _write(root / "scout" / "2026-09-24-x" / "report.md",
                  CHECKED_REPORT)
    report = os.path.realpath(str(path))
    scout = _promotable(d, store, root, monkeypatch, report, summary="why X")
    path.unlink()
    os.mkfifo(report)
    new, detail = await asyncio.wait_for(d.promote_card(scout["id"]), 10)
    assert new is not None, detail
    assert new["title"] == "why X"
    assert new["prompt"].startswith(
        f"From report: {report}\n\nReport: {report}\n\n")
    assert not d._board_write_lock.locked()
    # The bounded read itself refuses a FIFO without blocking.
    assert await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
        None, scout_report.read_header, report), 10) == {}


@pytest.mark.asyncio
async def test_promote_with_a_report_replaced_by_a_link_out_is_headerless(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    path = _write(root / "scout" / "2026-09-24-x" / "report.md",
                  CHECKED_REPORT)
    report = os.path.realpath(str(path))
    outside = _write(tmp_path / "elsewhere" / "report.md", CHECKED_REPORT)
    scout = _promotable(d, store, root, monkeypatch, report, summary="why X")
    path.unlink()
    os.symlink(str(outside), report)
    new, detail = await asyncio.wait_for(d.promote_card(scout["id"]), 10)
    assert new is not None, detail
    assert new["title"] == "why X"
    assert new["summary"] == "why X"
    assert "Recommendation:" not in new["prompt"]


def test_scout_start_opens_the_scout_skill_and_codex_is_told_where_it_is():
    card = {"kind": "scout", "title": "Scout: why X", "tool": "codex",
            "summary": "why does X stall after a wake"}
    prompt = dispatch.start_prompt(card)
    assert prompt.startswith("/scout why does X stall after a wake\n")
    assert "/ship scout" not in prompt
    assert "Read .agents/skills/scout/SKILL.md in this project" in prompt
    assert ".agents/skills/ship/SKILL.md" not in prompt
    claude = dispatch.start_prompt({**card, "tool": "claude"})
    assert "SKILL.md" not in claude


# --- the verdict on the card face ----------------------------------------------


@pytest.mark.asyncio
async def test_attach_report_by_session_records_the_header(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "scout" / "2026-09-25-x" / "report.md",
                    CHECKED_REPORT)
    card = _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    stored = store.get(card["id"])
    assert stored["report_verdict"] == "X waits on a lock nobody releases."
    assert stored["report_recommendation"] == "build"


@pytest.mark.asyncio
async def test_attach_report_by_session_prose_report_records_nothing(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "docs" / "research" / "why.md", "# why\n\nprose\n")
    card = _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    stored = store.get(card["id"])
    assert stored["report_path"] == os.path.realpath(str(report))
    assert stored["report_verdict"] == ""
    assert stored["report_recommendation"] == ""


@pytest.mark.asyncio
async def test_attach_report_by_session_reads_the_header_on_the_executor(
        daemon, tmp_path, monkeypatch):
    """The header read is a blocking file open, so it hops like the shape
    check above it — never inline on the loop."""
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "scout" / "2026-09-25-x" / "report.md",
                    CHECKED_REPORT)
    _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    loop_thread = []
    read_threads = []
    real = scout_report.read_header

    def spy(path):
        read_threads.append(threading.get_ident())
        return real(path)

    monkeypatch.setattr(scout_report, "read_header", spy)
    loop_thread.append(threading.get_ident())
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    assert read_threads and read_threads[0] != loop_thread[0]


@pytest.mark.asyncio
async def test_snapshot_card_carries_the_verdict(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = tmp_path / "proj"
    report = _write(root / "scout" / "2026-09-25-x" / "report.md",
                    CHECKED_REPORT)
    card = _scout_in_progress(store, root)
    _quiet(d, monkeypatch)
    attached, detail = await d.attach_report_by_session("sess-1", str(report))
    assert attached is not None, detail
    trimmed = BoardVerbsMixin._trim_card_for_snapshot(store.get(card["id"]))
    assert trimmed["report_verdict"] == "X waits on a lock nobody releases."
    assert trimmed["report_recommendation"] == "build"
    state = d._refresh_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[card["id"]]["report_verdict"] == (
        "X waits on a lock nobody releases.")
    assert by_id[card["id"]]["report_recommendation"] == "build"


def _block_after(text: str, needle: str, span: int = 700) -> str:
    at = text.find(needle)
    assert at >= 0, needle
    return text[at:at + span]


def test_the_three_surfaces_draw_the_verdict_through_the_shared_rule():
    tile = (ROOT / "panel/Sources/BobPanel/BoardCardView.swift").read_text()
    sheet = (ROOT / "panel/Sources/BobPanel/BoardCardSheet.swift").read_text()
    phone = (ROOT / "ios/BobPhone/CardDetailView.swift").read_text()
    for text in (tile, sheet, phone):
        assert text.count("ScoutVerdictLine.text(") == 1
        assert "ScoutVerdictLine.spoken(" in text
    # The tile draws one line, cut short at the end.
    tile_block = _block_after(tile, "ScoutVerdictLine.text(")
    assert ".lineLimit(1)" in tile_block
    assert ".truncationMode(.tail)" in tile_block
    # The card window draws it under the heading, before the branches.
    section = sheet[sheet.find("private var reportSection"):]
    assert section.find("ScoutVerdictLine.text(") < section.find(
        "if live.reportPath.isEmpty {")
    # The phone draws it in full.
    phone_block = _block_after(phone, "ScoutVerdictLine.text(")
    assert ".lineLimit(" not in phone_block.split("if let report")[0]
