# host/tests/test_phone_manual_checks.py
"""The Checks section's Mac/phone pair and the phone screen's text rules.

`ManualCheckRules` is Foundation-only and byte-pinned from the
`enum ManualCheckRules {` marker down (`test_comm_rules.py`'s idiom);
`ManualCheckModels.swift` is a whole-file byte copy
(`test_phone_knowledge.py`'s). The phone view is checked by grep for the
rules `docs/phone-contract.md` pins on every screen — no clipped prose, a
literal title — and the Xcode project must compile all three new files.
The rules themselves are run by the panel's `ManualCheckRulesTests`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "panel" / "Sources" / "BobPanel"
PHONE = REPO / "ios" / "BobPhone"
PROJECT = REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
MARKER = "enum ManualCheckRules {"


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


def test_the_rules_are_one_rule_on_both_sides():
    panel = _shared(PANEL / "ManualCheckRules.swift")
    phone = _shared(PHONE / "ManualCheckRules.swift")
    for region in (panel, phone):
        assert "static func openFirst(" in region
        assert "static func rowLine(" in region
        assert "static func outcomeLine(" in region
        assert "static let noteLimit = 400" in region
    assert panel == phone, (
        "ios/BobPhone/ManualCheckRules.swift has drifted from the panel's "
        "copy — mirror the edit; never re-baseline one side.")
    for path in (PANEL / "ManualCheckRules.swift", PHONE / "ManualCheckRules.swift"):
        text = path.read_text()
        assert "import Foundation" in text
        assert "SwiftUI" not in text


def test_the_models_are_byte_equal_and_tolerant():
    panel = (PANEL / "ManualCheckModels.swift").read_bytes()
    phone = (PHONE / "ManualCheckModels.swift").read_bytes()
    assert panel == phone
    text = panel.decode()
    assert "SwiftUI" not in text
    # Every field through the tolerant helper: one absent key never blanks.
    decodes = re.findall(r"^\s+(\w+) = c\.value\(\.(\w+), ", text, re.M)
    assert len(decodes) == 6 + 13 + 4
    assert "try c.decode(" not in text


def test_the_phone_project_compiles_all_three_new_files():
    project = PROJECT.read_text()
    for name in ("ManualCheckRules.swift", "ManualCheckModels.swift",
                 "ManualChecksView.swift"):
        assert f"/* {name} in Sources */," in project, name
        assert f"path = {name};" in project, name


def test_the_phone_screen_clips_no_prose_and_titles_are_literal():
    text = (PHONE / "ManualChecksView.swift").read_text()
    assert ".lineLimit(" not in text
    titles = re.findall(r"\.navigationTitle\(([^)]*)\)", text)
    assert titles == ['"manual checks"', '"manual checks"']
    # The reads ride the sealed kind, the path in the JSON body.
    client = (PHONE / "Client.swift").read_text()
    assert 'kind: "manual_checks"' in client
    assert '["path": path]' in client


def test_the_menu_tile_opens_the_screen_only_against_a_mac_that_serves_it():
    menu = (PHONE / "MenuView.swift").read_text()
    assert "manualChecks: client.snapshot.board.manualChecksSupported" in menu
    assert "if client.snapshot.board.manualChecksSupported {" in menu
    assert "ManualChecksView(client: client)" in menu
    assert 'MenuNotYet(path: "~/checks"' in menu


def test_mark_checked_steps_aside_for_a_card_with_a_check_file():
    actions = (PHONE / "Actions.swift").read_text()
    ack = actions.split("enum PhoneCardAck {", 1)[1]
    clear = ack.split("static func showsManualClear", 1)[1].split("}", 1)[0]
    assert "card.manualCheckPath.isEmpty" in clear
    outcome = ack.split("static func showsManualOutcome", 1)[1].split("}", 1)[0]
    assert "board.manualOutcomeWritable" in outcome
    assert "!card.manualCheckPath.isEmpty" in outcome
    assert 'static let boardManualOutcome = "board_manual_outcome"' in actions
    view = (PHONE / "CardDetailView.swift").read_text()
    assert "PhoneCardAck.showsManualOutcome(card: card, board: board)" in view
    assert "cardFull?.manualCheck" in view
    assert "as: .manualOutcome" in view
    assert "case manualOutcome" in (PHONE / "Arm.swift").read_text()


def test_the_phone_decodes_the_markers_and_the_path_false_and_empty():
    models = (PHONE / "Models.swift").read_text()
    assert "manualChecksSupported = c.value(.manualChecksSupported, false)" in models
    assert "manualOutcomeWritable = c.value(.manualOutcomeWritable, false)" in models
    assert 'manualCheckPath = c.value(.manualCheckPath, "")' in models
    assert 'case manualCheck = "manual_check"' in models
    assert "manualCheck = c.maybe(.manualCheck)" in models


def test_the_mac_hides_mark_checked_where_a_file_settles_the_check():
    sheet = (PANEL / "BoardCardSheet.swift").read_text()
    assert "canMarkChecked: live.needsManualCheck" in sheet
    assert "(live.manualCheckPath.isEmpty || manualCheckMissing)" in sheet
    assert "client.boardManualOutcome(" in sheet
    client = (PANEL / "BoardClient.swift").read_text()
    assert '"action": "board_manual_outcome"' in client
    assert "Authorization" not in client.split("func boardManualOutcome", 1)[1][:600]


def test_mark_checked_comes_back_when_the_mac_refuses_the_file():
    """A card whose check file the Mac no longer serves (moved, edited out of
    shape, outside the project's folder) must not keep its badge for ever:
    the on-open read's `manual_check.available == false` brings Mark checked
    back on the phone, and a failed read does the same on the Mac."""
    actions = (PHONE / "Actions.swift").read_text()
    over = actions.split("static func showsManualClearOverRefusedFile", 1)[1]
    over = over.split("}", 1)[0]
    assert "!card.manualCheckPath.isEmpty" in over
    view = (PHONE / "CardDetailView.swift").read_text()
    assert "cardFull?.manualCheck?.available == false" in view
    assert "PhoneCardAck.showsManualClearOverRefusedFile(card: card, board: board)" in view
    assert "&& offersManualClear" in view
    sheet = (PANEL / "BoardCardSheet.swift").read_text()
    assert "(live.manualCheckPath.isEmpty || manualCheckMissing)" in sheet
    # The Mac reads the file through the daemon, never on the main thread.
    load = sheet.split("private func loadManualCheck() async {", 1)[1]
    load = load.split("\n    }\n", 1)[0]
    assert "client.manualCheckText(path: path)" in load
    assert "BoardDocuments.read" not in load


def test_the_phone_reader_keeps_its_entry_after_a_reload():
    view = (PHONE / "ManualChecksView.swift").read_text()
    assert "@State private var openedEntry: ManualCheckEntry?" in view
    assert "(openedEntry?.path == path ? openedEntry : nil)" in view


def test_the_mac_window_clears_its_search_when_aimed_at_a_file():
    window = (PANEL / "ManualChecksWindow.swift").read_text()
    present = window.split("func present(root: String = \"\", path: String = \"\")", 1)[1]
    present = present.split("\n    }\n", 1)[0]
    assert 'state.query = ""' in present and 'state.filter = "all"' in present
    view = (PANEL / "ManualChecksView.swift").read_text()
    assert "root: query.2" in view


def test_the_phone_reader_header_follows_a_landed_press():
    view = (PHONE / "ManualChecksView.swift").read_text()
    reader = view.split("struct ManualCheckReaderView: View {", 1)[1]
    assert "private var shown: ManualCheckEntry { recordedEntry ?? entry }" in reader
    assert "ManualCheckRules.outcomeLine(shown)" in reader
    assert "ManualCheckRules.outcomeLine(entry)" not in reader
    assert "recordedEntry = landed" in reader
    assert "if let updated { openedEntry = updated }" in view


def test_a_failed_mac_read_is_not_a_refused_file():
    """Only the daemon's `available: false` swaps Passed / Failed for Mark
    checked; a read that failed says so and keeps the two buttons."""
    sheet = (PANEL / "BoardCardSheet.swift").read_text()
    load = sheet.split("private func loadManualCheck() async {", 1)[1]
    load = load.split("\n    }\n", 1)[0]
    failed = load.split("guard let document else {", 1)[1].split("}", 1)[0]
    assert "manualCheckFetchFailed = true" in failed
    assert "manualCheckMissing" not in failed
    assert "ManualCheckRules.fetchFailedLine" in sheet
