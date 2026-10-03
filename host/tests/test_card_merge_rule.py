# host/tests/test_card_merge_rule.py
"""Which review-and-merge controls a Done card offers — the pinned pair.

`plans/2026-10-03-review-and-merge-done-card.md`. `CardMerge` is one rule the
Mac's tile, the Mac's card window and the phone's card screen read, and its
two copies — `panel/Sources/BobPanel/CardMerge.swift` and
`ios/BobPhone/CardMerge.swift` — are byte-equal from `enum CardMerge {` down.
This file pins that, tables the rule by *running* it under `swiftc`, and pins
what the Swift tests cannot see: that all three surfaces draw through it and
re-derive none of it, that `CardActionWeight` is untouched, and that the
phone's controls sit behind the Mac's markers and never in the poll
(`host/tests/test_card_sections.py`'s shape).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"
RULE = PANEL / "CardMerge.swift"
PHONE_RULE = PHONE / "CardMerge.swift"
TILE = PANEL / "BoardCardView.swift"
SHEET = PANEL / "BoardCardSheet.swift"
PHONE_VIEW = PHONE / "CardDetailView.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "BoardMergeVerbTests.swift"

ENUM = "enum CardMerge {"

#: The table, as the plan states it, run under swiftc: a probe is
#: `offered:column:branch:state:due:offers`, `fix:state:offers`, `review:column:branch:running:due:offers`,
#: `merge:state:armed`, `fixlabel:armed`, `verdict:verdict:running:current`.
TABLE = (
    ("offered:done:card/x::false:true", "true"),
    ("offered:done:card/x:blocked:false:true", "true"),
    ("offered:done:card/x:conflict:false:true", "true"),
    ("offered:done:card/x:checks_failed:false:true", "true"),
    ("offered:done:card/x:merging:false:true", "false"),
    ("offered:done:card/x:merged:false:true", "false"),
    ("offered:done:card/x::true:true", "false"),
    ("offered:done:card/x::false:false", "false"),
    ("offered:done:::false:true", "false"),
    ("offered:backlog:card/x::false:true", "false"),
    ("offered:in_progress:card/x::false:true", "false"),
    ("fix:conflict:true", "true"),
    ("fix:conflict:false", "false"),
    ("fix:checks_failed:true", "true"),
    ("fix:blocked:true", "false"),
    ("fix::true", "false"),
    ("review:done:card/x:false:false:true", "true"),
    ("review:done:card/x:false:true:true", "false"),
    ("review:done:card/x:false:false:false", "false"),
    ("review:done:card/x:true:false:true", "false"),
    ("review:done::false:false:true", "false"),
    ("merge::false", "MERGE"),
    ("merge::true", "Merge into main?"),
    ("merge:merging:false", "MERGING…"),
    ("fixlabel:false", "Fix"),
    ("fixlabel:true", "Fix the merge?"),
    ("verdict:ship:false:true", "Review: SHIP"),
    ("verdict:stop:false:false", "Review: STOP — reviewed at an earlier version"),
    ("verdict::true:true", "Review running…"),
    ("verdict::false:true", "nil"),
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    assert start in text, f"missing block: {start!r}"
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _squashed(text: str) -> str:
    return " ".join(_code(text).split())


# --- the rule is pure and spelled the same on both sides ------------------------


def test_the_rule_is_pure_and_names_its_statics():
    src = _read(RULE)
    block = _code(_block(src, ENUM))
    for needle in (
            "static func offered(column: String, branch: String, mergeState: String,",
            "manualDue: Bool, daemonOffers: Bool) -> Bool",
            "static func fixOffered(mergeState: String, daemonOffers: Bool) -> Bool",
            "static func reviewOffered(column: String, branch: String,",
            "static func mergeLabel(mergeState: String, armed: Bool) -> String",
            "static func fixLabel(armed: Bool) -> String",
            "static func verdictLine(verdict: String, running: Bool, current: Bool) -> String?"):
        assert needle in block, needle
    # Foundation only: the phone compiles the same block with no panel type.
    assert "import SwiftUI" not in _code(src)
    assert "BoardColumn" not in _code(src)
    assert "ProgressView" not in src


def test_the_phone_copy_is_written_exactly_as_the_panels():
    panel = _read(RULE)
    phone = _read(PHONE_RULE)
    assert panel[panel.index(ENUM):] == phone[phone.index(ENUM):]
    assert _squashed(_block(panel, ENUM)) == _squashed(_block(phone, ENUM))


def test_a_doctored_copy_is_noticed():
    panel = _block(_read(RULE), ENUM)
    doctored = panel.replace('"MERGE"', '"MERGE NOW"', 1)
    assert doctored != panel
    assert _squashed(doctored) != _squashed(panel)


def test_the_phone_copy_is_in_the_project():
    project = (ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj").read_text()
    assert "path = CardMerge.swift;" in project  # the file reference
    assert project.count("CardMerge.swift in Sources") == 2  # build file + phase
    assert project.count("/* CardMerge.swift */,") == 1  # the group


# --- the table, by running the rule -----------------------------------------------


def test_the_table_by_running_the_rule(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    block = _block(_read(RULE), ENUM)
    harness = (
        "import Foundation\n" + block + "\n"
        "func b(_ s: String) -> Bool { s == \"true\" }\n"
        "for c in CommandLine.arguments.dropFirst() {\n"
        "    let p = c.components(separatedBy: \":\")\n"
        "    switch p[0] {\n"
        "    case \"offered\":\n"
        "        print(CardMerge.offered(column: p[1], branch: p[2], mergeState: p[3], manualDue: b(p[4]), daemonOffers: b(p[5])))\n"
        "    case \"fix\":\n"
        "        print(CardMerge.fixOffered(mergeState: p[1], daemonOffers: b(p[2])))\n"
        "    case \"review\":\n"
        "        print(CardMerge.reviewOffered(column: p[1], branch: p[2], reviewRunning: b(p[3]), manualDue: b(p[4]), daemonOffers: b(p[5])))\n"
        "    case \"merge\":\n"
        "        print(CardMerge.mergeLabel(mergeState: p[1], armed: b(p[2])))\n"
        "    case \"fixlabel\":\n"
        "        print(CardMerge.fixLabel(armed: b(p[1])))\n"
        "    case \"verdict\":\n"
        "        print(CardMerge.verdictLine(verdict: p[1], running: b(p[2]), current: b(p[3])) ?? \"nil\")\n"
        "    default: print(\"?\")\n"
        "    }\n"
        "}\n")
    path = tmp_path / "MergeProbe.swift"
    executable = tmp_path / "MergeProbe"
    path.write_text(harness)
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable), *(probe for probe, _ in TABLE)],
                         capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split("\n")[:-1] == [want for _, want in TABLE]


def test_the_swift_tests_table_it():
    tests = _read(SWIFT_TESTS)
    for needle in ("CardMerge.offered(", "CardMerge.fixOffered(",
                   "CardMerge.reviewOffered(", "CardMerge.mergeLabel(",
                   "CardMerge.fixLabel(", "CardMerge.verdictLine(",
                   "CardChangesReport", "mergeArmed"):
        assert needle in tests, needle
    assert tests.count("func test") >= 10


# --- all three surfaces draw through the rule and re-derive none of it ------------


def test_all_three_surfaces_read_the_rule():
    for path in (TILE, SHEET, PHONE_VIEW):
        text = _code(_read(path))
        assert "CardMerge.offered(" in text, path.name
        assert "CardMerge.fixOffered(" in text, path.name
        assert "CardMerge.reviewOffered(" in text, path.name
        assert "CardMerge.verdictLine(" in text, path.name
    # No surface spells the labels or the states itself.
    for path in (TILE, SHEET, PHONE_VIEW):
        text = _code(_read(path))
        assert '"Merge into main?"' not in text, path.name
        assert '"Fix the merge?"' not in text, path.name
        assert '"Review: ' not in text, path.name


def test_the_tile_sends_the_three_verbs_and_arms_merge_and_fix_in_their_own_slots():
    tile = _code(_read(TILE))
    assert "client.boardMerge(card.id)" in tile
    assert "client.boardMergeFix(card.id)" in tile
    assert "client.boardReviewRun(card.id)" in tile
    assert "state.armMerge(card.id)" in tile and "state.armFix(card.id)" in tile
    state = _code(_read(PANEL / "BoardState.swift"))
    assert "@Published var mergeArmed: String?" in state
    assert "@Published var fixArmed: String?" in state
    disarm = _block(state, "func disarm() {")
    assert "mergeArmed = nil" in disarm and "fixArmed = nil" in disarm
    client = _code(_read(PANEL / "BoardClient.swift"))
    for needle in ('"action": "board_merge"', '"action": "board_merge_fix"',
                   '"action": "board_review_run"', '"/api/card-changes?card='):
        assert needle in client, needle


def test_the_action_weight_rule_is_untouched():
    weight = _read(PANEL / "CardActionWeight.swift")
    assert "case merge" not in weight
    assert "enum Verb: String { case refine, start, startHere, done }" in weight


def test_the_phone_arms_merge_and_fix_and_sends_through_the_queue():
    view = _code(_read(PHONE_VIEW))
    assert "arm.confirm(.merge, id: card.id)" in view
    assert "arm.confirm(.mergeFix, id: card.id)" in view
    assert "PhoneActions.boardMerge, fields," in view.replace("\n", " ").replace(
        "  ", " ") or "PhoneActions.boardMerge" in view
    assert "as: .merge)" in view and "as: .mergeFix)" in view
    assert "as: .reviewRun)" in view
    # Run review is one press: no arm slot for it.
    assert "arm.confirm(.reviewRun" not in view and "arm.arm(.reviewRun" not in view
    # Behind the Mac's markers.
    assert "board.mergeWritable" in view and "board.reviewRunWritable" in view
    assert "board.cardChangesSupported" in view
    # The branch tip is echoed when the page was read.
    assert "PhoneCardAck.mergeFields(card, tip: changes?.branchTip ?? \"\")" in view


def test_the_changes_page_is_fetched_on_open_and_never_on_the_poll():
    view = _code(_read(PHONE_VIEW))
    assert view.count("client.fetchCardChanges(") == 1
    assert "client.fetchCardChangeDiff(" in view
    key = _block(view, "private var changesFetchKey: String {")
    assert "openedSections.contains(.changes)" in key and '"closed"' in key
    client = _read(PHONE / "Client.swift")
    assert "func fetchCardChanges(" in client
    assert 'kind: "card_changes"' in client
    # Never from the poll, the background check-in or the widget.
    for name in ("BobPhoneApp.swift", "BackgroundRefresh.swift"):
        assert "fetchCardChanges" not in _read(PHONE / name), name
    widget = ROOT / "ios" / "BobPhoneWidget"
    for path in widget.rglob("*.swift"):
        assert "fetchCardChanges" not in path.read_text(), path.name
    poll = _block(client, "func poll(")
    assert "CardChanges" not in poll
