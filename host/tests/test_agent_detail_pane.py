# host/tests/test_agent_detail_pane.py
"""The selected agent's detail is drawn in the board's place — source pins.

`plans/2026-09-06-agent-detail-in-board-pane.md`. The decisions are table-
tested in `panel/Tests/BobPanelTests/RailLayoutTests.swift`; what this file
pins is the wiring the Swift tests cannot see: the stdout and terminal panes
left `PanelView.swift` for `AgentDetailPane.swift`, the left pane is chosen
by `RailLayout.leftPane`, both focus callbacks still gate the letter verbs
through `setStripEditing`, the Escape ladder reads `RailLayout.escapeRung`
with `.deselect` between `.endEditing` and `.back`, and the constants that
were not to move (`PanelMetrics.width`, no `GeometryReader` in
`TerminalPane`) did not.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PANEL_VIEW = PANEL / "PanelView.swift"
DETAIL = PANEL / "AgentDetailPane.swift"
LAYOUT = PANEL / "RailLayout.swift"
MAIN = PANEL / "main.swift"
KEY_MONITOR = PANEL / "KeyMonitor.swift"
TRIAGE = PANEL / "Triage.swift"
TERMINAL = PANEL / "TerminalPane.swift"
TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "RailLayoutTests.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
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
    """`text` with its comments removed, so a pin judges code and not prose.

    A doc comment is allowed to name the construct it promises the file does
    not use; only a line of Swift may not.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def test_panes_left_the_rail_for_the_detail():
    view = _read(PANEL_VIEW)
    detail = _read(DETAIL)
    stdout = _read(PANEL / "ProcessTable.swift")
    assert view.count("StdoutPane(") == 0
    assert view.count("TerminalPane(") == 0
    assert detail.count("StdoutPane(") == 1
    assert detail.count("TerminalPane(") == 1
    assert "AgentDetailPane(" in view
    assert "var messageHidden: Bool = false" in stdout


def test_left_pane_is_decided_by_rail_layout():
    view = _read(PANEL_VIEW)
    assert view.count("RailLayout.leftPane(") == 1
    assert "RailLayout.selectionAfterTap(" in view
    assert "RailLayout.reaimAfterListChange(" in view


def test_both_focus_callbacks_gate_the_letter_verbs():
    view = _read(PANEL_VIEW)
    assert view.count("onReplyFocus: setStripEditing") == 1
    assert view.count("onInputFocus: setStripEditing") == 1


def test_escape_ladder_reads_rail_layout_in_order():
    main = _read(KEY_MONITOR)
    assert main.count("RailLayout.escapeRung(") == 1
    ladder = _block(main, "switch RailLayout.escapeRung(")
    ending = ladder.index("case .endEditing")
    close_run = ladder.index("case .closeHistoryRun")
    close_day = ladder.index("case .closeHistoryDay")
    leave = ladder.index("case .leaveHistory")
    deselect = ladder.index("case .deselect")
    back = ladder.index("case .back")
    hide = ladder.index("case .hide")
    assert ending < close_run < close_day < leave < deselect < back < hide
    assert "self.keys.send(.deselect)" in ladder
    assert "self.keys.send(.closeHistoryRun)" in ladder
    assert "self.keys.send(.leaveHistory)" in ladder


def test_deselect_intent_and_detail_flag_exist():
    triage = _read(TRIAGE)
    assert triage.count("case deselect") == 1
    assert "@Published var detailOpen = false" in triage
    view = _read(PANEL_VIEW)
    assert "keys.detailOpen = tab == .agents && selected != nil" in view
    assert "case .deselect:" in view


def test_rail_width_is_untouched():
    main = _read(MAIN)
    assert main.count("static let width: CGFloat = 520") == 1


def test_pty_size_is_arithmetic_on_the_detail_pane():
    main = _read(MAIN)
    assert "static func terminalCols(forWidth" in main
    assert "static func terminalRows(forHeight" in main
    assert "terminalMaxCols = 400" in main
    # The rail-sized fallback remains, for the first tick before layout.
    assert "static var terminalCols: Int { terminalCols(forWidth: width) }" in main


