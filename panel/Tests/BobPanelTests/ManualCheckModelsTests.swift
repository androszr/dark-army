import XCTest
@testable import BobPanel

/// The Checks section's models decode through tolerant helpers: an absent
/// key is its default, never a thrown error that blanks the list.
final class ManualCheckModelsTests: XCTestCase {
    func testAbsentKeysDecodeToDefaults() throws {
        let report = try JSONDecoder().decode(
            ManualChecksReport.self, from: Data("{}".utf8))
        XCTAssertFalse(report.supported)
        XCTAssertFalse(report.available)
        XCTAssertEqual(report.checks, [])
        XCTAssertFalse(report.truncated)
        XCTAssertEqual(report.reason, "")
        let entry = try JSONDecoder().decode(
            ManualCheckEntry.self, from: Data("{}".utf8))
        XCTAssertEqual(entry.path, "")
        XCTAssertEqual(entry.cardId, "")
        XCTAssertFalse(entry.malformed)
        XCTAssertEqual(entry.problem, "")
        let document = try JSONDecoder().decode(
            ManualCheckDocument.self, from: Data("{}".utf8))
        XCTAssertFalse(document.available)
        XCTAssertEqual(document.text, "")
    }

    func testTheWireNamesDecode() throws {
        let json = Data(#"""
        {"supported": true, "available": true, "root": "", "truncated": false,
         "checks": [{"path": "/p/manual-check/a/check.md", "title": "T",
                     "card": "Fit", "card_id": "c1", "project": "p",
                     "check": "fits", "created": "2026-09-25T14:32:00+02:00",
                     "status": "passed", "outcome": "fine",
                     "checked_at": "2026-09-25T15:00:00+02:00",
                     "steps_preview": "1. Open.", "malformed": false,
                     "problem": "", "unknown": 3}]}
        """#.utf8)
        let report = try JSONDecoder().decode(ManualChecksReport.self, from: json)
        XCTAssertTrue(report.supported && report.available)
        let entry = try XCTUnwrap(report.checks.first)
        XCTAssertEqual(entry.cardId, "c1")
        XCTAssertEqual(entry.checkedAt, "2026-09-25T15:00:00+02:00")
        XCTAssertEqual(entry.stepsPreview, "1. Open.")
        XCTAssertEqual(entry.id, "/p/manual-check/a/check.md")
    }

    func testTheCardDecodesItsCheckPathAsEmptyWhenAbsent() throws {
        let json = Data(#"{"id": "c1", "title": "t", "column_name": "done"}"#.utf8)
        let card = try JSONDecoder().decode(BoardCard.self, from: json)
        XCTAssertEqual(card.manualCheckPath, "")
        let flagged = Data(#"""
        {"id": "c1", "title": "t", "column_name": "done",
         "manual_steps": "1. look", "manual_check_path": "/p/manual-check/a/check.md"}
        """#.utf8)
        let with = try JSONDecoder().decode(BoardCard.self, from: flagged)
        XCTAssertEqual(with.manualCheckPath, "/p/manual-check/a/check.md")
    }

    /// Each project's Checks row opens that project's checks, not one
    /// all-projects window behind every row.
    func testEachProjectsChecksRowCarriesItsRoot() {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/a/one", label: "one"),
                               EnrolledProject(root: "/a/two", label: "two")]
        let rows = SettingsMenuModel.flattened(SettingsMenuModel.projectRows(
            enrollment, unenrolArmed: nil, pack: DaemonClient.AgentPack(),
            packArmed: nil, stopSyncArmed: nil))
        let roots: [String] = rows.compactMap {
            if case .custom(.manualChecks(let root)) = $0.kind { return root }
            return nil
        }
        XCTAssertEqual(roots, ["/a/one", "/a/two"])
    }
}
