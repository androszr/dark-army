import XCTest
@testable import BobPanel

/// The outlined box is the button, only while the button can be pressed, and
/// the numbers that draw that box are the same numbers the hit shape is
/// built from.
final class HitTargetTests: XCTestCase {

    func testLiveButtonOwnsItsBox() {
        XCTAssertTrue(AlarmOutline.boxTakesClick(isEnabled: true))
    }

    func testHeldButtonLetsTheCardBehindItHaveTheClick() {
        XCTAssertFalse(AlarmOutline.boxTakesClick(isEnabled: false))
    }

    func testHorizontalPadDrawsTheBox() {
        XCTAssertEqual(AlarmOutline.hPad, 10)
    }

    func testVerticalPadDrawsTheBox() {
        XCTAssertEqual(AlarmOutline.vPad, 8)
    }

    func testStrokeDrawsTheBorder() {
        XCTAssertEqual(AlarmOutline.stroke, 1)
    }

    /// Raised from 0.4 on 25 Sep 2026: alarm ink at 0.4 measured 1.79:1 on
    /// the ground, unreadable; 0.65 reads (`host/tests/test_theme_contrast.py`).
    func testHeldInkIsReadable() {
        XCTAssertEqual(AlarmOutline.heldInk, 0.65)
    }

    func testHeldEdgeIsReadable() {
        XCTAssertEqual(AlarmOutline.heldEdge, 0.45)
    }
}
