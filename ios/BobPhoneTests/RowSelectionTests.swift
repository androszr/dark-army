import XCTest
@testable import BobPhone

/// The Board tab's select mode on Prep and on Backlog: which cards tick,
/// and the batch button's words. The same tables
/// `host/tests/test_phone_batch_refine.py` and
/// `host/tests/test_phone_batch_start.py` run under `swiftc`.
final class RowSelectionTests: XCTestCase {
    private func card(_ fields: [String: String]) -> BoardCard {
        var json: [String: String] = ["id": "c1", "column_name": "prep", "root": "/r/a"]
        for (key, value) in fields { json[key] = value }
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(BoardCard.self, from: data)
    }

    func testTickableMirrorsTheTermsOfRefine() {
        XCTAssertTrue(PhoneRowSelection.tickable("prep", card: card([:]), dispatchEnabled: true))
        // Each term broken alone flips the answer.
        XCTAssertFalse(PhoneRowSelection.tickable("prep", card: card([:]), dispatchEnabled: false))
        for broken in [
            ["column_name": "backlog"],
            ["plan_path": "plans/x.md"],
            ["kind": "scout"],
            ["refine_state": "dispatching"],
            ["refine_state": "live"],
            ["session_id": "s1"],
            ["link_state": "dispatching"],
        ] {
            XCTAssertFalse(PhoneRowSelection.tickable("prep", card: card(broken),
                                                      dispatchEnabled: true), "\(broken)")
        }
        // An ended refinement may be refined again.
        XCTAssertTrue(PhoneRowSelection.tickable("prep", card: card(["refine_state": "ended"]),
                                                 dispatchEnabled: true))
        // Every other row answers false.
        for column in ["backlog", "in_progress", "done"] {
            XCTAssertFalse(PhoneRowSelection.tickable(column, card: card([:]),
                                                      dispatchEnabled: true))
        }
    }

    func testAdmitsRefusesASecondRootAndAcceptsTheSameOne() {
        let first = card(["id": "a", "root": "/r/a"])
        let same = card(["id": "b", "root": "/r/a"])
        let other = card(["id": "c", "root": "/r/b"])
        XCTAssertTrue(PhoneRowSelection.admits("prep", card: other, given: [],
                                               dispatchEnabled: true))
        XCTAssertTrue(PhoneRowSelection.admits("prep", card: same, given: [first],
                                               dispatchEnabled: true))
        XCTAssertFalse(PhoneRowSelection.admits("prep", card: other, given: [first],
                                                dispatchEnabled: true))
        // The card itself in the list is not "the first other".
        XCTAssertTrue(PhoneRowSelection.admits("prep", card: first, given: [first],
                                               dispatchEnabled: true))
        let scout = card(["id": "d", "root": "/r/a", "kind": "scout"])
        XCTAssertFalse(PhoneRowSelection.admits("prep", card: scout, given: [first],
                                                dispatchEnabled: true))
    }

    func testTheVerbAndTheArmedVerbCarryTheCount() {
        XCTAssertEqual(PhoneRowSelection.verb("prep", count: 3), "REFINE 3 TOGETHER")
        XCTAssertEqual(PhoneRowSelection.armedVerb("prep", count: 3), "Really refine 3?")
        XCTAssertEqual(PhoneRowSelection.minimum, 2)
        XCTAssertEqual(PhoneRowSelection.scope, "batch:refine")
    }

    func testOffersSelectNeedsTwoTickableCards() {
        let a = card(["id": "a"])
        let b = card(["id": "b"])
        let planned = card(["id": "p", "plan_path": "plans/x.md"])
        XCTAssertFalse(PhoneRowSelection.offersSelect("prep", cards: [a], dispatchEnabled: true))
        XCTAssertFalse(PhoneRowSelection.offersSelect("prep", cards: [a, planned],
                                                      dispatchEnabled: true))
        XCTAssertTrue(PhoneRowSelection.offersSelect("prep", cards: [a, b], dispatchEnabled: true))
        XCTAssertFalse(PhoneRowSelection.offersSelect("prep", cards: [a, b], dispatchEnabled: false))
    }

    func testOrderedIdsFollowBoardOrderNotTickOrder() {
        let cards = ["a", "b", "c"].map { card(["id": $0]) }
        XCTAssertEqual(PhoneRowSelection.orderedIds(selected: ["c", "a"], in: cards), ["a", "c"])
        XCTAssertEqual(PhoneRowSelection.orderedIds(selected: ["z"], in: cards), [])
    }

    func testPruneDropsACardThatStoppedBeingTickableOrLeftTheBoard() {
        let a = card(["id": "a"])
        let b = card(["id": "b", "refine_state": "live"])
        XCTAssertEqual(PhoneRowSelection.prune("prep", selected: ["c", "a", "b"], in: [a, b],
                                               dispatchEnabled: true),
                       ["a"])
        XCTAssertEqual(PhoneRowSelection.prune("prep", selected: ["a"], in: [a], dispatchEnabled: false),
                       [])
    }

