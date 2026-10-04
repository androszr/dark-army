"""Several Prep cards are refined from the phone at once.

The phone's Board tab puts its Prep row into select mode: a tick on every
card that could be refined now, and one armed-then-confirmed press that
sends `board_refine_batch` through the phone's press queue. The tick rule
(`PhoneRowSelection`, `ios/BobPhone/RowSelection.swift`) is the card
screen's own Refine rule and is run here under `swiftc` beside
`Receipts.swift`, whose `.cardsRefining` judge says when the press landed;
the XCTest twin is `ios/BobPhoneTests/RowSelectionTests.swift`. The wiring
is pinned by source greps, and the daemon half — the verb on both phone
tuples behind `refine_batch_supported` — by the tuples themselves.
Contract: `docs/phone-contract.md`, *Several Prep cards are refined from
the phone at once*; `docs/transport-contract.md`, *The LAN door is sealed*.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
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
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "RowSelectionTests.swift"
CONTRACT = ROOT / "docs" / "phone-contract.md"

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
    var column = "prep"
    var dispatchEnabled = true
    var selected: [String] = []
    var action = ""
    var fields: [String: String] = [:]
    var landed: [String] = []
    enum CodingKeys: String, CodingKey {
        case snapshot, column, dispatchEnabled, selected, action, fields, landed
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        snapshot = try c.decodeIfPresent(String.self, forKey: .snapshot) ?? ""
        column = try c.decodeIfPresent(String.self, forKey: .column) ?? "prep"
        dispatchEnabled = try c.decodeIfPresent(Bool.self, forKey: .dispatchEnabled) ?? true
        selected = try c.decodeIfPresent([String].self, forKey: .selected) ?? []
        action = try c.decodeIfPresent(String.self, forKey: .action) ?? ""
        fields = try c.decodeIfPresent([String: String].self, forKey: .fields) ?? [:]
        landed = try c.decodeIfPresent([String].self, forKey: .landed) ?? []
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
        }
        print("OFFERS \(PhoneRowSelection.offersSelect(p.column, cards: cards, dispatchEnabled: p.dispatchEnabled))")
        print("ORDERED " + PhoneRowSelection.orderedIds(selected: p.selected, in: cards).joined(separator: ","))
        print("PRUNED " + PhoneRowSelection.prune(p.column, selected: p.selected, in: cards,
                                                  dispatchEnabled: p.dispatchEnabled).joined(separator: ","))
        print("VERB \(PhoneRowSelection.verb(p.column, count: p.selected.count))")
        print("ARMED \(PhoneRowSelection.armedVerb(p.column, count: p.selected.count))")
        print("MINIMUM \(PhoneRowSelection.minimum)")
        print("SCOPE \(PhoneRowSelection.scope)")
        let effect = ReceiptLedger.effect(for: p.action, fields: p.fields,
                                          scope: PhoneRowSelection.scope)
        switch effect {
        case .cardsRefining(let ids): print("EFFECT cardsRefining " + ids.joined(separator: ","))
        case .none: print("EFFECT none")
        default: print("EFFECT other")
        }
        if !p.landed.isEmpty {
            print("LANDED \(ReceiptLedger.landed(.cardsRefining(cardIds: p.landed), in: snap))")
            print("EVIDENCE \(ReceiptLedger.evidenceBeforeSending(.cardsRefining(cardIds: p.landed), in: snap))")
        }
    }
}
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


@pytest.fixture(scope="module")
def rule_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-batch-refine")
    main = folder / "batch_main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-batch-refine"
    sources = [str(PHONE / name) for name in SOURCES] + [str(RULE)]
    built = subprocess.run(
        [swiftc] + sources + [str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _snapshot(cards: list[dict]) -> str:
    return json.dumps({"board": {"available": True, "cards": cards}})


def _card(**fields) -> dict:
    card = {"id": "c1", "column_name": "prep", "root": "/r/a"}
    card.update(fields)
    return card


def _run(binary: pathlib.Path, payload: dict) -> dict:
    home = binary.parent / "home"
    home.mkdir(exist_ok=True)
    proc = subprocess.run([str(binary)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30,
                          check=False, env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out: dict = {"TICK": {}, "ADMIT": {}}
    for line in proc.stdout.splitlines():
        key, _, rest = line.partition(" ")
        if key in ("TICK", "ADMIT"):
            card_id, _, value = rest.partition(" ")
            out[key][card_id] = value == "true"
        else:
            out[key] = rest
    return out


# --- the rule, run -------------------------------------------------------------

#: Each Refine term broken alone, as the card's JSON. The card screen's
#: Refine button reads the same function, so this is its table too.
BROKEN = [
    ("column", {"column_name": "backlog"}),
    ("plan", {"plan_path": "plans/x.md"}),
    ("scout", {"kind": "scout"}),
    ("refine dispatching", {"refine_state": "dispatching"}),
    ("refine live", {"refine_state": "live"}),
    ("session", {"session_id": "s1"}),
    ("start dispatching", {"link_state": "dispatching"}),
]


def test_tickable_mirrors_the_seven_terms_of_refine(rule_bin):
    cards = [_card(id="ok")] + [
        _card(id=f"b{i}", **fields) for i, (_, fields) in enumerate(BROKEN)]
    out = _run(rule_bin, {"snapshot": _snapshot(cards)})
    assert out["TICK"]["ok"] is True
    for i, (name, _) in enumerate(BROKEN):
        assert out["TICK"][f"b{i}"] is False, name
    # Dark Army's launcher off: nothing ticks.
    off = _run(rule_bin, {"snapshot": _snapshot(cards), "dispatchEnabled": False})
    assert not any(off["TICK"].values())
    # An ended refinement may be refined again.
    ended = _run(rule_bin, {"snapshot": _snapshot([_card(id="e", refine_state="ended")])})
    assert ended["TICK"]["e"] is True
    # Every other row answers false.
    for column in ("backlog", "in_progress", "done"):
        other = _run(rule_bin, {"snapshot": _snapshot([_card(id="ok")]), "column": column})
        assert other["TICK"]["ok"] is False, column


def test_admits_refuses_a_second_root_and_accepts_the_same_one(rule_bin):
    cards = [_card(id="a", root="/r/a"), _card(id="b", root="/r/a"),
             _card(id="c", root="/r/b"), _card(id="s", root="/r/a", kind="scout")]
    free = _run(rule_bin, {"snapshot": _snapshot(cards)})
    assert free["ADMIT"] == {"a": True, "b": True, "c": True, "s": False}
    first = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["a"]})
    assert first["ADMIT"] == {"a": True, "b": True, "c": False, "s": False}
    # The first *tick* names the project, not the first card on the board.
    other = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["c", "a"]})
    assert other["ADMIT"]["b"] is False


def test_the_verb_and_the_armed_verb_carry_the_count(rule_bin):
    out = _run(rule_bin, {"selected": ["a", "b", "c"]})
    assert out["VERB"] == "REFINE 3 TOGETHER"
    assert out["ARMED"] == "Really refine 3?"
    assert out["MINIMUM"] == "2"
    assert out["SCOPE"] == "batch:refine"


def test_offers_select_needs_two_tickable_cards(rule_bin):
    one = _run(rule_bin, {"snapshot": _snapshot([
        _card(id="a"), _card(id="p", plan_path="plans/x.md")])})
    assert one["OFFERS"] == "false"
    two = _run(rule_bin, {"snapshot": _snapshot([_card(id="a"), _card(id="b")])})
    assert two["OFFERS"] == "true"
    off = _run(rule_bin, {"snapshot": _snapshot([_card(id="a"), _card(id="b")]),
                          "dispatchEnabled": False})
    assert off["OFFERS"] == "false"


def test_ordered_ids_follow_board_order_not_tick_order(rule_bin):
    cards = [_card(id=i) for i in ("a", "b", "c")]
    out = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["c", "zz", "a"]})
    assert out["ORDERED"] == "a,c"


def test_prune_drops_a_card_that_stopped_being_tickable_or_left_the_board(rule_bin):
    cards = [_card(id="a"), _card(id="b", refine_state="live")]
    out = _run(rule_bin, {"snapshot": _snapshot(cards), "selected": ["gone", "a", "b"]})
    assert out["PRUNED"] == "a"


def test_the_batch_effect_is_every_cards_refining(rule_bin):
    """Success criterion — "every ticked card shows planning…": the press is
    judged landed only when the board shows every ticked card's planner at
    work, so the mark stays SENT until then."""
    effect = _run(rule_bin, {"action": "board_refine_batch",
                             "fields": {"card_ids": "a, b,"}})
    assert effect["EFFECT"] == "cardsRefining a,b"
    blank = _run(rule_bin, {"action": "board_refine_batch",
                            "fields": {"card_ids": " , "}})
    assert blank["EFFECT"] == "none"
    one = _snapshot([_card(id="a", refine_state="dispatching"), _card(id="b")])
    out = _run(rule_bin, {"snapshot": one, "landed": ["a", "b"]})
    assert out["LANDED"] == "false" and out["EVIDENCE"] == "false"
    gone = _snapshot([_card(id="a", refine_state="dispatching")])
    out = _run(rule_bin, {"snapshot": gone, "landed": ["a", "b"]})
    assert out["LANDED"] == "false"
    both = _snapshot([_card(id="a", refine_state="dispatching"),
                      _card(id="b", refine_state="live", refine_session_id="s9")])
    out = _run(rule_bin, {"snapshot": both, "landed": ["a", "b"]})
    assert out["LANDED"] == "true"


# --- the marker, both sides ---------------------------------------------------


class _PipelineStub(BoardVerbsMixin):
    def _observers_implementing(self, name):
        return False

    def _board_projects(self):
        return []


def test_the_marker_is_published_and_decoded():
    assert _PipelineStub()._pipeline_writable()["refine_batch_supported"] is True
    models = _read(MODELS)
    assert 'case refineBatchSupported = "refine_batch_supported"' in models
    assert "refineBatchSupported = c.value(.refineBatchSupported, false)" in models
    assert "var refineBatchSupported = false" in models


def test_the_verb_is_on_both_phone_tuples():
    assert ApiServer.LAN_ACTIONS.count("board_refine_batch") == 1
    assert ApiServer.REMOTE_ACTIONS.count("board_refine_batch") == 1
    assert "board_refine_batch" in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert 'static let boardRefineBatch = "board_refine_batch"' in _read(ACTIONS)


# --- the wiring ---------------------------------------------------------------


def test_the_card_screen_and_the_tick_share_one_rule():
    card = _read(CARD)
    start = card.index("private var canRefine: Bool {")
    body = card[start:card.index("\n    }", start)]
    assert 'PhoneRowSelection.tickable("prep", card: card,' in body
    for term in ('card.column == "prep"', "card.planPath.isEmpty",
                 'card.refineState != "dispatching"', 'card.refineState != "live"',
                 'card.linkState != "dispatching"'):
        assert term not in body, term
    rule = _read(RULE)
    for term in ("dispatchEnabled", 'card.column == "prep"', "card.planPath.isEmpty",
                 "!card.isScout", 'card.refineState != "dispatching"',
                 'card.refineState != "live"', "card.sessionId.isEmpty",
                 'card.linkState != "dispatching"'):
        assert term in rule, term


def test_the_board_sends_the_batch_through_the_queue_under_one_scope():
    board = _read(BOARD)
    assert board.count("client.enqueue(action: PhoneActions.boardRefineBatch") == 1
    assert "client.post(action: PhoneActions.boardRefineBatch" not in board
    send = board.split("private func sendBatch(", 1)[1].split("\n    }\n", 1)[0]
    assert "scope: PhoneRowSelection.scope" in send
    assert '"card_ids": ids.joined(separator: ",")' in send
    # The mark and the note are keyed on the same scope, the note drawn and
    # read (`readQueueNote`), and the ticks kept on a refusal.
    assert "client.queueMark(for: PhoneRowSelection.scope)" in board
    assert "client.queueNote(for: PhoneRowSelection.scope)" in board
    assert "client.readQueueNote(for: PhoneRowSelection.scope)" in board


def test_the_batch_is_armed_then_confirmed_on_the_joined_ids():
    board = _read(BOARD)
    press = board.split("private func pressBatch()", 1)[1].split("\n    }\n", 1)[0]
    assert "PhoneRowSelection.orderedIds(selected: selection" in board
    assert "arm.confirm(.refineBatch, id: key)" in press
    assert "arm.arm(.refineBatch, id: key)" in press
    assert 'let key = ids.joined(separator: ",")' in press
    arm = _read(ARM)
    assert "case refineBatch" in arm
    assert "@Published private(set) var refineBatch: String?" in arm
    assert "case .refineBatch: refineBatch = id" in arm
    assert "case .refineBatch: armed = refineBatch" in arm
    assert arm.count("refineBatch = nil") == 2


def test_select_is_drawn_only_where_the_mac_says_so():
    board = _read(BOARD)
    control = board.split("private var prepBatchControl: some View", 1)[1].split(
        "private var batchButton", 1)[0]
    assert "board.refineBatchSupported" in control
    assert 'PhoneRowSelection.offersSelect("prep", cards: visibleCards(in: "prep")' in control
    assert 'DecryptButton("SELECT")' in control
    assert 'DecryptButton("CANCEL")' in control
    # The assistant row moved to the bar above the tab bar.
    assert "batchAssistantRow" not in control
    # Pruned against the whole row — a search or a project filter hides a
    # ticked card, never unticks it — and never while a press is out.
    prune = board.split("private func pruneSelection()", 1)[1].split("\n    }\n", 1)[0]
    assert "batchMark == nil" in prune
    assert 'in: board.cards(in: "prep")' in prune
    assert "visibleCards" not in prune


def test_cancel_stays_enabled_while_the_press_is_out_and_select_hides():
    """A press queued offline must not lock the Prep row in select mode:
    CANCEL leaves select mode (the queued press stays on the QUEUE list),
    and SELECT is absent while the press is out."""
    board = _read(BOARD)
    control = board.split("private var prepBatchControl: some View", 1)[1].split(
        "private var batchButton", 1)[0]
    select = control.split('DecryptButton("SELECT")', 1)[0]
    assert "batchMark == nil" in select
    cancel = control.split('DecryptButton("CANCEL")', 1)[1].split(
        ".accessibilityLabel(", 1)[0]
    assert ".disabled(" not in cancel
    assert '{ leaveSelectMode() }' in control
    leave = board.split("private func leaveSelectMode()", 1)[1].split("\n    }\n", 1)[0]
    for step in ("selectingRow = nil", "selection = []", 'batchRefusal = ""'):
        assert step in leave, step
    # Leaving never touches the queue.
    assert "receipts" not in leave and "discard" not in leave.lower()


def test_a_barred_box_differs_in_shape_not_colour_alone():
    board = _read(BOARD)
    glyph = board.split("static func tickGlyph(", 1)[1].split("\n    }\n", 1)[0]
    assert 'case .ticked: return "[x]"' in glyph
    assert 'case .open: return "[ ]"' in glyph
    assert 'case .barred: return "[-]"' in glyph
    assert "Text(Self.tickGlyph(tick))" in board


def test_a_tick_is_drawn_and_spoken_only_in_select_mode():
    board = _read(BOARD)
    assert "var tick: PhoneRowSelection.Tick? = nil" in board
    link = board.split("private func cardLink(_ card: BoardCard)", 1)[1].split(
        "// MARK: - Select mode", 1)[0]
    assert 'if selectingRow == "prep" && card.column == "prep"' in link
    # Outside select mode the tile opens the card, as before.
    assert "sheets.show(.card(card))" in link
    for word in ('"ticked"', '"not ticked"', '"cannot be ticked"'):
        assert word in board, word
    assert ".lineLimit(" not in _code(board)


def test_the_rule_file_is_foundation_only():
    text = _read(RULE)
    imports = [line for line in text.splitlines() if line.startswith("import ")]
    assert imports == ["import Foundation"]
    for forbidden in ("SwiftUI", "PhoneClient", "URLSession", "UIKit"):
        assert forbidden not in text, forbidden


def test_both_swift_files_are_registered_in_the_project():
    pbx = _read(PBXPROJ)
    lines = pbx.splitlines()
    assert sum("RowSelection.swift" in line for line in lines) == 4
    assert sum("RowSelectionTests.swift" in line for line in lines) == 2
    assert pbx.count("BA7C4E1F0000000000000001") == 3
    assert pbx.count("BA7C4E1F0000000000000002") == 2
    assert pbx.count("BA7C4E1F0000000000000003") == 3
    assert pbx.count("BA7C4E1F0000000000000004") == 2
    assert XCTEST.exists()


def test_xctest_names_the_same_cases():
    xctest = _read(XCTEST)
    for case in (
        "testTickableMirrorsTheTermsOfRefine",
        "testAdmitsRefusesASecondRootAndAcceptsTheSameOne",
        "testTheVerbAndTheArmedVerbCarryTheCount",
        "testOffersSelectNeedsTwoTickableCards",
        "testOrderedIdsFollowBoardOrderNotTickOrder",
        "testPruneDropsACardThatStoppedBeingTickableOrLeftTheBoard",
        "testTheBatchEffectIsEveryCardsRefining",
    ):
        assert f"func {case}()" in xctest, case
    receipts = _read(RECEIPTS)
    assert "case cardsRefining(cardIds: [String])" in receipts
    assert re.search(r"cardIds\.allSatisfy \{ landed\(\.cardRefining\(cardId: \$0\)",
                     receipts)


def test_the_contract_names_the_section():
    assert "## Several Prep cards are refined from the phone at once" in _read(CONTRACT)


def test_ticks_outlive_leaving_the_tab_a_filter_and_a_fold():
    """Leaving the Board tab, a search, a project change or a fold keeps
    select mode and its ticks; the selecting row draws its ticks first."""
    board = _read(BOARD)
    gone = board.split(".onDisappear {", 1)[1].split("}", 1)[0]
    assert "leaveSelectMode()" not in gone
    assert "arm.disarm()" in gone
    assert ".onChange(of: query) { _, _ in disarmBatch() }" in board
    assert ".onChange(of: project) { _, _ in disarmBatch() }" in board
    # The ticks survive a fold: the bar is a bottom inset, always on screen,
    # so the folded heading no longer carries a copy of the controls.
    assert ".safeAreaInset(edge: .bottom, spacing: 0) { selectionBar }" in board
    assert 'if item.id == "prep" { prepBatchControl }' not in board
    assert "cardsStack(tickedFirst(visible, in: id))" in board
    first = board.split("private func tickedFirst(", 1)[1].split("\n    }\n", 1)[0]
    assert "guard selectingRow == id" in first
