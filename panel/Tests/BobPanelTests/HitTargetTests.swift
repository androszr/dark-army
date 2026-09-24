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
        XCTAssertEqual(AlarmOutline.vPad, 4)
    }

    func testStrokeDrawsTheBorder() {
        XCTAssertEqual(AlarmOutline.stroke, 1)
    }

    func testHeldInkIsUntouched() {
        XCTAssertEqual(AlarmOutline.heldInk, 0.4)
    }

    func testHeldEdgeIsUntouched() {
        XCTAssertEqual(AlarmOutline.heldEdge, 0.3)
    }
}
