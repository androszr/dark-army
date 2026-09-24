# host/tests/test_board_rows.py
"""The board as four stacked rows — source pins over the Swift.

`plans/2026-09-12-board-rows-instead-of-columns.md`. The fold rule itself is
tabled in `panel/Tests/BobPanelTests/BoardRowFoldTests.swift`; what this
file pins is what the Swift tests cannot see: that the phone's copy of
`BoardRowFold` is written exactly as the panel's, that the panel board is one
vertical scroll with no width bookkeeping and is never `.disabled`, that the
phone board has no pager and reads its folds from `@AppStorage`, that the
habit file gained a **new** key and still never reads the retired one, that
both views ask `BoardRowFold.drawnFolded` rather than re-deriving a fold,
that every static of the rule has a Swift test naming it, and that the
project guidance no longer blames sideways overflow.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
BOARD_VIEW = PANEL / "BoardView.swift"
BOARD_STATE = PANEL / "BoardState.swift"
PLACEMENT = PANEL / "Placement.swift"
METRICS = PANEL / "main.swift"
FOLD = PANEL / "BoardRowFold.swift"
PHONE_BOARD = ROOT / "ios" / "BobPhone" / "BoardView.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "BoardRowFoldTests.swift"
CLAUDE_MD = ROOT / "CLAUDE.md"

ENUM = "enum BoardRowFold {"

#: Every static the rule declares. A new one lands here *and* in the Swift
#: tests, or the test below names the one that is missing.
STATICS = (
    "static let rows",
    "static func defaultFolded(_ id: String) -> Bool",
    "static func folded(_ id: String, flipped: Set<String>) -> Bool",
    "static func drawnFolded(_ id: String, flipped: Set<String>, searching: Bool) -> Bool",
    "static func toggled(_ id: String, flipped: Set<String>) -> Set<String>",
    "static func decode(_ raw: [String]?) -> Set<String>",
    "static func encode(_ flipped: Set<String>) -> [String]",
    "static func decode(joined: String) -> Set<String>",
    "static func encode(joined flipped: Set<String>) -> String",
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
                block = text[i : j + 1]
                assert len(block) > len(start), f"empty block: {start!r}"
                return block
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _squashed(text: str) -> str:
    return " ".join(_code(text).split())


# --- the rule lives in one place, spelled the same on both sides -------------


def test_the_rule_is_pure_and_names_every_static():
    """Foundation only and no `BoardColumn`: the phone compiles the same block
    with no such type in reach."""
    src = _read(FOLD)
    block = _code(_block(src, ENUM))
    for needle in STATICS:
        assert needle in block, needle
    assert "import SwiftUI" not in _code(src)
    assert "BoardColumn" not in _code(src)
    assert "ProgressView" not in src


def test_the_phone_copy_is_written_exactly_as_the_panels():
    panel = _squashed(_block(_read(FOLD), ENUM))
    phone = _squashed(_block(_read(PHONE_BOARD), ENUM))
    assert panel == phone


def test_every_static_has_a_swift_test_naming_it():
    tests = _read(SWIFT_TESTS)
    for needle in STATICS:
        name = needle.split("static ")[1].split(" ")[1].split("(")[0]
        assert f"BoardRowFold.{name}" in tests, name
    assert tests.count("func test") >= 9


# --- the panel: one vertical scroll, rows, no width bookkeeping --------------


def test_the_panel_board_is_one_vertical_scroll_over_four_rows():
    code = _code(_read(BOARD_VIEW))
    assert "ScrollView([.vertical, .horizontal])" not in code
    assert "ScrollView(.vertical)" in code
    assert "boardContentWidth" not in code
    assert ".disabled(" not in code
    # The x term of the reveal's scroll is gone with the second axis.
    assert "doc.width" not in code
    assert "pendingScrollCard = id" in code
    assert "private var rows: some View" in code
    assert "private func rowHeading(_ column: BoardColumn)" in code
    assert "private func rowTiles(_ column: BoardColumn)" in code
    for gone in ("columnHeadings", "columnBand", "columnCell("):
        assert gone not in code, gone


def test_a_reveal_over_a_standing_search_lays_the_rows_out_afresh():
    # A reveal pressed while a search was already up changed no fold, so the
    # row stack's `.id` stayed put and the lazy stack kept the old result's
    # heights and offset: the revealed card was never laid out, never
    # reported its frame, and the board read as empty. Every reveal now bumps
    # an epoch the stack is keyed on, and drops the search caret.
    code = _code(_read(BOARD_VIEW))
    reveal = _block(code, "private func reveal(_ card: BoardCard)")
    assert "revealEpoch &+= 1" in reveal
    assert "searchFieldFocused = false" in reveal
    flat = " ".join(code.split())
    assert ("RowStackIdentity(folds: BoardColumn.allCases.map { state.rowFolded($0) }, "
            "revealEpoch: revealEpoch)") in flat


def test_the_tiles_wrap_between_the_two_metrics_and_measure_no_window():
    code = _code(_read(BOARD_VIEW))
    assert code.count("GridItem(.adaptive(minimum: PanelMetrics.boardTileMin") == 1
    assert "maximum: PanelMetrics.boardColumn" in code
    metrics = _code(_read(METRICS))
    assert "static let boardTileMin: CGFloat" in metrics
    assert "static let boardColumn: CGFloat" in metrics
    # The board's one `GeometryReader` is the revealed card's frame
    # reporter, which measures a card and never the window.
    tiles = _block(code, "private func rowTiles(_ column: BoardColumn)")
    assert code.count("GeometryReader") == 1
    assert "GeometryReader" in tiles
    assert "RevealFrameKey" in tiles


def test_a_folded_heading_still_takes_a_drop_and_the_rules_are_untouched():
    """The heading falls back to `.heading` and the tiles to `.column` — two
    values, one move, so a card can be filed into a folded pile and a late
    leave from one view cannot clear the other's tint; the gap targets keep
    `.gap`; and the drop rules themselves (`handleDrop` / `place` / `drop` /
    `confirmUnplanned`) are still the functions they were."""
    code = _code(_read(BOARD_VIEW))
    assert code.count("fallback: .column(column)") == 1
    assert code.count("fallback: .heading(column)") == 1
    heading = _block(code, "private func rowHeading(_ column: BoardColumn)")
    assert ".dropDestination(for: String.self)" in heading
    assert "fallback: .heading(column)" in heading
    assert "state.noteHover(.heading(column), hovering: hovering)" in heading
    assert "state.toggleRowFold(column)" in heading
    tiles = _block(code, "private func rowTiles(_ column: BoardColumn)")
    assert "fallback: .column(column)" in tiles
    assert "case .column(let column), .heading(let column):" in code
    gap = _block(code, "private func gapView(_ column: BoardColumn, beforeId: String,")
    assert "fallback: .gap(column, beforeId: beforeId)" in gap
    for fn in ("private func handleDrop(_ ids: [String], fallback: BoardDropTarget)",
               "private func place(_ card: BoardCard, into column: BoardColumn,",
               "private func drop(_ card: BoardCard, into column: BoardColumn)",
               "private func confirmUnplanned(_ pending: PendingMove)"):
        assert fn in code, fn


def test_the_panel_asks_the_rule_through_board_state():
    """`BoardView` reads `state.rowFolded`; `BoardState` is the one caller of
    `drawnFolded` on the panel and the one writer of the habit key."""
    view = _code(_read(BOARD_VIEW))
    state = _code(_read(BOARD_STATE))
    assert "state.rowFolded(column)" in view
    assert "BoardRowFold" not in view
    assert "BoardRowFold.drawnFolded(column.rawValue, flipped: rowFlips," in state
    assert "searching: !query.isEmpty)" in state
    assert "rowFlips = BoardRowFold.decode(PanelPlacement.boardRowFlips())" in state
    toggle = _block(state, "func toggleRowFold(_ column: BoardColumn)")
    assert "disarm()" in toggle
    assert "PanelPlacement.saveBoardRowFlips(BoardRowFold.encode(rowFlips))" in toggle


def test_the_habit_file_gained_a_new_key_and_never_reads_the_retired_one():
    code = _code(_read(PLACEMENT))
    assert code.count('"board_row_flips"') == 3, "read, remove, write"
    assert "static func boardRowFlips() -> [String]?" in code
    assert "static func saveBoardRowFlips(_ ids: [String])" in code
    assert "board_folded_lanes" not in _read(PLACEMENT)
    # Merge-written, like the ticks: the writer starts from the whole file.
    save = _block(code, "static func saveBoardRowFlips(_ ids: [String])")
    assert "var body = readBody()" in save
    assert 'body.removeValue(forKey: "board_row_flips")' in save
    assert "writeBody(body)" in save


# --- the phone: no pager, folds remembered ------------------------------------


def test_the_phone_board_is_four_foldable_rows_in_one_scroll():
    text = _read(PHONE_BOARD)
    code = _code(text)
    assert "TabView(" not in code
    assert '"board.column"' not in text
    assert "columnSelectionChanged" not in text
    assert code.count('@AppStorage("board.rowFlips")') == 1
    assert "BoardRowFold.decode(joined: rowFlipsJoined)" in code
    assert "BoardRowFold.drawnFolded(item.id, flipped: rowFlips," in code
    assert "private func rowHeading(for item:" in code
    assert "private func rowBody(for id: String)" in code
    for gone in ("chipStrip", "private var heading", "private func page(for id"):
        assert gone not in code, gone
    # The whole file carries no cap now that the chip strip is gone.
    assert ".lineLimit(" not in code


def test_the_phone_heading_is_a_decrypt_button_whose_only_action_is_the_fold():
    code = _code(_read(PHONE_BOARD))
    heading = _block(code, "private func rowHeading(for item:")
    assert "DecryptButton {" in heading
    assert ("rowFlipsJoined = BoardRowFold.encode(joined: "
            "BoardRowFold.toggled(item.id, flipped: rowFlips))") in " ".join(heading.split())
    assert ".accessibilityAddTraits(.isHeader)" in heading
    assert "Button(" not in heading.replace("DecryptButton", "")


# --- the guidance -------------------------------------------------------------


def test_claude_md_says_rows_and_names_the_key():
    text = _read(CLAUDE_MD)
    assert "overflow the pane sideways" not in text
    assert "board_row_flips" in text
    assert "BoardRowFold" in text


def test_the_mac_heading_is_a_spoken_fold_button():
    """The Mac row heading is a fold control, so VoiceOver must hear what it
    is, what state it is in and be able to work it: the phone's own label
    (`<title>, <n> cards, folded|open`) and hint, a button trait beside the
    header trait, an action that is the fold, and the chevron hidden because
    the label already says the state in words."""
    code = _code(_read(BOARD_VIEW))
    heading = _block(code, "private func rowHeading(_ column: BoardColumn)")
    flat = " ".join(heading.split())
    assert ('.accessibilityLabel( "\\(column.title), \\(displayedCount) cards, " '
            '+ (folded ? "folded" : "open"))') in flat
    assert '.accessibilityHint(folded ? "Shows the cards" : "Hides the cards")' in heading
    assert ".accessibilityAddTraits([.isHeader, .isButton])" in heading
    assert ".accessibilityAction { state.toggleRowFold(column) }" in heading
    chevron = heading[heading.index('Image(systemName: "chevron.down")'):]
    chevron = chevron[:chevron.index("Text(")]
    assert ".accessibilityHidden(true)" in chevron
