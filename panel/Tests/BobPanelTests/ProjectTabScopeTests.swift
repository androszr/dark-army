import XCTest
@testable import BobPanel

/// The per-project scoping rules that retired the machine-wide Finished tab:
/// the strip is the union of everything with something left to show, the
/// number beside a tab counts live rows only, and a tab's table appends the
/// finished/abandoned footers gated by their own fold.
final class ProjectTabScopeTests: XCTestCase {

    // MARK: - helpers

    private func agent(_ session: String, project: String,
                       start: Double? = nil) throws -> Agent {
        var json = #"{"session_id":"\#(session)","project":"\#(project)""#
        if let start { json += #","started_at":\#(start)"# }
        json += "}"
        return try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    private func row(_ session: String, project: String,
                     category: BobPanel.Category,
                     start: Double? = nil) throws -> SectionRow {
        SectionRow(agent: try agent(session, project: project, start: start),
                   category: category)
    }

    // MARK: - the union rule

    func testUnionIncludesAProjectWithOnlyFinishedRows() {
        let tabs = projectTabUnion(live: ["alpha"], finished: ["beta"],
                                   abandoned: [])
        XCTAssertTrue(tabs.contains(.project("beta")))
        XCTAssertTrue(tabs.contains(.project("alpha")))
    }

    func testUnionIncludesAProjectWithOnlyAbandonedRows() {
        let tabs = projectTabUnion(live: ["alpha"], finished: [],
                                   abandoned: ["gamma"])
        XCTAssertTrue(tabs.contains(.project("gamma")))
    }

    func testUnionDeduplicatesOrdersAndPutsOtherLast() {
        let tabs = projectTabUnion(live: ["beta", "alpha", ""],
                                   finished: ["alpha", "beta"],
                                   abandoned: ["beta"])
        XCTAssertEqual(tabs, [.project("alpha"), .project("beta"),
                              .project("")])
        // Never anything but a real project: the enum has no other case, and
        // the empty name is drawn as Other rather than as a Finished pool.
        XCTAssertEqual(tabs.last?.title, "Other")
        XCTAssertFalse(tabs.map(\.title).contains("Finished"))
    }

    func testUnionOfNothingIsEmpty() {
        // The tab is gone when the last tombstone ages out.
        XCTAssertEqual(projectTabUnion(live: [], finished: [], abandoned: []),
                       [])
    }

    // MARK: - the count rule

    func testAFinishedOnlyProjectCountsZeroAndRecordsNeverInflate() {
        let tabs = projectTabUnion(live: ["alpha", "alpha"],
                                   finished: ["alpha", "beta", "beta"],
                                   abandoned: ["alpha"])
        let counts = projectTabCounts(liveNames: ["alpha", "alpha"],
                                      tabs: tabs)
        XCTAssertEqual(counts[.project("alpha")], 2)
        XCTAssertEqual(counts[.project("beta")], 0)
    }

    // MARK: - the fallback

    func testAVanishedTabLandsOnItsSurvivingNeighbour() {
        let tabs: [ProjectTab] = [.project("alpha"), .project("gamma")]
        XCTAssertEqual(fallbackTab(from: .project("beta"), in: tabs),
                       .project("gamma"))
        XCTAssertEqual(fallbackTab(from: .project("zeta"), in: tabs),
                       .project("gamma"))
        XCTAssertEqual(fallbackTab(from: nil, in: tabs), .project("alpha"))
        XCTAssertNil(fallbackTab(from: .project("beta"), in: []))
    }

    // MARK: - the footer gating rule

    func testTabTableRowsOrdersLiveThenFinishedThenAbandoned() throws {
        let live = [try row("l1", project: "p", category: .running)]
        let finished = [try row("f1", project: "p", category: .finished)]
        let abandoned = [try row("a1", project: "p", category: .abandoned)]
        let rows = tabTableRows(live: live, finished: finished,
                                abandoned: abandoned, liveOpen: true,
                                finishedOpen: true, abandonedOpen: true)
        XCTAssertEqual(rows.map(\.id), ["l1", "f1", "a1"])
    }

    func testAClosedFooterIsOutOfTheList() throws {
        let live = [try row("l1", project: "p", category: .running)]
        let finished = [try row("f1", project: "p", category: .finished)]
        let abandoned = [try row("a1", project: "p", category: .abandoned)]

        let closed = tabTableRows(live: live, finished: finished,
                                  abandoned: abandoned, liveOpen: true,
                                  finishedOpen: false, abandonedOpen: false)
        XCTAssertEqual(closed.map(\.id), ["l1"])

        let finishedOnly = tabTableRows(live: live, finished: finished,
                                        abandoned: abandoned, liveOpen: true,
                                        finishedOpen: true,
                                        abandonedOpen: false)
        XCTAssertEqual(finishedOnly.map(\.id), ["l1", "f1"])

        let abandonedOnly = tabTableRows(live: live, finished: finished,
                                         abandoned: abandoned, liveOpen: true,
                                         finishedOpen: false,
                                         abandonedOpen: true)
        XCTAssertEqual(abandonedOnly.map(\.id), ["l1", "a1"])
    }
}