def test_one_geometry_reader_sizes_the_terminal_and_terminal_has_none():
    assert _code(_read(TERMINAL)).count("GeometryReader") == 0
    detail = _read(DETAIL)
    assert _code(detail).count("GeometryReader") == 1
    # No divider: the terminal takes the remaining height under the header
    # (and under the action strip, when that strip is present).
    assert "SplitHandle" not in detail
    assert "RailLayout.splitHeights(" not in detail
    assert "RailLayout.draggedFraction(" not in detail
    assert "PanelMetrics.terminalCols(forWidth: geo.size.width)" in detail
    assert "PanelMetrics.terminalRows(forHeight: geo.size.height)" in detail
    assert "messageHidden: true" in detail
    assert "private var showsActions" in detail


def test_the_hosted_pane_is_a_native_terminal():
    src = _read(TERMINAL)
    assert "import SwiftTerm" in src
    assert "struct NativeTerminalHost" in src
    assert "TerminalView" in src
    assert _code(src).count("GeometryReader") == 0
    assert "ptySize.cols" in src
    # One stream per pane, never a poll (`test_terminal_socket.py` pins the
    # rest of that shape).
    assert "terminalStream(session:" in src
    assert "nativeBackgroundColor = backgroundNS" in src


def test_no_spinner_in_the_new_files():
    assert "ProgressView" not in _read(DETAIL)
    assert "ProgressView" not in _read(LAYOUT)


def test_drawn_controls_take_the_pointer():
    # back, card, jump take the hand. There is no divider to resize.
    detail = _read(DETAIL)
    assert detail.count(".clickable()") >= 3
    assert "struct SplitHandle" not in detail
    assert ".resizableVertically()" not in detail
    assert "func resizableVertically(" in _read(PANEL / "Cursor.swift")


def test_hosted_jump_opens_the_detail_and_does_not_yield_the_panel():
    """A Dark Army-hosted terminal has no editor window. Jump selects the row so
    the agent's full pane (the live screen) is on show, and never posts
    `.panelDidJump`, which would hide the panel that holds it."""
    triage = _read(TRIAGE)
    jump = _block(triage, "func jump(_ agent: Agent")
    assert "agent.ownTerminal" in jump
    assert "pane below" not in jump
    assert ".panelShowSession" in jump
    assert "reveal_session" in jump
    assert jump.index("ownTerminal") < jump.index("reveal_session")
    hosted = jump.split("guard agent.canJump")[0]
    assert ".panelShowSession" in hosted
    assert "name: .panelDidJump" not in hosted
    view = _read(PANEL_VIEW)
    assert ".panelShowSession" in view
    # One receive, on the extracted publisher — a second `.onReceive` of
    # `.panelShowSession` would fire `show(row)` twice. The helper still
    # publishes that name and still runs `show(row)` on the session path.
    assert view.count(".onReceive(navigationRequests, perform: receiveNavigation)") == 1
    assert view.count("publisher(for: .panelShowSession)") == 1
    requests = _block(view, "private var navigationRequests:")
    assert ".panelShowSession" in requests
    receive = _block(view, "private func receiveNavigation(")
    # Card notes go to `showCard`; the session else-path is the jump.
    assert "showCard(card:" in receive
    assert "show(row)" in receive
    assert receive.index("showCard(card:") < receive.index("show(row)")
    names = _read(PANEL / "BoardState.swift")
    assert "static let panelShowSession" in names
    card = _read(PANEL / "BoardCardView.swift")
    card_jump = _block(card, "private func jump(to row: Agent)")
    assert "row.ownTerminal" in card_jump
    assert ".panelShowSession" in card_jump
    assert card_jump.index("ownTerminal") < card_jump.index("reveal_session")
    sheet = _read(PANEL / "BoardCardSheet.swift")
    sheet_jump = _block(sheet, "private func jump(_ row: Agent)")
    assert "row.ownTerminal" in sheet_jump
    assert ".panelShowSession" in sheet_jump
    header = _read(DETAIL)
    assert "agent.canJump && !agent.ownTerminal" in header
    models = _read(PANEL / "Models.swift")
    assert "var isJumpable: Bool { canJump || ownTerminal }" in models


