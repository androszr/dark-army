"""Several planned Backlog cards are started from the phone at once.

The phone's Board tab puts its Backlog row into select mode — the Prep row's
machinery, one row at a time — and one armed-then-confirmed press sends
`board_start_batch` **synchronously** (never through the press queue): the
Mac opens one session on the first card and lines the rest up behind it,
and its report is drawn under the Backlog heading. The tick rule
(`PhoneRowSelection.tickable("backlog", …)`, `ios/BobPhone/RowSelection.swift`)
and the batch mark's decode (`BoardCard.batch`, `ios/BobPhone/Models.swift`)
run here under `swiftc`; the XCTest twin is
`ios/BobPhoneTests/RowSelectionTests.swift`. The wiring is pinned by source
greps, and the daemon half — the verb on both phone tuples behind
`start_batch_supported` — by the tuples and a sealed press.
Contract: `docs/phone-contract.md`, *Several planned cards are started from
the phone at once*; `docs/transport-contract.md`, *The LAN door is sealed*.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import subprocess

import pytest

from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon_board import BoardVerbsMixin
from tests.test_phone_action_queue import SOURCES

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULE = PHONE / "RowSelection.swift"
BOARD = PHONE / "BoardView.swift"
CARD = PHONE / "CardDetailView.swift"
ARM = PHONE / "Arm.swift"
MODELS = PHONE / "Models.swift"
ACTIONS = PHONE / "Actions.swift"
RECEIPTS = PHONE / "Receipts.swift"
OUTBOX = PHONE / "Outbox.swift"
PANEL_MODELS = ROOT / "panel" / "Sources" / "BobPanel" / "BoardModels.swift"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "RowSelectionTests.swift"
CONTRACT = ROOT / "docs" / "phone-contract.md"
TRANSPORT = ROOT / "docs" / "transport-contract.md"
CONTEXT_BOARD = ROOT / "docs" / "context-board.md"

VERB = "board_start_batch"

HARNESS = r'''
import Foundation

enum PhoneClient {
    static let autostartSettlingKey = "board_autostart"
    static func parallelSettlingKey(_ root: String) -> String { "parallel:\(root)" }
}
enum PrepareRoute: Equatable { case mac, phone }
enum OutboxStore {
    static let backoffStart: TimeInterval = 10
    static let backoffCap: TimeInterval = 300
}

struct Payload: Decodable {
    var snapshot = ""
    var column = "backlog"
    var dispatchEnabled = true
    var selected: [String] = []
    enum CodingKeys: String, CodingKey {
        case snapshot, column, dispatchEnabled, selected
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        snapshot = try c.decodeIfPresent(String.self, forKey: .snapshot) ?? ""
        column = try c.decodeIfPresent(String.self, forKey: .column) ?? "backlog"
        dispatchEnabled = try c.decodeIfPresent(Bool.self, forKey: .dispatchEnabled) ?? true
        selected = try c.decodeIfPresent([String].self, forKey: .selected) ?? []
    }
}

@main
enum Runner {
    @MainActor
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let p = try JSONDecoder().decode(Payload.self, from: data)
        let snap = try p.snapshot.isEmpty ? Snapshot()
            : JSONDecoder().decode(Snapshot.self, from: Data(p.snapshot.utf8))
        let cards = snap.board.cards
        let given = p.selected.compactMap { id in cards.first { $0.id == id } }
        for card in cards {
            let tick = PhoneRowSelection.tickable(p.column, card: card,
                                                  dispatchEnabled: p.dispatchEnabled)
            let admit = PhoneRowSelection.admits(p.column, card: card, given: given,
                                                 dispatchEnabled: p.dispatchEnabled)
            print("TICK \(card.id) \(tick)")
            print("ADMIT \(card.id) \(admit)")
            print("BATCHLINE \(card.id) \(card.batchLine)")
            print("HOLDS \(card.id) \(card.holdsBatchMark)")
            print("WAITING \(card.id) \(card.isBatchWaiting)")
        }
        print("OFFERS \(PhoneRowSelection.offersSelect(p.column, cards: cards, dispatchEnabled: p.dispatchEnabled))")
        print("ORDERED " + PhoneRowSelection.orderedIds(selected: p.selected, in: cards).joined(separator: ","))
        print("PRUNED " + PhoneRowSelection.prune(p.column, selected: p.selected, in: cards,
                                                  dispatchEnabled: p.dispatchEnabled).joined(separator: ","))
        print("VERB \(PhoneRowSelection.verb(p.column, count: p.selected.count))")
        print("ARMED \(PhoneRowSelection.armedVerb(p.column, count: p.selected.count))")
        print("MINIMUM \(PhoneRowSelection.minimum)")
        print("START_SCOPE \(PhoneRowSelection.startScope)")
        print("SUPPORTED \(snap.board.startBatchSupported)")
    }
}
'''

_PER_CARD = ("TICK", "ADMIT", "BATCHLINE", "HOLDS", "WAITING")


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _slice(text: str, start: str, end: str = "\n    }\n") -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


@pytest.fixture(scope="module")
def rule_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-batch-start")
    main = folder / "batch_main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-batch-start"
    sources = [str(PHONE / name) for name in SOURCES] + [str(RULE)]
    built = subprocess.run(
        [swiftc] + sources + [str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _snapshot(cards: list[dict], **board) -> str:
    return json.dumps({"board": {"available": True, "cards": cards, **board}})


def _card(**fields) -> dict:
    card = {"id": "c1", "column_name": "backlog", "root": "/r/a",
            "plan_path": "plans/x.md", "tool": "claude"}
    card.update(fields)
    return card


def _run(binary: pathlib.Path, payload: dict) -> dict:
    home = binary.parent / "home"
    home.mkdir(exist_ok=True)
    proc = subprocess.run([str(binary)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30,
                          check=False, env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out: dict = {key: {} for key in _PER_CARD}
    for line in proc.stdout.splitlines():
        key, _, rest = line.partition(" ")
        if key in _PER_CARD:
            card_id, _, value = rest.partition(" ")
            out[key][card_id] = (value if key == "BATCHLINE" else value == "true")
        else:
            out[key] = rest
    return out


# --- the rule, run -------------------------------------------------------------

#: Each Start term broken alone, as the card's JSON.
BROKEN = [
    ("column", {"column_name": "prep"}),
    ("no plan", {"plan_path": ""}),
    ("scout", {"kind": "scout"}),
    ("no assistant", {"tool": ""}),
    ("session", {"session_id": "s1"}),
    ("link live", {"link_state": "live"}),
    ("link dispatching", {"link_state": "dispatching"}),
    ("refining", {"refine_state": "live"}),
    ("queued", {"queue_state": "queued"}),
    ("in a batch", {"batch": {"rank": 2, "size": 3, "state": "waiting"}}),
]


def test_tickable_mirrors_the_ten_terms_of_start(rule_bin):
    cards = [_card(id="ok")] + [
        _card(id=f"b{i}", **fields) for i, (_, fields) in enumerate(BROKEN)]
    out = _run(rule_bin, {"snapshot": _snapshot(cards)})
    assert out["TICK"]["ok"] is True
    for i, (name, _) in enumerate(BROKEN):
        assert out["TICK"][f"b{i}"] is False, name
    off = _run(rule_bin, {"snapshot": _snapshot(cards), "dispatchEnabled": False})
    assert not any(off["TICK"].values())
    # The other rows never judge a Backlog card by the Start rule.
    for column in ("prep", "done"):
        other = _run(rule_bin, {"snapshot": _snapshot([_card(id="ok")]),
                                "column": column})
        assert other["TICK"]["ok"] is False, column


def test_admits_refuses_a_second_root_on_the_backlog_row(rule_bin):
    cards = [_card(id="a", root="/r/a"), _card(id="b", root="/r/a"),
             _card(id="c", root="/r/b")]
    free = _run(rule_bin, {"snapshot": _snapshot(cards)})
    assert free["ADMIT"] == {"a": True, "b": True, "c": True}
    first = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["a"]})
    assert first["ADMIT"] == {"a": True, "b": True, "c": False}


def test_the_start_verb_and_armed_verb_carry_the_count(rule_bin):
    out = _run(rule_bin, {"selected": ["a", "b", "c"]})
    assert out["VERB"] == "START 3 TOGETHER"
    assert out["ARMED"] == "Really start 3?"
    assert out["MINIMUM"] == "2"
    assert out["START_SCOPE"] == "batch:start"
    prep = _run(rule_bin, {"selected": ["a", "b", "c"], "column": "prep"})
    assert prep["VERB"] == "REFINE 3 TOGETHER"
    assert prep["ARMED"] == "Really refine 3?"


def test_offers_select_on_backlog_needs_two_startable_cards(rule_bin):
    one = _run(rule_bin, {"snapshot": _snapshot([
        _card(id="a"), _card(id="q", queue_state="queued")])})
    assert one["OFFERS"] == "false"
    two = _run(rule_bin, {"snapshot": _snapshot([_card(id="a"), _card(id="b")])})
    assert two["OFFERS"] == "true"
    off = _run(rule_bin, {"snapshot": _snapshot([_card(id="a"), _card(id="b")]),
                          "dispatchEnabled": False})
    assert off["OFFERS"] == "false"


def test_prune_is_keyed_on_the_row(rule_bin):
    cards = [_card(id="a"), _card(id="q", queue_state="queued")]
    backlog = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["a", "q"]})
    assert backlog["PRUNED"] == "a"
    prep = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["a", "q"],
                           "column": "prep"})
    assert prep["PRUNED"] == ""


def test_the_batch_mark_decodes_tolerantly_and_spells_the_panels_line(rule_bin):
    cards = [
        {"id": "none"},
        {"id": "bad", "batch": 3},
        {"id": "nosize", "batch": {"rank": 2, "state": "waiting"}},
        {"id": "norank", "batch": {"size": 3, "state": "waiting"}},
        {"id": "work", "batch": {"rank": 1, "size": 3, "state": "working"}},
        {"id": "wait", "batch": {"rank": 2, "size": 3, "state": "waiting"}},
        {"id": "left", "batch": {"rank": 2, "size": 3, "state": "left"}},
    ]
    out = _run(rule_bin, {"snapshot": _snapshot(cards)})
    line = out["BATCHLINE"]
    assert line["none"] == "" and line["bad"] == "" and line["norank"] == ""
    assert line["nosize"] == "BATCH 2/2 · waiting"
    assert line["work"] == "BATCH 1/3 · working"
    assert line["wait"] == "BATCH 2/3 · waiting"
    assert line["left"] == "BATCH 2/3 · left the line"
    assert out["HOLDS"] == {"none": False, "bad": False, "nosize": True,
                            "norank": False, "work": False, "wait": True,
                            "left": True}
    assert out["WAITING"]["wait"] is True and out["WAITING"]["left"] is False


# --- the marker, both sides ---------------------------------------------------


class _PipelineStub(BoardVerbsMixin):
    def _observers_implementing(self, name):
        return False

    def _board_projects(self):
        return []


def test_the_marker_is_published_on_every_board_shape_and_decoded(rule_bin):
    assert _PipelineStub()._pipeline_writable()["start_batch_supported"] is True
    models = _read(MODELS)
    assert 'case startBatchSupported = "start_batch_supported"' in models
    assert "startBatchSupported = c.value(.startBatchSupported, false)" in models
    assert "var startBatchSupported = false" in models
    # The panel decodes no marker: the Mac honours its own verb.
    assert "startBatchSupported" not in _read(PANEL_MODELS)
    # Absent is false; present is read.
    assert _run(rule_bin, {"snapshot": _snapshot([])})["SUPPORTED"] == "false"
    on = _run(rule_bin, {"snapshot": _snapshot([], start_batch_supported=True)})
    assert on["SUPPORTED"] == "true"


def test_the_verb_is_on_both_phone_tuples():
    assert ApiServer.BOARD_ACTIONS.count(VERB) == 1
    assert ApiServer.LAN_ACTIONS.count(VERB) == 1
    assert ApiServer.REMOTE_ACTIONS.count(VERB) == 1
    assert VERB in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert 'static let boardStartBatch = "board_start_batch"' in _read(ACTIONS)


# --- the door -----------------------------------------------------------------


class _StubDaemon:
    def __init__(self):
        self.ids = []

    async def start_cards(self, card_ids):
        self.ids.append(list(card_ids))
        return True, "Alpha started · 2 waiting"


def _server(daemon):
    srv = object.__new__(ApiServer)
    srv._daemon = daemon
    return srv


def test_a_sealed_press_reaches_start_cards_with_the_ids():
    """Success criterion — "press START 3 TOGETHER and then Really start 3?,
    and one terminal opens": one sealed press is one `start_cards` call with
    the three ids, and the Mac's report comes back verbatim."""
    stub = _StubDaemon()
    status, _, body = asyncio.run(
        _server(stub)._lan_run(VERB, {"card_ids": "a,b,c"}))
    assert status == 200
    assert stub.ids == [["a", "b", "c"]]
    assert json.loads(body)["detail"] == "Alpha started · 2 waiting"
    for payload in ({}, {"card_ids": " , "}):
        stub = _StubDaemon()
        status, _, body = asyncio.run(_server(stub)._lan_run(VERB, payload))
        assert status == 400
        assert json.loads(body)["error"] == "no card_ids"
        assert stub.ids == []


