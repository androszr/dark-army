import XCTest
import AppKit
@testable import BobPanel

/// The cyber cursor's rules, each from its pure seam: the cell and the
/// cursor's rect, the input box read off the character grid, what a
/// keystroke and a bucket change mean, when the timer may run, and the
/// overlay's promises (it takes no click, it draws nothing when off, an
/// episode ends). The look itself is judged on a real screen.
@MainActor
final class CyberCursorTests: XCTestCase {

    private let t0 = Date(timeIntervalSinceReferenceDate: 1_000_000)

    // MARK: - Geometry

    func testCellIsSwiftTermsFormulaForMenlo12() {
        let font = NSFont(name: "Menlo", size: 12)!
        let ct = font as CTFont
        let height = ceil(CTFontGetAscent(ct) + CTFontGetDescent(ct) + CTFontGetLeading(ct))
        let width = font.advancement(forGlyph: font.glyph(withName: "W")).width
        let cell = CyberGeometry.cell(font: font)
        XCTAssertEqual(cell.height, height)
        XCTAssertEqual(cell.width, width)
        XCTAssertEqual(CyberGeometry.cell(font: TerminalLook.font), cell)
    }

    func testCursorRectForRowZeroAndTheLastRow() {
        let cell = CGSize(width: 7, height: 15)
        let top = CyberGeometry.cursorRect(col: 0, row: 0, cell: cell, viewHeight: 600)
        XCTAssertEqual(top, CGRect(x: 0, y: 585, width: 7, height: 15))
        let last = CyberGeometry.cursorRect(col: 3, row: 39, cell: cell, viewHeight: 600)
        XCTAssertEqual(last, CGRect(x: 21, y: 0, width: 7, height: 15))
    }

    func testAFullRuleIsABoxLine() {
        XCTAssertTrue(CyberGeometry.isBoxLine(String(repeating: "─", count: 80), cols: 80))
        XCTAssertTrue(CyberGeometry.isBoxLine(String(repeating: "━", count: 80), cols: 80))
        XCTAssertTrue(CyberGeometry.isBoxLine(String(repeating: "═", count: 80), cols: 80))
    }

    func testAHalfRuleIsNotABoxLine() {
        let row = String(repeating: "─", count: 40) + String(repeating: " ", count: 40)
        XCTAssertFalse(CyberGeometry.isBoxLine(row, cols: 80))
    }

    func testARuleWithAnEndCapIsStillABoxLine() {
        let row = String(repeating: "─", count: 79) + "┤"
        XCTAssertTrue(CyberGeometry.isBoxLine(row, cols: 80))
    }

    func testTextAndBlankRowsAreNotBoxLines() {
        XCTAssertFalse(CyberGeometry.isBoxLine("> hello world, this is text", cols: 27))
        XCTAssertFalse(CyberGeometry.isBoxLine(String(repeating: " ", count: 80), cols: 80))
        XCTAssertFalse(CyberGeometry.isBoxLine("", cols: 0))
    }

    private func screen(rule: Set<Int>, rows: Int = 24, cols: Int = 80) -> [String] {
        (0..<rows).map { rule.contains($0) ? String(repeating: "─", count: cols)
                                            : "text " + String(repeating: " ", count: cols - 5) }
    }

    func testBoxLinesFoundAboveAndBelowTheCursorRow() {
        let rows = screen(rule: [20, 22])
        let lines = CyberGeometry.boxLines(rows: rows, cursorRow: 21, cols: 80)
        XCTAssertEqual(lines.above, 20)
        XCTAssertEqual(lines.below, 22)
    }

    func testBoxLinesPastReachAreIgnored() {
        let rows = screen(rule: [5, 20])
        let lines = CyberGeometry.boxLines(rows: rows, cursorRow: 10, cols: 80)
        XCTAssertNil(lines.above)
        XCTAssertNil(lines.below)
        let tall = CyberGeometry.boxLines(rows: screen(rule: [16, 23]), cursorRow: 20, cols: 80)
        XCTAssertEqual(tall.above, 16)
        XCTAssertNil(tall.below, "the lower line is 3 rows away; reach below is 2")
    }

