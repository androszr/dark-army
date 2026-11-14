import AppKit
import SwiftUI
import XCTest
@testable import BobPanel

/// The board as one line per card, opened by a click.
///
/// 13 Nov 2026: with 48 cards in Backlog, reading the row for what is worth
/// starting meant scrolling through every card's summary. `BoardRowDensity`
/// is the whole decision — which rows draw lines under the Settings
/// choice, which face one card wears and what a press on a line does — and
/// the views re-derive none of it. The same day: every row, not only
/// Backlog; the opened card across the row's width; the choice in Settings.
@MainActor
final class BoardRowDensityTests: XCTestCase {
    // MARK: - The rule

    /// One choice for the whole board: on, all four rows draw lines; off,
    /// none does. An id the daemon does not speak draws tiles either way.
    func testEveryRowFollowsTheOneChoice() {
        XCTAssertEqual(BoardRowDensity.rows, BoardColumn.allCases.map(\.rawValue))
        for id in BoardRowDensity.rows {
            XCTAssertTrue(BoardRowDensity.compact(id, enabled: true), id)
            XCTAssertFalse(BoardRowDensity.compact(id, enabled: false), id)
        }
        XCTAssertFalse(BoardRowDensity.compact("bogus", enabled: true))
    }

    /// A tile row draws tiles whatever is opened; a compact row draws a
    /// line for every card but the one opened or revealed.
    func testFaceIsALineUnlessOpenedOrRevealed() {
        XCTAssertEqual(BoardRowDensity.face(compact: false, card: "a",
                                            opened: "a", revealed: "a"), .tile)
        XCTAssertEqual(BoardRowDensity.face(compact: true, card: "a",
                                            opened: nil, revealed: nil), .line)
        XCTAssertEqual(BoardRowDensity.face(compact: true, card: "a",
                                            opened: "a", revealed: nil), .opened)
        XCTAssertEqual(BoardRowDensity.face(compact: true, card: "a",
                                            opened: "b", revealed: "a"), .opened)
        XCTAssertEqual(BoardRowDensity.face(compact: true, card: "a",
                                            opened: "b", revealed: nil), .line)
    }

    /// One card open at a time: a second card replaces the first, and a
    /// press on the open one closes it.
    func testOneCardIsOpenAtATime() {
        XCTAssertEqual(BoardRowDensity.toggledOpen("a", opened: nil), "a")
        XCTAssertEqual(BoardRowDensity.toggledOpen("b", opened: "a"), "b")
        XCTAssertNil(BoardRowDensity.toggledOpen("a", opened: "a"))
    }

    /// The second click of a double click opens the card window; a single
    /// click and a key press toggle.
    func testADoubleClickReachesTheCardWindow() {
        XCTAssertEqual(BoardRowDensity.press(clickCount: 1), .toggle)
        XCTAssertEqual(BoardRowDensity.press(clickCount: 0), .toggle)
        XCTAssertEqual(BoardRowDensity.press(clickCount: 2), .window)
        XCTAssertEqual(BoardRowDensity.press(clickCount: 3), .window)
    }

    /// The marks a line carries, in the order they are drawn, and none on
    /// a card with nothing in its way.
    func testLineMarksNameWhatStandsBeforeAStart() {
        XCTAssertEqual(BoardCardView.lineMarks(manualCheck: false, starting: false,
                                               queued: false, waits: false,
                                               refused: false), [])
        XCTAssertEqual(BoardCardView.lineMarks(manualCheck: true, starting: true,
                                               queued: true, waits: true,
                                               refused: true),
                       ["MANUAL CHECK", "REFUSED", "STARTING", "QUEUED", "WAITS"])
        XCTAssertEqual(BoardCardView.lineMarks(manualCheck: false, starting: false,
                                               queued: true, waits: true,
                                               refused: false),
                       ["QUEUED", "WAITS"])
    }

