import XCTest
@testable import BobPanel

/// Pure decisions behind the cursor overlays: whether a covered subtree still
/// advertises a pointing hand, and whether a relayout rebuilds its tracking
/// area. The class itself cannot be hovered from `swift test`.
final class CursorCostTests: XCTestCase {

    func testLiveWhenRequestedAndSubtreeAllows() {
        XCTAssertTrue(CursorAffordance.live(requested: true, subtree: true))
    }

    func testCoveredSubtreeTurnsTheHandOff() {
        XCTAssertFalse(CursorAffordance.live(requested: true, subtree: false))
    }

    func testDisabledControlStaysOffEvenWhenTheSubtreeIsLive() {
        XCTAssertFalse(CursorAffordance.live(requested: false, subtree: true))
    }

    func testRelayoutWithAnExistingAreaRechecksThePointer() {
        XCTAssertEqual(
            CursorTracking.onRelayout(hasArea: true, wantsCursor: true),
            .recheckPointer)
    }

    func testRelayoutWithoutAnAreaRebuilds() {
        XCTAssertEqual(
            CursorTracking.onRelayout(hasArea: false, wantsCursor: true),
            .rebuild)
    }

    func testRelayoutWithNoCursorAndAnAreaDoesNothing() {
        XCTAssertEqual(
            CursorTracking.onRelayout(hasArea: true, wantsCursor: false),
            .nothing)
    }

    func testRelayoutWithNoCursorAndNoAreaDoesNothing() {
        XCTAssertEqual(
            CursorTracking.onRelayout(hasArea: false, wantsCursor: false),
            .nothing)
    }
}