    func testRainSpanIsTheBoxInsideOrTheCursorsRowAndNeighbours() {
        XCTAssertEqual(CyberGeometry.rainSpan(above: 20, below: 24, cursorRow: 22, rows: 40),
                       21...23)
        XCTAssertEqual(CyberGeometry.rainSpan(above: nil, below: nil, cursorRow: 10, rows: 40),
                       9...11)
        XCTAssertEqual(CyberGeometry.rainSpan(above: 3, below: nil, cursorRow: 0, rows: 40),
                       0...1)
        XCTAssertEqual(CyberGeometry.rainSpan(above: nil, below: nil, cursorRow: 39, rows: 40),
                       38...39)
    }

    // MARK: - Triggers

    func testEnterIsExactlyCarriageReturn() {
        XCTAssertEqual(CyberTriggers.classify([0x0d]), .enter)
        XCTAssertEqual(CyberTriggers.classify([0x1b, 0x0d]), .other, "ESC CR is Shift-Return")
        XCTAssertEqual(CyberTriggers.classify([0x0a]), .other)
    }

    func testOnePrintableCharacterIsTyped() {
        XCTAssertEqual(CyberTriggers.classify(Array("a".utf8)), .typed)
        XCTAssertEqual(CyberTriggers.classify(Array("ą".utf8)), .typed)
        XCTAssertEqual(CyberTriggers.classify([0x7f]), .other)
    }

    func testArrowsAndPastesAreNeither() {
        XCTAssertEqual(CyberTriggers.classify([0x1b, 0x5b, 0x41]), .other)
        XCTAssertEqual(CyberTriggers.classify(Array(String(repeating: "x", count: 40).utf8)), .other)
        XCTAssertEqual(CyberTriggers.classify([]), .other)
        XCTAssertEqual(CyberTriggers.classify([0xff, 0xfe]), .other)
    }

    func testHopTableAcrossEveryBucketPair() {
        let buckets: [String?] = [nil, "waiting", "running", "sleeping", "finished", "abandoned"]
        for from in buckets {
            for to in buckets {
                let got = CyberTriggers.hop(from: from, to: to, localEnterAt: nil, now: t0)
                var want = CyberTriggers.Hop.none
                if let f = from, let t = to, f != t {
                    if t == "running" { want = .enter }
                    else if f == "running" && (t == "waiting" || t == "sleeping") { want = .done }
                }
                XCTAssertEqual(got, want, "\(from ?? "nil") -> \(to ?? "nil")")
            }
        }
    }

    func testALocalEnterSuppressesTheSnapshotsRunningHopForThreeSeconds() {
        let recent = t0.addingTimeInterval(-1)
        XCTAssertEqual(CyberTriggers.hop(from: "waiting", to: "running", localEnterAt: recent, now: t0), .none)
        let old = t0.addingTimeInterval(-4)
        XCTAssertEqual(CyberTriggers.hop(from: "waiting", to: "running", localEnterAt: old, now: t0), .enter)
        XCTAssertEqual(CyberTriggers.hop(from: "running", to: "waiting", localEnterAt: recent, now: t0), .done,
                       "a local Enter never hides the done burst")
    }

    // MARK: - Scheduler

