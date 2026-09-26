import XCTest
@testable import BobPhone

final class AgentScreenTests: XCTestCase {
    func testMainIsTheFirstScreenWhateverTheMacServes() {
        XCTAssertEqual(AgentScreen.defaultScreen(supported: true), .main)
        XCTAssertEqual(AgentScreen.defaultScreen(supported: false), .main)
        XCTAssertEqual(AgentScreen.defaultScreen, .main)
    }

    func testChipsHostedAndUnhosted() {
        XCTAssertEqual(AgentScreen.chips(hosted: false),
                       [.main, .conversation, .details])
        XCTAssertEqual(AgentScreen.chips(hosted: true),
                       [.main, .conversation, .details, .terminal])
    }

    func testOnlyMainDrawsTheWholeLead() {
        XCTAssertFalse(AgentScreen.titleOnly(.main))
        XCTAssertTrue(AgentScreen.titleOnly(.conversation))
        XCTAssertTrue(AgentScreen.titleOnly(.details))
        XCTAssertTrue(AgentScreen.titleOnly(.terminal))
    }

    func testPaneWithoutAHostedTerminalFallsToDetails() {
        XCTAssertEqual(AgentScreen.pane(hosted: false, screen: .terminal),
                       .details)
        XCTAssertEqual(AgentScreen.pane(hosted: true, screen: .terminal),
                       .terminal)
    }

    func testDetailTabMapsConversationToDetails() {
        XCTAssertEqual(AgentScreen.detailTab(.terminal), .terminal)
        XCTAssertEqual(AgentScreen.detailTab(.conversation), .details)
        XCTAssertEqual(AgentScreen.detailTab(.details), .details)
        XCTAssertEqual(AgentScreen.detailTab(.main), .details)
    }

    func testClockHidesZeroAndFormatsTheRest() {
        XCTAssertEqual(ConversationRows.clock(0), "")
        XCTAssertFalse(ConversationRows.clock(1_800_000_000).isEmpty)
    }

    func testToolAndResultLines() {
        let tool = ConversationTurn(seq: 0, kind: "tool", tool: "Bash",
                                    brief: "ls")
        XCTAssertEqual(ConversationRows.toolLine(tool), "⚙ Bash · ls")
        let result = ConversationTurn(seq: 1, kind: "result", text: "ok",
                                      resultBytes: 12)
        XCTAssertEqual(ConversationRows.resultLine(result), "↳ ok · 12 bytes")
    }
}

/// The journey rail's rule (`CardJourney`): the current stage from the
/// board card alone, a passed stage timed from the timeline only where both
/// ends were witnessed, and nothing guessed.
final class CardJourneyTests: XCTestCase {
    private func card(_ column: String, link: String = "", refine: String = "",
                      plan: String = "", queue: String = "", manual: String = "",
                      kind: String = "") -> BoardCard {
        var c = BoardCard()
        c.id = "c1"
        c.column = column
        c.linkState = link
        c.refineState = refine
        c.planPath = plan
        c.queueState = queue
        c.manualSteps = manual
        c.kind = kind
        return c
    }

    private func step(_ kind: String, _ at: Double?) -> CardTimelineStep {
        CardTimelineStep(kind: kind, label: kind, at: at)
    }

    func testTheCurrentStageComesFromTheBoardCard() {
        XCTAssertEqual(CardJourney.current(card("prep")), .idea)
        XCTAssertEqual(CardJourney.current(card("prep", refine: "live")), .plan)
        XCTAssertEqual(CardJourney.current(card("backlog")), .plan)
        XCTAssertEqual(CardJourney.current(card("backlog", plan: "plans/x.md")), .build)
        XCTAssertEqual(CardJourney.current(card("backlog", kind: "scout")), .build)
        XCTAssertEqual(CardJourney.current(card("in_progress", link: "live")), .build)
        XCTAssertEqual(CardJourney.current(card("in_progress", queue: "queued")), .build)
        XCTAssertEqual(CardJourney.current(card("in_progress", link: "ended")), .check)
        XCTAssertEqual(CardJourney.current(card("in_progress", manual: "1. Open it")), .check)
        XCTAssertEqual(CardJourney.current(card("done")), .done)
    }

    func testMarksFollowTheCurrentStage() {
        let steps = CardJourney.steps(card: card("in_progress", link: "live"), report: nil)
        XCTAssertEqual(steps.map(\.mark),
                       [.passed, .passed, .current, .ahead, .ahead])
        XCTAssertTrue(steps.allSatisfy { $0.time.isEmpty })
    }

    func testPassedStagesAreTimedBetweenWitnessedStarts() {
        let report = CardTimelineReport(steps: [
            step("created", 1000),
            step("refine_started", 1600),     // idea took 10m
            step("plan_attached", 2000),
            step("queued", 3000),             // not a stage
            step("started", 5200),            // plan took 1h
            step("picked_up", 5300),
            step("ended", 9000),              // build took 1h 3m
            step("moved_done", 9600),         // check took 10m
        ])
        let steps = CardJourney.steps(card: card("done"), report: report)
        XCTAssertEqual(steps.map(\.time), ["10m", "1h", "1h 3m", "10m", ""])
        XCTAssertEqual(steps.last?.mark, .current)
    }

