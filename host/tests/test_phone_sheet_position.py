"""The agent sheet's "2 of 5" position mark: the rules run under swiftc.

`ios/BobPhone/AgentSheetSwipe.swift` holds the Foundation-only rules — where
the agent sits in the Needs you order, the words drawn and spoken, and when
the mark yields to the text size. They are compiled here and executed case by
case; the phone job runs the same table (`AgentSheetSwipeTests.swift`). The
source pins hold the sheet's header to them.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_detail_tabs import _block

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULES = PHONE / "AgentSheetSwipe.swift"
HOST = PHONE / "PhoneSheetHost.swift"
CONTRACT = ROOT / "docs" / "phone-contract.md"
ACCESSIBILITY = ROOT / "host" / "tests" / "test_phone_accessibility.py"

MAIN = r'''
import Foundation

typealias S = AgentSheetSwipe

func at(_ i: Int, _ c: Int) -> S.Position { S.Position(index: i, count: c) }

switch CommandLine.arguments[1] {
case "position_three":
    let o = ["a", "b", "c"]
    precondition(S.position(of: "a", in: o) == at(1, 3))
    precondition(S.position(of: "b", in: o) == at(2, 3))
    precondition(S.position(of: "c", in: o) == at(3, 3))
case "position_absent":
    precondition(S.position(of: "zz", in: ["a", "b", "c"]) == nil)
case "position_one_waiter":
    precondition(S.position(of: "a", in: ["a"]) == nil)
case "position_empty":
    precondition(S.position(of: "a", in: []) == nil)
case "position_first_index":
    precondition(S.position(of: "a", in: ["a", "b", "a"]) == at(1, 3))
case "mark_words":
    precondition(S.mark(at(2, 5)) == "2 of 5")
    precondition(S.mark(at(1, 2)) == "1 of 2")
case "spoken_words":
    precondition(S.spokenMark(at(2, 5)) == "2 of 5 waiting")
case "shows_below_accessibility":
    precondition(S.showsMark(at(2, 5), accessibilitySize: false))
case "hidden_at_accessibility":
    precondition(!S.showsMark(at(2, 5), accessibilitySize: true))
case "hidden_without_position":
    precondition(!S.showsMark(nil, accessibilitySize: false))
    precondition(!S.showsMark(nil, accessibilitySize: true))
default:
    fatalError("unknown case")
}
print("ok")
'''

CASES = [
    "position_three",
    "position_absent",
    "position_one_waiter",
    "position_empty",
    "position_first_index",
    "mark_words",
    "spoken_words",
    "shows_below_accessibility",
    "hidden_at_accessibility",
    "hidden_without_position",
]


@pytest.fixture(scope="module")
def rules_binary(tmp_path_factory):
    swift = shutil.which("swiftc")
    if swift is None:
        pytest.skip("Swift toolchain unavailable")
    folder = tmp_path_factory.mktemp("sheet-position")
    (folder / "main.swift").write_text(MAIN)
    binary = folder / "rules"
    subprocess.run([swift, str(RULES), str(folder / "main.swift"), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("case", CASES)
def test_the_position_table_runs(rules_binary, case):
    out = subprocess.run([str(rules_binary), case], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_the_rules_file_is_foundation_only():
    text = RULES.read_text()
    assert "import Foundation" in text
    assert "import SwiftUI" not in text and "import UIKit" not in text


def test_the_header_draws_the_mark_as_a_run_of_the_title():
    text = HOST.read_text()
    for call in ("AgentSheetSwipe.position(", "AgentSheetSwipe.mark(",
                 "AgentSheetSwipe.spokenMark(", "AgentSheetSwipe.showsMark("):
        assert text.count(call) == 1, call
    assert text.count("accessibilitySize: dynamicTypeSize.isAccessibilitySize") == 1
    header = _block(text, "private var header: some View")
    assert header.count("titleText(position)") == 1
    assert header.count(".accessibilityLabel(spokenTitle(position))") == 1
    assert "Text(router.top?.title" not in header
    assert 'Text("Close")' in header
    assert ".accessibilityHidden" not in header
    assert text.count("DecryptCaption(") == 1
    title = _block(text, "private func titleText(")
    assert title.count("+ Text(") == 1
    assert title.count("Theme.dim") == 1


def test_the_budget_rule_adds_no_clip_and_the_kept_pins_hold():
    text = HOST.read_text()
    assert ".lineLimit(" not in text and ".dynamicTypeSize(" not in text
    assert text.count(".id(router.topState?.id)") == 1
    assert text.count("static let MAX_DEPTH = 3") == 1
    assert "DetailTab" not in text
    assert text.count("PhoneInbox.groups(client.snapshot.decisionItems)") == 1


def test_the_contract_and_the_reflow_list_name_the_header():
    assert CONTRACT.read_text().count(
        "The sheet's header says where you are in Needs you") == 1
    assert '"PhoneSheetHost.swift"' in ACCESSIBILITY.read_text()
