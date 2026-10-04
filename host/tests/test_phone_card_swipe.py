"""A Prep or Backlog card is swiped on the phone's Board tab.

`plans/2026-10-03-phone-card-swipe-actions.md`. A sideways swipe on a card in
the Prep or Backlog row shows two buttons: the card's one next action (START,
or Refine in Prep) and Delete; each sends on its first press.
The rule (`ios/BobPhone/CardSwipe.swift`) is Foundation-only and runs here
under `swiftc`, beside the Mac's `CardActionWeight`; the wiring in
`BoardView.swift` and the card screen is pinned by source greps; the
XCTest twin is `ios/BobPhoneTests/CardSwipeTests.swift`. The gesture itself
is the one thing only a real phone can confirm.
Contract: `docs/phone-contract.md`, *A Prep or Backlog card is swiped on
the Board tab*.
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
RULE = PHONE / "CardSwipe.swift"
ROW_RULE = PHONE / "RowSelection.swift"
WEIGHT = ROOT / "panel" / "Sources" / "BobPanel" / "CardActionWeight.swift"
BOARD = PHONE / "BoardView.swift"
CARD = PHONE / "CardDetailView.swift"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "CardSwipeTests.swift"
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

struct Payload: Decodable {
    var snapshot = ""
    var dispatchEnabled = true
    var canRefine = true
    var canStart = true
    var columns: [String] = []
    var translations: [Double] = []
}

@main
enum Runner {
    @MainActor
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let p = try JSONDecoder().decode(Payload.self, from: data)
        let snap = try p.snapshot.isEmpty ? Snapshot()
            : JSONDecoder().decode(Snapshot.self, from: Data(p.snapshot.utf8))
        for card in snap.board.cards {
            let start = PhoneCardSwipe.canStart(card: card, dispatchEnabled: p.dispatchEnabled)
            let refine = PhoneRowSelection.tickable("prep", card: card,
                                                    dispatchEnabled: p.dispatchEnabled)
            let primary = PhoneCardSwipe.primary(column: card.column, canRefine: refine,
                                                 canStart: start)
            let weight = CardActionWeight.primary(column: card.column, kind: card.kind)
            print("START \(card.id) \(start)")
            print("PRIMARY \(card.id) \(primary.map { "\($0)" } ?? "nil")")
            print("WEIGHT \(card.id) \(weight.map { $0.rawValue } ?? "nil")")
        }
        for column in p.columns {
            let r = PhoneCardSwipe.primary(column: column, canRefine: p.canRefine,
                                           canStart: p.canStart)
            print("COLUMN \(column) \(r.map { "\($0)" } ?? "nil")")
        }
        print("LABEL start \(PhoneCardSwipe.startLabel)")
        print("LABEL refine \(PhoneCardSwipe.refineLabel)")
        print("LABEL delete \(PhoneCardSwipe.deleteLabel)")
        print("LABEL delete-spoken \(PhoneCardSwipe.deleteSpoken)")
        print("LABEL delete-icon \(PhoneCardSwipe.deleteIcon)")
        print("ROWS " + PhoneCardSwipe.rows.joined(separator: ","))
        print("WIDTHS \(PhoneCardSwipe.minimumDrag) \(PhoneCardSwipe.revealTravel) \(PhoneCardSwipe.actionsWidth)")
        for t in p.translations {
            print("OFFSET \(t) shut \(PhoneCardSwipe.offset(forTranslation: CGFloat(t), revealed: false))")
            print("OFFSET \(t) open \(PhoneCardSwipe.offset(forTranslation: CGFloat(t), revealed: true))")
            print("AFTER \(t) shut \(PhoneCardSwipe.revealAfter(translation: CGFloat(t), revealed: false))")
            print("AFTER \(t) open \(PhoneCardSwipe.revealAfter(translation: CGFloat(t), revealed: true))")
        }
        print("FLICK \(PhoneCardSwipe.flickSpeed)")
        for (t, v, open) in [(-20, -400, false), (-20, -100, false), (20, 400, true),
                             (20, 100, true), (20, -400, false), (-80, 0, false)] {
            print("FLICKAFTER \(t) \(v) \(open ? "open" : "shut") \(PhoneCardSwipe.revealAfter(translation: CGFloat(t), velocity: CGFloat(v), revealed: open))")
        }
        print("DOMINANT 30 10 \(PhoneCardSwipe.dominantHorizontal(dx: 30, dy: 10))")
        print("DOMINANT 10 30 \(PhoneCardSwipe.dominantHorizontal(dx: 10, dy: 30))")
        print("DOMINANT -30 10 \(PhoneCardSwipe.dominantHorizontal(dx: -30, dy: 10))")
    }
}
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


@pytest.fixture(scope="module")
def rule_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-card-swipe")
    main = folder / "swipe_main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-card-swipe"
    sources = [str(PHONE / name) for name in SOURCES] + [
        str(ROW_RULE), str(RULE), str(WEIGHT)]
    built = subprocess.run(
        [swiftc] + sources + [str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _card(**fields) -> dict:
    card = {"id": "c1", "column_name": "backlog", "root": "/r/a",
            "plan_path": "plans/x.md", "tool": "claude"}
    card.update(fields)
    return card


def _run(binary: pathlib.Path, payload: dict) -> list[str]:
    home = binary.parent / "home"
    home.mkdir(exist_ok=True)
    payload = {"dispatchEnabled": True, "canRefine": True, "canStart": True,
               "columns": [], "translations": [], **payload}
    cards = payload.pop("cards", [])
    payload["snapshot"] = json.dumps(
        {"board": {"available": True, "cards": cards}}) if cards else ""
    proc = subprocess.run([str(binary)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30,
                          check=False, env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.splitlines()


def _table(lines: list[str], key: str) -> dict[str, str]:
    out = {}
    for line in lines:
        head, _, rest = line.partition(" ")
        if head == key:
            name, _, value = rest.partition(" ")
            out[name] = value
    return out


# --- the rule, run -------------------------------------------------------------

BROKEN = [
    ("column", {"column_name": "in_progress"}),
    ("no assistant", {"tool": ""}),
    ("queued", {"queue_state": "queued"}),
    ("session", {"session_id": "s1"}),
    ("link dispatching", {"link_state": "dispatching"}),
    ("link live", {"link_state": "live"}),
    ("refine dispatching", {"refine_state": "dispatching"}),
    ("refine live", {"refine_state": "live"}),
    ("in a batch", {"batch": {"rank": 2, "size": 3, "state": "waiting"}}),
]


def test_can_start_mirrors_the_card_screens_seven_terms(rule_bin):
    cards = [_card(id="ok")] + [
        _card(id=f"b{i}", **fields) for i, (_, fields) in enumerate(BROKEN)]
    out = _table(_run(rule_bin, {"cards": cards}), "START")
    assert out["ok"] == "true"
    for i, (name, _) in enumerate(BROKEN):
        assert out[f"b{i}"] == "false", name
    off = _table(_run(rule_bin, {"cards": cards, "dispatchEnabled": False}), "START")
    assert set(off.values()) == {"false"}


def test_primary_agrees_with_card_action_weight_on_both_rows(rule_bin):
    cards = [
        _card(id="prep", column_name="prep", plan_path=""),
        _card(id="scout", column_name="prep", plan_path="", kind="scout"),
        _card(id="backlog"),
    ]
    lines = _run(rule_bin, {"cards": cards})
    primary = _table(lines, "PRIMARY")
    weight = _table(lines, "WEIGHT")
    for card_id in ("prep", "scout", "backlog"):
        assert primary[card_id] == weight[card_id], card_id
    assert primary == {"prep": "refine", "scout": "start", "backlog": "start"}
    # Both gates closed: nothing is offered, and nothing is promoted.
    shut = _table(_run(rule_bin, {"cards": cards, "dispatchEnabled": False}), "PRIMARY")
    assert set(shut.values()) == {"nil"}
    # The other columns never swipe, whatever the gates say.
    out = _table(_run(rule_bin, {
        "columns": ["in_progress", "done", "", "prep", "backlog"],
        "canRefine": True, "canStart": True}), "COLUMN")
    assert out["in_progress"] == out["done"] == out[""] == "nil"
    assert (out["prep"], out["backlog"]) == ("refine", "start")
    # A Prep card that cannot be refined but can be started offers Start.
    out = _table(_run(rule_bin, {"columns": ["prep", "backlog"],
                                 "canRefine": False, "canStart": True}), "COLUMN")
    assert (out["prep"], out["backlog"]) == ("start", "start")
    out = _table(_run(rule_bin, {"columns": ["prep", "backlog"],
                                 "canRefine": True, "canStart": False}), "COLUMN")
    assert (out["prep"], out["backlog"]) == ("refine", "nil")


def test_the_labels_are_the_card_screens_words(rule_bin):
    lines = _run(rule_bin, {})
    labels = {}
    for line in lines:
        if line.startswith("LABEL "):
            _, name, value = line.split(" ", 2)
            labels[name] = value
    assert labels == {
        "start": "START",
        "refine": "Refine",
        "delete": "Delete",
        "delete-spoken": "Delete card",
        "delete-icon": "trash",
    }
    card = _read(CARD)
    for word in ("Really start?", "Start unplanned?", "Really refine?"):
        assert f'"{word}"' in card, word


def test_the_gesture_helpers_clamp_and_threshold(rule_bin):
    lines = _run(rule_bin, {"translations": [-300, -57, -55, 0, 55, 57, 300]})
    assert "ROWS prep,backlog" in lines
    assert "WIDTHS 20.0 56.0 148.0" in lines
    offsets = {}
    after = {}
    for line in lines:
        parts = line.split(" ")
        if parts[0] == "OFFSET":
            offsets[(float(parts[1]), parts[2])] = float(parts[3])
        if parts[0] == "AFTER":
            after[(float(parts[1]), parts[2])] = parts[3] == "true"
    for (_, _), value in offsets.items():
        assert -148 <= value <= 0
    assert offsets[(-300.0, "shut")] == -148
    assert offsets[(-57.0, "shut")] == -57
    assert offsets[(0.0, "open")] == -148
    assert offsets[(300.0, "open")] == 0
    assert offsets[(300.0, "shut")] == 0
    assert after[(-57.0, "shut")] is True
    assert after[(-55.0, "shut")] is False
    assert after[(57.0, "open")] is False
    assert after[(0.0, "open")] is True
    assert after[(55.0, "open")] is True
    assert after[(0.0, "shut")] is False
    assert "DOMINANT 30 10 true" in lines
    assert "DOMINANT 10 30 false" in lines
    assert "DOMINANT -30 10 true" in lines


def test_a_quick_flick_opens_or_shuts_whatever_the_travel(rule_bin):
    """The system's swipe rows answer a flick; so does the card."""
    lines = _run(rule_bin, {})
    assert "FLICK 300.0" in lines
    assert "FLICKAFTER -20 -400 shut true" in lines      # a short fast flick opens
    assert "FLICKAFTER -20 -100 shut false" in lines     # a short slow drag does not
    assert "FLICKAFTER 20 400 open false" in lines       # a fast flick back shuts
    assert "FLICKAFTER 20 100 open true" in lines
    assert "FLICKAFTER 20 -400 shut false" in lines      # speed against the travel is ignored
    assert "FLICKAFTER -80 0 shut true" in lines         # travel alone still opens


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
        assert f"PhoneCardSwipe.{name}" in xctest, name


