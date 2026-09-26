import XCTest
@testable import BobPhone

/// The agent sheet's lead and the conversation fold, as the phone runs them.
/// The same table runs on the host under `swiftc`
/// (`host/tests/test_phone_sheet_lead.py`).
final class AgentSheetLeadTests: XCTestCase {
    private func t(_ seq: Int, _ kind: String, _ tool: String = "") -> ConversationTurn {
        ConversationTurn(seq: seq, kind: kind, tool: tool)
    }

    private func shape(_ rows: [ConversationFold.Row]) -> [String] {
        rows.map {
            switch $0 {
            case .turn(let turn): return "turn:\(turn.kind)"
            case .run(let tools): return "run(\(tools.count))"
            }
        }
    }

    private var mixed: [ConversationTurn] {
        [t(0, "user"), t(1, "agent"), t(2, "tool", "Bash"), t(3, "result"),
         t(4, "tool", "Read"), t(5, "result"), t(6, "agent"),
         t(7, "tool", "Grep"), t(8, "user")]
    }

    // MARK: - the lead

    func testTheStillIsCompactOnlyAtHalfHeight() {
        XCTAssertEqual(AgentSheetLead.stillSize(detent: .medium), 40)
        XCTAssertEqual(AgentSheetLead.stillSize(detent: .large), 96)
        XCTAssertEqual(AgentSheetLead.stillSize(detent: nil), 96)
    }

    func testTheStatusLineJoinsWhatItHas() {
        XCTAssertEqual(AgentSheetLead.statusLine(head: "Working — running Bash",
                                                 age: "1h", ctx: "ctx 20%↑"),
                       "Working — running Bash · 1h · ctx 20%↑")
        XCTAssertEqual(AgentSheetLead.statusLine(head: "Idle in the editor",
                                                 age: "", ctx: ""),
                       "Idle in the editor")
        XCTAssertEqual(AgentSheetLead.statusLine(head: "Stopped, waiting for you",
                                                 age: "—", ctx: "ctx 3%"),
                       "Stopped, waiting for you · ctx 3%")
    }

    func testTheContextFigure() {
        XCTAssertEqual(AgentSheetLead.ctxText(pct: nil, marker: "↑"), "")
        XCTAssertEqual(AgentSheetLead.ctxText(pct: 19.6, marker: "↑"), "ctx 20%↑")
    }

    func testTheQuestionTrimsDropsAndJoins() {
        XCTAssertEqual(AgentSheetLead.questionText(["  Which one?\n", "", "  ", "And why?"]),
                       "Which one?\n\nAnd why?")
        XCTAssertEqual(AgentSheetLead.questionText([]), "")
    }

    func testTheStatusLineIsSpokenInWords() {
        XCTAssertEqual(AgentSheetLead.spokenCtx(pct: 19.6, pace: "rising"),
                       "context 20 percent, rising")
        XCTAssertEqual(AgentSheetLead.spokenStatus(head: "Working — running Bash",
                                                   age: "1 hour",
                                                   ctx: "context 20 percent, rising"),
                       "Working — running Bash, 1 hour, context 20 percent, rising")
    }

    func testTheLiveBucketWinsOverTheSeed() {
        XCTAssertEqual(AgentSheetLead.liveBucket([("waiting", false), ("running", true)]),
                       "running")
        XCTAssertNil(AgentSheetLead.liveBucket([("waiting", false), ("running", false)]))
    }

    // MARK: - the fold

    func testARunHoldingAnExpandedCallStaysOpen() {
        let tools = [t(2, "tool", "Bash"), t(4, "tool", "Read")]
        XCTAssertFalse(ConversationFold.runIsOpen(tools, id: 2, openRuns: [], expanded: []))
        XCTAssertTrue(ConversationFold.runIsOpen(tools, id: 2, openRuns: [], expanded: [4]))
        XCTAssertTrue(ConversationFold.runIsOpen(tools, id: 2, openRuns: [2], expanded: []))
    }

    func testAMixedListFoldsAndDropsResults() {
        let rows = ConversationFold.rows(mixed)
        XCTAssertEqual(shape(rows), ["turn:user", "turn:agent", "run(2)",
                                     "turn:agent", "turn:tool", "turn:user"])
        XCTAssertEqual(rows.map(\.id), [0, 1, 2, 6, 7, 8])
    }

    func testAHigherMinimumLeavesShortRunsAlone() {
        XCTAssertEqual(shape(ConversationFold.rows(mixed, minRun: 3)),
                       ["turn:user", "turn:agent", "turn:tool", "turn:tool",
                        "turn:agent", "turn:tool", "turn:user"])
    }

    func testAGrowingRunKeepsItsId() {
        let rows = ConversationFold.rows([t(0, "agent"), t(1, "tool", "Bash"),
                                          t(2, "tool", "Bash"), t(3, "result"),
                                          t(4, "tool", "Read")])
        XCTAssertEqual(shape(rows), ["turn:agent", "run(3)"])
        XCTAssertEqual(rows.last?.id, 1)
    }

    func testTheSummaryCountsInFirstSeenOrder() {
        let tools = [t(0, "tool", "Bash"), t(1, "tool", "Read"), t(2, "tool", "Bash"),
                     t(3, "tool", "Grep"), t(4, "tool", "Bash")]
        XCTAssertEqual(ConversationFold.summary(tools),
                       "⚙ 5 tool calls · Bash ×3, Read, Grep")
        XCTAssertEqual(ConversationFold.spoken(tools),
                       "5 tool calls: Bash 3 times, Read, Grep")
    }
}