def test_board_stays_mounted_under_the_detail():
    # The board is never unmounted by a selection: its `@State` (the applied
    # card-focus mark above all) has to survive, or every deselect after a
    # reverse jump would re-run the last reveal and overwrite the search.
    view = _read(PANEL_VIEW)
    body = _block(view, "ZStack {")
    assert body.count("BoardView(") == 1
    assert view.count("BoardView(") == 1
    # History covers the board the same way a detail does: opacity 0, no
    # hit testing, hidden from VoiceOver. Comm only narrows it.
    assert ".opacity(boardCovered ? 0 : 1)" in body
    assert ".allowsHitTesting(!boardCovered)" in body
    assert "case .detail, .history: return true" in view
    # Never `.disabled`: `isEnabled` propagates into the board's own
    # presentations (the plan-gate dialog, the rename alert, the folder and
    # drafts sheets), and one up when a detail opens from outside would stay
    # modal with a greyed-out Cancel — a wedge only Quit recovers.
    assert ".disabled(" not in body
    assert ".accessibilityHidden(boardCovered)" in body
    assert "AgentDetailPane(" in body
    # And the applied mark stays where it was: on the view, as `@State`.
    board = _read(PANEL / "BoardView.swift")
    assert "@State private var appliedCardFocus" in board


def test_aim_rides_a_deliberate_open_not_the_sse_gate():
    view = _read(PANEL_VIEW)
    visible = _block(view, ".onChange(of: client.visible)")
    assert "selectTopAttentionIfNeeded" not in visible
    shown = _block(view, ".onChange(of: focus.shown)")
    assert "selectTopAttentionIfNeeded()" in shown
    assert "syncFocus()" in shown
    main = _read(MAIN)
    assert main.count("focus.noteShown()") == 1
    show = _block(main, "func show(x: Double?, y: Double?)")
    assert "focus.noteShown()" in _block(show, "if !seen {")
    assert "@Published private(set) var shown = 0" in _read(TRIAGE)


def test_show_card_deselects_whole():
    view = _read(PANEL_VIEW)
    show_card = _block(view, "private func showCard(card: BoardCard)")
    assert "deselect()" in show_card
    assert "expanded = nil" not in show_card


def test_show_card_asks_the_board_to_reveal_and_writes_no_query_itself():
    # The pipeline band's ⌗ on a Done card used to write the `#card` query
    # from PanelView and scroll nothing: with four fixed-width columns
    # overflowing the pane sideways, the reader landed on a board narrowed
    # to a card two columns off the right edge — it looked empty, and the
    # card looked gone. The panel now *asks* the board, and `BoardView.reveal`
    # is the one place that narrows, scrolls and glows.
    view = _read(PANEL_VIEW)
    show_card = _block(view, "private func showCard(card: BoardCard)")
    assert "board.requestReveal(live.id)" in show_card
    assert "board.query =" not in show_card
    assert "includeProjectInFilter" not in show_card
    board_view = _read(PANEL / "BoardView.swift")
    applier = _block(board_view, ".onChange(of: state.revealRequest)")
    assert "board.cards.first(where: { $0.id == request.id })" in applier
    assert "reveal(card)" in applier
    reveal = _block(board_view, "private func reveal(_ card: BoardCard)")
    assert "state.includeProjectInFilter(card.project)" in reveal
    assert "state.query = CardToken.query(for: card)" in reveal
    # The scroll is AppKit on the revealed card's own reported frame:
    # `ScrollViewProxy.scrollTo` left a card off the pane's edge while
    # reporting success. The reveal arms the scroll and the frame the
    # revealed card reports (`RevealFrameKey`) fires it once, clamped to the
    # document, with no guessed delay.
    assert "pendingScrollCard = id" in reveal
    assert "scrollTo(" not in _code(board_view)
    assert "ScrollViewReader" not in _code(board_view)
    scroll = _block(board_view, "private func scrollRevealed(to frame: CGRect)")
    assert "clip.animator().setBoundsOrigin(target)" in scroll
    flat = " ".join(scroll.split())
    assert "max(0, doc.height - viewport.height)" in flat
    assert ".onPreferenceChange(RevealFrameKey.self)" in board_view
    assert "if state.revealedCard == card.id" in board_view
    state = _read(PANEL / "BoardState.swift")
    assert "func requestReveal(_ cardId: String)" in state
    assert "struct CardRevealRequest: Equatable" in state


