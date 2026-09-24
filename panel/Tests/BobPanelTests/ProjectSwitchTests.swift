import XCTest
@testable import BobPanel

/// `StripPanGate`'s threshold and release semantics, pinned without a window.
final class ProjectSwitchTests: XCTestCase {

    func testThresholdIsFourPoints() {
        XCTAssertEqual(StripPanGate.threshold, 4)
    }

    func testThreePointsNeitherPansNorSuppresses() {
        let gate = StripPanGate()
        XCTAssertFalse(gate.note(dx: 3))
        XCTAssertFalse(gate.panning)
        XCTAssertFalse(gate.suppressesToggle)
    }

    func testFivePointsPansAndSuppresses() {
        let gate = StripPanGate()
        XCTAssertTrue(gate.note(dx: 5))
        XCTAssertTrue(gate.panning)
        XCTAssertTrue(gate.suppressesToggle)
    }

    func testLatchSurvivesASubsequentOnePointNote() {
        let gate = StripPanGate()
        XCTAssertTrue(gate.note(dx: 5))
        XCTAssertTrue(gate.note(dx: 1))
        XCTAssertTrue(gate.panning)
        XCTAssertTrue(gate.suppressesToggle)
    }

    func testNegativeFiveLatchesToo() {
        let gate = StripPanGate()
        XCTAssertTrue(gate.note(dx: -5))
        XCTAssertTrue(gate.panning)
        XCTAssertTrue(gate.suppressesToggle)
    }

    func testEndStopsPanningButKeepsSuppression() {
        let gate = StripPanGate()
        XCTAssertTrue(gate.note(dx: 5))
        gate.end()
        XCTAssertFalse(gate.panning)
        XCTAssertTrue(gate.suppressesToggle)
    }

    func testReleaseClearsSuppression() {
        let gate = StripPanGate()
        XCTAssertTrue(gate.note(dx: 5))
        gate.end()
        gate.release()
        XCTAssertFalse(gate.panning)
        XCTAssertFalse(gate.suppressesToggle)
    }

    func testResetReturnsBothToFalse() {
        let gate = StripPanGate()
        XCTAssertTrue(gate.note(dx: 5))
        gate.reset()
        XCTAssertFalse(gate.panning)
        XCTAssertFalse(gate.suppressesToggle)
    }
}
