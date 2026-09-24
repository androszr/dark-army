import XCTest
@testable import BobPanel

/// The row heading and the row's tiles are two views writing one shared
/// hover slot, and `noteHover` clears the slot on a leave only while it
/// still holds the leaver's own value. That test only works while every
/// view writes a *distinct* value — so the heading has its own case,
/// `.heading`, and both views read the row through `row` to draw one tint.
/// Pinned here because the failure was a drag from the pinned heading down
/// into the row's top padding: tiles-enter, then heading-leave, and the
/// leave wiped the tint the tiles had just set.
final class BoardDropTargetTests: XCTestCase {
    func testHeadingAndTilesAreDistinctValuesOfOneRow() {
        XCTAssertNotEqual(BoardDropTarget.heading(.backlog),
                          BoardDropTarget.column(.backlog))
        XCTAssertEqual(BoardDropTarget.heading(.backlog).row, .backlog)
        XCTAssertEqual(BoardDropTarget.column(.backlog).row, .backlog)
        XCTAssertNil(BoardDropTarget.gap(.backlog, beforeId: "").row)
        XCTAssertNil(BoardDropTarget.card("c1").row)
    }

    @MainActor
    func testLateLeaveFromTheHeadingKeepsTheTilesTint() {
        let state = BoardState()
        state.noteHover(.heading(.backlog), hovering: true)
        XCTAssertEqual(state.dropTarget?.row, .backlog)
        // Pointer crosses from the heading into the tiles; the enter lands
        // before the leave, as it commonly does.
        state.noteHover(.column(.backlog), hovering: true)
        state.noteHover(.heading(.backlog), hovering: false)
        XCTAssertEqual(state.dropTarget, .column(.backlog),
                       "the heading's late leave must not clear the tiles' slot")
        XCTAssertEqual(state.dropTarget?.row, .backlog)
    }

    @MainActor
    func testTilesLeaveAfterHeadingEnterKeepsTheHeading() {
        let state = BoardState()
        state.noteHover(.column(.done), hovering: true)
        state.noteHover(.heading(.done), hovering: true)
        state.noteHover(.column(.done), hovering: false)
        XCTAssertEqual(state.dropTarget, .heading(.done))
    }

    @MainActor
    func testADropOnTheHeadingResolvesToTheHeading() {
        let state = BoardState()
        state.noteHover(.heading(.inProgress), hovering: true)
        state.noteHover(.heading(.inProgress), hovering: false)
        XCTAssertEqual(state.takeDrop(fallback: .column(.inProgress)),
                       .heading(.inProgress))
    }
}
