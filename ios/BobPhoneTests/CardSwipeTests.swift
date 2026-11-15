import XCTest
@testable import BobPhone

/// The Board tab's swipe on a Prep or Backlog card: which verb the first
/// button wears, its words and the arithmetic of the slide. The same tables
/// `host/tests/test_phone_card_swipe.py` runs under `swiftc`.
final class CardSwipeTests: XCTestCase {
    private func card(_ fields: [String: Any]) -> BoardCard {
        var json: [String: Any] = ["id": "c1", "column_name": "backlog", "root": "/r/a",
                                   "plan_path": "plans/x.md", "tool": "claude"]
        for (key, value) in fields { json[key] = value }
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(BoardCard.self, from: data)
    }

    func testCanStartMirrorsTheCardScreensSevenTerms() {
        XCTAssertTrue(PhoneCardSwipe.canStart(card: card([:]), dispatchEnabled: true))
        XCTAssertFalse(PhoneCardSwipe.canStart(card: card([:]), dispatchEnabled: false))
        for broken: [String: Any] in [
            ["column_name": "in_progress"],
            ["tool": ""],
            ["queue_state": "queued"],
            ["session_id": "s1"],
            ["link_state": "dispatching"],
            ["link_state": "live"],
            ["refine_state": "dispatching"],
            ["refine_state": "live"],
            ["batch": ["rank": 2, "size": 3, "state": "waiting"]],
        ] {
            XCTAssertFalse(PhoneCardSwipe.canStart(card: card(broken), dispatchEnabled: true),
                           "\(broken)")
        }
    }

    func testPrimaryFollowsTheColumnAndTheGates() {
        XCTAssertEqual(PhoneCardSwipe.primary(column: "prep", canRefine: true, canStart: true),
                       .refine)
        XCTAssertEqual(PhoneCardSwipe.primary(column: "prep", canRefine: false, canStart: true),
                       .start)
        XCTAssertNil(PhoneCardSwipe.primary(column: "prep", canRefine: false, canStart: false))
        XCTAssertEqual(PhoneCardSwipe.primary(column: "backlog", canRefine: true, canStart: true),
                       .start)
        XCTAssertNil(PhoneCardSwipe.primary(column: "backlog", canRefine: true, canStart: false))
        for column in ["in_progress", "done", ""] {
            XCTAssertNil(PhoneCardSwipe.primary(column: column, canRefine: true, canStart: true))
        }
        XCTAssertEqual(PhoneCardSwipe.rows, ["prep", "backlog"])
    }

    // MARK: Review & merge on a Done card

    func testReviewMergeIsTheDoneLinesVerbWhereTheMacOffersIt() {
        XCTAssertEqual(PhoneCardSwipe.primary(column: "done", canRefine: false,
                                              canStart: false, reviewMerge: true),
                       .reviewMerge)
        XCTAssertNil(PhoneCardSwipe.primary(column: "done", canRefine: false,
                                            canStart: false, reviewMerge: false))
        // Only Done reads the flag.
        XCTAssertNil(PhoneCardSwipe.primary(column: "in_progress", canRefine: false,
                                            canStart: false, reviewMerge: true))
        XCTAssertEqual(PhoneCardSwipe.primary(column: "backlog", canRefine: false,
                                              canStart: true, reviewMerge: true), .start)
    }

    func testReviewMergeIsOfferedAlsoWhileAManualCheckIsOpen() {
        let open = card(["column_name": "done", "worktree_branch": "card/x",
                         "review_merge_offered": true,
                         "manual_steps": "1. Open it.", "manual_check_due": true])
        XCTAssertTrue(open.reviewMergeOffered)
        XCTAssertFalse(open.manualSteps.isEmpty)
        XCTAssertTrue(CardMerge.reviewMergeOffered(
            column: open.column, branch: open.worktreeBranch,
            running: open.reviewMergeRunning,
            daemonOffers: open.reviewMergeOffered))
    }

