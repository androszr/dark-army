import XCTest
@testable import BobPanel

/// "Start the work by itself once the plan lands", on the decode side.
///
/// The one direction that matters: **absent is off**. A daemon older than the
/// `start_when_planned` column sends neither key, and decoding either as true
/// would make every card look armed and draw a control the daemon would 400.
final class AutoStartTickTests: XCTestCase {
    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func board(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    func testOneIsOn() throws {
        let c = try card(#"{"id":"a","start_when_planned":"1"}"#)
        XCTAssertTrue(c.startWhenPlanned)
    }

    func testEmptyIsOff() throws {
        let c = try card(#"{"id":"a","start_when_planned":""}"#)
        XCTAssertFalse(c.startWhenPlanned)
    }

    func testAbsentIsOff() throws {
        let c = try card(#"{"id":"a"}"#)
        XCTAssertFalse(c.startWhenPlanned)
    }

    func testAnUnexpectedValueIsOff() throws {
        // The store normalises to `'1'` / `''`, so anything else is a daemon
        // that does not mean what this client would read into it.
        let c = try card(#"{"id":"a","start_when_planned":"true"}"#)
        XCTAssertFalse(c.startWhenPlanned)
    }
}
