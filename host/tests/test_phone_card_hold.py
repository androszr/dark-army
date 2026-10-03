"""A Prep or Backlog card is held on the phone's Board tab to select it.

`plans/2026-10-03-phone-long-press-select-action-bar.md`. A half-second hold
on a Prep or Backlog card enters that row's select mode with the card
ticked, and a bar above the tab bar carries the count, the row's one verb,
CANCEL and the assistant row. The rule (`ios/BobPhone/CardHold.swift`) is
Foundation-only and runs here under `swiftc`, beside `RowSelection.swift`;
the wiring in `BoardView.swift` is pinned by source greps; the XCTest twin is
`ios/BobPhoneTests/CardHoldTests.swift`. How a hold, the release after it and
a scroll share one touch is the one thing only a real phone can confirm.
Contract: `docs/phone-contract.md`, *A held card opens the batch bar*.
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
from tests.test_phone_action_queue import SOURCES

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULE = PHONE / "CardHold.swift"
ROW_RULE = PHONE / "RowSelection.swift"
BOARD = PHONE / "BoardView.swift"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "CardHoldTests.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
CONTRACT = ROOT / "docs" / "phone-contract.md"
PANEL_DOC = ROOT / "docs" / "context-panel.md"

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

struct Case: Decodable {
    var held = ""
    var drawn: [String] = []
    var column = "backlog"
    var supported = true
    var pressOut = false
    var selecting: String? = nil
    var dispatchEnabled = true
    enum CodingKeys: String, CodingKey {
        case held, drawn, column, supported, pressOut, selecting, dispatchEnabled
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        held = try c.decodeIfPresent(String.self, forKey: .held) ?? ""
        drawn = try c.decodeIfPresent([String].self, forKey: .drawn) ?? []
        column = try c.decodeIfPresent(String.self, forKey: .column) ?? "backlog"
        supported = try c.decodeIfPresent(Bool.self, forKey: .supported) ?? true
        pressOut = try c.decodeIfPresent(Bool.self, forKey: .pressOut) ?? false
        selecting = try c.decodeIfPresent(String.self, forKey: .selecting)
        dispatchEnabled = try c.decodeIfPresent(Bool.self, forKey: .dispatchEnabled) ?? true
    }
}

struct Payload: Decodable {
    var snapshot = ""
    var cases: [Case] = []
    enum CodingKeys: String, CodingKey { case snapshot, cases }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        snapshot = try c.decodeIfPresent(String.self, forKey: .snapshot) ?? ""
        cases = try c.decodeIfPresent([Case].self, forKey: .cases) ?? []
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
        for (index, c) in p.cases.enumerated() {
            guard let held = cards.first(where: { $0.id == c.held }) else {
                print("ENTERS \(index) missing")
                continue
            }
            let drawn = c.drawn.compactMap { id in cards.first { $0.id == id } }
            let enters = PhoneCardHold.entersSelect(
                column: c.column, card: held, cards: drawn, supported: c.supported,
                pressOut: c.pressOut, selecting: c.selecting,
                dispatchEnabled: c.dispatchEnabled)
            let rule = c.supported && !c.pressOut && c.selecting == nil
                && PhoneRowSelection.tickable(c.column, card: held,
                                              dispatchEnabled: c.dispatchEnabled)
                && PhoneRowSelection.offersSelect(c.column, cards: drawn,
                                                  dispatchEnabled: c.dispatchEnabled)
                && PhoneCardHold.rows.contains(c.column)
            print("ENTERS \(index) \(enters)")
            print("RULE \(index) \(rule)")
        }
        let t0 = Date(timeIntervalSince1970: 1000)
        let soon = t0.addingTimeInterval(0.3)
        let late = t0.addingTimeInterval(PhoneCardHold.releaseWindow + 0.1)
        print("TAP nil a \(PhoneCardHold.consumesTap(held: nil, tapped: "a", heldAt: t0, now: soon))")
        print("TAP a a nodate \(PhoneCardHold.consumesTap(held: "a", tapped: "a", heldAt: nil, now: soon))")
        print("TAP a a \(PhoneCardHold.consumesTap(held: "a", tapped: "a", heldAt: t0, now: soon))")
        print("TAP a b \(PhoneCardHold.consumesTap(held: "a", tapped: "b", heldAt: t0, now: soon))")
        print("TAP a a late \(PhoneCardHold.consumesTap(held: "a", tapped: "a", heldAt: t0, now: late))")
        print("COUNT 1 \(PhoneCardHold.countLine(count: 1))")
        print("COUNT 3 \(PhoneCardHold.countLine(count: 3))")
        print("BAR prep \(PhoneCardHold.barLabel(column: "prep", count: 2))")
        print("BAR backlog \(PhoneCardHold.barLabel(column: "backlog", count: 2))")
        print("BAR nil [\(PhoneCardHold.barLabel(column: nil, count: 0))]")
        print("DURATION \(PhoneCardHold.minimumDuration)")
        print("DISTANCE \(PhoneCardHold.maximumDistance)")
        print("ROWS " + PhoneCardHold.rows.joined(separator: ","))
    }
}
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _slice(text: str, start: str, end: str = "\n    }\n") -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


@pytest.fixture(scope="module")
def rule_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-card-hold")
    main = folder / "hold_main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-card-hold"
    sources = [str(PHONE / name) for name in SOURCES] + [str(ROW_RULE), str(RULE)]
    built = subprocess.run(
        [swiftc] + sources + [str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _snapshot(cards: list[dict]) -> str:
    return json.dumps({"board": {"available": True, "cards": cards}})


def _backlog(**fields) -> dict:
    card = {"id": "a", "column_name": "backlog", "root": "/r/a",
            "plan_path": "plans/x.md", "tool": "claude"}
    card.update(fields)
    return card


def _prep(**fields) -> dict:
    card = {"id": "a", "column_name": "prep", "root": "/r/a"}
    card.update(fields)
    return card


def _lines(binary: pathlib.Path, payload: dict) -> list[str]:
    home = binary.parent / "home"
    home.mkdir(exist_ok=True)
    proc = subprocess.run([str(binary)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30,
                          check=False, env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.splitlines()


def _enters(binary, cards, cases) -> tuple[list[bool], list[bool]]:
    lines = _lines(binary, {"snapshot": _snapshot(cards), "cases": cases})
    enters = {int(l.split()[1]): l.split()[2] == "true"
              for l in lines if l.startswith("ENTERS ")}
    rule = {int(l.split()[1]): l.split()[2] == "true"
            for l in lines if l.startswith("RULE ")}
    return ([enters[i] for i in range(len(cases))],
            [rule[i] for i in range(len(cases))])


# --- the rule, run -------------------------------------------------------------

def test_enters_select_needs_every_term(rule_bin):
    backlog = [_backlog(id="a"), _backlog(id="b"),
               _backlog(id="s", session_id="s1")]
    base = {"held": "a", "drawn": ["a", "b"], "column": "backlog"}
    cases = [
        base,
        {**base, "column": "in_progress"},
        {**base, "column": "done"},
        {**base, "supported": False},
        {**base, "pressOut": True},
        {**base, "selecting": "prep"},
        {**base, "selecting": "backlog"},
        {**base, "held": "s"},
        {**base, "drawn": ["a"]},
        {**base, "dispatchEnabled": False},
    ]
    got, _ = _enters(rule_bin, backlog, cases)
    assert got == [True] + [False] * 9


def test_enters_select_needs_every_term_on_prep(rule_bin):
    prep = [_prep(id="a"), _prep(id="b"), _prep(id="s", kind="scout"),
            _prep(id="p", plan_path="plans/x.md")]
    base = {"held": "a", "drawn": ["a", "b"], "column": "prep"}
    cases = [
        base,
        {**base, "supported": False},
        {**base, "pressOut": True},
        {**base, "selecting": "backlog"},
        {**base, "held": "s"},
        {**base, "held": "p"},
        {**base, "drawn": ["a"]},
        {**base, "dispatchEnabled": False},
    ]
    got, _ = _enters(rule_bin, prep, cases)
    assert got == [True] + [False] * 7


def test_the_hold_rule_is_the_select_words_rule(rule_bin):
    cards = [_backlog(id="a"), _backlog(id="b"), _backlog(id="q", queue_state="queued"),
             _prep(id="p1"), _prep(id="p2")]
    cases = []
    for held, drawn, column in (("a", ["a", "b"], "backlog"),
                                ("a", ["a", "q"], "backlog"),
                                ("q", ["a", "b", "q"], "backlog"),
                                ("p1", ["p1", "p2"], "prep"),
                                ("p1", ["p1"], "prep"),
                                ("a", ["a", "b"], "in_progress")):
        for supported in (True, False):
            for press_out in (True, False):
                for selecting in (None, "prep", "backlog"):
                    for enabled in (True, False):
                        case = {"held": held, "drawn": drawn, "column": column,
                                "supported": supported, "pressOut": press_out,
                                "dispatchEnabled": enabled}
                        if selecting:
                            case["selecting"] = selecting
                        cases.append(case)
    got, rule = _enters(rule_bin, cards, cases)
    assert got == rule
    assert any(got) and not all(got)


def test_consumes_tap_once_on_the_held_id(rule_bin):
    lines = _lines(rule_bin, {})
    assert "TAP nil a false" in lines
    assert "TAP a a true" in lines
    assert "TAP a b false" in lines
    assert "TAP a a nodate false" in lines
    assert "TAP a a late false" in lines


def test_the_words(rule_bin):
    lines = _lines(rule_bin, {})
    assert "COUNT 1 1 selected" in lines
    assert "COUNT 3 3 selected" in lines
    assert "BAR prep Batch refine, 2 selected" in lines
    assert "BAR backlog Batch start, 2 selected" in lines
    assert "BAR nil []" in lines
    assert "DURATION 0.5" in lines
    assert "DISTANCE 10.0" in lines
    assert "ROWS prep,backlog" in lines


# --- source pins ---------------------------------------------------------------

def test_the_rule_is_foundation_only_and_names_every_static():
    text = _read(RULE)
    imports = [line for line in text.splitlines() if line.startswith("import ")]
    assert imports == ["import Foundation"]
    assert "import SwiftUI" not in text
    xctest = _read(XCTEST)
    statics = re.findall(r"^    static (?:let|func) (\w+)", text, flags=re.M)
    assert statics
    for name in statics:
        assert f"PhoneCardHold.{name}" in xctest, name


def test_the_hold_is_wired_beside_the_tap():
    board = _read(BOARD)
    gesture = ("simultaneousGesture(LongPressGesture("
               "minimumDuration: PhoneCardHold.minimumDuration, "
               "maximumDistance: PhoneCardHold.maximumDistance)")
    assert board.count("." + gesture) == 1
    assert board.count(".highPriorityGesture(") == 0
    # The one `.gesture(` is the swipe's own UIKit pan (`CardSwipePan`),
    # which landed before the hold; the hold adds none.
    assert board.count(".gesture(") == 1
    assert "tile.gesture(CardSwipePan(" in board
    assert board.count("hold(card") >= 2
    assert "hold(card, fromTouch: true)" in board
    assert 'DecryptButton("Select") { hold(card) }' in board
    assert board.count(".sensoryFeedback(.selection, trigger: selection)") == 1
    hold = _slice(board, "private func hold(_ card: BoardCard, fromTouch: Bool = false)")
    for term in ("PhoneCardHold.entersSelect(", "selection = [card.id]",
                 "selectingRow = card.column", "heldCard = card.id",
                 "heldAt = Date()", "client.cardLeaving(card.id)"):
        assert term in hold, term
    # Only the finger path records a release to swallow.
    assert "if fromTouch {" in hold
    assert hold.index("if fromTouch {") < hold.index("heldCard = card.id")
    tickable = _slice(board, "private func tickableCard(_ card: BoardCard)")
    assert "PhoneCardHold.consumesTap(held: heldCard, tapped: card.id," in tickable
    assert "heldAt: heldAt, now: Date()" in tickable
    assert "heldCard = nil" in tickable
    assert board.count('DecryptButton("Select")') == 1
    assert board.count(".accessibilityActions {") == 1
    leave = _slice(board, "private func leaveSelectMode()")
    assert "heldCard = nil" in leave


def test_the_bar_is_a_bottom_inset_and_holds_the_verb():
    board = _read(BOARD)
    assert board.count(".safeAreaInset(edge: .bottom, spacing: 0) { selectionBar }") == 1
    bar = board.split("private var selectionBar: some View", 1)[1].split(
        "\n    /// In select mode, once a card is ticked", 1)[0].split(
        "private var batchAssistantRow", 1)[0]
    for term in ("PhoneCardHold.countLine(count: selection.count)", "batchButton",
                 "startBatchButton", 'DecryptButton("CANCEL")', "batchAssistantRow",
                 "batchRefusal", ".accessibilityElement(children: .contain)",
                 "PhoneCardHold.barLabel("):
        assert term in bar, term
    # The row's heading draws the assistant row nowhere: it is in the bar once.
    assert board.count("batchAssistantRow") == 2
    assert "} else if selectingRow == item.id {" not in board
    assert board.count("client.post(") == 2
    assert board.count(".refreshable {") == 2
    assert "withAnimation(" not in board
    assert board.count('DecryptButton("CANCEL")') == 3
    assert board.count('DecryptButton("SELECT")') == 2


def test_the_headings_keep_cancel_alone_in_select_mode():
    board = _read(BOARD)
    prep = board.split("private var prepBatchControl: some View", 1)[1].split(
        "private var batchButton", 1)[0]
    backlog = board.split("private var backlogBatchControl: some View", 1)[1].split(
        "private var startBatchButton", 1)[0]
    for control in (prep, backlog):
        assert "batchAssistantRow" not in control
        assert "batchRefusal = \"\"" in control  # the SELECT word's own step
        assert "Text(batchRefusal)" not in control
        assert "{ leaveSelectMode() }" in control
    assert "batchButton" not in prep.split('DecryptButton("CANCEL")', 1)[1]
    assert "startBatchButton" not in backlog.split('DecryptButton("CANCEL")', 1)[1]


def test_the_three_writes_are_already_on_both_phone_tuples():
    for verb in ("board_update", "board_refine_batch", "board_start_batch"):
        assert ApiServer.LAN_ACTIONS.count(verb) == 1, verb
        assert ApiServer.REMOTE_ACTIONS.count(verb) == 1, verb


def test_the_project_lists_both_new_files():
    pbx = _read(PBXPROJ)
    lines = pbx.splitlines()
    assert sum("CardHold.swift" in line for line in lines) == 4
    assert sum("CardHoldTests.swift" in line for line in lines) == 2
    assert pbx.count("BA7C4E1F0000000000000201") == 3
    assert pbx.count("BA7C4E1F0000000000000202") == 2
    assert pbx.count("BA7C4E1F0000000000000203") == 3
    assert pbx.count("BA7C4E1F0000000000000204") == 2
    assert XCTEST.exists()


def test_xctest_names_the_same_cases():
    xctest = _read(XCTEST)
    for case in (
        "testEntersSelectNeedsEveryTermOnBacklog",
        "testEntersSelectNeedsEveryTermOnPrep",
        "testTheHoldRuleIsTheSelectWordsRule",
        "testConsumesTapOnTheHeldIdOnly",
        "testTheWordsAndTheConstants",
    ):
        assert f"func {case}()" in xctest, case


def test_the_contract_says_so():
    contract = _read(CONTRACT)
    heading = "## A held card opens the batch bar"
    assert contract.count("\n" + heading + "\n") == 1
    section = contract.split(heading, 1)[1].split("\n## ", 1)[0]
    for word in ("entersSelect", "consumesTap", "safeAreaInset", "Select",
                 "test_phone_card_hold.py"):
        assert word in section, word
    assert "A held phone card opens the batch bar" in _read(PANEL_DOC)
