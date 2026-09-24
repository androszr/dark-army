import XCTest
@testable import BobPanel

/// Compact AGE rungs and the dash-when-undated rule. Injected `now` — this
/// is the seam, not a window.
final class FleetAgeTests: XCTestCase {

    func testUndatedNilIsAnEmDash() {
        XCTAssertEqual(FleetAge.text(startedAt: nil, now: 1_000), "—")
        XCTAssertEqual(FleetAge.spoken(startedAt: nil, now: 1_000), "")
    }

    func testUndatedZeroIsAnEmDash() {
        XCTAssertEqual(FleetAge.text(startedAt: 0, now: 1_000), "—")
        XCTAssertEqual(FleetAge.spoken(startedAt: 0, now: 1_000), "")
    }

    func testUndatedNegativeIsAnEmDash() {
        XCTAssertEqual(FleetAge.text(startedAt: -1, now: 1_000), "—")
        XCTAssertEqual(FleetAge.spoken(startedAt: -1, now: 1_000), "")
    }

    func testZeroSeconds() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 1_000), "0s")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 1_000), "0 seconds")
    }

    func testOneSecondIsSingular() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 1_001), "1s")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 1_001), "1 second")
    }

    func testTwelveSeconds() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 1_012), "12s")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 1_012), "12 seconds")
    }

    func testFiftyNineSecondsStaySeconds() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 1_059), "59s")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 1_059), "59 seconds")
    }

    func testSixtySecondsBecomeOneMinute() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 1_060), "1m")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 1_060), "1 minute")
    }

    func testOneHundredNineteenSecondsStayOneMinute() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 1_119), "1m")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 1_119), "1 minute")
    }

    func testFiftyNineMinutes() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 4_599), "59m")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 4_599), "59 minutes")
    }

    func testOneHour() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 4_600), "1h")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 4_600), "1 hour")
    }

    func testMinutesPastTheHourAreDropped() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 4_661), "1h")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 4_661), "1 hour")
    }

    func testTwoHours() {
        XCTAssertEqual(FleetAge.text(startedAt: 1_000, now: 8_200), "2h")
        XCTAssertEqual(FleetAge.spoken(startedAt: 1_000, now: 8_200), "2 hours")
    }

    func testClockSkewIsNeverNegative() {
        XCTAssertEqual(FleetAge.text(startedAt: 2_000, now: 1_000), "0s")
        XCTAssertEqual(FleetAge.spoken(startedAt: 2_000, now: 1_000), "0 seconds")
    }

    func testWidthIsThirtyTwo() {
        XCTAssertEqual(FleetAge.width, 32)
    }
}