def test_the_card_screen_reads_the_moved_gate():
    card = _read(CARD)
    head = "    private var canStart: Bool {"
    body = card.split(head, 1)[1].split("\n    }\n", 1)[0]
    assert body.count(
        "PhoneCardSwipe.canStart(card: card, dispatchEnabled: board.dispatchEnabled)") == 1
    for term in ("card.tool.isEmpty", "card.sessionId.isEmpty", "holdsBatchMark",
                 'card.column == "prep"'):
        assert term not in body, term
    assert card.count(
        "PhoneCardSwipe.canStart(card: card, dispatchEnabled: board.dispatchEnabled)") == 1


def test_the_swipe_is_wired_to_the_card_screens_presses():
    raw = _read(BOARD)
    board = _code(raw)
    assert board.count("client.enqueue(action: PhoneActions.boardDispatch") == 1
    assert board.count("client.enqueue(action: PhoneActions.boardRefine,") == 1
    assert board.count("client.enqueue(action: PhoneActions.boardDelete") == 1
    assert board.count("client.post(") == 2
    # One press sends (4 Oct 2026): no arm, no dialog. An unplanned card
    # skips the plan gate on that press, since there is no second one.
    assert board.count("skip_plan_gate") == 1
    start = board.split("    private func pressSwipeStart(", 1)[1].split("\n    }\n", 1)[0]
    assert "arm." not in start
    assert start.index("card.planPath.isEmpty && !card.isScout") < start.index("skip_plan_gate")
    refine = board.split("    private func pressSwipeRefine(", 1)[1].split("\n    }\n", 1)[0]
    assert "arm." not in refine
    buttons = board.split("    private func swipeButtons(for", 1)[1].split("\n    }\n", 1)[0]
    assert "sendSwipeDelete(card)" in buttons
    assert "own_terminal" not in board
    assert board.count(
        ".simultaneousGesture(DragGesture(minimumDistance: PhoneCardSwipe.minimumDrag") == 1
    # iOS 18+: one UIKit pan the scroll view waits on, begun only sideways
    # (the reason a SwiftUI drag in a ScrollView took two or three swipes).
    assert board.count(".gesture(") == 1
    assert board.count("tile.gesture(CardSwipePan(") == 1
    pan = board.split("private struct CardSwipePan", 1)[1]
    assert "PhoneCardSwipe.dominantHorizontal(dx: velocity.x, dy: velocity.y)" in pan
    assert "other is UIPanGestureRecognizer && other.view is UIScrollView" in pan
    assert "shouldBeRequiredToFailBy" in pan
    assert "case .cancelled, .failed:" in pan
    assert ".highPriorityGesture(" not in board
    assert ".confirmationDialog(" not in board
    assert "role: .destructive" not in board
    link = board.split("    private func cardLink(_ card: BoardCard)", 1)[1].split(
        "    // MARK:", 1)[0]
    assert link.count(".accessibilityActions {") == 1
    primary = board.split("    private func swipePrimary(for", 1)[1].split("\n    }\n", 1)[0]
    assert 'PhoneRowSelection.tickable("prep"' in primary
    assert "PhoneCardSwipe.canStart(" in primary
    assert "withAnimation(" not in board
    assert board.count("revealed = nil") >= 4
    # The slide honours Reduce Motion; no raw Button.
    assert "Motion.animation(.snappy, reduced: reduceMotion)" in board
    assert not re.search(r"(?<![A-Za-z])Button\(", board)
    assert ".lineLimit(" not in board