    func testReviewMergeHidesWhereTheMacDoesNotOfferOrItIsAtWork() {
        func offered(column: String = "done", branch: String = "card/x",
                     running: Bool = false, offers: Bool = true) -> Bool {
            CardMerge.reviewMergeOffered(column: column, branch: branch,
                                         running: running, daemonOffers: offers)
        }
        XCTAssertTrue(offered())
        XCTAssertFalse(offered(offers: false))      // an older Mac, or a Failed check
        XCTAssertFalse(offered(column: "in_progress"))
        XCTAssertFalse(offered(branch: ""))
        XCTAssertFalse(offered(running: true))
    }

    func testReviewMergeKeysDecodeTolerantly() {
        let bare = card([:])
        XCTAssertFalse(bare.reviewMergeOffered)
        XCTAssertFalse(bare.reviewMergeRunning)
        let live = card(["review_merge_offered": true, "review_merge_running": true])
        XCTAssertTrue(live.reviewMergeOffered)
        XCTAssertTrue(live.reviewMergeRunning)
    }

    func testTheOpenedDoneCardListsReviewMergeThenViewMore() {
        XCTAssertEqual(PhoneCardLine.actions(primary: .reviewMerge, canStart: false,
                                             deletes: false, selects: false),
                       [.reviewMerge, .more])
        XCTAssertNotEqual(PhoneCardLine.verbIcon(.reviewMerge),
                          PhoneCardLine.verbIcon(.start))
    }

    func testReviewMergeWireNameAndReceiptWords() {
        XCTAssertEqual(PhoneActions.boardReviewMerge, "board_review_merge")
        let receipt = try! JSONDecoder().decode(
            Receipt.self, from: Data(#"{"id":"t1","action":"board_review_merge"}"#.utf8))
        XCTAssertEqual(receipt.wording, "Review and merge a card")
        XCTAssertEqual(PhoneCardSwipe.reviewMergeLabel, "REVIEW & MERGE")
        XCTAssertEqual(PhoneCardSwipe.reviewMergeShort, "Review & merge")
        XCTAssertEqual(PhoneCardSwipe.reviewMergeSpoken, "Review and merge")
        XCTAssertEqual(PhoneCardSwipe.reviewMergeRunningLabel, "Reviewing…")
    }

    func testTheLabelsAreTheCardScreensWords() {
        XCTAssertEqual(PhoneCardSwipe.startLabel, "START")
        XCTAssertEqual(PhoneCardSwipe.refineLabel, "Refine")
        XCTAssertEqual(PhoneCardSwipe.deleteLabel, "Delete")
        XCTAssertEqual(PhoneCardSwipe.deleteSpoken, "Delete card")
        XCTAssertEqual(PhoneCardSwipe.deleteIcon, "trash")
    }

    func testTheGestureHelpersClampAndThreshold() {
        XCTAssertEqual(PhoneCardSwipe.minimumDrag, 20)
        XCTAssertEqual(PhoneCardSwipe.revealTravel, 56)
        XCTAssertEqual(PhoneCardSwipe.offset(forTranslation: -300, revealed: false),
                       -PhoneCardSwipe.actionsWidth)
        XCTAssertEqual(PhoneCardSwipe.offset(forTranslation: 300, revealed: true), 0)
        XCTAssertEqual(PhoneCardSwipe.offset(forTranslation: -40, revealed: false), -40)
        XCTAssertTrue(PhoneCardSwipe.revealAfter(translation: -57, revealed: false))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: -55, revealed: false))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: 57, revealed: true))
        XCTAssertTrue(PhoneCardSwipe.revealAfter(translation: 0, revealed: true))
        // A quick flick opens or shuts whatever the travel.
        XCTAssertEqual(PhoneCardSwipe.flickSpeed, 300)
        XCTAssertTrue(PhoneCardSwipe.revealAfter(translation: -20, velocity: -400, revealed: false))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: -20, velocity: -100, revealed: false))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: 20, velocity: 400, revealed: true))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: 20, velocity: -400, revealed: false))
        XCTAssertTrue(PhoneCardSwipe.dominantHorizontal(dx: 30, dy: 10))
        XCTAssertFalse(PhoneCardSwipe.dominantHorizontal(dx: 10, dy: 30))
    }
}