    /// The marks the other rows add: whose turn it is leads, then what
    /// went wrong, then what is happening.
    func testLineMarksSayWhatARunningOrFinishedCardIsDoing() {
        XCTAssertEqual(BoardCardView.lineMarks(manualCheck: true, starting: true,
                                               queued: true, waits: true,
                                               refused: true, review: true,
                                               needsYou: true, ended: true,
                                               refining: true, working: true),
                       ["MANUAL CHECK", "REVIEW", "NEEDS YOU", "ENDED", "REFUSED",
                        "STARTING", "REFINING", "WORKING", "QUEUED", "WAITS"])
        XCTAssertEqual(BoardCardView.lineMarks(manualCheck: false, starting: false,
                                               queued: false, waits: false,
                                               refused: false, needsYou: true),
                       ["NEEDS YOU"])
    }

    /// A line's jump chip reaches the refiner while the card is refining
    /// and the bound session otherwise.
    func testALinesJumpFollowsTheRefinerWhileRefining() {
        XCTAssertTrue(BoardCardView.lineJumpsToRefiner(refining: true))
        XCTAssertFalse(BoardCardView.lineJumpsToRefiner(refining: false))
    }

    // MARK: - The state

    /// A fresh board draws every row compact with nothing opened, and the
    /// Settings choice turns all of them back to tiles together.
    func testAFreshBoardIsCompactAndTheChoiceTurnsEveryRow() {
        let state = BoardState()
        for column in BoardColumn.allCases {
            XCTAssertTrue(state.rowCompact(column), column.rawValue)
        }
        XCTAssertNil(state.openedLine)
        state.compactCards = false
        for column in BoardColumn.allCases {
            XCTAssertFalse(state.rowCompact(column), column.rawValue)
        }
    }

    /// The choice arrives on the context push under `board_compact`; an
    /// absent key (an older menu bar) is compact, the default.
    func testTheChoiceDecodesFromTheContextAndDefaultsOn() {
        let base = DaemonClient.Settings()
        XCTAssertTrue(base.boardCompact)
        XCTAssertTrue(DaemonClient.Settings([:], base: base).boardCompact)
        let off = DaemonClient.Settings(["board_compact": false], base: base)
        XCTAssertFalse(off.boardCompact)
        // A push that leaves the key out keeps what is held.
        XCTAssertFalse(DaemonClient.Settings([:], base: off).boardCompact)
    }

    /// Opening a line disarms a waiting Start: moving on disarms.
    func testOpeningALineDisarmsAndReplacesTheOpenCard() {
        let state = BoardState()
        state.arm("a")
        state.toggleLine("a")
        XCTAssertNil(state.armed)
        XCTAssertEqual(state.openedLine, "a")
        state.toggleLine("b")
        XCTAssertEqual(state.openedLine, "b")
        state.toggleLine("b")
        XCTAssertNil(state.openedLine)
    }

    // MARK: - The board, hosted

