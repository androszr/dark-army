import XCTest
@testable import BobPanel

/// The band on a card: which stages it lists, in what order, in what phase,
/// whose face each wears, what it drops when the row is full and what it says
/// out loud.
///
/// Pure model assertions on `CrewBand`'s computed properties and statics — no
/// window is opened and nothing is measured. The geometry is constants
/// (`faceSize`, `stripCap`) and the one thing a test cannot see is whether
/// they add up on a real display, which is the manual check the plan carries.
final class CrewBandTests: XCTestCase {

    private func band(workflow: [String] = [], trail: [String] = [],
                      live: Set<String> = [], crew: [String: String] = [:]) -> CrewBand {
        CrewBand(workflow: workflow, trail: trail, live: live, crew: crew)
    }

    // ── order ────────────────────────────────────────────────────────────

    /// Declared first, in the order declared; then anything observed that was
    /// never declared. What ran outranks the forecast and is never dropped.
    func testDeclaredOrderThenUndeclaredObserved() {
        let b = band(workflow: ["bc-planner", "bc-verifier"],
                     trail: ["bc-planner", "bc-implementer"])
        XCTAssertEqual(b.stages.map(\.name),
                       ["bc-planner", "bc-verifier", "bc-implementer"])
    }

    func testADuplicateIsListedOnce() {
        let b = band(workflow: ["bc-planner", "bc-planner"], trail: ["bc-planner"])
        XCTAssertEqual(b.stages.map(\.name), ["bc-planner"])
    }

    // ── phases ───────────────────────────────────────────────────────────

    func testTheThreePhases() {
        let b = band(workflow: ["bc-planner", "bc-implementer", "bc-verifier"],
                     trail: ["bc-planner", "bc-implementer"],
                     live: ["bc-implementer"])
        XCTAssertEqual(b.stages.map(\.phase), [.done, .active, .pending])
    }

    /// A card whose session has ended has an empty live set, and every stage
    /// it reached stays done rather than becoming pending.
    func testNoLiveSetLeavesTheTrailDone() {
        let b = band(workflow: ["bc-planner"], trail: ["bc-planner"])
        XCTAssertEqual(b.stages.map(\.phase), [.done])
    }

    // ── faces ────────────────────────────────────────────────────────────

    /// A recorded character wins; a stage without one falls back to its
    /// role's anchor. A pending stage never names anybody.
    func testAPendingStageDrawsTheAnchorAndNoName() {
        let b = band(workflow: ["bc-verifier"], trail: [], crew: [:])
        let stage = b.stages[0]
        XCTAssertEqual(stage.phase, .pending)
        XCTAssertEqual(stage.character, "")
        XCTAssertEqual(CrewBand.face(stage), "")
        XCTAssertEqual(CrewBand.line(stage), "expected")
    }

    func testARecordedCharacterWinsOverTheAnchor() {
        let b = band(workflow: ["bc-verifier"], trail: ["bc-verifier"],
                     crew: ["bc-verifier": "zosia"])
        XCTAssertEqual(CrewBand.face(b.stages[0]), "zosia")
        XCTAssertEqual(CrewBand.line(b.stages[0]), "Zosia · done")
    }

    /// A card recorded before the crew existed has no characters at all, and
    /// draws exactly what the board drew before this arrived.
    func testACardWithNoCrewFallsBackToAnchorsThroughout() {
        let b = band(workflow: ["bc-planner", "bc-implementer"],
                     trail: ["bc-planner"])
        XCTAssertEqual(b.stages.map { CrewBand.face($0) }, ["", ""])
    }

    /// A role Dark Army has never heard of still gets one stable face rather than
    /// none — `Specialists.face(for:)`'s hash, unchanged.
    func testAnUnknownStageStillWearsAFace() {
        let b = band(trail: ["totally-made-up-agent"])
        let face = CrewBand.face(b.stages[0])
        XCTAssertFalse(face.isEmpty)
        XCTAssertEqual(face, Specialists.face(for: "totally-made-up-agent"))
    }

    // ── the cap ──────────────────────────────────────────────────────────

    /// Five non-running stages against a cap of four: one goes, and the `+n`
    /// says how many the row is not drawing.
    func testTheCapDropsTheOldestDoneFirst() {
        let names = ["a", "b", "c", "d", "e"]
        let b = band(workflow: names, trail: names)
        XCTAssertEqual(b.drawnStrip.map(\.name), ["b", "c", "d", "e"])
        XCTAssertEqual(b.droppedCount, 1)
    }

    /// The running stage is never dropped, whatever else is on the row: it is
    /// the fact the band is consulted for and the only one carrying a name.
    func testTheRunningStageSurvivesAFullRow() {
        let names = ["a", "b", "c", "d", "e", "f"]
        let b = band(workflow: names, trail: names, live: ["a"])
        XCTAssertTrue(b.drawnStrip.contains { $0.name == "a" })
        XCTAssertEqual(b.drawnStrip.filter { $0.phase != .active }.count,
                       CrewBand.stripCap)
        XCTAssertEqual(b.droppedCount, names.count - b.drawnStrip.count)
    }

