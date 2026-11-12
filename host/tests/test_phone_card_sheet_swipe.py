"""The card sheet's swipe: the row-order rules run under swiftc, the source is pinned.

`ios/BobPhone/CardSheetSwipe.swift` holds the Foundation-only rule the Board
tab's row draws from and the In progress card sheet's swipe walks, so the two
cannot disagree. It is compiled here beside `AgentSheetSwipe.swift` (whose
`neighbour` the lane walks it with) and executed case by case; the phone job
runs the same table (`CardSheetSwipeTests.swift`).
"""
from pathlib import Path
import shutil
import subprocess

import pytest

def _block(text, start, length=900):
    assert start in text
    return text.split(start, 1)[1][:length]


ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULES = PHONE / "CardSheetSwipe.swift"
AGENT_RULES = PHONE / "AgentSheetSwipe.swift"
HOST = PHONE / "PhoneSheetHost.swift"
BOARD = PHONE / "BoardView.swift"
PROJECT = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
CONTRACT = ROOT / "docs" / "phone-contract.md"

MAIN = r'''
import Foundation

struct C { let id: String; let project: String }

func c(_ id: String, _ project: String = "") -> C { C(id: id, project: project) }

func shape(_ groups: [CardSheetSwipe.Group<C>]) -> [String] {
    groups.map { $0.name + ":" + $0.cards.map(\.id).joined(separator: ",") }
}

switch CommandLine.arguments[1] {
case "groups_one_project_is_flat":
    let g = CardSheetSwipe.drawnGroups([c("a1", "a"), c("a2", "a")], project: \.project)
    precondition(shape(g) == [":a1,a2"], "\(shape(g))")
case "groups_several_projects_sorted_unnamed_last":
    let g = CardSheetSwipe.drawnGroups([c("b1", "b"), c("a2", "a"), c("u3"), c("b4", "b")], project: \.project)
    precondition(shape(g) == ["a:a2", "b:b1,b4", "—:u3"], "\(shape(g))")
case "groups_unnamed_only_is_flat":
    let g = CardSheetSwipe.drawnGroups([c("u1"), c("u2")], project: \.project)
    precondition(shape(g) == [":u1,u2"], "\(shape(g))")
case "groups_one_named_plus_unnamed_is_flat":
    let g = CardSheetSwipe.drawnGroups([c("a1", "a"), c("u2")], project: \.project)
    precondition(shape(g) == [":a1,u2"], "\(shape(g))")
case "groups_empty_is_one_empty_group":
    let g = CardSheetSwipe.drawnGroups([C](), project: \.project)
    precondition(shape(g) == [":"], "\(shape(g))")
case "order_follows_the_drawn_groups":
    let o = CardSheetSwipe.order([c("b1", "b"), c("a2", "a"), c("u3"), c("b4", "b")],
                                 project: \.project, id: \.id)
    precondition(o == ["a2", "b1", "b4", "u3"], "\(o)")
case "order_dedupes_keeping_the_first":
    precondition(CardSheetSwipe.order([c("a1"), c("a1")], project: \.project, id: \.id) == ["a1"])
case "order_empty":
    precondition(CardSheetSwipe.order([C](), project: \.project, id: \.id) == [])
case "order_ends_stop_and_steps_walk":
    let o = CardSheetSwipe.order([c("b1", "b"), c("a2", "a"), c("u3")], project: \.project, id: \.id)
    precondition(AgentSheetSwipe.neighbour(of: "u3", in: o, .next) == nil)
    precondition(AgentSheetSwipe.neighbour(of: "a2", in: o, .previous) == nil)
    precondition(AgentSheetSwipe.neighbour(of: "a2", in: o, .next) == "b1")
    precondition(AgentSheetSwipe.neighbour(of: "u3", in: o, .previous) == "b1")
case "ungrouped_name":
    precondition(CardSheetSwipe.ungroupedName == "—")
default:
    fatalError("unknown case")
}
print("ok")
'''