def test_reverse_jump_keeps_the_detail():
    # The editor's Dark Army button asks to come back to the session it is
    # looking at, and what that session is doing lives in the detail. The
    # card request still rides along — `BoardView` lands it underneath —
    # but `syncFocus` neither drops the selection nor holds the board.
    view = _read(PANEL_VIEW)
    sync = _block(view, "private func syncFocus()")
    assert "deselect()" not in sync
    assert "selected = nil" not in sync
    assert "revealedForCard" not in sync
    assert "appliedCardReveal" not in view


def test_card_reveal_holds_the_board_through_the_open_aim():
    # On a hidden panel `client.visible` flips before `focus.shown`, so the
    # reveal runs first and the open's aim would draw the top waiter's
    # detail over the card just revealed. The card route sets the hold
    # *after* `deselect()` (which clears it), and the aim reads it.
    view = _read(PANEL_VIEW)
    block = _block(view, "private func showCard(card: BoardCard)")
    assert block.index("deselect()") < block.index("revealedForCard = true")
    aim = _block(view, "private func selectTopAttentionIfNeeded()")
    assert "if revealedForCard { return }" in aim
    assert "revealedForCard = false" not in aim
    deselect = _block(view, "private func deselect()")
    assert "revealedForCard = false" in deselect
    assert "aimPendingSinceShown = false" in deselect
    click = _block(view, "onSelect: {")
    assert "revealedForCard = false" in click
    assert "aimPendingSinceShown = false" in click


def test_open_aim_carries_over_to_the_first_live_snapshot():
    # `hide()` stops the SSE client, so the `focus.shown` aim runs against a
    # frozen snapshot; the first live one spends a one-shot through the
    # table-tested rule, and a hidden panel cancels it.
    view = _read(PANEL_VIEW)
    shown = _block(view, ".onChange(of: focus.shown)")
    assert "aimPendingSinceShown = true" in shown
    snapshot = _block(view, ".onChange(of: snapshot.generatedAt)")
    assert snapshot.count("RailLayout.aimAfterSnapshot(") == 1
    assert "aimPendingSinceShown = false" in snapshot
    assert "revealedForCard = false" in snapshot
    visible = _block(view, ".onChange(of: client.visible)")
    assert "aimPendingSinceShown = false" in visible
    assert "revealedForCard = false" in visible
    assert view.count("RailLayout.aimAfterSnapshot(") == 1


def test_table_tests_name_every_rail_layout_function():
    tests = _read(TESTS)
    layout = _read(LAYOUT)
    functions = re.findall(r"static func (\w+)\(", layout)
    assert functions, "RailLayout declares no functions"
    for name in functions:
        assert f"RailLayout.{name}(" in tests, f"untested: {name}"


def test_permitted_hide_is_visible_and_uses_selected_agent_action():
    stdout = _read(PANEL / 'ProcessTable.swift')
    assert 'if agent.canHide {' in stdout
    assert 'Button("Hide") { actions.dismiss(agent, client: client) }' in stdout
    assert 'if agent.canHide { return true }' in _read(DETAIL)
    assert 'agent.canHide ? "hide_session" : "dismiss"' in _read(TRIAGE)
