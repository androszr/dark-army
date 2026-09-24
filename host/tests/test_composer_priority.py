# host/tests/test_composer_priority.py
"""A priority number on the new-card form, on the Mac and on the phone
(`plans/2026-09-06-priority-on-new-card.md`).

Three daemon halves and a set of source pins. The daemon: `BoardStore.create`
refuses an off-range number in `PRIORITY_REFUSAL`'s words rather than
silently storing "no opinion"; `board_create` on a real `ApiServer` stores a
typed number and refuses a bad one; and the scorer's `_consider_priority` —
the seam `_reconcile_board` calls — leaves a card created with a number alone.
The pins: both composers draw the box, both clients send it as a string only
when typed, and both draft stores bank it.
"""

import asyncio
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import board, card_priority
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon
from tests.free_ports import free_port

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"

BAD_PRIORITIES = ["101", "-1", "7.5", "high"]


# --- the store ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = board.BoardStore(tmp_path / "board.db")
    s.connect()
    try:
        yield s
    finally:
        s.close()


@pytest.mark.parametrize("bad", BAD_PRIORITIES)
def test_create_refuses_an_off_range_priority_and_writes_nothing(store, bad):
    """Before this, `create` took `normalise_priority(...)[0]` and dropped the
    refusal: 101 on a new card was silently stored as "no opinion" while the
    same 101 on an existing card was refused. One sentence, both paths."""
    card, detail = store.create({"title": "x", "priority": bad})
    assert card is None
    assert detail == board.PRIORITY_REFUSAL
    assert store.total() == 0


def test_create_stores_a_typed_priority_from_the_start(store):
    card, detail = store.create({"title": "x", "priority": " 42 "})
    assert card is not None, detail
    assert card["priority"] == "42"
    assert store.get(card["id"])["priority"] == "42"


# --- the API -----------------------------------------------------------------


def _free_port() -> int:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_port()


PORT = _free_port()


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    """`test_api_server.py`'s guard: keep the real fleet out of `start()`."""
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture
def token_path(tmp_path, monkeypatch):
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    return path


@pytest_asyncio.fixture
async def board_server(token_path, tmp_path):
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    s = board.BoardStore(tmp_path / "board.db")
    s.connect()
    daemon._board = s
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=PORT)
    await srv.start()
    try:
        yield srv, daemon, s
    finally:
        await srv.stop()
        s.close()


def _post(body: dict, token: str):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/action",
        data=json.dumps(body).encode(),
        headers={"X-Bob-Token": token}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.asyncio
async def test_board_create_stores_the_priority_it_was_sent(board_server):
    srv, _, s = board_server
    status, body = await asyncio.to_thread(
        _post, {"action": "board_create", "title": "scored by hand",
                "priority": "42"}, srv.token)
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    cards = s.cards()
    assert [c["title"] for c in cards] == ["scored by hand"]
    assert cards[0]["priority"] == "42"


@pytest.mark.asyncio
async def test_board_create_refuses_a_bad_priority_in_the_stores_words(board_server):
    srv, _, s = board_server
    status, body = await asyncio.to_thread(
        _post, {"action": "board_create", "title": "x", "priority": "101"},
        srv.token)
    assert status != 200
    assert json.loads(body)["detail"] == board.PRIORITY_REFUSAL
    assert s.total() == 0


# --- the scorer --------------------------------------------------------------


@pytest.fixture()
def scoring_daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d._board = board.BoardStore(path=tmp_path / "board.db")
    d._board.connect()
    d._priority_shop = card_priority.PriorityShop(
        path=tmp_path / "card-priority.json")
    d._priority_queue = []
    d._priority_task = None
    return d


def test_the_scorer_leaves_a_card_created_with_a_number_alone(scoring_daemon):
    """The seam `_reconcile_board` calls per card. A number typed on the
    composer is never put in front of the helper — and is learnt, so a later
    clear does not re-score it either."""
    card, detail = scoring_daemon._board.create(
        {"project": "p", "root": "/tmp/p", "title": "a card",
         "summary": "some words", "column_name": "prep", "priority": "42"})
    assert card is not None, detail
    row = scoring_daemon._board.get(card["id"])
    scoring_daemon._consider_priority(row)
    scoring_daemon._consider_priority(row)
    assert scoring_daemon._priority_queue == []
    assert scoring_daemon._priority_shop.consider(card["id"]) == card_priority.PASS


# --- the panel ---------------------------------------------------------------


def _body(text: str, head: str) -> str:
    """The brace-balanced body of the first declaration starting `head`."""
    start = text.index(head)
    open_brace = text.index("{", start)
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace:i + 1]
    raise AssertionError(f"unbalanced body after {head!r}")


