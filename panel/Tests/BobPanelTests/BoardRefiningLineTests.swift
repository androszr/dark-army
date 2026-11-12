import XCTest
@testable import BobPanel

/// The one "refining — …" line the board tile and the card window share:
/// `BoardCard.refiningLine`, read off the daemon's `refine_state` alone.
/// `plans/2026-10-03-card-window-refining-status-line.md`.
final class BoardRefiningLineTests: XCTestCase {

    private static let waiting = "refining — waiting for the session to appear"
    private static let interview = "refining — answer the interview in the terminal"

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    func testNoRefinementDrawsNoLine() throws {
        XCTAssertEqual(try card(#"{"id":"a"}"#).refiningLine, "")
        XCTAssertEqual(try card(#"{"id":"a","refine_state":""}"#).refiningLine, "")
        XCTAssertEqual(try card(#"{"id":"a","refine_state":"ended"}"#).refiningLine, "")
    }

    func testDispatchingSaysWaitingForTheSession() throws {
        let c = try card(#"{"id":"a","refine_state":"dispatching"}"#)
        XCTAssertEqual(c.refiningLine, Self.waiting)
    }

    func testLiveSaysAnswerTheInterview() throws {
        let c = try card(#"{"id":"a","refine_state":"live"}"#)
        XCTAssertEqual(c.refiningLine, Self.interview)
    }

    func testTheStaticReadsTheTilesOptimisticPress() {
        XCTAssertEqual(BoardCard.refiningLine(state: ""), Self.waiting)
        XCTAssertEqual(BoardCard.refiningLine(state: "live"), Self.interview)
        XCTAssertEqual(BoardCard.refiningLine(state: "dispatching"), Self.waiting)
    }
}
