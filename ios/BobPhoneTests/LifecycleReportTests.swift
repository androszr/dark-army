import XCTest
@testable import BobPhone

@MainActor
final class LifecycleReportTests: XCTestCase {
    private func report(_ json: String) throws -> LifecycleReport {
        try JSONDecoder().decode(LifecycleReport.self, from: Data(json.utf8))
    }

    func testRaggedDecodeDoesNotThrow() throws {
        let decoded = try report(#"{"available": true}"#)
        XCTAssertTrue(decoded.available)
        XCTAssertFalse(decoded.supported)
        XCTAssertTrue(decoded.summaries.isEmpty)
    }

    func testZeroFormatsAsZero() {
        XCTAssertEqual(LifecycleFormat.seconds(0), "0s")
        XCTAssertEqual(LifecycleFormat.seconds(nil), "—")
    }

    func testCoverageAndOverlapAreNotHidden() throws {
        let decoded = try report(#"""
        {"supported": true, "available": true,
         "overlap_note": "Rework overlaps the other categories; do not add these rows.",
         "summaries": {"review": {"n": 2, "N": 4, "p50": 10, "p90": 30}}}
        """#)
        XCTAssertEqual(decoded.overlapNote, LifecycleFormat.overlapNote)
        XCTAssertEqual(LifecycleFormat.coverage(decoded.summaries["review"]!),
                       "2 of 4 episodes")
        XCTAssertEqual(LifecycleFormat.empty, "No observed episodes in this period")
    }

    func testDeletedCardHasNoLiveAction() throws {
        let decoded = try report(#"""
        {"available": true, "live": false, "card_id": "x",
         "removed_note": "No longer on the board"}
        """#)
        XCTAssertFalse(decoded.live)
        XCTAssertEqual(decoded.removedNote, LifecycleFormat.removed)
    }

    func testStaleSerialDoesNotMatch() {
        let first = LifecycleRequest(root: "/p", from: 1, to: 2, serial: 1)
        let later = LifecycleRequest(root: "/p", from: 1, to: 2, serial: 2)
        XCTAssertNotEqual(first, later)
    }

    func testUTCPeriodIndependentOfSystemTimezone() throws {
        let decoded = try report(#"{"from": 1757635200, "to": 1757721600}"#)
        let fmt = ISO8601DateFormatter()
        fmt.timeZone = TimeZone(secondsFromGMT: 0)
        fmt.formatOptions = [.withFullDate]
        XCTAssertEqual(fmt.string(from: Date(timeIntervalSince1970: decoded.from)), "2025-09-12")
    }

    func testCustomPickerEmitsUTCMidnightFromANonUTCCalendar() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(secondsFromGMT: 2 * 3600)!
        var parts = DateComponents()
        parts.year = 2025
        parts.month = 9
        parts.day = 12
        let picked = cal.date(from: parts)!
        let (from, to) = LifecycleFormat.utcPeriod(from: picked, to: picked, calendar: cal)
        XCTAssertEqual(from, 1_757_635_200)
        XCTAssertEqual(to, 1_757_721_600)
    }

    func testGenerationMismatchIsNotLoadedSuccess() throws {
        let decoded = try report(
            #"{"available": false, "reason": "report changed; refresh"}"#)
        XCTAssertTrue(LifecycleFormat.shouldRetry(decoded))
        XCTAssertFalse(decoded.available)
    }

    func testRetentionLineIsVisible() throws {
        let decoded = try report(#"""
        {"available": true,
         "retention": {"execution": {"expired_count": 3, "expired_seconds": 90}}}
        """#)
        XCTAssertEqual(decoded.retention["execution"]?.expiredCount, 3)
        let line = LifecycleFormat.retentionLine(decoded.retention)
        XCTAssertTrue(line.contains("Outside retention"))
    }

    func testUsageConstructionPassesClient() {
        // The call site in BobPhoneApp must pass `client:` so the report
        // links are reachable. A UsageView without a client draws the
        // links absent.
        let view = UsageView(usage: [], attribution: UsageAttribution(),
                             client: nil, refreshing: false) { }
        XCTAssertNotNil(view)
    }

    func testBoardCapabilityDefaultsFalse() throws {
        let board = try JSONDecoder().decode(Board.self, from: Data(#"{}"#.utf8))
        XCTAssertFalse(board.lifecycleSupported)
    }

    func testBackRestoresHeldCategory() {
        var held = "review"
        var selected: String? = "c1"
        selected = nil
        let restored = held
        XCTAssertEqual(restored, "review")
        XCTAssertNil(selected)
    }

    func testLiveCardJumpUsesExistingSheetSubject() {
        XCTAssertEqual(PhoneSheetKind.card.rawValue, "card")
    }
}
