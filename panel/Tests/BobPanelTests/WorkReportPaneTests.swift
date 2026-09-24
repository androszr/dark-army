import XCTest
@testable import BobPanel

/// The report the daemon keeps beside the latest words, and the two rules for
/// drawing it: nothing where there is none, and never twice.
final class WorkReportPaneTests: XCTestCase {
    func testNoReportDrawsNothing() {
        XCTAssertEqual(StdoutPane.reportToShow(latest: "anything", report: ""), "")
        XCTAssertEqual(StdoutPane.reportToShow(latest: "anything", report: "  \n "), "")
    }

    func testTheReportIsDrawnUnderAFollowUpThatReplacedIt() {
        let report = "## Work done\n**Asked:** why refines open in the editor."
        XCTAssertEqual(
            StdoutPane.reportToShow(latest: "That was the watcher — nothing new.",
                                    report: report),
            report)
    }

    func testAMessageThatIsTheReportIsNotDrawnTwice() {
        let report = "## Work done\n**Asked:** why refines open in the editor."
        XCTAssertEqual(StdoutPane.reportToShow(latest: report, report: report), "")
    }

    func testAnAgentDecodesAnAbsentReportAsEmpty() throws {
        let json = "{\"session_id\":\"s1\",\"nickname\":\"Velvet\",\"last_text\":\"hi\"}"
        let agent = try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
        XCTAssertEqual(agent.lastReport, "")
        let withReport =
            "{\"session_id\":\"s1\",\"last_report\":\"## Work done\\nx\"}"
        let two = try JSONDecoder().decode(Agent.self, from: Data(withReport.utf8))
        XCTAssertEqual(two.lastReport, "## Work done\nx")
    }
}

extension WorkReportPaneTests {
    func testCodexRetainedReportAfterCommentaryAndNextTask() throws {
        let wire = ###"{"session_id":"codex:root","provider":"codex","last_text":"Card left open for live checks.","last_report":"## Work done\n**Unchecked:** Check the original session."}"###
        let agent = try JSONDecoder().decode(Agent.self, from: Data(wire.utf8))
        XCTAssertEqual(StdoutPane.reportToShow(latest: agent.lastText, report: agent.lastReport), agent.lastReport)
        XCTAssertEqual(StdoutPane.reportToShow(latest: agent.lastReport, report: agent.lastReport), "")
        let next = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"codex:root","last_text":"Starting the next task.","last_report":""}"#.utf8))
        XCTAssertEqual(StdoutPane.reportToShow(latest: next.lastText, report: next.lastReport), "")
    }
}
