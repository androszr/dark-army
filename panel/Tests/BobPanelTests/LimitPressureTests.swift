import XCTest
@testable import BobPanel

/// The Mac's limit chart is the shared fold's picture. `LimitPressure` is the
/// one rule both clients carry byte for byte
/// (`host/tests/test_limit_pressure.py` pins the copies and runs it); these
/// tests prove the Mac's `HistoryLimits` *calls* it over what the daemon sent.
final class LimitPressureTests: XCTestCase {
    private func report(_ json: String) throws -> HistoryReport {
        try JSONDecoder().decode(HistoryReport.self, from: Data(json.utf8))
    }

    func testTheDecodedLimitsFoldToTheSharedPicture() throws {
        let decoded = try report(#"""
        {"limits": {"from": 0, "to": 604800, "resets": [150],
          "series": [{"ts": 100, "five_hour_pct": 10, "seven_day_pct": 5},
                     {"ts": 200, "five_hour_pct": 80, "seven_day_pct": null}]}}
        """#)
        let direct = LimitPressure.picture(
            points: [LimitPressure.Point(ts: 100, fiveHour: 10, sevenDay: 5),
                     LimitPressure.Point(ts: 200, fiveHour: 80, sevenDay: nil)],
            resets: [150], from: 0, to: 604800)
        XCTAssertNotNil(direct)
        XCTAssertEqual(decoded.limits.pressure(), direct)
        XCTAssertEqual(direct?.columns.count, 84)
        XCTAssertEqual(direct?.columns[0].fiveHour, 80)
        XCTAssertEqual(direct?.columns[0].reset, true)
        XCTAssertEqual(direct?.headline, "5h peak 80% · 7d peak 5%")
    }

    func testNothingMeasuredDrawsNoChart() throws {
        let empty = try report(#"{"limits": {"series": [], "from": 0, "to": 100}}"#)
        XCTAssertNil(empty.limits.pressure())
        let absent = try report("{}")
        XCTAssertNil(absent.limits.pressure())
    }
}
