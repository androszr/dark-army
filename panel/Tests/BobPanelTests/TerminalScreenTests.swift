import XCTest
@testable import BobPanel

/// `TerminalScreen.apply` is the renderer's whole memory: a full reset on a
/// new session, a size change or an `overflowed` frame, else only the rows
/// the frame carries. Pure, so it is table-tested here rather than by eye.
final class TerminalScreenTests: XCTestCase {
    private func frame(session: String = "s1", cols: Int = 4, rows: Int = 2,
                       revision: Int = 1, changes: [TerminalRowChange] = [],
                       overflowed: Bool = false, available: Bool = true) -> TerminalFrame {
        var f = TerminalFrame()
        f.available = available
        f.session = session
        f.cols = cols
        f.rows = rows
        f.revision = revision
        f.rowsChanged = changes
        f.overflowed = overflowed
        f.cursor = [1, 0]
        return f
    }

    func testAFirstFrameLaysOutTheGridAndKeepsUntouchedRowsBlank() {
        var screen = TerminalScreen()
        screen.apply(frame(changes: [TerminalRowChange(y: 1, runs: [TerminalRun(text: "hi")])]))
        XCTAssertEqual(screen.rows, 2)
        XCTAssertEqual(screen.text(0), "")
        XCTAssertEqual(screen.text(1), "hi")
        XCTAssertEqual(screen.revision, 1)
        XCTAssertEqual(screen.cursor.0, 1)
    }

    func testAnUnchangedFrameLeavesRowsAlone() {
        var screen = TerminalScreen()
        screen.apply(frame(changes: [TerminalRowChange(y: 0, runs: [TerminalRun(text: "a")])]))
        screen.apply(frame(revision: 2))
        XCTAssertEqual(screen.text(0), "a")
        XCTAssertEqual(screen.revision, 2)
    }

    func testAnOverflowedFrameResetsBeforeApplying() {
        var screen = TerminalScreen()
        screen.apply(frame(changes: [TerminalRowChange(y: 0, runs: [TerminalRun(text: "old")])]))
        screen.apply(frame(revision: 5, changes: [TerminalRowChange(y: 1, runs: [TerminalRun(text: "new")])],
                           overflowed: true))
        XCTAssertEqual(screen.text(0), "")
        XCTAssertEqual(screen.text(1), "new")
    }

    func testANewSessionOrSizeResets() {
        var screen = TerminalScreen()
        screen.apply(frame(changes: [TerminalRowChange(y: 0, runs: [TerminalRun(text: "a")])]))
        screen.apply(frame(session: "s2", revision: 2))
        XCTAssertEqual(screen.text(0), "")
        XCTAssertEqual(screen.session, "s2")
        screen.apply(frame(session: "s2", cols: 8, rows: 3, revision: 3))
        XCTAssertEqual(screen.lines.count, 3)
    }

    func testAnUnavailableFrameEmptiesTheScreen() {
        var screen = TerminalScreen()
        screen.apply(frame(changes: [TerminalRowChange(y: 0, runs: [TerminalRun(text: "a")])]))
        screen.apply(frame(available: false))
        XCTAssertEqual(screen, TerminalScreen())
    }

    func testTheWireShapeDecodesPositionally() throws {
        let json = """
        {"available": true, "session": "s1", "revision": 3, "cols": 5, "rows": 1,
         "rows_changed": [[0, [["ab", -1, -1, 1], ["c", 196, 16777216, 0]]]],
         "more": false, "overflowed": false, "unchanged": false}
        """.data(using: .utf8)!
        let f = try JSONDecoder().decode(TerminalFrame.self, from: json)
        XCTAssertEqual(f.rowsChanged.count, 1)
        XCTAssertEqual(f.rowsChanged[0].runs[0], TerminalRun(text: "ab", attrs: 1))
        XCTAssertEqual(f.rowsChanged[0].runs[1].fg, 196)
        XCTAssertEqual(f.rowsChanged[0].runs[1].bg, 0x1000000)
        // Every key absent decodes to the defaults, never a throw.
        let bare = try JSONDecoder().decode(TerminalFrame.self, from: "{}".data(using: .utf8)!)
        XCTAssertFalse(bare.available)
        XCTAssertTrue(bare.rowsChanged.isEmpty)
        XCTAssertFalse(bare.unchanged)
    }

    func testColsFillThePaneMinusPaddingAndClamp() {
        let atRail = PanelMetrics.terminalCols(forWidth: PanelMetrics.width)
        let atWide = PanelMetrics.terminalCols(forWidth: 1200)
        XCTAssertEqual(PanelMetrics.terminalCols, atRail)
        XCTAssertGreaterThan(atWide, atRail)
        XCTAssertEqual(PanelMetrics.terminalCols(forWidth: 10), 40)
        XCTAssertEqual(PanelMetrics.terminalCols(forWidth: 10_000), 400)
    }

    /// A `GeometryReader` in an unbounded context reports infinity, and
    /// `Int(_:)` of an infinite `Double` traps. The smallest grid, never a
    /// crash.
    func testANonFiniteDimensionIsTheSmallestGrid() {
        XCTAssertEqual(PanelMetrics.terminalCols(forWidth: .infinity), 40)
        XCTAssertEqual(PanelMetrics.terminalCols(forWidth: .nan), 40)
        XCTAssertEqual(PanelMetrics.terminalRows(forHeight: .infinity), 8)
        XCTAssertEqual(PanelMetrics.terminalRows(forHeight: .nan), 8)
    }

    func testRowsFillTheTerminalRegionMinusChromeAndClamp() {
        let chrome = PanelMetrics.terminalHeader + PanelMetrics.terminalPadY * 2
        let atThirty = PanelMetrics.terminalRows(forHeight: chrome + PanelMetrics.terminalRowHeight * 30)
        let atTall = PanelMetrics.terminalRows(forHeight: 800)
        XCTAssertEqual(atThirty, 30)
        XCTAssertGreaterThan(atTall, atThirty)
        XCTAssertEqual(PanelMetrics.terminalRows(forHeight: 10), 8)
        XCTAssertEqual(PanelMetrics.terminalRows(forHeight: 10_000), 200)
    }

    func testApplyReplacesHistoryAndKeepsItWhenTheKeyIsOmitted() {
        var screen = TerminalScreen()
        var withHistory = frame(rows: 1, changes: [])
        withHistory.history = [[TerminalRun(text: "old")]]
        screen.apply(withHistory)
        XCTAssertEqual(screen.history.count, 1)
        XCTAssertEqual(screen.history[0][0].text, "old")
        var incremental = frame(revision: 2)
        incremental.history = nil
        screen.apply(incremental)
        XCTAssertEqual(screen.history[0][0].text, "old")
        var cleared = frame(revision: 3)
        cleared.history = []
        screen.apply(cleared)
        XCTAssertEqual(screen.history, [])
    }
}
