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

    // MARK: - Claude's limit pressure

    func testAbsentLimitsGiveAnEmptyReportAndNoPicture() throws {
        let decoded = try report(#"{"available": true, "cards": []}"#)
        XCTAssertTrue(decoded.limits.series.isEmpty)
        XCTAssertNil(decoded.limits.pressure())
    }

    func testANullFiveHourReadingStaysNil() throws {
        let decoded = try report(#"""
        {"available": true, "limits": {"from": 0, "to": 86400,
          "series": [{"ts": 100, "five_hour_pct": null, "seven_day_pct": 40}],
          "resets": []}}
        """#)
        XCTAssertNil(decoded.limits.series[0].fiveHourPct)
        XCTAssertEqual(decoded.limits.series[0].sevenDayPct, 40)
        let picture = decoded.limits.pressure()
        XCTAssertNil(picture?.fiveHourPeak)
        XCTAssertEqual(picture?.sevenDayPeak, 40)
        XCTAssertEqual(picture?.headline, "7d peak 40%")
    }

    func testAFullBlockGivesTheSharedFoldsPicture() throws {
        let decoded = try report(#"""
        {"available": true, "limits": {"from": 0, "to": 604800,
          "series": [{"ts": 100, "five_hour_pct": 10, "seven_day_pct": 5},
                     {"ts": 200, "five_hour_pct": 80, "seven_day_pct": 6}],
          "resets": [150]}}
        """#)
        let direct = LimitPressure.picture(
            points: [LimitPressure.Point(ts: 100, fiveHour: 10, sevenDay: 5),
                     LimitPressure.Point(ts: 200, fiveHour: 80, sevenDay: 6)],
            resets: [150], from: 0, to: 604800)
        XCTAssertEqual(decoded.limits.pressure(), direct)
        XCTAssertEqual(direct?.columns.count, 84)
        XCTAssertEqual(direct?.columns[0].fiveHour, 80)
        XCTAssertEqual(direct?.columns[0].reset, true)
    }
}