def test_the_swipe_survives_a_cancelled_drag_and_reads_every_note():
    board = _code(_read(BOARD))
    row = board.split("private struct SwipeRevealRow", 1)[1].split(
        "private struct CardSwipePan", 1)[0]
    # iOS 17's drag resets itself on a cancel; iOS 18's pan zeroes its travel
    # on end, cancel and failure, so the tile never stays part-slid.
    assert "@GestureState private var legacyDrag" in row
    assert ".updating($legacyDrag)" in row and ".onChanged" not in row
    assert row.count("PhoneCardSwipe.dominantHorizontal(") == 2
    assert row.count("panDrag = 0") == 2
    arrived = board.split("    private func swipeNoteArrived(", 1)[1].split("\n    }\n", 1)[0]
    assert "client.readQueueNote(for: card.id)" in arrived
    assert "isPlanGateRefusal" not in arrived
    # The Mac's note is drawn ahead of the phone's own refusal.
    note = board.split("    private func swipeNote(for", 1)[1].split("\n    }\n", 1)[0]
    assert note.index("client.queueNote(for: card.id)?.text") < note.index("swipeRefusal[card.id]")
    assert "PhoneActions.boardDelete" in note and "client.queueMark(for: card.id)" in note
    assert ".onChange(of: client.queueMark(for: card.id))" in board
    # A reveal change, and only one, disarms.
    assert board.count("arm.disarm()") >= 3
    assert "deleting = card" not in board