    @MainActor
    func testTheBatchEffectIsEveryCardsRefining() {
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.boardRefineBatch,
                                            fields: ["card_ids": "a,b"],
                                            scope: PhoneRowSelection.scope),
                       .cardsRefining(cardIds: ["a", "b"]))
        var board = try! JSONDecoder().decode(Board.self,
                                              from: Data(#"{"available": true}"#.utf8))
        board.cards = [card(["id": "a", "refine_state": "dispatching"]), card(["id": "b"])]
        var snapshot = Snapshot()
        snapshot.board = board
        let batch = ReceiptEffect.cardsRefining(cardIds: ["a", "b"])
        XCTAssertFalse(ReceiptLedger.landed(batch, in: snapshot))
        board.cards = [card(["id": "a", "refine_state": "dispatching"]),
                       card(["id": "b", "refine_state": "live"])]
        snapshot.board = board
        XCTAssertTrue(ReceiptLedger.landed(batch, in: snapshot))
    }

    // MARK: - The Backlog row: batch Start

    private func decoded(_ json: String) -> BoardCard {
        try! JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func planned(_ fields: [String: String] = [:]) -> BoardCard {
        var json: [String: String] = ["column_name": "backlog", "plan_path": "plans/x.md",
                                      "tool": "claude"]
        for (key, value) in fields { json[key] = value }
        return card(json)
    }

    func testTickableMirrorsTheTermsOfStart() {
        XCTAssertTrue(PhoneRowSelection.tickable("backlog", card: planned(), dispatchEnabled: true))
        XCTAssertFalse(PhoneRowSelection.tickable("backlog", card: planned(), dispatchEnabled: false))
        for broken in [
            ["column_name": "prep"],
            ["plan_path": ""],
            ["kind": "scout"],
            ["tool": ""],
            ["session_id": "s1"],
            ["link_state": "live"],
            ["link_state": "dispatching"],
            ["refine_state": "live"],
            ["queue_state": "queued"],
        ] {
            XCTAssertFalse(PhoneRowSelection.tickable("backlog", card: planned(broken),
                                                      dispatchEnabled: true), "\(broken)")
        }
        let waiting = decoded(#"{"id": "w", "column_name": "backlog", "plan_path": "plans/x.md", "tool": "claude", "batch": {"rank": 2, "size": 3, "state": "waiting"}}"#)
        XCTAssertFalse(PhoneRowSelection.tickable("backlog", card: waiting, dispatchEnabled: true))
        // The other rows never judge a Backlog card by the Start rule.
        for column in ["prep", "in_progress", "done"] {
            XCTAssertFalse(PhoneRowSelection.tickable(column, card: planned(),
                                                      dispatchEnabled: true), column)
        }
        let other = planned(["id": "o", "root": "/r/b"])
        XCTAssertFalse(PhoneRowSelection.admits("backlog", card: other, given: [planned()],
                                                dispatchEnabled: true))
    }

    func testTheStartVerbAndArmedVerbCarryTheCount() {
        XCTAssertEqual(PhoneRowSelection.verb("backlog", count: 3), "START 3 TOGETHER")
        XCTAssertEqual(PhoneRowSelection.armedVerb("backlog", count: 3), "Really start 3?")
        XCTAssertEqual(PhoneRowSelection.armedVerb("prep", count: 3), "Really refine 3?")
        XCTAssertEqual(PhoneRowSelection.verb("done", count: 3), "")
        XCTAssertEqual(PhoneRowSelection.startScope, "batch:start")
        XCTAssertNotEqual(PhoneRowSelection.startScope, PhoneRowSelection.scope)
    }

    func testPruneIsKeyedOnTheRow() {
        let a = planned(["id": "a"])
        let q = planned(["id": "q", "queue_state": "queued"])
        XCTAssertEqual(PhoneRowSelection.prune("backlog", selected: ["a", "q"], in: [a, q],
                                               dispatchEnabled: true),
                       ["a"])
        XCTAssertEqual(PhoneRowSelection.prune("prep", selected: ["a", "q"], in: [a, q],
                                               dispatchEnabled: true),
                       [])
    }

    func testABatchMarkDecodesTolerantlyAndSpellsThePanelsLine() {
        let none = decoded(#"{"id": "n"}"#)
        XCTAssertNil(none.batch)
        XCTAssertEqual(none.batchLine, "")
        XCTAssertFalse(none.holdsBatchMark)
        let malformed = decoded(#"{"id": "m", "batch": 3}"#)
        XCTAssertNil(malformed.batch)
        let waiting = decoded(#"{"id": "w", "batch": {"rank": 2, "size": 3, "state": "waiting"}}"#)
        XCTAssertEqual(waiting.batchLine, "BATCH 2/3 \u{00B7} waiting")
        XCTAssertTrue(waiting.isBatchWaiting)
        XCTAssertTrue(waiting.holdsBatchMark)
        let left = decoded(#"{"id": "l", "batch": {"rank": 2, "size": 3, "state": "left"}}"#)
        XCTAssertEqual(left.batchLine, "BATCH 2/3 \u{00B7} left the line")
        XCTAssertTrue(left.holdsBatchMark)
        XCTAssertFalse(left.isBatchWaiting)
        let working = decoded(#"{"id": "k", "batch": {"rank": 1, "size": 3, "state": "working"}}"#)
        XCTAssertEqual(working.batchLine, "BATCH 1/3 \u{00B7} working")
        XCTAssertFalse(working.holdsBatchMark)
        let board = try! JSONDecoder().decode(Board.self, from: Data(#"{"available": true}"#.utf8))
        XCTAssertFalse(board.startBatchSupported)
    }

    func testTheAssistantRowNamesTheSharedToolAndWritesOnlyTheOthers() {
        let a = card(["id": "a", "tool": "claude"])
        let b = card(["id": "b", "tool": "codex"])
        let c = card(["id": "c", "tool": "claude"])
        XCTAssertEqual(PhoneRowSelection.sharedTool([]), "")
        XCTAssertEqual(PhoneRowSelection.sharedTool([a, c]), "claude")
        XCTAssertEqual(PhoneRowSelection.sharedTool([a, b, c]), "")
        XCTAssertEqual(PhoneRowSelection.retoolIds([a, b, c], to: "claude"), ["b"])
        XCTAssertEqual(PhoneRowSelection.retoolIds([a, b, c], to: "codex"), ["a", "c"])
        XCTAssertEqual(PhoneRowSelection.retoolIds([a, c], to: "claude"), [])
    }
}
