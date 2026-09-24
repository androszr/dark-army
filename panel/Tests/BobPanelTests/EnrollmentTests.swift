import XCTest
@testable import BobPanel

/// The enrolment section of the snapshot, and the one distinction the whole
/// surface rests on: **absent is not empty**.
///
/// A daemon too old to carry the section says nothing, and an empty `enrolled`
/// list decoding as "nothing is enrolled" would replace a working fleet with an
/// enrolment prompt. So `available` is a field the daemon states, exactly as
/// `Board.available` is, and this pins both readings.
final class EnrollmentTests: XCTestCase {

    private func decode(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    func testAnAbsentSectionIsNotAnEmptyOne() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {}}
        """)
        XCTAssertFalse(snap.enrollment.available)
        XCTAssertTrue(snap.enrollment.enrolled.isEmpty)
        // The prompt must not be drawn: the daemon did not say.
        XCTAssertFalse(snap.enrollment.isEmpty)
    }

    func testAnEmptyListOnANewDaemonIsThePromptState() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "enrollment": {"available": true, "enrolled": [], "pending": [],
                        "other": 0}}
        """)
        XCTAssertTrue(snap.enrollment.available)
        XCTAssertTrue(snap.enrollment.isEmpty)
    }

    func testEnrolledAndPendingProjectsDecode() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "enrollment": {"available": true,
                        "enrolled": [{"root": "/a/proj", "label": "proj"}],
                        "pending": [{"root": "/b/other", "label": "other",
                                     "last_seen": 1724900000.5}],
                        "other": 3}}
        """)
        XCTAssertEqual(snap.enrollment.enrolled.map(\.label), ["proj"])
        XCTAssertEqual(snap.enrollment.enrolled.first?.root, "/a/proj")
        XCTAssertEqual(snap.enrollment.pending.first?.label, "other")
        XCTAssertEqual(snap.enrollment.pending.first?.lastSeen ?? 0,
                       1724900000.5, accuracy: 0.001)
        XCTAssertEqual(snap.enrollment.other, 3)
        // Something is enrolled, so the prompt stays away.
        XCTAssertFalse(snap.enrollment.isEmpty)
    }

    func testARaggedProjectRowDoesNotThrow() throws {
        // `last_seen` exists only on pending rows, and a future daemon may add
        // fields. One absent key must never blank the panel.
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "enrollment": {"available": true,
                        "enrolled": [{"root": "/a/proj"},
                                     {"label": "nameless"}],
                        "a_future_field": 7}}
        """)
        XCTAssertEqual(snap.enrollment.enrolled.count, 2)
        XCTAssertEqual(snap.enrollment.enrolled[0].label, "")
        XCTAssertEqual(snap.enrollment.enrolled[1].root, "")
        XCTAssertTrue(snap.enrollment.pending.isEmpty)
    }

    func testAWrongShapedSectionFallsBackRatherThanThrowing() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "enrollment": {"available": "yes", "enrolled": "nope"}}
        """)
        XCTAssertFalse(snap.enrollment.available)
        XCTAssertTrue(snap.enrollment.enrolled.isEmpty)
        XCTAssertFalse(snap.enrollment.isEmpty)
    }
}
