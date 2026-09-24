import XCTest
@testable import BobPanel

/// The whole inbox is a pure function over decoded JSON, so this is where it
/// is judged: membership and each kind's near miss, one item per row and per
/// card, ranking, grouping, the count and what Dismiss covers.
final class InboxTests: XCTestCase {
    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func agent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    private func prompt(_ json: String) throws -> PermissionPrompt {
        try JSONDecoder().decode(PermissionPrompt.self, from: Data(json.utf8))
    }

    private func row(_ json: String, _ category: BobPanel.Category = .waiting) throws -> SectionRow {
        SectionRow(agent: try agent(json), category: category)
    }

    private let planned = #"{"id":"p","column_name":"backlog","plan_path":"plans/a.md"}"#

    // MARK: the row's clock

    func testASessionEntryIsDatedFromTheSnapshotsOwnClock() throws {
        let json = #"{"session_id":"s1","idle_seconds":90}"#
        let items = Inbox.items(rows: [try row(json)], prompts: [], cards: [],
                                now: 1_000)
        XCTAssertEqual(items.count, 1)
        // 90s idle at t=1000 means it went quiet at t=910 — an absolute
        // moment, so a quiet minute does not freeze the row's clock.
        XCTAssertEqual(items[0].since, 910, accuracy: 0.001)
        XCTAssertEqual(items[0].sessionId, "s1")
    }

    func testAWaitingAgeKeepsClimbingAcrossAgentsOmittedFrames() throws {
        // The failure this change had to avoid. Rows decoded at T=1000 with
        // 60s idle; two minutes of frames follow that omit an unchanged
        // fleet, so nothing re-states the 60. `Inbox.items` is handed the
        // *fleet's* stamp, so `since` is the absolute moment the session went
        // quiet, and `InboxView` reads it against a live clock.
        let json = #"{"session_id":"s1","idle_seconds":60}"#
        let items = Inbox.items(rows: [try row(json)], prompts: [], cards: [],
                                now: 1_000)                 // agentsStamp
        XCTAssertEqual(items[0].since, 940, accuracy: 0.001)
        XCTAssertEqual(FleetAge.text(startedAt: items[0].since, now: 1_180),
                       FleetAge.text(startedAt: 940, now: 1_180))
        XCTAssertEqual(Int(1_180 - items[0].since), 240)
        let again = Inbox.items(rows: [try row(json)], prompts: [], cards: [],
                                now: 1_000)                 // stamp unmoved
        XCTAssertEqual(again[0].since, 940, accuracy: 0.001)
    }

    func testAnUndatedSessionEntryCarriesNoClock() throws {
        let json = #"{"session_id":"s1","idle_seconds":90}"#
        XCTAssertEqual(Inbox.items(rows: [try row(json)], prompts: [],
                                   cards: [], now: 0)[0].since, 0)
    }

    func testACardEntryIsDatedFromTheThingThatNeedsAPerson() throws {
        let ended = #"{"id":"c","needs_you":true,"session_ended_at":500,"created_at":10}"#
        XCTAssertEqual(try Inbox.items(rows: [], prompts: [],
                                       cards: [card(ended)])[0].since, 500)
        // A card the daemon dated with nothing falls back to when it was
        // written, never to now.
        let flagged = #"{"id":"m","manual_steps":"look","manual_check_due":true,"created_at":10}"#
        XCTAssertEqual(try Inbox.items(rows: [], prompts: [],
                                       cards: [card(flagged)])[0].since, 10)
    }

    // MARK: three kinds, named by what you do

    func testTheThreeKindsAreRankedAnswerLookStopped() {
        XCTAssertEqual(InboxKind.allCases, [.answer, .look, .stopped])
        XCTAssertEqual(InboxKind.allCases.map(\.rawValue), [0, 1, 2])
        XCTAssertEqual(InboxKind.allCases.map(\.word), ["ANSWER", "LOOK AT", "STOPPED"])
    }

    func testEveryWireKindFoldsIntoOneOfTheThree() {
        XCTAssertEqual(InboxWireKind.permission.kind, .answer)
        XCTAssertEqual(InboxWireKind.question.kind, .answer)
        XCTAssertEqual(InboxWireKind.endedWork.kind, .look)
        XCTAssertEqual(InboxWireKind.manualCheck.kind, .look)
        XCTAssertEqual(InboxWireKind.waiting.kind, .stopped)
        XCTAssertEqual(
            [InboxWireKind.permission, .question, .endedWork, .manualCheck, .waiting].map(\.name),
            ["permission", "question", "ended_work", "manual_check", "waiting"])
    }