# --- the wiring ---------------------------------------------------------------


def test_the_press_is_synchronous_and_never_banked():
    board = _read(BOARD)
    send = _slice(board, "private func sendStartBatch(")
    assert "client.post(action: PhoneActions.boardStartBatch" in send
    assert "scope: PhoneRowSelection.startScope" in send
    assert '"card_ids": ids.joined(separator: ",")' in send
    for banned in ("enqueue(", "OutboxStore", "refreshNow("):
        assert banned not in send, banned
    assert "enqueue(action: PhoneActions.boardStartBatch" not in board
    for path in (OUTBOX, RECEIPTS):
        text = _read(path)
        assert "boardStartBatch" not in text and VERB not in text, path.name


def test_the_batch_is_armed_then_confirmed_on_the_joined_ids():
    board = _read(BOARD)
    press = _slice(board, "private func pressStartBatch()")
    assert "arm.confirm(.startBatch, id: key)" in press
    assert "arm.arm(.startBatch, id: key)" in press
    assert 'let key = ids.joined(separator: ",")' in press
    arm = _read(ARM)
    assert "case startBatch" in arm
    assert "@Published private(set) var startBatch: String?" in arm
    assert "case .startBatch: startBatch = id" in arm
    assert "case .startBatch: armed = startBatch" in arm
    assert arm.count("startBatch = nil") == 2


