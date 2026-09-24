import XCTest
@testable import BobPanel

/// A row that says where it came from, and the join it must not break.
///
/// Two things are pinned here. The three origin fields are **flat strings**
/// decoded tolerantly, because Swift's synthesized `Decodable` throws on a
/// missing key even where the property has a default — one absent field must
/// never blank the panel. And `SubagentRow.qualified` carries the parent
/// card's name while `label` stays the bare stage name, because `label` is a
/// `Set` key matched against a card's declared workflow to decide which stage
/// is running: qualifying it there would silently stop every card
/// highlighting its running step.
final class OriginRowTests: XCTestCase {

    private func agent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    private func row(_ json: String) throws -> SubagentRow {
        try JSONDecoder().decode(SubagentRow.self, from: Data(json.utf8))
    }

    // ── the three flat fields ────────────────────────────────────────────

    func testAllThreePresentDecode() throws {
        let a = try agent("""
        {"session_id": "s1", "origin_by": "card-start", "origin_card": "c1",
         "origin_line": "Dark Army started this to build Rename the ladder"}
        """)
        XCTAssertEqual(a.originBy, "card-start")
        XCTAssertEqual(a.originCard, "c1")
        XCTAssertEqual(a.originLine,
                       "Dark Army started this to build Rename the ladder")
    }

    /// An older daemon, and every session a person started themselves. Absent
    /// draws nothing at all, which is the screen as it was.
    func testAllThreeAbsentDecodeEmpty() throws {
        let a = try agent(#"{"session_id": "s1"}"#)
        XCTAssertEqual(a.originBy, "")
        XCTAssertEqual(a.originCard, "")
        XCTAssertEqual(a.originLine, "")
    }

    func testOnePresentDoesNotThrow() throws {
        let a = try agent(#"{"session_id": "s1", "origin_card": "c1"}"#)
        XCTAssertEqual(a.originCard, "c1")
        XCTAssertEqual(a.originLine, "")
    }

    /// The panel never composes the sentence — it draws what the daemon sent.
    /// A wrong `by` with a good line still draws the line.
    func testTheLineIsDrawnVerbatimAndNotDerived() throws {
        let a = try agent("""
        {"session_id": "s1", "origin_by": "", "origin_line": "whatever Dark Army said"}
        """)
        XCTAssertEqual(a.originLine, "whatever Dark Army said")
    }

    // ── the helper row ───────────────────────────────────────────────────

    func testQualifiedComposesWhileLabelStaysBare() throws {
        let r = try row("""
        {"agent_id": "a1", "subagent_type": "bc-verifier",
         "card_title": "Rename the ladder"}
        """)
        XCTAssertEqual(r.label, "bc-verifier")
        XCTAssertEqual(r.qualified, "bc-verifier · Rename the ladder")
    }

    func testQualifiedIsTheLabelWithoutATitle() throws {
        let r = try row(#"{"agent_id": "a1", "subagent_type": "bc-verifier"}"#)
        XCTAssertEqual(r.cardTitle, "")
        XCTAssertEqual(r.qualified, "bc-verifier")
    }

    /// The regression this whole split exists to prevent.
    func testALiveStageNamesJoinStillMatches() throws {
        let rows = [
            try row("""
            {"agent_id": "a1", "subagent_type": "bc-implementer",
             "card_title": "Rename the ladder"}
            """),
            try row("""
            {"agent_id": "a2", "subagent_type": "bc-verifier",
             "card_title": "Rename the ladder"}
            """),
        ]
        let live = Set(rows.map(\.label).filter { !$0.isEmpty })
        let workflow = ["bc-planner", "bc-implementer", "bc-verifier"]
        XCTAssertEqual(workflow.filter { live.contains($0) },
                       ["bc-implementer", "bc-verifier"])
        // And the same set built from `qualified` would match nothing, which
        // is exactly the silent failure `label` is kept bare to avoid.
        let wrong = Set(rows.map(\.qualified))
        XCTAssertTrue(workflow.filter { wrong.contains($0) }.isEmpty)
    }

    func testSubagentSummaryNamesTheWork() throws {
        let a = try agent("""
        {"session_id": "s1", "subagents": 2, "subagent_rows": [
          {"agent_id": "a1", "subagent_type": "bc-implementer"},
          {"agent_id": "a2", "subagent_type": "bc-verifier",
           "card_title": "Rename the ladder"}]}
        """)
        XCTAssertEqual(a.subagentSummary, "+2 · bc-verifier · Rename the ladder")
    }
}