    /// Pending stages go before done ones survive: the recent history is the
    /// useful half, and the last-declared forecast is the first to go.
    func testPendingGoesLastDeclaredFirst() {
        let b = band(workflow: ["p1", "p2", "p3", "p4", "p5"], trail: [])
        XCTAssertEqual(b.drawnStrip.map(\.name), ["p1", "p2", "p3", "p4"])
    }

    func testAShortRowDropsNothing() {
        let b = band(workflow: ["a", "b"], trail: ["a"])
        XCTAssertEqual(b.drawnStrip.count, 2)
        XCTAssertEqual(b.droppedCount, 0)
    }

    // ── the sentence ─────────────────────────────────────────────────────

    func testTheRunningCaption() {
        let caption = CrewBand.caption(
            workflow: ["bc-planner", "bc-implementer", "bc-verifier"],
            trail: ["bc-planner", "bc-implementer"],
            live: ["bc-implementer"],
            crew: ["bc-planner": "overwatch", "bc-implementer": "relay"])
        XCTAssertEqual(caption, "Relay running bc-implementer · step 2 of 3")
    }

    func testTheQuietCaptionNamesWhoIsDone() {
        let caption = CrewBand.caption(
            workflow: ["bc-planner", "bc-verifier"],
            trail: ["bc-planner"], live: [],
            crew: ["bc-planner": "overwatch"])
        XCTAssertEqual(caption, "1 of 2 done: Overwatch")
    }

    func testNothingDoneYetStillCounts() {
        let caption = CrewBand.caption(workflow: ["bc-planner"], trail: [],
                                       live: [], crew: [:])
        XCTAssertEqual(caption, "0 of 1 done")
    }

    /// Nothing at all where there is nothing to say — the band's whole rule,
    /// and the caption follows it so a row never speaks an empty sentence.
    func testTheEmptyCaseSaysNothing() {
        XCTAssertTrue(band().stages.isEmpty)
        XCTAssertEqual(CrewBand.caption(workflow: [], trail: [], live: [],
                                        crew: [:]), "")
    }

    /// `overwatch` is not what a sentence says out loud, and a cast name keeps
    /// its own spelling rather than the slug's.
    func testTheSpokenSpelling() {
        XCTAssertEqual(CrewBand.display("overwatch"), "Overwatch")
        XCTAssertEqual(CrewBand.display("zosia"), "Zosia")
        XCTAssertEqual(CrewBand.display("relay"), "Relay")
    }

    // ── the step counter ─────────────────────────────────────────────────

    /// The one honest gap the band had: the arithmetic existed, but only as
    /// `caption`'s spoken text, so on screen four-done-of-six and
    /// one-done-of-six looked the same.

    func testNoStagesCountsNothing() {
        XCTAssertEqual(CrewBand.counter(workflow: [], trail: [], live: [],
                                        crew: [:]), "")
    }

    func testAllPendingReadsZeroOfN() {
        XCTAssertEqual(
            CrewBand.counter(workflow: ["bc-planner", "bc-implementer"],
                             trail: [], live: [], crew: [:]),
            "0 of 2")
    }

    func testSomeDoneAndNoneActive() {
        XCTAssertEqual(
            CrewBand.counter(
                workflow: ["bc-planner", "bc-implementer", "bc-verifier"],
                trail: ["bc-planner", "bc-implementer"], live: [], crew: [:]),
            "2 of 3")
    }

    /// The number is the running stage's **position**, which is what "which
    /// step is this?" means — not how many finished before it.
    func testOneActiveMidListNamesItsPosition() {
        XCTAssertEqual(
            CrewBand.counter(
                workflow: ["bc-planner", "bc-implementer", "bc-verifier"],
                trail: ["bc-planner", "bc-implementer"],
                live: ["bc-implementer"], crew: [:]),
            "step 2 of 3")
    }

    func testTheLastStageActive() {
        XCTAssertEqual(
            CrewBand.counter(
                workflow: ["bc-planner", "bc-implementer", "bc-verifier"],
                trail: ["bc-planner", "bc-implementer", "bc-verifier"],
                live: ["bc-verifier"], crew: [:]),
            "step 3 of 3")
    }

    /// An observed stage nobody declared still counts — `stages` puts it on
    /// the end, and the counter is arithmetic over exactly that list.
    func testAnUndeclaredObservedStageIsCounted() {
        XCTAssertEqual(
            CrewBand.counter(workflow: ["bc-planner"],
                             trail: ["bc-planner", "bc-implementer"],
                             live: [], crew: [:]),
            "2 of 2")
    }

