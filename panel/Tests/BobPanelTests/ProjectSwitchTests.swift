import XCTest
@testable import BobPanel

/// `StripPanGate`'s threshold and release semantics, pinned without a window.
final class ProjectSwitchTests: XCTestCase {

    /// The strip names only projects with an open VS Code window: a card's
    /// project whose window is closed is not a chip.
    func testSwitchNamesAreOpenWindowsOnly() throws {
        let board = try JSONDecoder().decode(Board.self, from: Data(#"""
            {"available":true,
             "cards":[{"id":"a","project":"bob-main","column_name":"backlog"},
                      {"id":"b","project":"store-sim","column_name":"prep"}],
             "projects":[{"name":"store-sim","root":"/p/store-sim"},
                         {"name":"","root":"/p/blank"},
                         {"name":"Dark-army","root":"/p/dark-army"},
                         {"name":"store-sim","root":"/p/store-sim-2"}]}
            """#.utf8))
        XCTAssertEqual(board.openProjectNames, ["Dark-army", "store-sim"])
        XCTAssertTrue(board.projectNames.contains("bob-main"))
    }

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