def test_select_is_drawn_only_where_the_mac_says_so_and_one_row_at_a_time():
    board = _read(BOARD)
    control = board.split("private var backlogBatchControl: some View", 1)[1].split(
        "private var startBatchButton", 1)[0]
    for term in ("board.startBatchSupported", "selectingRow == nil",
                 'PhoneRowSelection.offersSelect("backlog", cards: visibleCards(in: "backlog")',
                 'DecryptButton("SELECT")', 'DecryptButton("CANCEL")',
                 "{ leaveSelectMode() }"):
        assert term in control, term
    prep = board.split("private var prepBatchControl: some View", 1)[1].split(
        "private var batchButton", 1)[0]
    assert "selectingRow == nil" in prep.split('DecryptButton("SELECT")', 1)[0]
    link = board.split("private func cardLink(_ card: BoardCard)", 1)[1].split(
        "// MARK: - Select mode", 1)[0]
    assert 'else if selectingRow == "backlog" && card.column == "backlog"' in link
    # A Refine receipt leaving the queue never ends a Backlog selection.
    mark = board.split(".onChange(of: client.queueMark(for: PhoneRowSelection.scope))",
                       1)[1].split("leaveSelectMode()", 1)[0]
    assert 'guard selectingRow == "prep"' in mark


def test_the_tile_and_the_card_screen_draw_the_batch_line():
    board = _read(BOARD)
    assert board.count("card.batchLine") >= 2
    spoken = _slice(board, "private var spoken: String {")
    assert "card.batchLine" in spoken
    card = _read(CARD)
    can_start = _slice(card, "private var canStart: Bool {")
    assert "!card.holdsBatchMark" in can_start
    status = card.split("private var statusSection: some View {", 1)[1].split(
        "liveState", 1)[0]
    assert "card.batchLine" in status