CASES = [
    "groups_one_project_is_flat",
    "groups_several_projects_sorted_unnamed_last",
    "groups_unnamed_only_is_flat",
    "groups_one_named_plus_unnamed_is_flat",
    "groups_empty_is_one_empty_group",
    "order_follows_the_drawn_groups",
    "order_dedupes_keeping_the_first",
    "order_empty",
    "order_ends_stop_and_steps_walk",
    "ungrouped_name",
]


@pytest.fixture(scope="module")
def rules_binary(tmp_path_factory):
    swift = shutil.which("swiftc")
    if swift is None:
        pytest.skip("Swift toolchain unavailable")
    folder = tmp_path_factory.mktemp("card-sheet-swipe")
    (folder / "main.swift").write_text(MAIN)
    binary = folder / "rules"
    subprocess.run([swift, str(RULES), str(AGENT_RULES), str(folder / "main.swift"), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("case", CASES)
def test_the_rule_table_runs(rules_binary, case):
    out = subprocess.run([str(rules_binary), case], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_the_rules_file_is_foundation_only():
    text = RULES.read_text()
    assert "import Foundation" in text
    assert "import SwiftUI" not in text and "import UIKit" not in text


def test_both_new_files_are_in_the_project():
    project = PROJECT.read_text()
    for name in ("CardSheetSwipe.swift", "CardSheetSwipeTests.swift"):
        assert project.count(f"{name} in Sources") == 1
        assert len([l for l in project.splitlines() if f"/* {name} */" in l]) >= 2


def test_the_board_row_draws_from_the_shared_groups():
    text = BOARD.read_text()
    stack = _block(text, "private func cardsStack(", 700).split("\n    }\n", 1)[0]
    assert stack.count("CardSheetSwipe.drawnGroups(") == 1
    assert stack.count("groups.count > 1") == 1
    assert stack.count("BoardProjectHeader(title: group.name)") == 1
    assert "names.sorted()" not in stack
    assert 'BoardProjectHeader(title: "—")' not in stack
    assert text.count("cardsStack(tickedFirst(visible, in: id))") == 1


def test_the_lane_reads_the_boards_order_under_its_project_choice():
    text = HOST.read_text()
    assert text.count('@SceneStorage("board.project")') == 1
    assert text.count("CardSheetSwipe.order(") == 1
    assert text.count("PhoneSheetKind.answeringColumn") == 1
    assert text.count("BoardProjects.matches(") == 1
    resolve = [l for l in text.splitlines() if "FleetProjects.resolve(" in l]
    assert len(resolve) == 1
    boundary = text.split("FleetProjects.resolve(", 1)[1][:260]
    assert "heard: board.available" in boundary
    assert text.count("AgentSheetSwipe.neighbour(") == 1
    lane = _block(text, "private var swipeLane", 600).split("\n    }\n", 1)[0]
    assert lane.count("PhoneSheetKind.answers(.card, column:") == 1
    assert text.count("PhoneSheetKind.answers(.card, column:") == 1
    move = _block(text, "private func move(", 300)
    assert move.count("router.replaceTop(next)") == 1
    assert text.count("router.replaceTop(next)") == 1


def test_the_labels_live_in_one_block():
    text = HOST.read_text()
    labels = _block(text, "private var swipeLabels", 500).split("\n    }\n", 1)[0]
    for label in ('"Next In progress card"', '"Previous In progress card"',
                  '"Next waiting agent"', '"Previous waiting agent"'):
        assert text.count(label) == 1
        assert label in labels
    assert text.count(".accessibilityActions {") == 1


def test_no_new_literals_and_no_view_drift():
    text = HOST.read_text()
    assert '"in_progress"' not in text
    assert ".lineLimit(" not in text
    assert "DetailTab" not in text


def test_the_contract_names_the_swipe_once():
    heading = "The card sheet swipes to the next In progress card"
    assert CONTRACT.read_text().count(heading) == 1
    assert (ROOT / "docs" / "context-panel.md").read_text().count(heading) == 1
