import XCTest
@testable import BobPhone

/// `AwaySpan` — the phone's one formatter for the away window.
///
/// The window's length is a grant made on the Mac; the phone is handed the
/// moment it closes and nothing else. These are the arithmetic and the
/// wording, which is all there is to get wrong here: a countdown that reads
/// `336h 0m` on a fortnight's grant, or a warning that only fires once it is
/// too late to do anything about it.
final class AwayWindowTests: XCTestCase {

    private let now: Double = 1_700_000_000

    private func away(_ secondsLeft: Double) -> (text: String, urgent: Bool)? {
        AwaySpan.line(leaseExpiresAt: now + secondsLeft, via: .relay, now: now)
    }

    // MARK: - the span

    func testASpanOverADayIsDaysAndHours() {
        XCTAssertEqual(AwaySpan.span(52 * 3600), "2d 4h")
        XCTAssertEqual(AwaySpan.span(14 * 86400), "14d 0h")
    }

    func testASpanUnderADayIsHoursAndMinutes() {
        XCTAssertEqual(AwaySpan.span(3 * 3600), "3h 0m")
        XCTAssertEqual(AwaySpan.span(3 * 3600 + 12 * 60), "3h 12m")
        XCTAssertEqual(AwaySpan.span(45 * 60), "45m")
    }

    func testTheBoundaryItselfIsDaysAndHours() {
        XCTAssertEqual(AwaySpan.span(AwaySpan.finalDay), "1d 0h")
        XCTAssertEqual(AwaySpan.span(AwaySpan.finalDay - 60), "23h 59m")
    }

    // MARK: - urgency

    /// The warning is the last day, not the last moment. A person on a trip
    /// needs the notice while they can still act on it.
    func testUrgentOnlyInsideTheFinalDay() {
        XCTAssertEqual(away(25 * 3600)?.urgent, false)
        XCTAssertEqual(away(23 * 3600)?.urgent, true)
        XCTAssertEqual(away(60)?.urgent, true)
    }

    func testTheAwayLineCountsDown() {
        XCTAssertEqual(away(52 * 3600)?.text,
                       "// AWAY — write access ends in 2d 4h")
    }

    // MARK: - the two edges

    /// Byte-identical to the Mac's own `LEASE_REFUSAL`, so the phone says
    /// what the daemon would say if it tried to act.
    func testALapsedWindowSaysWhatToDo() {
        let line = AwaySpan.line(leaseExpiresAt: now - 60, via: .relay,
                                 now: now)
        XCTAssertEqual(
            line?.text,
            "// away access lapsed — check in on home Wi-Fi to renew it")
        XCTAssertEqual(line?.urgent, true)
    }

    /// No window at all is no line — never "expired", which would be a claim
    /// about something that may simply not exist.
    func testNoWindowDrawsNothingAtHome() {
        XCTAssertNil(AwaySpan.line(leaseExpiresAt: 0, via: .lan, now: now))
    }

    func testAtHomeItShowsTheWindowRenewing() {
        let line = AwaySpan.line(leaseExpiresAt: now + 3600, via: .lan,
                                 now: now)
        XCTAssertEqual(line?.urgent, false)
        XCTAssertEqual(line?.text.hasPrefix("// away window renewed — good until "),
                       true)
    }

    /// A grant can be a fortnight, so a bare time of day would name the
    /// wrong day; today keeps today's shape.
    func testTheGoodUntilGainsTheDateOnceItIsNotToday() {
        // Local noon, so "an hour from now" is unambiguously still today
        // whatever time zone the test runs in.
        let noon = Calendar.current.date(
            bySettingHour: 12, minute: 0, second: 0, of: Date())!
            .timeIntervalSince1970
        let today = AwaySpan.until(noon + 3600, now: noon)
        let later = AwaySpan.until(noon + 3 * 86400, now: noon)
        XCTAssertNotEqual(today, later)
        XCTAssertTrue(later.count > today.count)
    }
}
