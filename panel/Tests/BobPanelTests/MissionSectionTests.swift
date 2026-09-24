import XCTest
@testable import BobPanel

/// The `mission` section decodes tolerantly: absent is `available == false`,
/// a section with only `available` defaults the rest, and a stub row
/// carries nothing but its id.
final class MissionSectionTests: XCTestCase {
    func testAnAbsentSectionIsNotAvailableAndIsCarried() throws {
        let snap = try JSONDecoder().decode(
            Snapshot.self, from: Data(#"{"generated_at": 1}"#.utf8))
        XCTAssertFalse(snap.mission.available)
        XCTAssertFalse(snap.mission.alive)
        XCTAssertEqual(snap.mission.sessionId, "")
        XCTAssertTrue(snap.carried.contains(.mission))
    }

    func testASectionWithOnlyAvailableDefaultsTheRest() throws {
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data(
            #"{"generated_at": 1, "mission": {"available": true}}"#.utf8))
        XCTAssertTrue(snap.mission.available)
        XCTAssertFalse(snap.mission.alive)
        XCTAssertFalse(snap.mission.exited)
        XCTAssertEqual(snap.mission.sessionId, "")
        XCTAssertEqual(snap.mission.root, "")
        XCTAssertEqual(snap.mission.openedAt, 0)
        XCTAssertFalse(snap.carried.contains(.mission))
    }

    func testAFullSectionDecodesEveryKey() throws {
        let section = try JSONDecoder().decode(MissionSection.self, from: Data(#"""
        {"available": true, "alive": true, "exited": false,
         "session_id": "s-1", "root": "/a/dark-army",
         "name": "Mission Control", "opened_at": 12.5, "future": 1}
        """#.utf8))
        XCTAssertEqual(section, MissionSection(
            available: true, alive: true, exited: false, sessionId: "s-1",
            root: "/a/dark-army", name: "Mission Control", openedAt: 12.5))
    }

    func testAStubAgentCarriesOnlyItsId() {
        let stub = Agent.stub(sessionId: "s-1")
        XCTAssertEqual(stub.sessionId, "s-1")
        XCTAssertEqual(stub.nickname, "")
        XCTAssertEqual(stub.lastText, "")
        XCTAssertFalse(stub.ownTerminal)
    }

    func testTheStatusWordAndTheGates() {
        XCTAssertEqual(CommRules.status(available: false, alive: false, exited: false,
                                        sessionId: "", rowState: ""), "older Mac")
        XCTAssertEqual(CommRules.status(available: true, alive: false, exited: false,
                                        sessionId: "", rowState: ""), "off")
        XCTAssertEqual(CommRules.status(available: true, alive: false, exited: true,
                                        sessionId: "s", rowState: ""), "ended")
        XCTAssertEqual(CommRules.status(available: true, alive: true, exited: false,
                                        sessionId: "", rowState: ""), "starting")
        XCTAssertEqual(CommRules.status(available: true, alive: true, exited: false,
                                        sessionId: "s", rowState: "running"), "thinking")
        XCTAssertEqual(CommRules.status(available: true, alive: true, exited: false,
                                        sessionId: "s", rowState: "sleeping"), "ready")
        XCTAssertTrue(CommRules.canAsk(status: "ready"))
        XCTAssertFalse(CommRules.canAsk(status: "starting"))
        XCTAssertTrue(CommRules.showsEnd(status: "starting"))
        XCTAssertFalse(CommRules.showsEnd(status: "off"))
        XCTAssertEqual(CommRules.line(from: "a\n\nb  c"), "a b c")
        XCTAssertEqual(CommRules.line(from: "   "), "")
        XCTAssertEqual(CommRules.line(from: String(repeating: "x", count: 2001)).count, 2000)
    }
}
