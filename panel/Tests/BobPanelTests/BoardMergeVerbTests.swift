import XCTest
@testable import BobPanel

/// Review and merge on a Done card: the one rule the tile, the card window
/// and the phone read (`CardMerge`), the tolerant decode of the four card
/// keys, and the Changes page's shape. `docs/card-worktrees.md`,
/// `plans/2026-10-03-review-and-merge-done-card.md`.
final class BoardMergeVerbTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func report(_ json: String) throws -> CardChangesReport {
        try JSONDecoder().decode(CardChangesReport.self, from: Data(json.utf8))
    }

    // MARK: - CardMerge.offered

    /// MERGE is offered only on a Done card with a recorded branch, no
    /// hand-check due, and a merge state of nothing or a stop.
    func testOfferedTable() {
        for state in ["", "blocked", "conflict", "checks_failed"] {
            XCTAssertTrue(CardMerge.offered(column: "done", branch: "card/x",
                                            mergeState: state, manualDue: false, daemonOffers: true), state)
        }
        for state in ["merging", "merged", "bogus"] {
            XCTAssertFalse(CardMerge.offered(column: "done", branch: "card/x",
                                             mergeState: state, manualDue: false, daemonOffers: true), state)
        }
        XCTAssertFalse(CardMerge.offered(column: "done", branch: "card/x",
                                         mergeState: "", manualDue: true, daemonOffers: true))
        XCTAssertFalse(CardMerge.offered(column: "done", branch: "",
                                         mergeState: "", manualDue: false, daemonOffers: true))
        for column in ["prep", "backlog", "in_progress", "", "bogus"] {
            XCTAssertFalse(CardMerge.offered(column: column, branch: "card/x",
                                             mergeState: "", manualDue: false, daemonOffers: true), column)
        }
    }

    func testFixIsOfferedOnlyWhereAMergeStopped() {
        XCTAssertTrue(CardMerge.fixOffered(mergeState: "conflict", daemonOffers: true))
        XCTAssertTrue(CardMerge.fixOffered(mergeState: "checks_failed", daemonOffers: true))
        for state in ["", "blocked", "merging", "merged"] {
            XCTAssertFalse(CardMerge.fixOffered(mergeState: state, daemonOffers: true), state)
        }
    }

    func testReviewIsOfferedOnADoneCardWithABranchAndNoReviewRunning() {
        XCTAssertTrue(CardMerge.reviewOffered(column: "done", branch: "card/x",
                                              reviewRunning: false, manualDue: false,
                                              daemonOffers: true))
        XCTAssertFalse(CardMerge.reviewOffered(column: "done", branch: "card/x",
                                               reviewRunning: true, manualDue: false,
                                              daemonOffers: true))
        XCTAssertFalse(CardMerge.reviewOffered(column: "done", branch: "",
                                               reviewRunning: false, manualDue: false,
                                              daemonOffers: true))
        XCTAssertFalse(CardMerge.reviewOffered(column: "backlog", branch: "card/x",
                                               reviewRunning: false, manualDue: false,
                                              daemonOffers: true))
    }

    /// The daemon's own fact is part of the rule: without it (a Failed
    /// hand-check, or an older daemon that publishes no key) nothing is
    /// offered, and an open check hides Run review as it hides MERGE.
    func testTheDaemonsFactDecidesAndAnOpenCheckHidesReview() throws {
        XCTAssertFalse(CardMerge.offered(column: "done", branch: "card/x",
                                         mergeState: "", manualDue: false,
                                         daemonOffers: false))
        XCTAssertFalse(CardMerge.fixOffered(mergeState: "conflict",
                                            daemonOffers: false))
        XCTAssertFalse(CardMerge.reviewOffered(column: "done", branch: "card/x",
                                               reviewRunning: false, manualDue: false,
                                               daemonOffers: false))
        XCTAssertFalse(CardMerge.reviewOffered(column: "done", branch: "card/x",
                                               reviewRunning: false, manualDue: true,
                                               daemonOffers: true))
        let absent = try card(#"{"id":"a","column_name":"done","worktree_branch":"card/x"}"#)
        XCTAssertFalse(absent.mergeOffered)
        let present = try card(#"{"id":"a","merge_offered":true}"#)
        XCTAssertTrue(present.mergeOffered)
        let malformed = try card(#"{"id":"a","merge_offered":"yes"}"#)
        XCTAssertFalse(malformed.mergeOffered)
    }

    // MARK: - The words

    func testTheLabels() {
        XCTAssertEqual(CardMerge.mergeLabel(mergeState: "", armed: false), "MERGE")
        XCTAssertEqual(CardMerge.mergeLabel(mergeState: "", armed: true),
                       "Merge into main?")
        XCTAssertEqual(CardMerge.mergeLabel(mergeState: "merging", armed: false),
                       "MERGING…")
        XCTAssertEqual(CardMerge.mergeLabel(mergeState: "merging", armed: true),
                       "MERGING…")
        XCTAssertEqual(CardMerge.fixLabel(armed: false), "Fix")
        XCTAssertEqual(CardMerge.fixLabel(armed: true), "Fix the merge?")
    }

    func testTheVerdictLine() {
        XCTAssertEqual(CardMerge.verdictLine(verdict: "ship", running: false,
                                             current: true), "Review: SHIP")
        XCTAssertEqual(CardMerge.verdictLine(verdict: "stop", running: false,
                                             current: false),
                       "Review: STOP — reviewed at an earlier version")
        XCTAssertEqual(CardMerge.verdictLine(verdict: "", running: true,
                                             current: true), "Review running…")
        XCTAssertEqual(CardMerge.verdictLine(verdict: "ship", running: true,
                                             current: false), "Review running…")
        XCTAssertNil(CardMerge.verdictLine(verdict: "", running: false, current: true))
        XCTAssertNil(CardMerge.verdictLine(verdict: "maybe", running: false,
                                           current: true))
    }

    /// `CardActionWeight` names no merge verb: every one is dim words, and an
    /// absent primary promotes nothing.
    func testNoMergeVerbIsPrimary() {
        XCTAssertNil(CardActionWeight.primary(column: "done"))
    }

    // MARK: - The CHANGES section

    func testChangesIsInTheDoneAndEndedOrdersBehindMore() {
        XCTAssertEqual(CardSections.Section.changes.rawValue, "CHANGES")
        for stage in [CardSections.Stage.done, .ended] {
            let order = CardSections.order(for: stage)
            XCTAssertEqual(order.filter { $0 == .changes }.count, 1, "\(stage)")
            XCTAssertGreaterThan(order.firstIndex(of: .changes)!,
                                 order.firstIndex(of: .more)!, "\(stage)")
            XCTAssertTrue(CardSections.hidden(for: stage).contains(.changes))
            XCTAssertFalse(CardSections.isOpen(.changes, stage: stage, opened: []))
        }
    }

    // MARK: - Decoding

    func testACardWithoutTheKeysDecodesEmpty() throws {
        let decoded = try card(#"{"id":"a","title":"kept","column_name":"done"}"#)
        XCTAssertEqual(decoded.mergeState, "")
        XCTAssertEqual(decoded.mergeLine, "")
        XCTAssertEqual(decoded.reviewVerdict, "")
        XCTAssertFalse(decoded.reviewRunning)
        XCTAssertEqual(decoded.title, "kept")
    }

    func testACardWithTheKeysDecodesThem() throws {
        let decoded = try card(#"""
            {"id":"a","column_name":"done","worktree_branch":"card/abcd1234-x",
             "merge_state":"conflict","merge_line":"Merge needs you: stopped.",
             "review_verdict":"stop","review_running":true}
            """#)
        XCTAssertEqual(decoded.mergeState, "conflict")
        XCTAssertEqual(decoded.mergeLine, "Merge needs you: stopped.")
        XCTAssertEqual(decoded.reviewVerdict, "stop")
        XCTAssertTrue(decoded.reviewRunning)
        XCTAssertTrue(CardMerge.offered(column: decoded.column,
                                        branch: decoded.worktreeBranch,
                                        mergeState: decoded.mergeState,
                                        manualDue: decoded.needsManualCheck, daemonOffers: true))
    }

    func testAMalformedKeyNeverBlanksTheCard() throws {
        let decoded = try card(#"""
            {"id":"a","title":"kept","merge_state":3,"merge_line":null,
             "review_verdict":{},"review_running":"yes"}
            """#)
        XCTAssertEqual(decoded.title, "kept")
        XCTAssertEqual(decoded.mergeState, "")
        XCTAssertEqual(decoded.mergeLine, "")
        XCTAssertEqual(decoded.reviewVerdict, "")
        XCTAssertFalse(decoded.reviewRunning)
    }

    /// A page with no `files` key decodes to an empty list, never a failure.
    func testAChangesReportWithoutFilesDecodesEmpty() throws {
        let decoded = try report(#"{"available":true,"branch":"card/x","ahead":0}"#)
        XCTAssertTrue(decoded.available)
        XCTAssertEqual(decoded.files, [])
        XCTAssertEqual(decoded.commits, [])
        XCTAssertEqual(decoded.ahead, 0)
        XCTAssertEqual(decoded.review, CardChangeReview())
        XCTAssertFalse(decoded.mergeOffered)
    }

    func testAChangesReportDecodesItsRowsAndKeepsTheTip() throws {
        let decoded = try report(#"""
            {"available":true,"card_id":"a","branch":"card/x","trunk":"main",
             "merge_base":"abcd1234","branch_tip":"\#(String(repeating: "a", count: 40))",
             "ahead":2,"behind":1,
             "commits":[{"sha8":"11112222","author":"Ada","at":1700000000,"subject":"edit"}],
             "files":[{"path":"a.txt","added":3,"removed":1,"binary":false},
                      {"path":"img.png","added":0,"removed":0,"binary":true}],
             "files_total":2,"files_truncated":false,"commits_truncated":false,
             "merge_offered":true,"merge_refusal":"",
             "review":{"verdict":"ship","tip":"aaaaaaaa","current":true}}
            """#)
        XCTAssertEqual(decoded.ahead, 2)
        XCTAssertEqual(decoded.behind, 1)
        XCTAssertEqual(decoded.commits.map(\.subject), ["edit"])
        XCTAssertEqual(decoded.files.map(\.path), ["a.txt", "img.png"])
        XCTAssertEqual(decoded.files[0].added, 3)
        XCTAssertTrue(decoded.files[1].binary)
        XCTAssertEqual(decoded.branchTip.count, 40)
        XCTAssertTrue(decoded.mergeOffered)
        XCTAssertEqual(decoded.review.verdict, "ship")
        XCTAssertTrue(decoded.review.current)
    }

    func testADiffDecodesAndTheUnreadableOneHasWords() throws {
        let diff = try JSONDecoder().decode(CardChangeDiff.self, from: Data(
            #"{"available":true,"path":"a.txt","text":"+two","truncated":true}"#.utf8))
        XCTAssertTrue(diff.available)
        XCTAssertEqual(diff.text, "+two")
        XCTAssertTrue(diff.truncated)
        XCTAssertFalse(CardChangeDiff.unreadable.available)
        XCTAssertFalse(CardChangeDiff.unreadable.reason.isEmpty)
    }

    // MARK: - The armed slots

    @MainActor
    func testMergeAndFixArmInTheirOwnSlotsAndDisarmTogether() {
        let state = BoardState()
        state.armMerge("a")
        XCTAssertEqual(state.mergeArmed, "a")
        state.armFix("a")
        XCTAssertNil(state.mergeArmed)
        XCTAssertEqual(state.fixArmed, "a")
        state.arm("b")
        XCTAssertNil(state.fixArmed)
        state.armMerge("a")
        state.disarm()
        XCTAssertNil(state.mergeArmed)
        XCTAssertNil(state.fixArmed)
        XCTAssertNil(state.armed)
    }
}
