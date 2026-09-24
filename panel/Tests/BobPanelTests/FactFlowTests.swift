import XCTest
import SwiftUI
@testable import BobPanel

/// The facts grid's arithmetic, and the probe that killed the panel.
///
/// SwiftUI hands a `Layout` infinite and zero proposals as a matter of course.
/// `Int(_:)` of an infinite or NaN `Double` is a trap in Swift, so the column
/// count had to be made *total* before the detail pane could be opened at all
/// (6 Sep 2026: selecting any agent brought the whole window down).
final class FactFlowTests: XCTestCase {

    private func cols(_ width: CGFloat) -> Int {
        FactFlow.columnCount(width: width, unit: 120, spacing: 8)
    }

    func testAnInfiniteWidthIsOneColumnRatherThanACrash() {
        XCTAssertEqual(cols(.infinity), 1)
        XCTAssertEqual(cols(-.infinity), 1)
    }

    func testANaNWidthIsOneColumn() {
        XCTAssertEqual(cols(.nan), 1)
    }

    func testAnEmptyOrNegativeWidthIsOneColumn() {
        XCTAssertEqual(cols(0), 1)
        XCTAssertEqual(cols(-40), 1)
    }

    func testOrdinaryWidthsDivideIntoEqualColumns() {
        XCTAssertEqual(cols(120), 1)
        XCTAssertEqual(cols(247), 1)
        XCTAssertEqual(cols(248), 2)   // 120 + 8 + 120
        XCTAssertEqual(cols(376), 3)
    }

    func testIdealWidthLaysEveryTileInOneRow() {
        XCTAssertEqual(FactFlow.idealWidth(spans: [1, 1], unit: 120, spacing: 8),
                       248)
        // A two-column tile asks for two columns' worth.
        XCTAssertEqual(FactFlow.idealWidth(spans: [2], unit: 120, spacing: 8),
                       248)
    }

    func testIdealWidthOfNothingIsOneColumn() {
        XCTAssertEqual(FactFlow.idealWidth(spans: [], unit: 120, spacing: 8), 120)
    }

    /// The ideal width is the width that yields exactly that many columns —
    /// the two halves of the answer to an unbounded proposal agree.
    func testIdealWidthRoundTripsThroughTheColumnCount() {
        let ideal = FactFlow.idealWidth(spans: [1, 1, 2], unit: 120, spacing: 8)
        XCTAssertEqual(FactFlow.columnCount(width: ideal, unit: 120, spacing: 8), 4)
    }
}