def test_both_clients_spell_the_batch_line_the_same_way():
    for literal in ('"BATCH \\(batch.rank)/\\(batch.size) \\u{00B7} \\(state)"',
                    'batch.state == "left" ? "left the line" : batch.state'):
        assert _read(MODELS).count(literal) == 1, literal
        assert _read(PANEL_MODELS).count(literal) == 1, literal
    models = _read(MODELS)
    assert models.count("struct BatchMark") == 1
    assert models.count("batch = c.maybe(.batch)") == 1
    assert models.count("var holdsBatchMark") == 1


def test_the_rule_file_stays_foundation_only():
    text = _read(RULE)
    imports = [line for line in text.splitlines() if line.startswith("import ")]
    assert imports == ["import Foundation"]
    assert 'static let startScope = "batch:start"' in text


def test_xctest_names_the_same_cases():
    xctest = _read(XCTEST)
    for case in (
        "testTickableMirrorsTheTermsOfStart",
        "testTheStartVerbAndArmedVerbCarryTheCount",
        "testPruneIsKeyedOnTheRow",
        "testABatchMarkDecodesTolerantlyAndSpellsThePanelsLine",
    ):
        assert f"func {case}()" in xctest, case


def test_the_contract_names_the_section():
    contract = _read(CONTRACT)
    assert "## Several planned cards are started from the phone at once" in contract
    assert "Five presses keep the synchronous" in contract
    assert "start_batch_supported" in _read(TRANSPORT)
    assert "start_batch_supported" in _read(CONTEXT_BOARD)
