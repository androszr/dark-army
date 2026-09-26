import XCTest
@testable import BobPanel

/// `ManualCheckRules` — the one set of words and the one order both the Mac
/// window and the phone screen draw.
final class ManualCheckRulesTests: XCTestCase {
    private func entry(_ path: String, status: String, created: String,
                       outcome: String = "", malformed: Bool = false,
                       project: String = "proj") -> ManualCheckEntry {
        var e = ManualCheckEntry()
        e.path = path
        e.status = status
        e.created = created
        e.outcome = outcome
        e.malformed = malformed
        e.project = project
        return e
    }

    func testOpenFirstPartitionsAndOrdersNewestFirst() {
        let rows = [
            entry("a", status: "passed", created: "2026-09-25T10:00:00+02:00"),
            entry("b", status: "open", created: "2026-09-20T10:00:00+02:00"),
            entry("c", status: "failed", created: "2026-09-21T10:00:00+02:00"),
            entry("d", status: "open", created: "2026-09-24T10:00:00+02:00"),
            // Same instant, other zone: ties keep the daemon's order.
            entry("e", status: "open", created: "2026-09-24T08:00:00Z"),
        ]
        XCTAssertEqual(ManualCheckRules.openFirst(rows).map(\.path),
                       ["d", "e", "b", "a", "c"])
    }

    func testRowLineAndOutcomeLineWords() {
        let open = entry("a", status: "open", created: "2026-09-25T14:32:00+02:00")
        XCTAssertEqual(ManualCheckRules.rowLine(open), "2026-09-25 14:32 · proj · OPEN")
        XCTAssertEqual(ManualCheckRules.outcomeLine(open), "OPEN")
        let passed = entry("b", status: "passed",
                           created: "2026-09-25T14:32:00+02:00",
                           outcome: "looked fine")
        XCTAssertEqual(ManualCheckRules.outcomeLine(passed), "PASSED — looked fine")
        let failed = entry("c", status: "failed", created: "", outcome: "none")
        XCTAssertEqual(ManualCheckRules.outcomeLine(failed), "FAILED")
        XCTAssertEqual(ManualCheckRules.rowLine(failed), "proj · FAILED")
        let broken = entry("d", status: "open", created: "", malformed: true)
        XCTAssertEqual(ManualCheckRules.statusWord(broken), "MALFORMED")
        XCTAssertFalse(ManualCheckRules.mayRecord(broken))
        XCTAssertTrue(ManualCheckRules.mayRecord(open))
        XCTAssertFalse(ManualCheckRules.mayRecord(passed))
    }

    func testFiltersAndTheNoteClamp() {
        XCTAssertEqual(ManualCheckRules.statusFilters, ["all", "open", "passed", "failed"])
        XCTAssertEqual(ManualCheckRules.filterWord("open"), "OPEN")
        let long = String(repeating: "x", count: 500)
        XCTAssertEqual(ManualCheckRules.clampedNote(long).count, ManualCheckRules.noteLimit)
        XCTAssertEqual(ManualCheckRules.clampedNote(" two\nlines "), "two lines")
    }
}