def test_the_panel_composer_draws_the_priority_box():
    """The `!isComposer` guard existed because a box on the composer sent
    nothing; it goes only because `saveComposer` now passes the field."""
    text = (PANEL / "BoardCardSheet.swift").read_text()
    assert "prioritySupported && !isComposer" not in text
    for branch in ("private var composerPhaseTwo: some View {",
                   "private var savedCardEditor: some View {"):
        assert "priorityField" in _body(text, branch), branch
    assert "priority: draft.priority" in _body(text, "func saveComposer(")


def test_the_panel_client_sends_the_priority_as_a_string_only_when_typed():
    text = (PANEL / "BoardClient.swift").read_text()
    create = _body(text, "func boardCreate(")
    assert 'priority: String = ""' in text[text.index("func boardCreate("):
                                          text.index("func boardCreate(") + 2000]
    assert 'if !priority.isEmpty { body["priority"] = priority }' in create


def test_the_panel_draft_store_banks_the_priority():
    drafts = (PANEL / "Drafts.swift").read_text()
    assert '"priority": priority' in drafts
    assert 'dict["priority"] as? String ?? ""' in drafts
    assert "draft.priority" in _body(drafts, "static func worthKeeping(")
    state = (PANEL / "BoardState.swift").read_text()
    assert "priority: draft.priority," in state
    assert "next.priority = d.priority" in state


# --- the phone ---------------------------------------------------------------


def test_the_phone_composer_draws_the_priority_box_gated_and_accessible():
    text = (PHONE / "ComposerView.swift").read_text()
    assert "@State private var draftPriority" in text
    assert '_draftPriority = State(initialValue: d?.priority ?? "")' in text
    assert text.count("if board.prioritySupported {") == 1
    start = text.index("if board.prioritySupported {")
    block = _body(text, "if board.prioritySupported {")
    assert ".keyboardType(.numberPad)" in block
    assert ".accessibilityLabel(" in block
    assert ".lineLimit(" not in block
    assert start < text.index("photosRow", start)


def test_the_phone_composer_sends_and_banks_the_priority():
    text = (PHONE / "ComposerView.swift").read_text()
    save = _body(text, "private func save(refine: Bool = false) async")
    assert ('if !draftPriority.isEmpty {\n'
            '            fields["priority"] = draftPriority\n'
            '        }') in save
    assert "priority: draftPriority" in _body(text, "private func currentDraft(")
    assert "priority: draftPriority" in _body(text, "private func bank()")
    key = _body(text, "private var draftKey: [String]")
    assert "draftPriority" in key
    assert 'beneficiary, intendedBenefit, successCriterion, expanded ? "1" : "", "\\u{0}"]' in key


def test_the_phone_outbox_carries_the_priority_end_to_end():
    text = (PHONE / "Outbox.swift").read_text()
    assert len(re.findall(r"^\s*case (?=[^\n]*\bpriority\b)", text, re.M)) == 2
    assert len(re.findall(r"^\s*case (?=[^\n]*\barea\b)", text, re.M)) == 2
    assert text.count('priority = c.value(.priority, "")') == 2
    assert ('if !entry.priority.isEmpty {\n'
            '            fields["priority"] = entry.priority\n'
            '        }') in text
    enqueue = text[text.index("func enqueue("):text.index(") -> OutboxEntry", text.index("func enqueue("))]
    assert 'priority: String = ""' in enqueue
    assert 'area: String = ""' in enqueue
    draft_keep = _body(text[text.index("struct ComposerDraft"):],
                       "var worthKeeping: Bool")
    assert "priority" in draft_keep


def test_the_phone_composer_refuses_a_bad_priority_before_banking_it():
    """The offline route banks the card unchecked, and an outbox row can be
    retried or discarded but never corrected — so a bad number is refused on
    the form, in the store's own sentence, before either `bank()` call."""
    from dark_army_daemon import board

    text = (PHONE / "ComposerView.swift").read_text()
    save = _body(text, "private func save(refine: Bool = false) async")
    check = save.index("if !priorityAcceptable(draftPriority) {")
    assert f'note = "{board.PRIORITY_REFUSAL}"' in save
    # The bank rule is "offline, or a photo still on this phone".
    assert check < save.index("if offline || !localPhotos.isEmpty {")
    assert check < save.index("\n            bank()\n")
    rule = _body(text, "private func priorityAcceptable(_ text: String) -> Bool")
    assert "$0.isASCII && $0.isNumber" in rule
    assert "number <= 100" in rule