    /// The visible counter and the spoken caption are one arithmetic. They
    /// used to be two: the strip drew "step 2 of 3" inside a container
    /// VoiceOver read as "1 done of 3", so the person who could not see the
    /// faces was told a different number about the same band.
    func testTheSpokenCaptionCarriesTheDrawnCount() {
        let cases: [(w: [String], t: [String], l: Set<String>)] = [
            (["bc-planner", "bc-implementer", "bc-verifier"],
             ["bc-planner", "bc-implementer"], ["bc-implementer"]),
            (["bc-planner", "bc-verifier"], ["bc-planner"], []),
            (["bc-planner"], [], []),
            (["a", "b", "c", "d", "e", "f"], ["a", "b", "c", "d", "e", "f"],
             ["f"]),
        ]
        for c in cases {
            let count = CrewBand.counter(workflow: c.w, trail: c.t,
                                         live: c.l, crew: [:])
            let caption = CrewBand.caption(workflow: c.w, trail: c.t,
                                           live: c.l, crew: [:])
            XCTAssertFalse(count.isEmpty)
            XCTAssertTrue(caption.contains(count),
                          "caption \(caption) does not carry \(count)")
        }
        // And nothing to say stays nothing to say, on both.
        XCTAssertEqual(CrewBand.counter(workflow: [], trail: [], live: [],
                                        crew: [:]), "")
        XCTAssertEqual(CrewBand.caption(workflow: [], trail: [], live: [],
                                        crew: [:]), "")
    }

    /// The counter is an addition and nothing else: the never-drop rule and
    /// the cap are unchanged for the same inputs, and the caption states the
    /// same figures it always did in the same words.
    func testTheBandItselfIsUnchanged() {
        let workflow = ["bc-planner", "bc-implementer", "bc-verifier"]
        let trail = ["bc-planner", "bc-implementer"]
        let live: Set<String> = ["bc-implementer"]
        let b = band(workflow: workflow, trail: trail, live: live)
        XCTAssertEqual(b.drawnStrip.map(\.name), workflow)
        XCTAssertEqual(b.droppedCount, 0)
        XCTAssertEqual(CrewBand.caption(workflow: workflow, trail: trail,
                                        live: live, crew: [:]),
                       "bc-implementer · step 2 of 3")
        // And the running stage is still never dropped, however full the row.
        let long = ["a", "b", "c", "d", "e", "f"]
        let full = band(workflow: long, trail: long, live: ["f"])
        XCTAssertTrue(full.drawnStrip.contains { $0.name == "f" })
        XCTAssertEqual(CrewBand.counter(workflow: long, trail: long,
                                        live: ["f"], crew: [:]),
                       "step 6 of 6")
    }
}

extension CrewBandTests {
    func testCodexCanonicalStageSurvivesCompletedHelperWire() throws {
        let card = try JSONDecoder().decode(BoardCard.self, from: Data(#"{"id":"parity","workflow":"bc-implementer\nbc-verifier","agent_trail":"bc-implementer","crew":{"bc-implementer":"relay"}}"#.utf8))
        let live = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"codex:root","provider":"codex","subagent_rows":[{"subagent_type":"bc-implementer","activity":"working"}]}"#.utf8))
        let active = CrewBand(workflow: card.workflow, trail: card.agentTrail,
                              live: Set(live.subagentRows.map(\.label)), crew: card.crew)
        XCTAssertEqual(active.stages.map(\.phase), [.active, .pending])
        let ended = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"codex:root","subagent_rows":[]}"#.utf8))
        let history = CrewBand(workflow: card.workflow, trail: card.agentTrail,
                               live: Set(ended.subagentRows.map(\.label)), crew: card.crew)
        XCTAssertEqual(history.stages.map(\.phase), [.done, .pending])
        XCTAssertEqual(history.stages[0].character, "relay")
    }
    func test_lead() {
        let b = CrewBand(workflow: [], trail: [], live: [], crew: [:], lead: "backbone")
        XCTAssertEqual(b.leadFace, "relay")
        XCTAssertEqual(CrewBand.caption(workflow: [], trail: [], live: [], crew: [:], lead: "backbone"), "Relay, Backbone lead")
        XCTAssertTrue(b.stages.isEmpty)
    }
    func test_marker() {
        let b = band(workflow: ["bc-implementer"])
        XCTAssertEqual(CrewBand.face(b.stages[0]), "")
        XCTAssertEqual(Specialists.short(b.stages[0].name), "impl")
    }
    func test_recordedFaceSurvives() {
        let b = band(trail: ["bc-verifier"], crew: ["bc-verifier": "ledger"])
        XCTAssertEqual(CrewBand.face(b.stages[0]), "ledger")
        XCTAssertEqual(CrewBand.line(b.stages[0]), "Ledger · done")
    }

}