    func testSchedulerStopsWhenHiddenScrolledOrQuiet() {
        func interval(bursting: Bool = false, visible: Bool = true, scrolled: Bool = false,
                      reduced: Bool = false, idle: TimeInterval = 1) -> TimeInterval? {
            CyberScheduler.interval(now: t0, lastActivity: t0.addingTimeInterval(-idle),
                                    bursting: bursting, visible: visible,
                                    scrolledBack: scrolled, reduced: reduced)
        }
        XCTAssertNil(interval(visible: false), "hidden")
        XCTAssertNil(interval(bursting: true, visible: false), "hidden mid-episode")
        XCTAssertNil(interval(scrolled: true), "scrolled back")
        XCTAssertNil(interval(reduced: true), "Reduce Motion")
        XCTAssertEqual(interval(bursting: true), 1.0 / 60.0)
        XCTAssertEqual(interval(), 1.0 / 15.0)
        XCTAssertNil(interval(idle: 31), "quiet past thirty seconds")
        XCTAssertNil(interval(idle: 30), "thirty seconds is already quiet")
        XCTAssertEqual(interval(idle: 29), 1.0 / 15.0)
    }

    func testSchedulerSleepsWhileTheCaretIsHidden() {
        func interval(bursting: Bool) -> TimeInterval? {
            CyberScheduler.interval(now: t0, lastActivity: t0, bursting: bursting,
                                    visible: true, scrolledBack: false, reduced: false,
                                    caretShown: false)
        }
        XCTAssertNil(interval(bursting: false), "a spinner under a hidden caret draws nothing")
        XCTAssertEqual(interval(bursting: true), 1.0 / 60.0, "an episode still plays")
    }

    func testSchedulerWakesOnActivity() {
        let quiet = CyberScheduler.interval(now: t0, lastActivity: t0.addingTimeInterval(-60),
                                            bursting: false, visible: true,
                                            scrolledBack: false, reduced: false)
        XCTAssertNil(quiet)
        let woken = CyberScheduler.interval(now: t0, lastActivity: t0, bursting: false,
                                            visible: true, scrolledBack: false, reduced: false)
        XCTAssertNotNil(woken)
    }

    // MARK: - Episode

    private func episode(_ kind: CyberEpisode.Kind) -> CyberEpisode {
        CyberEpisode(kind: kind, start: t0, span: 21...23, sweepRows: [20, 24], cols: 80)
    }

    func testAnEpisodeEndsWithinEnterTime() {
        let e = episode(.enter)
        XCTAssertTrue(e.isActive(t0.addingTimeInterval(0.1)))
        XCTAssertTrue(e.isActive(t0.addingTimeInterval(CyberLook.enterTime - 0.01)))
        XCTAssertFalse(e.isActive(t0.addingTimeInterval(CyberLook.enterTime)))
        XCTAssertTrue(e.rainCells(t0.addingTimeInterval(CyberLook.enterTime + 0.1)).isEmpty)
        XCTAssertTrue(e.sweeps(t0.addingTimeInterval(CyberLook.enterTime + 0.1)).isEmpty)
    }

    func testRainStaysInsideTheBoxAndStartsColumnByColumn() {
        let e = episode(.enter)
        for step in stride(from: 0.05, to: 1.4, by: 0.05) {
            for cell in e.rainCells(t0.addingTimeInterval(step)) {
                XCTAssertTrue(e.span.contains(cell.row))
                XCTAssertTrue((0..<80).contains(cell.col))
                XCTAssertTrue(cell.brightness > 0 && cell.brightness <= 1)
            }
        }
        XCTAssertTrue(e.rainCells(t0.addingTimeInterval(0.01)).count
                      <= e.rainCells(t0.addingTimeInterval(0.6)).count + 40)
        XCTAssertFalse(e.rainCells(t0.addingTimeInterval(0.5)).isEmpty)
    }

    func testTheDoneSweepsMeetInTheMiddleAndFlare() {
        let e = episode(.done)
        let early = e.sweeps(t0.addingTimeInterval(0.2))
        XCTAssertEqual(early.count, 2)
        XCTAssertLessThan(early[0].x1, 40)
        XCTAssertGreaterThan(early[1].x0, 40)
        XCTAssertEqual(e.flare(t0.addingTimeInterval(0.2)), 0)
        let met = e.sweeps(t0.addingTimeInterval(CyberEpisode.meetTime))
        XCTAssertEqual(met[0].x1, 40, accuracy: 0.001)
        XCTAssertEqual(met[1].x0, 40, accuracy: 0.001)
        XCTAssertGreaterThan(e.flare(t0.addingTimeInterval(CyberEpisode.meetTime + 0.05)), 0.5)
        XCTAssertTrue(e.rainCells(t0.addingTimeInterval(0.5)).isEmpty, "done has no rain")
    }