    /// A window tall enough that every row is laid out: the board's outer
    /// stack is lazy and estimates the rows below the fold.
    private func hostedBoard(cards count: Int, columns: [String] = ["backlog"])
        -> (DaemonClient, BoardState, NSWindow, NSHostingView<PanelView>) {
        _ = NSApplication.shared
        let client = DaemonClient()
        var board = Board()
        board.available = true
        board.dispatchEnabled = true
        board.tools = ["claude", "codex"]
        board.counts = Dictionary(columns.map { ($0, count / columns.count) },
                                  uniquingKeysWith: { a, _ in a })
        board.cards = (0..<count).map { i in
            var card = BoardCard()
            card.id = String(format: "%032x", i + 1)
            card.title = "Backlog card \(i)"
            card.summary = String(repeating: "What this card is for, in a sentence or two. ",
                                  count: 3)
            card.column = columns[i % columns.count]
            card.project = "alpha"
            card.tool = "claude"
            card.priority = String(90 - i)
            card.position = Double(i)
            return card
        }
        var seed = Snapshot()
        seed.board = board
        client.apply(seed, normalized: "seed", via: "test")
        let state = BoardState()
        state.projectFilter = []
        state.query = ""
        // Every row unfolded, Done included (folded by default).
        state.rowFlips = BoardRowFold.decode(
            columns.filter { BoardRowFold.folded($0, flipped: []) })
        let host = NSHostingView(rootView: PanelView(client: client, keys: KeyRouter(),
                                                     focus: FocusRouter(), board: state))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 5000),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderFront(nil)
        return (client, state, window, host)
    }

    private func settle(_ host: NSView, _ seconds: Double = 0.5) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        host.layoutSubtreeIfNeeded()
    }

    private func boardHeight(in view: NSView) -> CGFloat {
        func all(_ v: NSView) -> [NSScrollView] {
            ((v as? NSScrollView).map { [$0] } ?? []) + v.subviews.flatMap(all)
        }
        return all(view).map { $0.documentView?.frame.height ?? 0 }.max() ?? 0
    }

    /// The point of the change, measured on the hosted board: the same 24
    /// cards take well under half the height as lines, and opening one
    /// grows the row by one tile, not back to a wall.
    func testLinesTakeFarLessHeightThanTilesAndOpeningOneAddsOneTile() {
        let (client, state, window, host) = hostedBoard(cards: 24)
        defer { window.close() }
        settle(host)
        let lines = boardHeight(in: host)

        state.toggleLine(String(format: "%032x", 3))
        settle(host)
        let oneOpen = boardHeight(in: host)

        // The Settings choice, the way it arrives: on the client's context.
        state.openedLine = nil
        client.context.settings.boardCompact = false
        settle(host)
        XCTAssertFalse(state.compactCards, "the choice must reach the board")
        let tiles = boardHeight(in: host)

        XCTAssertGreaterThan(lines, 24 * BoardCardView.lineHeight - 1)
        XCTAssertLessThan(lines, tiles * 0.5,
                          "lines \(lines)pt against tiles \(tiles)pt")
        XCTAssertGreaterThan(oneOpen, lines + 20, "an opened line is a tile")
        XCTAssertLessThan(oneOpen, lines + 320, "and only one tile")
    }

    /// Every row draws lines, not only Backlog: the same cards spread over
    /// all four rows are far shorter compact than standard.
    func testEveryRowDrawsLines() {
        let (client, _, window, host) = hostedBoard(
            cards: 48, columns: ["prep", "backlog", "in_progress", "done"])
        defer { window.close() }
        settle(host)
        let lines = boardHeight(in: host)
        client.context.settings.boardCompact = false
        settle(host)
        let tiles = boardHeight(in: host)
        XCTAssertGreaterThan(lines, 48 * BoardCardView.lineHeight - 1)
        XCTAssertLessThan(lines, tiles * 0.6,
                          "lines \(lines)pt against tiles \(tiles)pt")
    }

    /// START on a line arms and disarms like START on a tile: the line
    /// observes nothing, so the row has to hand it fresh facts
    /// (`BoardScrollRedrawTests.testArmingStartRedrawsThatTile`, for lines).
    func testArmingStartRedrawsThatLine() {
        let (_, state, window, host) = hostedBoard(cards: 24)
        defer { window.close() }
        settle(host)
        #if DEBUG
        BoardCardView.armedDrawn = []
        #endif
        state.arm(String(format: "%032x", 1))
        settle(host)
        #if DEBUG
        XCTAssertEqual(BoardCardView.armedDrawn, [String(format: "%032x", 1)],
                       "the armed line must draw its armed face")
        #endif
        state.disarm()
        settle(host)
        #if DEBUG
        XCTAssertEqual(BoardCardView.armedDrawn, [], "disarming must redraw the line")
        #endif
    }

    /// A compact row draws every card: 120 lines are all laid out, where
    /// 120 tiles are held back at `BoardVisible.rowTileCap`.
    func testACompactRowIsNotHeldBackAtTheTileCap() {
        let (_, _, window, host) = hostedBoard(cards: 120)
        defer { window.close() }
        settle(host)
        XCTAssertGreaterThan(boardHeight(in: host),
                             120 * BoardCardView.lineHeight - 1)
    }
}
