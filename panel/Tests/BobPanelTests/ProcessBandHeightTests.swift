import XCTest
@testable import BobPanel

/// The rail's Active / Recently finished / Abandoned lists size their
/// viewport from the rows' measured height, never from the 24pt estimate
/// alone: rows draw 26pt, and a counted viewport under a disabled scroll
/// cut the last row mid-line.
final class ProcessBandHeightTests: XCTestCase {
    func testEmptyBandHasNoHeight() {
        XCTAssertEqual(processViewportHeight(count: 0), 0)
        XCTAssertEqual(processViewportHeight(count: 0, measured: 260), 0)
    }

    func testUnmeasuredBandUsesTheEstimate() {
        XCTAssertEqual(processViewportHeight(count: 3),
                       3 * PanelMetrics.processRow)
        XCTAssertEqual(processViewportHeight(count: 20),
                       PanelMetrics.railVisibleItems * PanelMetrics.processRow)
    }

    func testMeasuredBandUnderTheCapShowsEveryRowWhole() {
        // Seven rows drawn 26pt each: the viewport is the content, so the
        // seventh row is not cut, and scrolling may be switched off.
        let measured: CGFloat = 7 * 26
        let height = processViewportHeight(count: 7, measured: measured)
        XCTAssertEqual(height, measured)
        XCTAssertTrue(processBandFits(measured: measured, viewport: height))
    }

    func testMeasuredBandOverTheCapScrolls() {
        // Nine rows drawn 26pt each: 7.5 measured rows show, and the rest
        // is reached by scrolling, never hidden behind a disabled scroll.
        let measured: CGFloat = 9 * 26
        let height = processViewportHeight(count: 9, measured: measured)
        XCTAssertEqual(height, PanelMetrics.railVisibleItems * 26)
        XCTAssertFalse(processBandFits(measured: measured, viewport: height))
    }

    func testUnmeasuredContentIsNeverAssumedToFit() {
        let height = processViewportHeight(count: 2)
        XCTAssertFalse(processBandFits(measured: 0, viewport: height))
    }
}