    // MARK: - Overlay

    private func overlay() -> CyberCursorOverlay {
        let o = CyberCursorOverlay(frame: NSRect(x: 0, y: 0, width: 640, height: 400))
        o.cursorProvider = { (col: 4, row: 21, cols: 80, rows: 24) }
        o.rowTextProvider = { self.screen(rule: [20, 22]) }
        return o
    }

    func testTheOverlayTakesNoClickAnywhere() {
        let o = overlay()
        for point in [NSPoint(x: 0, y: 0), NSPoint(x: 100, y: 100), NSPoint(x: 639, y: 399),
                      NSPoint(x: -5, y: 50)] {
            XCTAssertNil(o.hitTest(point))
        }
        XCTAssertFalse(o.acceptsFirstResponder)
        XCTAssertFalse(o.isOpaque)
    }

    func testSwitchedOffIsInertAfterInput() {
        let o = overlay()
        o.setEnabled(false)
        o.needsDisplay = false
        o.noteInput([0x0d])
        o.noteInput(Array("a".utf8))
        o.noteOutput()
        o.noteBucket("waiting")
        o.noteBucket("running")
        XCTAssertFalse(o.needsDisplay)
        XCTAssertNil(o.episode)
        XCTAssertTrue(o.decodes.isEmpty)
        XCTAssertFalse(o.isTicking)
    }

    func testEnterStartsAnEpisodeThatReadsTheBoxOnce() {
        var now = t0
        let o = overlay()
        o.clock = { now }
        var reads = 0
        o.rowTextProvider = { reads += 1; return self.screen(rule: [20, 22]) }
        o.noteInput([0x0d])
        XCTAssertEqual(o.episode?.kind, .enter)
        XCTAssertEqual(o.episode?.span, 21...21)
        XCTAssertEqual(o.episode?.sweepRows, [20, 22])
        XCTAssertTrue(o.bursting)
        XCTAssertTrue(o.isTicking)
        now = t0.addingTimeInterval(CyberLook.enterTime + 0.1)
        o.noteOutput()
        XCTAssertNil(o.episode, "an episode ends within ENTER_TIME")
        XCTAssertFalse(o.bursting)
        XCTAssertEqual(reads, 1)
    }

    func testTheSnapshotHopAfterALocalEnterDoesNotPlayTwice() {
        var now = t0
        let o = overlay()
        o.clock = { now }
        o.noteBucket("waiting")
        o.noteInput([0x0d])
        let first = o.episode?.start
        now = t0.addingTimeInterval(1)
        o.noteBucket("running")
        XCTAssertEqual(o.episode?.start, first)
        now = t0.addingTimeInterval(2)
        o.noteBucket("waiting")
        XCTAssertEqual(o.episode?.kind, .done)
    }

    func testReducedMotionDrawsNoEpisodeDecodeOrTimer() {
        let o = overlay()
        o.setReduced(true)
        o.noteInput([0x0d])
        o.noteInput(Array("a".utf8))
        XCTAssertNil(o.episode)
        XCTAssertTrue(o.decodes.isEmpty)
        XCTAssertFalse(o.isTicking)
    }

    func testHiddenAndScrolledBackStopTheTimerAndWakeOnTheEdge() {
        let o = overlay()
        o.noteOutput()
        XCTAssertTrue(o.isTicking)
        o.setVisible(false)
        XCTAssertFalse(o.isTicking)
        o.noteOutput()
        XCTAssertFalse(o.isTicking, "a hidden pane is never woken by output")
        o.setVisible(true)
        XCTAssertTrue(o.isTicking)
        o.setScrolledBack(true)
        XCTAssertFalse(o.isTicking)
        o.setScrolledBack(false)
        XCTAssertTrue(o.isTicking)
    }