def test_the_doors_already_carry_the_three_verbs():
    for verb in ("board_dispatch", "board_refine", "board_delete"):
        assert ApiServer.LAN_ACTIONS.count(verb) == 1, verb
        assert ApiServer.REMOTE_ACTIONS.count(verb) == 1, verb
        assert verb in ApiServer._LAN_BOARD, verb


def test_the_project_lists_both_new_files():
    pbx = _read(PBXPROJ)
    lines = pbx.splitlines()
    assert sum("CardSwipe.swift" in line for line in lines) == 4
    assert sum("CardSwipeTests.swift" in line for line in lines) == 2
    assert pbx.count("BA7C4E1F0000000000000101") == 3
    assert pbx.count("BA7C4E1F0000000000000102") == 2
    assert pbx.count("BA7C4E1F0000000000000103") == 3
    assert pbx.count("BA7C4E1F0000000000000104") == 2
    assert XCTEST.exists()


def test_xctest_names_the_same_cases():
    xctest = _read(XCTEST)
    for case in (
        "testCanStartMirrorsTheCardScreensSevenTerms",
        "testPrimaryFollowsTheColumnAndTheGates",
        "testTheLabelsAreTheCardScreensWords",
        "testTheGestureHelpersClampAndThreshold",
    ):
        assert f"func {case}()" in xctest, case


def test_the_contract_says_so():
    contract = _read(CONTRACT)
    heading = "## A Prep or Backlog card is swiped on the Board tab"
    assert contract.count("\n" + heading + "\n") == 1
    section = contract.split(heading, 1)[1].split("\n## ", 1)[0]
    for word in ("CardSections.nextAction", "enqueue", "Delete card",
                 "first press", "VoiceOver", "test_phone_card_swipe.py"):
        assert word in section, word
    assert "A Prep or Backlog card swipes on the phone too" in _read(PANEL_DOC)
