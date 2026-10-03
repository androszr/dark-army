"""The agent sheet's swipe: the rules run under swiftc, the source is pinned.

`ios/BobPhone/AgentSheetSwipe.swift` holds the Foundation-only rules — which
way a swipe counts, who the neighbour is, how the Needs you list is flattened.
They are compiled here and executed case by case; the phone job runs the same
table (`AgentSheetSwipeTests.swift`). The source pins hold the sheet to them.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULES = PHONE / "AgentSheetSwipe.swift"
HOST = PHONE / "PhoneSheetHost.swift"
DECRYPT = PHONE / "DecryptFeedback.swift"
PROJECT = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
CONTRACT = ROOT / "docs" / "phone-contract.md"

MAIN = r'''
import Foundation

typealias S = AgentSheetSwipe

switch CommandLine.arguments[1] {
case "direction_next_and_previous":
    precondition(S.direction(dx: -80, dy: 5) == .next)
    precondition(S.direction(dx: 80, dy: -10) == .previous)
case "direction_short_is_nil":
    precondition(S.direction(dx: -40, dy: 0) == nil)
case "direction_diagonal_is_nil":
    precondition(S.direction(dx: -80, dy: 50) == nil)
    precondition(S.direction(dx: 0, dy: -120) == nil)
case "direction_exactly_dominant_counts":
    precondition(S.direction(dx: -120, dy: -60) == .next)
case "neighbour_steps":
    let o = ["a", "b", "c"]
    precondition(S.neighbour(of: "b", in: o, .next) == "c")
    precondition(S.neighbour(of: "b", in: o, .previous) == "a")
case "neighbour_ends_stop":
    let o = ["a", "b", "c"]
    precondition(S.neighbour(of: "c", in: o, .next) == nil)
    precondition(S.neighbour(of: "a", in: o, .previous) == nil)
case "neighbour_absent_session":
    let o = ["a", "b", "c"]
    precondition(S.neighbour(of: "zz", in: o, .next) == "a")
    precondition(S.neighbour(of: "zz", in: o, .previous) == nil)
case "neighbour_empty":
    precondition(S.neighbour(of: "a", in: [], .next) == nil)
    precondition(S.neighbour(of: "a", in: [], .previous) == nil)
case "order_flattens":
    precondition(S.order([["a", "b"], ["c"]]) { $0 } == ["a", "b", "c"])
case "order_skips_and_dedupes":
    precondition(S.order([["a", "skip", "a"], ["d"]]) { $0 == "skip" ? nil : $0 } == ["a", "d"])
default:
    fatalError("unknown case")
}
print("ok")
'''

CASES = [
    "direction_next_and_previous",
    "direction_short_is_nil",
    "direction_diagonal_is_nil",
    "direction_exactly_dominant_counts",
    "neighbour_steps",
    "neighbour_ends_stop",
    "neighbour_absent_session",
    "neighbour_empty",
    "order_flattens",
    "order_skips_and_dedupes",
]


@pytest.fixture(scope="module")
def rules_binary(tmp_path_factory):
    swift = shutil.which("swiftc")
    if swift is None:
        pytest.skip("Swift toolchain unavailable")
    folder = tmp_path_factory.mktemp("sheet-swipe")
    (folder / "main.swift").write_text(MAIN)
    binary = folder / "rules"
    subprocess.run([swift, str(RULES), str(folder / "main.swift"), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("case", CASES)
def test_the_rule_table_runs(rules_binary, case):
    out = subprocess.run([str(rules_binary), case], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def _block(text, start, length=900):
    assert start in text
    return text.split(start, 1)[1][:length]


def test_the_rules_file_is_foundation_only():
    text = RULES.read_text()
    assert "import Foundation" in text
    assert "import SwiftUI" not in text and "import UIKit" not in text


def test_both_new_files_are_in_the_project():
    project = PROJECT.read_text()
    for name in ("AgentSheetSwipe.swift", "AgentSheetSwipeTests.swift"):
        assert project.count(f"{name} in Sources") == 1
        assert len([l for l in project.splitlines()
                    if f"/* {name} */" in l]) >= 2


def test_the_router_replaces_the_top_rung_and_parks_the_reply():
    text = HOST.read_text()
    assert text.count("func replaceTop(_ entry: PhoneSheet)") == 1
    body = _block(text, "func replaceTop(_ entry: PhoneSheet)", 600).split("\n    }\n", 1)[0]
    assert body.count("keepDrafts(of:") == 1
    assert text.count("keepDrafts(of:") == 1
    assert body.count("swapTop(") == 1
    assert text.count("swapTop(") >= 3


def test_the_header_swipes_and_offers_voiceover_actions():
    text = HOST.read_text()
    assert text.count("DragGesture(") == 1
    line = [l for l in text.splitlines() if "DragGesture(" in l][0]
    assert ".simultaneousGesture(" in line
    assert ".highPriorityGesture(" not in text and ".gesture(" not in text
    assert text.count("AgentSheetSwipe.direction(") == 1
    assert text.count("AgentSheetSwipe.neighbour(") == 1
    assert text.count("PhoneInbox.groups(client.snapshot.decisionItems)") == 1
    move = _block(text, "private func move(", 300)
    assert "guard !router.terminalPresented" in move
    header = text.split("private var header: some View", 1)[1].split("private func content", 1)[0]
    assert header.count(".accessibilityActions {") == 1
    assert text.count('"Next waiting agent"') == 1
    assert text.count('"Previous waiting agent"') == 1


def test_the_kept_pins_still_hold():
    text = HOST.read_text()
    assert text.count("static let MAX_DEPTH = 3") == 1
    assert text.count(".id(router.topState?.id)") == 1
    assert text.count("PhonePlaceStore.shared.takeOrphanDraft(for: entry.id)") == 1
    restore = _block(text, "func restore(", 500).split("\n    }\n", 1)[0]
    assert "show(" not in restore
    assert "DetailTab" not in text
    decrypt = DECRYPT.read_text()
    assert decrypt.count("host == nil") == 1
    assert decrypt.count(".safeAreaInset(edge: .top, spacing: 0)") == 1


def test_the_contract_names_the_swipe_once():
    assert CONTRACT.read_text().count(
        "The agent sheet swipes to the next agent that needs you") == 1


def test_the_whole_header_row_takes_the_swipe():
    header = HOST.read_text().split("private var header: some View", 1)[1].split(
        "private func content", 1)[0]
    shape = header.index(".contentShape(Rectangle())")
    assert shape < header.index(".simultaneousGesture(DragGesture(")