    func testAQuietTerminalStopsTheTimerAndAKeyWakesIt() {
        var now = t0
        let o = overlay()
        o.clock = { now }
        o.noteOutput()
        XCTAssertTrue(o.isTicking)
        now = t0.addingTimeInterval(31)
        o.noteBucket(nil)
        o.setReduced(false)
        o.setEnabled(false)
        o.setEnabled(true)
        o.noteInput([0x1b, 0x5b, 0x41])
        XCTAssertTrue(o.isTicking, "an arrow key is activity")
    }

    func testATypedLetterDecryptsForDecodeTime() {
        var now = t0
        let o = overlay()
        o.clock = { now }
        o.noteInput(Array("h".utf8))
        XCTAssertEqual(o.decodes.count, 1)
        XCTAssertEqual(o.decodes.first?.col, 4)
        XCTAssertEqual(o.decodes.first?.row, 21)
        o.noteInput([0x20])
        XCTAssertEqual(o.decodes.count, 1, "a space does not decrypt")
        now = t0.addingTimeInterval(CyberLook.decodeTime + 0.1)
        o.noteOutput()
        XCTAssertTrue(o.decodes.isEmpty)
    }

    func testNoScrollbackAtPositionZeroIsNotScrolledBack() {
        XCTAssertFalse(CyberScheduler.isScrolledBack(canScroll: false, position: 0))
        XCTAssertFalse(CyberScheduler.isScrolledBack(canScroll: true, position: 1))
        XCTAssertTrue(CyberScheduler.isScrolledBack(canScroll: true, position: 0.4))
    }

    func testTheOverlayStaysLastWhenASubviewIsAddedAfterAttach() {
        let host = NSView(frame: NSRect(x: 0, y: 0, width: 640, height: 400))
        let o = overlay()
        host.addSubview(NSView(frame: .zero))
        host.addSubview(o)
        host.addSubview(NSView(frame: NSRect(x: 0, y: 0, width: 7, height: 15)))
        XCTAssertFalse(host.subviews.last === o)
        o.raise()
        XCTAssertTrue(host.subviews.last === o)
        o.noteOutput()
        host.addSubview(NSView(frame: .zero))
        o.noteOutput()
        XCTAssertTrue(host.subviews.last === o)
    }

    func testAHiddenCursorGetsNoDecryptAndNoFallbackEpisode() {
        let o = overlay()
        o.caretPresentProvider = { false }
        o.noteInput(Array("a".utf8))
        XCTAssertTrue(o.decodes.isEmpty)
        o.rowTextProvider = { self.screen(rule: []) }
        o.noteInput([0x0d])
        XCTAssertNil(o.episode, "no box and no cursor: nothing to anchor on")
        o.rowTextProvider = { self.screen(rule: [20, 22]) }
        o.noteInput([0x0d])
        XCTAssertNotNil(o.episode, "a found box needs no cursor")
    }

    func testTheGlyphAlphabetAlwaysLightsAtLeastFourStrokes() {
        for seed in 0..<500 {
            XCTAssertGreaterThanOrEqual(CyberGlyph.mask(seed: UInt32(seed)).nonzeroBitCount, 4)
        }
    }

    func testRainColourIsConvertedFromLinearLight() {
        XCTAssertEqual(CyberLook.srgb(fromLinear: 1), 1, accuracy: 1e-9)
        XCTAssertEqual(CyberLook.srgb(fromLinear: 0), 0, accuracy: 1e-9)
        XCTAssertGreaterThan(CyberLook.srgb(fromLinear: 0.35), 0.6)
        XCTAssertEqual(CyberLook.cursorHex, 0x50e080)
    }
}
