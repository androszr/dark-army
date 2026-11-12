# host/tests/test_card_window_refining_status.py
"""The card window says what the tile says while a card is being refined.

`plans/2026-10-03-card-window-refining-status-line.md`. One rule on the
model (`BoardCard.refiningLine`), read by the tile and the window; the phone
spells the same two sentences inline and this file pins the copies equal.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
MODELS = PANEL / "BoardModels.swift"
TILE = PANEL / "BoardCardView.swift"
SHEET = PANEL / "BoardCardSheet.swift"
PHONE_VIEW = ROOT / "ios" / "BobPhone" / "CardDetailView.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "BoardRefiningLineTests.swift"
PANEL_DOC = ROOT / "docs" / "context-panel.md"

INTERVIEW = '"refining — answer the interview in the terminal"'
WAITING = '"refining — waiting for the session to appear"'
RULE = "static func refiningLine(state: String) -> String {"
TILE_LIVE = "private var liveState: some View {"
STATUS = "private var statusSection: some View {"
FOOTER = "private var footer: some View {"


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


def test_the_refining_words_have_one_home_on_the_mac():
    models = _read(MODELS)
    rule = _block(models, RULE)
    for literal in (INTERVIEW, WAITING):
        assert models.count(literal) == 1
        assert rule.count(literal) == 1
        assert _read(TILE).count(literal) == 0
        assert _read(SHEET).count(literal) == 0


def test_the_phone_spells_them_the_same():
    phone = _read(PHONE_VIEW)
    block = _block(phone, TILE_LIVE)
    for literal in (INTERVIEW, WAITING):
        assert phone.count(literal) == 1
        assert block.count(literal) == 1


def test_the_tile_reads_the_rule():
    tile = _read(TILE)
    block = _block(tile, TILE_LIVE)
    call = "BoardCard.refiningLine(state: card.refineState)"
    assert tile.count(call) == 1
    assert block.count(call) == 1
    assert 'card.refineState == "live"' not in block


def test_the_window_reads_the_rule_and_the_batch_line():
    sheet = _read(SHEET)
    block = _block(sheet, STATUS)
    assert block.count("live.refiningLine") == 2
    assert block.count("live.batchPlanningLine") == 3
    assert block.count("AgentChatterView(.caret, wait: .refining, seed: live.id,") == 1
    assert block.count(".id(live.id)") == 1
    assert sheet.count("CardSections.stateWords(") == 1


def test_the_unplanned_note_has_one_home_in_the_window():
    sheet = _read(SHEET)
    footer = _block(sheet, FOOTER)
    assert sheet.count("live.dispatchError") == 2
    assert footer.count("live.dispatchError") == 2
    assert "live.dispatchError" not in _block(sheet, STATUS)
    assert "plan never arrived" not in sheet
    assert "plan never arrived" not in _read(TILE)


def test_the_window_gate_is_the_daemons():
    models = _read(MODELS)
    assert models.count("var refiningLine: String {") == 1
    assert "isRefining ?" in _block(models, "var refiningLine: String {")
    assert "state.refining" not in _block(_read(SHEET), STATUS)


def test_xctest_names_the_cases():
    text = _read(SWIFT_TESTS)
    for name in (
        "testNoRefinementDrawsNoLine",
        "testDispatchingSaysWaitingForTheSession",
        "testLiveSaysAnswerTheInterview",
        "testTheStaticReadsTheTilesOptimisticPress",
    ):
        assert f"func {name}()" in text


def test_the_contract_names_the_window():
    assert _read(PANEL_DOC).count("refiningLine") == 1