    func testAnUnwitnessedOrSkippedStageIsBlankNotGuessed() {
        let report = CardTimelineReport(steps: [
            step("created", nil),             // never witnessed
            step("started", 2000),            // a scout: no plan
            step("ended", 2600),
        ])
        let steps = CardJourney.steps(card: card("in_progress", link: "ended"),
                                      report: report)
        XCTAssertEqual(steps.map(\.time), ["", "", "10m", "", ""])
    }

    func testAnOutOfOrderMomentDropsTheNegativeFigure() {
        // A card done, reopened and done again keeps its first Done, which
        // can sit before the latest Check: that figure would be negative,
        // so it is left blank rather than drawn.
        let report = CardTimelineReport(steps: [
            step("created", 1000),
            step("started", 2000),            // idea took 16m
            step("ended", 3000),
            step("moved_done", 2500),         // before Check began
        ])
        // Build ends at the earliest later moment (Done, 8m); Check's only
        // later moment is before it, so Check is blank.
        let steps = CardJourney.steps(card: card("done"), report: report)
        XCTAssertEqual(steps.map(\.time), ["16m", "", "8m", "", ""])
        XCTAssertEqual(steps.map(\.mark), [.passed, .passed, .passed, .passed, .current])
    }

    func testTheRailIsOneSentenceForAScreenReader() {
        let steps = [
            CardJourney.Step(stage: .idea, mark: .passed, time: "10m"),
            CardJourney.Step(stage: .plan, mark: .passed, time: ""),
            CardJourney.Step(stage: .build, mark: .current, time: ""),
            CardJourney.Step(stage: .check, mark: .ahead, time: ""),
            CardJourney.Step(stage: .done, mark: .ahead, time: ""),
        ]
        XCTAssertEqual(CardJourney.spoken(steps),
                       "Journey. Idea, passed, took 10m; Plan, passed; Build, now; "
                       + "Check, ahead; Done, ahead")
    }

    func testAnAgentsOwnCloseDoesNotReadAsAZeroSecondCheck() {
        // `declare_done` writes `submitted` and `moved_done` at one instant.
        let report = CardTimelineReport(steps: [
            step("created", 1000),
            step("started", 1600),
            step("submitted", 4000),
            step("moved_done", 4000),
            step("ended", 4100),
        ])
        let steps = CardJourney.steps(card: card("done"), report: report)
        XCTAssertEqual(steps.map(\.time), ["10m", "", "40m", "", ""])
        XCTAssertFalse(steps.contains { $0.time == "0s" })
    }

    func testTheFetchKeyMovesWithEveryInputOfTheOpenLine() {
        let base = card("done")
        var reviewed = base; reviewed.reviewedAt = 5000
        var accepted = base; accepted.outcomeStatus = "accepted"
        var closed = base; closed.closedBy = "claude"
        var queued = card("backlog"); queued.queuedAt = 10
        var ended = card("in_progress", link: "ended")
        ended.manualSteps = "1. Open it"
        let keys = [base, reviewed, accepted, closed, queued, ended,
                    card("backlog"), card("backlog", plan: "plans/x.md")]
            .map(CardJourney.fetchKey)
        XCTAssertEqual(Set(keys).count, keys.count)
        XCTAssertEqual(CardJourney.fetchKey(base), CardJourney.fetchKey(card("done")))
    }

    func testDoneReadsAsReachedNotNow() {
        let done = CardJourney.Step(stage: .done, mark: .current, time: "")
        let build = CardJourney.Step(stage: .build, mark: .current, time: "")
        XCTAssertEqual(CardJourney.timeWords(done), "✓")
        XCTAssertEqual(CardJourney.timeWords(build), "now")
        XCTAssertEqual(CardJourney.timeWords(
            CardJourney.Step(stage: .plan, mark: .passed, time: "12m")), "12m")
        XCTAssertTrue(CardJourney.spoken([done]).hasSuffix("Done, reached"))
    }

    func testEveryTimelineKindIsPlacedOrDeliberatelyNot() {
        let placed = ["created", "refine_started", "refine_ended", "plan_attached",
                      "plan_approved", "started", "picked_up", "restarted", "ended",
                      "manual_flagged", "manual_cleared", "submitted", "reviewed",
                      "moved_done", "accepted"]
        for kind in placed { XCTAssertNotNil(CardJourney.stage(forKind: kind), kind) }
        for kind in ["queued", "unqueued", "permission_asks", "start_failed",
                     "start_abandoned", "reopened", "revision_requested", "mystery"] {
            XCTAssertNil(CardJourney.stage(forKind: kind), kind)
        }
    }
}