    func testOnlyAPermissionAskIsNotDismissable() {
        XCTAssertFalse(InboxWireKind.permission.dismissable)
        XCTAssertTrue(InboxWireKind.question.dismissable)
        XCTAssertTrue(InboxWireKind.endedWork.dismissable)
        XCTAssertTrue(InboxWireKind.manualCheck.dismissable)
        XCTAssertTrue(InboxWireKind.waiting.dismissable)
    }

    func testDismissAllCoversEverythingButThePermissionAsk() throws {
        let waiting = #"{"session_id":"s1","idle_seconds":5}"#
        let asked = #"{"session_id":"s2","idle_seconds":5,"question":{"text":"which?"}}"#
        let items = Inbox.items(
            rows: [try row(waiting), try row(asked)],
            prompts: [try prompt(#"{"request_id":"r","session_id":"s1","tool_name":"Bash"}"#)],
            cards: [try card(#"{"id":"e","needs_you":true}"#)])
        XCTAssertEqual(items.count, 3)
        let dismissable = Inbox.dismissable(items)
        // s1 is stopped at a permission ask — hiding it would not unblock
        // the tool call, so it is the one entry the press leaves.
        XCTAssertEqual(dismissable.map(\.wire), [.question, .endedWork])
    }

    // MARK: what is no longer here

    func testAReadyPlanIsNotAnInboxEntry() throws {
        XCTAssertTrue(Inbox.items(rows: [], prompts: [], cards: [try card(planned)]).isEmpty)
    }

    func testAClosedCardAwaitingReviewIsNotAnInboxEntry() throws {
        let json = #"{"id":"d","column_name":"done","closed_by":"s1","done_at":700}"#
        XCTAssertTrue(Inbox.items(rows: [], prompts: [], cards: [try card(json)]).isEmpty)
    }

    // MARK: membership

    func testStepsTheDaemonSaysAreNotDueAreNotAManualCheck() throws {
        let json = #"{"id":"m","manual_steps":"press the thing","manual_check_due":false}"#
        XCTAssertTrue(Inbox.items(rows: [], prompts: [], cards: [try card(json)]).isEmpty)
    }

    /// `manualCheckDue` defaults **true** behind non-empty steps — the
    /// older-daemon shim, which the inbox inherits rather than second-guesses.
    func testAnOlderDaemonsStepsStillReachTheInbox() throws {
        let json = #"{"id":"m","manual_steps":"press the thing"}"#
        XCTAssertEqual(Inbox.items(rows: [], prompts: [], cards: [try card(json)]).map(\.wire),
                       [.manualCheck])
    }

    func testTheDaemonsManualFlagIsALookEntry() throws {
        let json = #"{"id":"m","manual_steps":"press the thing","manual_check_due":true}"#
        let items = Inbox.items(rows: [], prompts: [], cards: [try card(json)])
        XCTAssertEqual(items.map(\.wire), [.manualCheck])
        XCTAssertEqual(items.map(\.kind), [.look])
        XCTAssertEqual(items[0].detail, "press the thing")
    }

    func testTheDaemonsNeedsYouFlagIsALookEntry() throws {
        let items = Inbox.items(rows: [], prompts: [],
                                cards: [try card(#"{"id":"e","needs_you":true}"#)])
        XCTAssertEqual(items.map(\.wire), [.endedWork])
        XCTAssertEqual(items.map(\.kind), [.look])
        XCTAssertEqual(items[0].cardId, "e")
    }

    func testAnEmptyCardYieldsNoItem() throws {
        XCTAssertTrue(Inbox.items(rows: [], prompts: [], cards: [try card(#"{"id":"a"}"#)]).isEmpty)
    }

    // MARK: one per row, one per card

    func testAPromptAndAQuestionOnOneRowIsOnePermissionItem() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Ada","project":"bob","question":{"text":"which one?"}}"#)]
        let prompts = [try prompt(#"{"request_id":"r1","session_id":"s1","tool_name":"Bash"}"#)]
        let items = Inbox.items(rows: rows, prompts: prompts, cards: [])
        XCTAssertEqual(items.map(\.wire), [.permission])
        XCTAssertEqual(items.map(\.kind), [.answer])
    }

    func testAQuestionWithoutAPromptIsAQuestionItem() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Ada","question":{"text":"which one?"}}"#)]
        let items = Inbox.items(rows: rows, prompts: [], cards: [])
        XCTAssertEqual(items.map(\.wire), [.question])
        XCTAssertEqual(items.map(\.kind), [.answer])
    }

    func testARowWithNeitherIsStopped() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Ada","tool_name":"Bash"}"#)]
        let items = Inbox.items(rows: rows, prompts: [], cards: [])
        XCTAssertEqual(items.map(\.kind), [.stopped])
        XCTAssertEqual(items[0].title, "Ada")
        XCTAssertEqual(items[0].target, .session("s1"))
    }

    func testAStoppedRowsDetailIsItsOwnSummaryWhereItLeftOne() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Ada","tool_name":"Bash","last_summary":"Pick a name."}"#)]
        XCTAssertEqual(Inbox.items(rows: rows, prompts: [], cards: [])[0].detail, "Pick a name.")
    }

    func testAnAnsweredPromptRaisesNoPermissionItem() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Ada"}"#)]
        let prompts = [try prompt(#"{"request_id":"r1","session_id":"s1","tool_name":"Bash"}"#)]
        let items = Inbox.items(rows: rows, prompts: prompts, cards: [], answered: ["r1"])
        XCTAssertEqual(items.map(\.wire), [.waiting])
    }

    // MARK: a card and its session are one subject

    /// The screenshot case (20 Sep 2026): Vex finished a card, flagged
    /// its manual check and stopped with a message — one agent, listed twice
    /// as STOPPED and LOOK AT, so the header said "2 need you". The card's
    /// check wins over the plain wait.
    func testAWaitingSessionAndItsFlaggedCardAreOneEntry() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Vex","project":"stock","last_summary":"Written up."}"#)]
        let cards = [try card(#"{"id":"c","title":"USD/PLN tile","project":"stock","session_id":"s1","manual_steps":"1. Run it.","manual_check_due":true}"#)]
        let items = Inbox.items(rows: rows, prompts: [], cards: cards)
        XCTAssertEqual(items.map(\.wire), [.manualCheck])
        XCTAssertEqual(items[0].cardId, "c")
        XCTAssertEqual(items.count, 1)
    }

    /// A permission ask on the same session outranks the card's check: the
    /// tool call is what is actually blocked.
    func testAPermissionOnTheBoundSessionOutranksTheCardsCheck() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Vex"}"#)]
        let prompts = [try prompt(#"{"request_id":"r1","session_id":"s1","tool_name":"Bash"}"#)]
        let cards = [try card(#"{"id":"c","session_id":"s1","manual_steps":"look","manual_check_due":true}"#)]
        let items = Inbox.items(rows: rows, prompts: prompts, cards: cards)
        XCTAssertEqual(items.map(\.wire), [.permission])
    }

    /// Dismissing the card's entry lets the session's own come back: the
    /// join runs after the acks, never before.
    func testDismissingTheCardsEntryRevealsTheSessionsOwn() throws {
        let rows = [try row(#"{"session_id":"s1","nickname":"Vex"}"#)]
        let cards = [try card(#"{"id":"c","session_id":"s1","manual_steps":"look","manual_check_due":true}"#)]
        let fp = InboxFingerprint.value(wire: .manualCheck, card: cards[0])
        let ack = InboxAckRecord(key: "c:c", kind: "manual_check", fp: fp)
        let items = Inbox.items(rows: rows, prompts: [], cards: cards, acks: [ack])
        XCTAssertEqual(items.map(\.wire), [.waiting])
    }

    /// A card bound to a session that is *working* (no row in the waiting
    /// slice) still lists its check: the join only ever drops a duplicate.
    func testACardWhoseSessionIsNotWaitingStillLists() throws {
        let cards = [try card(#"{"id":"c","session_id":"s1","manual_steps":"look","manual_check_due":true}"#)]
        XCTAssertEqual(Inbox.items(rows: [], prompts: [], cards: cards).map(\.wire), [.manualCheck])
    }

    func testACardThatIsBothEndedAndFlaggedIsOneEndedWorkItem() throws {
        let json = #"{"id":"e","needs_you":true,"manual_steps":"look","manual_check_due":true}"#
        XCTAssertEqual(Inbox.items(rows: [], prompts: [], cards: [try card(json)]).map(\.wire),
                       [.endedWork])
    }

    // MARK: ranking, grouping, counting

    func testItemsRankByKindThenWireThenTitleThenID() throws {
        let rows = [
            try row(#"{"session_id":"s1","nickname":"Ada"}"#),
            try row(#"{"session_id":"s2","nickname":"Bea","question":{"text":"?"}}"#),
            try row(#"{"session_id":"s3","nickname":"Cy"}"#)
        ]
        let prompts = [try prompt(#"{"request_id":"r1","session_id":"s3","tool_name":"Bash"}"#)]
        let cards = [
            try card(planned),
            try card(#"{"id":"e","needs_you":true,"title":"zeta"}"#),
            try card(#"{"id":"m","manual_steps":"x","manual_check_due":true,"title":"alpha"}"#),
            try card(#"{"id":"d","column_name":"done","closed_by":"s9"}"#)
        ]
        let items = Inbox.items(rows: rows, prompts: prompts, cards: cards)
        XCTAssertEqual(items.map(\.wire),
                       [.permission, .question, .endedWork, .manualCheck, .waiting])
        XCTAssertEqual(items.map(\.kind), [.answer, .answer, .look, .look, .stopped])
    }

    func testTiesBreakOnTitleThenID() throws {
        let cards = [
            try card(#"{"id":"z","needs_you":true,"title":"same"}"#),
            try card(#"{"id":"a","needs_you":true,"title":"same"}"#),
            try card(#"{"id":"b","needs_you":true,"title":"earlier"}"#)
        ]
        let items = Inbox.items(rows: [], prompts: [], cards: cards)
        XCTAssertEqual(items.map(\.cardId), ["b", "a", "z"])
    }

    func testGroupsFollowProjectOrderWithTheNamelessOneLast() throws {
        let cards = [
            try card(#"{"id":"1","needs_you":true,"project":"zeta"}"#),
            try card(#"{"id":"2","needs_you":true}"#),
            try card(#"{"id":"3","needs_you":true,"project":"alpha"}"#)
        ]
        let groups = Inbox.groups(Inbox.items(rows: [], prompts: [], cards: cards))
        XCTAssertEqual(groups.map(\.project), ["alpha", "zeta", ""])
        XCTAssertEqual(groups.map(\.heading), ["alpha", "zeta", "Other"])
        XCTAssertEqual(groups.map { $0.items.count }, [1, 1, 1])
    }

    func testEveryEntryCountsAndNothingFYIIsListed() throws {
        let cards = [
            try card(planned),
            try card(#"{"id":"e","needs_you":true}"#),
            try card(#"{"id":"m","manual_steps":"x","manual_check_due":true}"#),
            try card(#"{"id":"d","column_name":"done","closed_by":"s9"}"#)
        ]
        let rows = [try row(#"{"session_id":"s1","nickname":"Ada"}"#)]
        let items = Inbox.items(rows: rows, prompts: [], cards: cards)
        // The plan and the closed card are not here; everything that is
        // blocks somebody, so the count is the length and nothing else.
        XCTAssertEqual(items.count, 3)
    }

    func testAnEmptyInboxGroupsNothing() {
        XCTAssertTrue(Inbox.groups([]).isEmpty)
    }

    // MARK: Dark Army's words, or nothing

    func testARefusalWinsThenTheQueueReasonThenNothing() throws {
        let item = Inbox.items(rows: [], prompts: [],
                               cards: [try card(#"{"id":"e","needs_you":true}"#)])[0]
        XCTAssertEqual(Inbox.sentence(for: item, refusal: "no.", queueReason: "waiting."), "no.")
        XCTAssertEqual(Inbox.sentence(for: item, refusal: "  ", queueReason: "waiting."), "waiting.")
        XCTAssertEqual(Inbox.sentence(for: item), "")
    }

    func testTargetAndWireMakeTheIdentity() throws {
        let items = Inbox.items(rows: [], prompts: [],
                                cards: [try card(#"{"id":"e","needs_you":true}"#)])
        XCTAssertEqual(items[0].id, "c:e#\(InboxWireKind.endedWork.rawValue)")
        XCTAssertEqual(Set(items.map(\.id)).count, items.count)
    }

    // MARK: dismiss-hide

    func testMatchingAckOmitsTheSubject() throws {
        let asked = try row(#"{"session_id":"q1","question":{"text":"ok?"}}"#)
        XCTAssertEqual(Inbox.items(rows: [asked], prompts: [], cards: []).count, 1)
        let fp = InboxFingerprint.value(
            wire: .question, questions: asked.agent.questionList)
        let hidden = Inbox.items(
            rows: [asked], prompts: [], cards: [],
            acks: [InboxAckRecord(key: "s:q1", kind: "question", fp: fp)])
        XCTAssertTrue(hidden.isEmpty)
    }

    func testAnEndedCardCanBeDismissed() throws {
        let ended = try card(#"{"id":"e","needs_you":true}"#)
        let fp = InboxFingerprint.value(wire: .endedWork, card: ended)
        XCTAssertEqual(fp, InboxFingerprint.value(kind: "ended_work", material: ""))
        let hidden = Inbox.items(
            rows: [], prompts: [], cards: [ended],
            acks: [InboxAckRecord(key: "c:e", kind: "ended_work", fp: fp)])
        XCTAssertTrue(hidden.isEmpty)
    }

    func testMismatchedFingerprintLeavesTheRow() throws {
        let asked = try row(#"{"session_id":"q1","question":{"text":"ok?"}}"#)
        let hidden = Inbox.items(
            rows: [asked], prompts: [], cards: [],
            acks: [InboxAckRecord(key: "s:q1", kind: "question",
                                  fp: "question:deadbeef")])
        XCTAssertEqual(hidden.map(\.wire), [.question])
    }

    func testPermissionStaysWhenAnAckForThatKeyExists() throws {
        let waiter = try row(#"{"session_id":"p1"}"#)
        let ask = try prompt(
            #"{"session_id":"p1","request_id":"r","summary":"Bash"}"#)
        let items = Inbox.items(
            rows: [waiter], prompts: [ask], cards: [],
            acks: [InboxAckRecord(key: "s:p1", kind: "waiting", fp: "waiting")])
        XCTAssertEqual(items.map(\.wire), [.permission])
    }

    func testWaitingFingerprintIsTheToken() {
        XCTAssertEqual(InboxFingerprint.value(wire: .waiting), "waiting")
    }

    func testQuestionIdPresentIgnoresText() {
        var withId = AgentQuestion()
        withId.id = "tool-1"
        withId.text = "hello?"
        var renamed = withId
        renamed.text = "changed wording"
        XCTAssertEqual(
            InboxFingerprint.questionMaterial([withId]),
            InboxFingerprint.questionMaterial([renamed]))
        XCTAssertEqual(
            InboxFingerprint.value(wire: .question, questions: [withId]),
            InboxFingerprint.value(wire: .question, questions: [renamed]))
    }

    func testEmptyQuestionIdUsesText() {
        var noId = AgentQuestion()
        noId.text = "hello?"
        XCTAssertEqual(InboxFingerprint.questionMaterial([noId]), "hello?")
        var other = noId
        other.text = "changed"
        XCTAssertNotEqual(
            InboxFingerprint.value(wire: .question, questions: [noId]),
            InboxFingerprint.value(wire: .question, questions: [other]))
    }

    func testDefaultAcksKeepTodaysList() throws {
        let asked = try row(#"{"session_id":"q1","question":{"text":"ok?"}}"#)
        XCTAssertEqual(
            Inbox.items(rows: [asked], prompts: [], cards: []).map(\.wire),
            Inbox.items(rows: [asked], prompts: [], cards: [], acks: []).map(\.wire))
    }
}

extension InboxTests {
    func testCodexPendingQuestionAndAnsweredSnapshot() throws {
        let pending = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"codex:root","provider":"codex","can_type":false,"channel":false,"question":{"id":"ask","text":"Choose the surface?","options":["Panel"]},"interaction_note":"Answer in the original Codex session.","subagent_rows":[{"subagent_type":"bc-planner","activity":"working"}]}"#.utf8))
        let items = Inbox.items(rows: [SectionRow(agent: pending, category: .waiting)], prompts: [], cards: [])
        XCTAssertEqual(items.map(\.wire), [.question])
        XCTAssertFalse(pending.canType)
        XCTAssertFalse(pending.channel)
        XCTAssertTrue(pending.interactionNote.contains("original Codex session"))
        let answered = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"codex:root","provider":"codex","questions":[],"question":{},"subagent_rows":[]}"#.utf8))
        XCTAssertTrue(answered.questionList.isEmpty)
        // Inbox receives the attention slice, so an answered running row is absent.
        XCTAssertTrue(Inbox.items(rows: [], prompts: [], cards: []).isEmpty)
    }
}
