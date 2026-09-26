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

/// The labelled parts the daemon publishes beside the raw report
/// (`work_report`), and the shared wording rule both clients draw them by.
extension WorkReportPaneTests {
    private func decode(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    func testAnAbsentWorkReportDecodesNil() throws {
        let agent = try decode(###"{"session_id":"s1","last_report":"## Work done\nx"}"###)
        XCTAssertNil(agent.workReport)
    }

    func testAnEmptyOrWrongTypedWorkReportNeverBlanksTheRow() throws {
        let empty = try decode(#"{"session_id":"s1","nickname":"Vex","work_report":{}}"#)
        XCTAssertEqual(empty.workReport, WorkReport.Parsed())
        XCTAssertEqual(empty.nickname, "Vex")
        let wrong = try decode(#"{"session_id":"s1","nickname":"Vex","work_report":"text"}"#)
        XCTAssertNil(wrong.workReport)
        XCTAssertEqual(wrong.nickname, "Vex")
        let member = try decode(#"{"session_id":"s1","work_report":{"labelled":true,"changed":"x","headline":"h"}}"#)
        XCTAssertEqual(member.workReport?.changed, [])
        XCTAssertEqual(member.workReport?.headline, "h")
    }

    func testAPresentWorkReportDecodesItsLists() throws {
        let wire = ###"""
        {"session_id":"s1","last_report":"## Work done","work_report":{
         "labelled":true,"cut":false,"asked":"why refines open",
         "changed":["the spawner follows the preference","a test"],
         "verified":["the suite passed"],"unchecked":["Open it.","Press Restart."],
         "nothing_unchecked":false,"why_not_automated":"Why not automated: a real screen.",
         "card":"closed","headline":"Changed: the spawner follows the preference · 1 verified · 2 unchecked"}}
        """###
        let parsed = try XCTUnwrap(try decode(wire).workReport)
        XCTAssertTrue(parsed.labelled)
        XCTAssertEqual(parsed.asked, "why refines open")
        XCTAssertEqual(parsed.changed.count, 2)
        XCTAssertEqual(parsed.verified, ["the suite passed"])
        XCTAssertEqual(parsed.unchecked, ["Open it.", "Press Restart."])
        XCTAssertEqual(parsed.whyNotAutomated, "Why not automated: a real screen.")
        XCTAssertEqual(parsed.card, "closed")
        XCTAssertEqual(WorkReport.sections(parsed),
                       [.asked, .changed, .verified, .unchecked, .card])
    }

    func testRowLead() {
        let h = "Changed: x · 1 verified · nothing unchecked"
        XCTAssertEqual(WorkReport.rowLead(headline: h, base: "Mobile cards"),
                       "\(h) · Mobile cards")
        XCTAssertEqual(WorkReport.rowLead(headline: "", base: "Mobile cards"), "Mobile cards")
        XCTAssertEqual(WorkReport.rowLead(headline: h, base: "—"), h)
    }

    /// The row names what clicking it opens: the detail header's card
    /// line, never a finished run's headline pushed in front of it — the
    /// headline cut the title off the column's two lines.
    func testTheRowNamesWhatTheDetailOpens() throws {
        let agent = try decode(#"{"session_id":"s1","name":"Mobile cards","work_report":{"labelled":true,"headline":"Changed: x"}}"#)
        for category: BobPanel.Category? in [.sleeping, .finished, .running, nil] {
            XCTAssertEqual(ProcessRow.commandText(for: agent, category: category), "Mobile cards")
            XCTAssertEqual(ProcessRow.commandLines(for: agent, category: category).title,
                           "Mobile cards")
        }
        let line = "Add a Plans library to the phone"
        XCTAssertEqual(ProcessRow.commandLines(for: agent, category: .sleeping,
                                               cardLine: line).title, line)
        XCTAssertEqual(ProcessRow.commandText(for: agent, category: .finished,
                                              cardLine: line), line)
    }

    func testUncheckedCaptionAndNothingUnchecked() {
        XCTAssertEqual(WorkReport.uncheckedCaption(count: 0, nothing: true), "UNCHECKED")
        XCTAssertEqual(WorkReport.uncheckedCaption(count: 3, nothing: false), "UNCHECKED (3)")
        XCTAssertEqual(WorkReport.nothingUnchecked, "Nothing unchecked — every check ran")
        let nothing = WorkReport.Parsed(labelled: true, nothingUnchecked: true)
        XCTAssertEqual(WorkReport.sections(nothing), [.unchecked])
        XCTAssertEqual(WorkReport.numbered(["Open it.", "Press it."]),
                       ["1. Open it.", "2. Press it."])
    }

    func testTotalsDecodeAndNeverFallBelowTheList() throws {
        let clamped = try XCTUnwrap(try decode(#"{"session_id":"s1","work_report":{"labelled":true,"changed":["a","b"],"changed_total":20,"unchecked":["x"],"unchecked_total":14}}"#).workReport)
        XCTAssertEqual(WorkReport.total(clamped, .changed), 20)
        XCTAssertEqual(WorkReport.total(clamped, .unchecked), 14)
        XCTAssertEqual(WorkReport.more(shown: clamped.changed.count,
                                       total: WorkReport.total(clamped, .changed)), "+18 more")
        XCTAssertEqual(WorkReport.uncheckedCaption(count: WorkReport.total(clamped, .unchecked),
                                                   nothing: false), "UNCHECKED (14)")
        // An older daemon sends no totals: the list's own length stands.
        let older = try XCTUnwrap(try decode(#"{"session_id":"s1","work_report":{"labelled":true,"verified":["a","b"]}}"#).workReport)
        XCTAssertEqual(WorkReport.total(older, .verified), 2)
        XCTAssertEqual(WorkReport.more(shown: 2, total: 2), "")
    }

    func testACutReportSaysSo() throws {
        let cut = try XCTUnwrap(try decode(#"{"session_id":"s1","work_report":{"labelled":true,"cut":true,"changed":["tail"]}}"#).workReport)
        XCTAssertTrue(cut.cut)
        XCTAssertEqual(WorkReport.cutNote, "(report cut — start not kept)")
    }

    func testAnInlineMentionOfTheHeadingCutsNothing() {
        let latest = "Intro mentions ## Work done inline.\n\n## Work done\n**Asked:** x"
        XCTAssertEqual(WorkReport.prose(before: latest), "Intro mentions ## Work done inline.")
        XCTAssertEqual(WorkReport.prose(before: "No heading, only ## Work done in passing."),
                       "No heading, only ## Work done in passing.")
    }

    func testADeeperHeadingIsNotTheReport() {
        let latest = "Keep this.\n### Work done\nmore"
        XCTAssertEqual(WorkReport.prose(before: latest), latest)
        XCTAssertFalse(WorkReport.isReportHeading("### Work done"))
        XCTAssertFalse(WorkReport.isReportHeading("##Work done"))
        XCTAssertTrue(WorkReport.isReportHeading("##\tWork done  "))
    }

    func testTheLastHeadingWins() {
        let latest = "a\n## Work done\nfirst\nb\n##  Work done  \nsecond"
        XCTAssertEqual(WorkReport.prose(before: latest), "a\n## Work done\nfirst\nb")
        XCTAssertEqual(WorkReport.prose(before: "p\r\n## Work done\r\n**Asked:** x"), "p")
    }

    func testAMessageThatIsALabelledReportKeepsOnlyItsProse() {
        let latest = "Root cause found.\n\n## Work done\n**Asked:** x"
        XCTAssertEqual(StdoutPane.latestToShow(latest: latest, labelled: true),
                       "Root cause found.")
        XCTAssertEqual(StdoutPane.latestToShow(latest: "## Work done\n**Asked:** x",
                                               labelled: true), "")
        XCTAssertEqual(StdoutPane.latestToShow(latest: latest, labelled: false), latest)
        XCTAssertEqual(StdoutPane.latestToShow(latest: "a follow-up", labelled: true),
                       "a follow-up")
    }
}
