import XCTest
@testable import BobPanel

final class ProjectFactsTests: XCTestCase {

    func testComposePicksTheFirstNonEmptyBranch() {
        let line = ProjectFactsLine.compose(
            branches: ["", "main", "dev"], running: 1, facts: nil, now: 0)
        XCTAssertEqual(line.branch, "main")
    }

    func testComposeOmitsABranchWhenEveryEntryIsEmpty() {
        let line = ProjectFactsLine.compose(
            branches: ["", ""], running: 1, facts: nil, now: 0)
        XCTAssertNil(line.branch)
    }

    func testComposeSaysNowWhileAnythingIsRunning() {
        let facts = ProjectFacts(project: "p", sessions: 10, lastActive: 100)
        let line = ProjectFactsLine.compose(
            branches: ["main"], running: 2, facts: facts, now: 1_000)
        XCTAssertEqual(line.lastActive, "now")
        XCTAssertEqual(line.running, 2)
    }

    func testComposeFormatsAgoWhenNothingIsRunning() {
        let facts = ProjectFacts(project: "p", sessions: 4, lastActive: 1_000)
        let line = ProjectFactsLine.compose(
            branches: [], running: 0, facts: facts, now: 1_180)
        XCTAssertEqual(line.lastActive, Format.duration(180) + " ago")
    }

    func testComposeOmitsHistoryWhenFactsAreMissing() {
        let line = ProjectFactsLine.compose(
            branches: ["main"], running: 0, facts: nil, now: 1_000)
        XCTAssertNil(line.lastActive)
        XCTAssertEqual(line.segments, ["main", "0 running"])
    }

    func testVisibleOnlyForAProjectOnTheAgentsTab() {
        XCTAssertTrue(projectFactsVisible(
            tab: .agents, resolvedTab: .project("dark-army")))
        XCTAssertFalse(projectFactsVisible(tab: .agents, resolvedTab: nil))
        XCTAssertFalse(projectFactsVisible(
            tab: .inbox, resolvedTab: .project("dark-army")))
    }

    func testAgentsDecodeFillsProjectFacts() throws {
        let agents = try JSONDecoder().decode(Agents.self, from: Data("""
        {"project_facts": [{"project": "bob", "sessions": 148,
                            "last_active": 1700000000}]}
        """.utf8))
        XCTAssertEqual(agents.projectFacts.count, 1)
        XCTAssertEqual(agents.projectFacts[0].project, "bob")
        XCTAssertEqual(agents.projectFacts[0].sessions, 148)
        XCTAssertEqual(agents.projectFacts[0].lastActive, 1_700_000_000)
    }

    func testAgentsDecodeWithoutTheKeyYieldsEmptyFacts() throws {
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {}}
        """.utf8))
        XCTAssertTrue(snap.agents.projectFacts.isEmpty)
        XCTAssertEqual(snap.generatedAt, 1)
    }

    func testSegmentsAreThreeFactsOnOneLine() {
        let facts = ProjectFacts(project: "p", sessions: 148, lastActive: 100)
        let full = ProjectFactsLine.compose(
            branches: ["main"], running: 2, facts: facts, now: 100)
        XCTAssertEqual(full.segments, ["main", "2 running", "active now"])
        XCTAssertFalse(full.segments.contains { $0.contains("sessions") })
        XCTAssertLessThanOrEqual(full.segments.count, 3)

        let thin = ProjectFactsLine.compose(
            branches: [], running: 0, facts: nil, now: 0)
        XCTAssertEqual(thin.segments, ["0 running"])
    }

    func testJoinedLineUsesTheRailSeparator() {
        let facts = ProjectFacts(project: "p", sessions: 148, lastActive: 100)
        let line = ProjectFactsLine.compose(
            branches: ["main"], running: 2, facts: facts, now: 100)
        XCTAssertEqual(line.segments.joined(separator: " · "),
                       "main · 2 running · active now")
    }

    func testALongBranchClampsWithoutDroppingASegment() {
        let branch = String(repeating: "a", count: 60)
        let facts = ProjectFacts(project: "p", sessions: 1, lastActive: 100)
        let line = ProjectFactsLine.compose(
            branches: [branch], running: 2, facts: facts, now: 100)
        XCTAssertEqual(line.segments[0].count, 41)
        XCTAssertTrue(line.segments[0].hasSuffix("…"))
        XCTAssertTrue(line.segments.contains { $0.contains("running") })
        XCTAssertEqual(line.segments.last, "active now")
    }
}
