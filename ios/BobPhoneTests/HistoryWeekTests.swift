import XCTest
@testable import BobPhone

/// The sealed `history_week` read decodes tolerantly, and absent money is
/// never zero money on the way into the week fold.
@MainActor
final class HistoryWeekTests: XCTestCase {
    private func report(_ json: String) throws -> HistoryWeekReport {
        try JSONDecoder().decode(HistoryWeekReport.self, from: Data(json.utf8))
    }

    func testAnEmptyBodyIsNotAvailable() throws {
        let decoded = try report("{}")
        XCTAssertFalse(decoded.available)
        XCTAssertTrue(decoded.cards.isEmpty)
        XCTAssertTrue(decoded.otherDays.isEmpty)
    }

    func testMissingOtherDaysIsAnEmptyList() throws {
        let decoded = try report(#"{"available": true, "cards": []}"#)
        XCTAssertTrue(decoded.available)
        XCTAssertTrue(decoded.otherDays.isEmpty)
    }

    func testAnAbsentTokenCostIsNilAndAReportedZeroIsZero() throws {
        let decoded = try report(#"""
        {"available": true, "cards": [{"card_id": "c", "sessions": [
          {"session_id": "s", "provider": "claude", "phase": "implementation",
           "bound_at": 1758000000, "known": true, "reported_cost_usd": 0}]}]}
        """#)
        let session = try XCTUnwrap(decoded.cards.first?.sessions.first)
        XCTAssertNil(session.tokenCostUsd)
        XCTAssertEqual(session.reportedCostUsd, 0)
        let input = decoded.asLedgerInput()
        XCTAssertNil(input.cards.first?.sessions.first?.tokenCostUsd)
        XCTAssertEqual(input.cards.first?.sessions.first?.reportedCostUsd, 0)
    }

    func testAWeekWithNoPriceSaysSoRatherThanZero() throws {
        let decoded = try report(#"""
        {"available": true, "cards": [{"card_id": "c", "sessions": [
          {"session_id": "s", "provider": "grok", "phase": "implementation",
           "bound_at": 1758000000, "known": false}]}]}
        """#)
        let input = decoded.asLedgerInput()
        let now = Date(timeIntervalSince1970: 1_758_000_000)
        let picture = LedgerWeek.picture(cards: input.cards, others: input.others, now: now)
        XCTAssertEqual(picture.total, "not priced")
        XCTAssertEqual(picture.days.last?.figure, "—")
    }
}
