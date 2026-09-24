import XCTest
import SwiftUI
@testable import BobPanel

final class NeedsYouCardsTests: XCTestCase {
    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    func testAbsentFlagDecodesFalse() throws {
        XCTAssertFalse(try card(#"{"id":"a"}"#).needsYou)
    }

    func testTrueFlagDecodesTrue() throws {
        XCTAssertTrue(try card(#"{"id":"a","needs_you":true}"#).needsYou)
    }

    func testMalformedFlagDecodesFalse() throws {
        XCTAssertFalse(try card(#"{"id":"a","needs_you":"yes"}"#).needsYou)
    }

    func testACardWithEndedLinkButNoFlagIsNotListed() throws {
        var board = Board()
        board.cards = [
            try card(#"{"id":"a","needs_you":true}"#),
            try card(#"{"id":"b","link_state":"ended","column_name":"in_progress"}"#)
        ]
        XCTAssertEqual(board.needsYouCards().map(\.id), ["a"])
    }

    func testRowsUseFinishedClockThenID() throws {
        var board = Board()
        board.cards = [
            try card(#"{"id":"b","needs_you":true,"created_at":2}"#),
            try card(#"{"id":"c","needs_you":true,"session_ended_at":1}"#),
            try card(#"{"id":"a","needs_you":true,"session_ended_at":2}"#)
        ]
        XCTAssertEqual(board.needsYouCards().map(\.id), ["a", "b", "c"])
    }

    func testSendBackWithPlanGoesToBacklog() throws {
        XCTAssertEqual(try card(#"{"plan_path":"plans/work.md"}"#).sendBackColumn,
                       .backlog)
    }

    func testSendBackWithoutPlanGoesToPrep() throws {
        XCTAssertEqual(try card("{}").sendBackColumn, .prep)
    }

    func testDoneArmsOnlyALiveLink() throws {
        XCTAssertTrue(try card(#"{"link_state":"live"}"#).doneArms)
        XCTAssertFalse(try card(#"{"link_state":"ended"}"#).doneArms)
    }

    // Mark done left the inbox (20 Sep 2026): the inbox routes and does
    // not answer, so the arm-then-confirm on a live link is the card
    // window's alone.

}
