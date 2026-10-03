import ActivityKit
import XCTest
@testable import BobPhone

/// `NeedsYouActivityRule` over literal snapshots: which decision is the live
/// card, what it says, and what step follows from the card already up.
/// Pure — no ActivityKit, no controller.
@MainActor
final class LiveActivityRuleTests: XCTestCase {

    private func snapshot(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    private func state(_ kind: String = "attention", nickname: String = "Vex",
                       slug: String = "vex", work: String = "", since: Double = 100,
                       sessionId: String = "s1", working: Int? = nil, needsYou: Int? = nil,
                       standingBy: Int? = nil, costUsd: Double? = nil,
                       tokensK: Int? = nil) -> NeedsYouAttributes.ContentState {
        NeedsYouAttributes.ContentState(nickname: nickname, slug: slug, kind: kind,
                                        work: work, since: since, sessionId: sessionId,
                                        working: working, needsYou: needsYou,
                                        standingBy: standingBy, costUsd: costUsd,
                                        tokensK: tokensK)
    }

    // MARK: - the subject

    func testTheSubjectIsTheFirstSessionItemAndCardsAreSkipped() throws {
        // Two card entries rank above the waiting row (`look` before
        // `stopped`) and neither carries a face: the session is the card.
        let snap = try snapshot("""
        {
          "generated_at": 1000,
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":40}
          ]},
          "board": {"cards": [
            {"id":"e1","title":"Ended","project":"P","needs_you":true},
            {"id":"m1","title":"Manual","project":"P",
             "manual_check_due":true,"manual_steps":"look"}
          ]}
        }
        """)
        XCTAssertEqual(snap.decisionItems.map(\.target.key), ["c:e1", "c:m1", "s:w1"])
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap))
        XCTAssertEqual(subject.sessionId, "w1")
        XCTAssertEqual(subject.kind, "attention")
        XCTAssertEqual(subject.nickname, "Vex")
        XCTAssertEqual(subject.slug, "vex")
        XCTAssertEqual(subject.since, 960)
    }

    func testAPromptOutranksAQuestionOutranksAWaitingRow() throws {
        let snap = try snapshot("""
        {
          "generated_at": 1000,
          "agents": {
            "running": [{"session_id":"p1","nickname":"Cipher","project":"P","idle_seconds":5}],
            "waiting": [
              {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":900},
              {"session_id":"q1","nickname":"Mira","project":"P","idle_seconds":9,
               "questions":[{"text":"ok?"}]}
            ]
          },
          "permissions": [
            {"request_id":"pr","session_id":"p1","tool_name":"Bash","description":"ls"}
          ]
        }
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: snap)?.sessionId, "p1")
        XCTAssertEqual(NeedsYouActivityRule.subject(from: snap)?.kind, "permission")

        let noPrompt = try snapshot("""
        {
          "generated_at": 1000,
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":900},
            {"session_id":"q1","nickname":"Mira","project":"P","idle_seconds":9,
             "questions":[{"text":"ok?"}]}
          ]}
        }
        """)
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: noPrompt))
        XCTAssertEqual(subject.sessionId, "q1")
        XCTAssertEqual(subject.kind, "question")
    }

    func testAnAcknowledgedItemIsNotASubject() throws {
        let snap = try snapshot("""
        {
          "generated_at": 1000,
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":40}
          ]},
          "inbox": {"available": true, "acks": [
            {"key":"s:w1","kind":"waiting","fp":"waiting"}
          ]}
        }
        """)
        XCTAssertTrue(snap.decisionItems.isEmpty)
        XCTAssertNil(NeedsYouActivityRule.subject(from: snap))
    }

    func testAnEmptyListHasNoSubject() throws {
        XCTAssertNil(NeedsYouActivityRule.subject(from: try snapshot("{}")))
        let running = try snapshot("""
        {"agents": {"running": [{"session_id":"r1","nickname":"Vex","project":"P"}]}}
        """)
        XCTAssertNil(NeedsYouActivityRule.subject(from: running))
    }

    func testAnOrphanPermissionWithNoRowIsNotASubject() throws {
        // A published prompt with no agent row is a read-only inbox entry;
        // with no row there is no face and no clock, so no card.
        let snap = try snapshot("""
        {
          "permissions": [
            {"request_id":"pr","session_id":"ghost","tool_name":"Bash","description":"ls"}
          ]
        }
        """)
        XCTAssertEqual(snap.decisionItems.count, 1)
        XCTAssertNil(NeedsYouActivityRule.subject(from: snap))
    }

    // MARK: - the work line

    func testWorkIsTheBoundCardsTitleElseTheDaemonsCardLineElseTheName() throws {
        let bound = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","name":"the row's name","project":"P","idle_seconds":4}
          ]},
          "board": {"cards": [
            {"id":"c1","title":"  Rename   the strip ","project":"P","session_id":"w1","link_state":"live"}
          ]}
        }
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: bound)?.work, "Rename the strip")

        let refined = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","name":"the row's name","project":"P","idle_seconds":4}
          ]},
          "board": {"cards": [
            {"id":"c1","title":"Plan it","project":"P","refine_session_id":"w1","refine_state":"live"}
          ]}
        }
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: refined)?.work, "Plan it")

        let named = try snapshot("""
        {"agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","name":"the row's name","project":"P","idle_seconds":4}
        ]}}
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: named)?.work, "the row's name")

        // The daemon publishes `card_title` only where the row would be
        // unnamed; the phone takes that line over the placeholder name and
        // never learns the placeholder string itself.
        let unnamed = try snapshot("""
        {"agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","name":"placeholder","card_title":"Dark Army's card",
           "project":"P","idle_seconds":4}
        ]}}
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: unnamed)?.work, "Dark Army's card")
    }

    func testAnUnnamedCardlessRowSaysNothingNeverThePlaceholder() throws {
        // The daemon publishes `unnamed: true` where the row wears its
        // placeholder name; with no card to name it, `work` is empty — the
        // Mac's `_compose_push_work` — so the phone's card and the Mac's
        // first update agree. The subject itself still stands.
        let bare = try snapshot("""
        {"agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","name":"placeholder","unnamed":true,
           "project":"P","idle_seconds":4}
        ]}}
        """)
        let subject = NeedsYouActivityRule.subject(from: bare)
        XCTAssertEqual(subject?.sessionId, "w1")
        XCTAssertEqual(subject?.work, "")

        // An older daemon sends no flag: the name draws, as it always did.
        let older = try snapshot("""
        {"agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","name":"placeholder","project":"P","idle_seconds":4}
        ]}}
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: older)?.work, "placeholder")
    }

    func testWorkIsClampedAtEightyWithAnEllipsis() throws {
        let long = String(repeating: "x", count: 200)
        let snap = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":4}
          ]},
          "board": {"cards": [
            {"id":"c1","title":"\(long)","project":"P","session_id":"w1","link_state":"live"}
          ]}
        }
        """)
        let work = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap)?.work)
        XCTAssertEqual(work.count, NeedsYouActivityRule.workChars)
        XCTAssertTrue(work.hasSuffix("\u{2026}"))
        XCTAssertEqual(NeedsYouActivityRule.workChars, 80)
    }

    func testSinceIsTheFramesClockMinusTheIdleFigure() throws {
        let snap = try snapshot("""
        {"generated_at": 5000, "agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":123}
        ]}}
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: snap)?.since, 4877)
        // The daemon's own stamp on the row wins over the arithmetic: it is
        // the number the Mac's push carries, so the two ends agree exactly.
        let stamped = try snapshot("""
        {"generated_at": 5000, "agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":123,
           "quiet_since":4870}
        ]}}
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: stamped)?.since, 4870)
        // An undated frame leaves the clock at zero rather than inventing one.
        let undated = try snapshot("""
        {"agents": {"waiting": [
          {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":123}
        ]}}
        """)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: undated)?.since, 0)
    }

    func testANicknamelessRowFallsBackToTheItemsTitle() throws {
        let snap = try snapshot("""
        {"agents": {"waiting": [
          {"session_id":"w1","name":"Bare","project":"P","idle_seconds":4}
        ]}}
        """)
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap))
        XCTAssertEqual(subject.nickname, "Bare")
        XCTAssertFalse(subject.slug.isEmpty)   // the session-id hash, never empty
    }

    // MARK: - the plan

    func testPlanNothingToNothingIsNone() {
        XCTAssertEqual(NeedsYouActivityRule.plan(current: nil, subject: nil), .none)
    }

    func testPlanNothingToSomethingIsStart() {
        let s = state()
        XCTAssertEqual(NeedsYouActivityRule.plan(current: nil, subject: s), .start(s))
    }

    func testPlanSameToSameIsNoneEvenWithAClockTick() {
        let a = state()
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: a), .none)
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: state(since: 101)), .none)
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: state(since: 103)),
                       .update(state(since: 103)))
    }

    func testPlanChangedIsUpdate() {
        let a = state()
        let b = state("question", nickname: "Mira", slug: "mira", sessionId: "s2")
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: b), .update(b))
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: state(work: "x")),
                       .update(state(work: "x")))
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: state("permission")),
                       .update(state("permission")))
    }

    func testPlanSomethingToNothingIsEnd() {
        XCTAssertEqual(NeedsYouActivityRule.plan(current: state(), subject: nil), .end)
    }

    // MARK: - what is standing

    func testAnActivityStillUpIsTheRememberedState() {
        let s = state()
        XCTAssertEqual(NeedsYouActivityRule.standing(remembered: s, activityState: .active,
                                                     listed: true), s)
        XCTAssertEqual(NeedsYouActivityRule.standing(remembered: s, activityState: .stale,
                                                     listed: true), s)
    }

    func testAnActivityEndedOutFromUnderTheControllerIsNothing() {
        let s = state()
        XCTAssertNil(NeedsYouActivityRule.standing(remembered: s, activityState: .ended,
                                                   listed: true))
        XCTAssertNil(NeedsYouActivityRule.standing(remembered: s, activityState: .dismissed,
                                                   listed: true))
        XCTAssertNil(NeedsYouActivityRule.standing(remembered: s, activityState: .active,
                                                   listed: false))
        XCTAssertNil(NeedsYouActivityRule.standing(remembered: s, activityState: nil,
                                                   listed: true))
        XCTAssertNil(NeedsYouActivityRule.standing(remembered: nil, activityState: .active,
                                                   listed: true))
    }

    func testAfterAnExternalEndTheNextSubjectIsAStartNeverAnUpdate() {
        // The Mac pushed `end` while the app was alive in the background (or
        // the person swiped the card off): the controller still remembers a
        // state, ActivityKit says ended, and the next agent must get a card.
        let gone = state()
        let next = state("question", nickname: "Mira", slug: "mira", sessionId: "s2")
        let current = NeedsYouActivityRule.standing(remembered: gone, activityState: .ended,
                                                    listed: true)
        XCTAssertEqual(NeedsYouActivityRule.plan(current: current, subject: next), .start(next))
        XCTAssertEqual(NeedsYouActivityRule.plan(current: current, subject: nil), .none)
        // Still up: the same picture is an update, as before.
        let up = NeedsYouActivityRule.standing(remembered: gone, activityState: .active,
                                               listed: true)
        XCTAssertEqual(NeedsYouActivityRule.plan(current: up, subject: next), .update(next))
    }

    // MARK: - the marker

    func testRegistrationIsGatedOnTheMacsMarkerWhichDecodesFalseWhenAbsent() throws {
        let older = try snapshot("{}")
        XCTAssertFalse(older.board.liveActivitySupported)
        let newer = try snapshot("""
        {"board": {"live_activity_supported": true}}
        """)
        XCTAssertTrue(newer.board.liveActivitySupported)
        XCTAssertEqual(PhoneActions.registerActivityToken, "register_activity_token")
    }

    func testTheControllerReadsTheMarkerOffEverySnapshotAndAnEmptyListRequestsNothing() throws {
        // An empty picture: the plan is `.none`, so no `Activity.request`
        // runs here; what is exercised is the marker read the token send is
        // gated on. Against the older Mac it reads false, so no
        // `register_activity_token` can be posted.
        let controller = LiveActivityController.shared
        let client = PhoneClient()
        controller.reconcile(snapshot: try snapshot("{}"), client: client)
        XCTAssertFalse(controller.supported)
        XCTAssertNil(controller.activity)
        controller.reconcile(snapshot: try snapshot("""
        {"board": {"live_activity_supported": true}}
        """), client: client)
        XCTAssertTrue(controller.supported)
        XCTAssertNil(controller.activity)
        XCTAssertNil(controller.current)
    }

    // MARK: - the fleet card

    func testWithoutTheFleetMarkerTheCardIsTodaysSubject() throws {
        let workingOnly = try snapshot("""
        {"agents": {"running": [{"session_id":"r1","nickname":"Vex","project":"P"}]},
         "counts": {"working": 1}}
        """)
        XCTAssertFalse(workingOnly.board.liveActivityFleet)
        XCTAssertNil(NeedsYouActivityRule.fleetState(from: workingOnly))
        XCTAssertNil(NeedsYouActivityRule.subject(from: workingOnly))
    }

    func testAWorkingFleetDrawsCountsAndNoFace() throws {
        let snap = try snapshot("""
        {
          "agents": {"running": [
            {"session_id":"r1","nickname":"Vex","project":"P"}
          ]},
          "counts": {"working": 1, "idle": 0, "attention": 0},
          "fleet_figures": {"cost_usd": 1.5, "tokens_k": 45, "cost_measured": 1, "cost_rows": 1,
                            "cost_usd_hour": 4.2, "tokens_k_hour": 1100},
          "board": {"live_activity_fleet": true}
        }
        """)
        let state = try XCTUnwrap(NeedsYouActivityRule.fleetState(from: snap))
        XCTAssertEqual(state.working, 1)
        XCTAssertEqual(state.needsYou, 0)
        XCTAssertEqual(state.standingBy, 0)
        XCTAssertFalse(state.hasFace)
        XCTAssertEqual(state.costUsd, 1.5)
        XCTAssertEqual(state.tokensK, 45)
        XCTAssertEqual(state.costUsdHour, 4.2)
        XCTAssertEqual(state.tokensKHour, 1100)
    }

    func testAWaiterSitsOnTopOfTheCounts() throws {
        let snap = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":4}
          ]},
          "counts": {"working": 2, "idle": 0, "attention": 1},
          "fleet_figures": {"cost_usd": 2, "tokens_k": 10},
          "board": {"live_activity_fleet": true}
        }
        """)
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap))
        let state = try XCTUnwrap(NeedsYouActivityRule.fleetState(from: snap))
        XCTAssertTrue(state.hasFace)
        XCTAssertEqual(state.sessionId, subject.sessionId)
        XCTAssertEqual(state.nickname, subject.nickname)
        XCTAssertEqual(state.kind, subject.kind)
        XCTAssertEqual(state.working, 2)
        XCTAssertEqual(state.needsYou, 1)
        XCTAssertEqual(state.standingBy, 0)
    }

    func testOnlyIdleRowsAreNotACard() throws {
        let snap = try snapshot("""
        {
          "agents": {"sleeping": [
            {"session_id":"s1","nickname":"Vex","project":"P"}
          ]},
          "counts": {"working": 0, "idle": 3, "attention": 0},
          "board": {"live_activity_fleet": true}
        }
        """)
        XCTAssertNil(NeedsYouActivityRule.fleetState(from: snap))
    }

    func testMissingFleetFiguresAreNilNeverZero() throws {
        let snap = try snapshot("""
        {
          "agents": {"running": [
            {"session_id":"r1","nickname":"Vex","project":"P"}
          ]},
          "counts": {"working": 1},
          "board": {"live_activity_fleet": true}
        }
        """)
        let state = try XCTUnwrap(NeedsYouActivityRule.fleetState(from: snap))
        XCTAssertNil(state.costUsd)
        XCTAssertNil(state.tokensK)
    }

    func testAFleetPictureUpdatesWhenTheFaceLeavesAndEndsWhenTheFleetDoes() {
        let fleet = state(sessionId: "s1", working: 1, needsYou: 1, standingBy: 0, costUsd: 1)
        let faceGone = state(sessionId: "", working: 1, needsYou: 0, standingBy: 0, costUsd: 1)
        XCTAssertEqual(NeedsYouActivityRule.plan(current: fleet, subject: faceGone), .update(faceGone))
        XCTAssertEqual(NeedsYouActivityRule.plan(current: fleet, subject: nil), .end)
    }

    func testSameTreatsAMissingCostAsDifferentFromZeroAndTheRegistrationSaysShapeTwo() {
        XCTAssertFalse(NeedsYouActivityRule.same(state(costUsd: nil), state(costUsd: 0)))
        XCTAssertEqual(
            LiveActivityController.registrationFields(token: "ab", env: "dev"),
            ["token": "ab", "env": "dev", "shape": "2"])
    }

    // MARK: - a review run waiting on picks

    private func reviewSnapshot(sessionId: String = "w1", extra: String = "",
                                waiting: String = "") throws -> Snapshot {
        try snapshot("""
        {
          "generated_at": 1000,
          "agents": {"waiting": [\(waiting)]\(extra)},
          "review": {"available": true, "runs": [
            {"id":"r-1","project":"P","state":"picks","session_id":"\(sessionId)",
             "started_at":500,"findings_at":900,
             "findings":[{"index":1,"line":"a"},{"index":2,"line":"b"}]}
          ]}
        }
        """)
    }

    func testARunInPicksIsTheSubjectWithItsBoundRowsNicknameAndFace() throws {
        let snap = try reviewSnapshot(
            waiting: #"{"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":40}"#)
        // The bound session's plain wait is the run's, never a second entry.
        XCTAssertEqual(snap.decisionItems.map(\.target.key), ["r:r-1"])
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap))
        XCTAssertEqual(subject.kind, "picks")
        XCTAssertEqual(subject.runId, "r-1")
        XCTAssertEqual(subject.sessionId, "w1")
        XCTAssertEqual(subject.nickname, "Vex")
        XCTAssertEqual(subject.slug, "vex")
        XCTAssertEqual(subject.since, 900)
        XCTAssertEqual(subject.kindWord, "pick fixes")
    }

    func testAnUnboundRunSaysItsProjectWithNoFaceAndNoSession() throws {
        let snap = try reviewSnapshot(sessionId: "")
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap))
        XCTAssertEqual(subject.kind, "picks")
        XCTAssertEqual(subject.runId, "r-1")
        XCTAssertEqual(subject.nickname, "P")
        XCTAssertEqual(subject.slug, "")
        XCTAssertEqual(subject.sessionId, "")
        XCTAssertEqual(subject.work, "")
        XCTAssertTrue(subject.hasFace)
    }

    func testAPromptOnTheBoundSessionOutranksTheRun() throws {
        let snap = try snapshot("""
        {
          "generated_at": 1000,
          "agents": {"running": [
            {"session_id":"w1","nickname":"Vex","project":"P","idle_seconds":5}]},
          "permissions": [
            {"request_id":"pr","session_id":"w1","tool_name":"Bash","description":"ls"}
          ],
          "review": {"available": true, "runs": [
            {"id":"r-1","project":"P","state":"picks","session_id":"w1","findings_at":900}
          ]}
        }
        """)
        let subject = try XCTUnwrap(NeedsYouActivityRule.subject(from: snap))
        XCTAssertEqual(subject.kind, "permission")
        XCTAssertEqual(subject.sessionId, "w1")
        XCTAssertEqual(subject.runId, "")
    }

    func testARunOutranksAPlainWaitingRow() throws {
        let snap = try reviewSnapshot(
            sessionId: "",
            waiting: #"{"session_id":"w2","nickname":"Cipher","project":"P","idle_seconds":900}"#)
        XCTAssertEqual(NeedsYouActivityRule.subject(from: snap)?.runId, "r-1")
    }

    func testAChangedRunIdIsAnUpdate() {
        let a = NeedsYouAttributes.ContentState(nickname: "P", kind: "picks",
                                                since: 900, runId: "r-1")
        var b = a
        b.runId = "r-2"
        XCTAssertFalse(NeedsYouActivityRule.same(a, b))
        XCTAssertEqual(NeedsYouActivityRule.plan(current: a, subject: b), .update(b))
        XCTAssertEqual(NeedsYouActivityRule.kindWord(.reviewPicks), "picks")
    }
}
