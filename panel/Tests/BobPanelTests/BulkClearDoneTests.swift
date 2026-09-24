import XCTest
@testable import BobPanel

final class BulkClearDoneTests: XCTestCase {
    private let tokenA = String(repeating: "a", count: 64)
    private let tokenB = String(repeating: "b", count: 64)

    private func board(done: Int, token: String) -> Board {
        var board = Board()
        board.available = true
        board.counts = ["prep": 0, "backlog": 0,
                        "in_progress": 0, "done": done]
        board.doneClearToken = token
        return board
    }

    @MainActor
    func testIntentAndFirstConfirmationCannotSend() {
        let scope = BulkClearDoneScope(board: board(done: 2, token: tokenA))
        let state = BoardState()

        XCTAssertNil(state.beginClearDone(currentScope: scope))
        XCTAssertEqual(state.clearDoneGate, .idle)

        state.armClearDone(scope: scope)
        XCTAssertEqual(state.clearDoneGate, .firstConfirmation(scope))
        XCTAssertNil(state.beginClearDone(currentScope: scope))
        XCTAssertEqual(state.clearDoneGate, .firstConfirmation(scope))

        state.advanceClearDone(currentScope: scope)
        XCTAssertEqual(state.clearDoneGate, .finalConfirmation(scope))
        XCTAssertEqual(state.beginClearDone(currentScope: scope), scope)
        XCTAssertEqual(state.clearDoneGate, .requesting(scope))
    }

    @MainActor
    func testBothConfirmationStagesCanBeCancelled() {
        let scope = BulkClearDoneScope(board: board(done: 3, token: tokenA))
        let state = BoardState()

        state.armClearDone(scope: scope)
        state.cancelClearDone()
        XCTAssertEqual(state.clearDoneGate, .idle)

        state.armClearDone(scope: scope)
        state.advanceClearDone(currentScope: scope)
        state.cancelClearDone()
        XCTAssertEqual(state.clearDoneGate, .idle)
    }

    @MainActor
    func testDisarmCancelsTheGate() {
        let scope = BulkClearDoneScope(board: board(done: 1, token: tokenA))
        let state = BoardState()
        state.armClearDone(scope: scope)
        state.disarm()
        XCTAssertEqual(state.clearDoneGate, .idle)
    }

    @MainActor
    func testCountChangeDisarmsAndExplainsNothingWasCleared() {
        let state = BoardState()
        state.armClearDone(scope: BulkClearDoneScope(
            board: board(done: 2, token: tokenA)))

        state.reconcile(with: board(done: 3, token: tokenB))

        XCTAssertEqual(state.clearDoneGate, .idle)
        XCTAssertTrue(state.clearDoneRefusal.contains("Nothing was cleared"))
    }

    @MainActor
    func testEqualCountMembershipChangeAlsoDisarms() {
        let state = BoardState()
        state.armClearDone(scope: BulkClearDoneScope(
            board: board(done: 2, token: tokenA)))
        state.advanceClearDone(currentScope: BulkClearDoneScope(
            board: board(done: 2, token: tokenA)))

        state.reconcile(with: board(done: 2, token: tokenB))

        XCTAssertEqual(state.clearDoneGate, .idle)
        XCTAssertTrue(state.clearDoneRefusal.contains("new set"))
    }

    @MainActor
    func testRefusalEndsProgressWithDaemonDetail() {
        let current = board(done: 2, token: tokenA)
        let scope = BulkClearDoneScope(board: current)
        let state = BoardState()
        state.armClearDone(scope: scope)
        state.advanceClearDone(currentScope: scope)
        XCTAssertNotNil(state.beginClearDone(currentScope: scope))

        state.finishClearDone(
            ActionResult(ok: false, detail: "Done changed; nothing was cleared"),
            currentBoard: current)

        XCTAssertEqual(state.clearDoneGate, .idle)
        XCTAssertEqual(state.clearDoneRefusal,
                       "Done changed; nothing was cleared")
    }

    @MainActor
    func testAcceptedRequestWaitsForAChangedSnapshot() {
        let current = board(done: 2, token: tokenA)
        let scope = BulkClearDoneScope(board: current)
        let state = BoardState()
        state.armClearDone(scope: scope)
        state.advanceClearDone(currentScope: scope)
        XCTAssertNotNil(state.beginClearDone(currentScope: scope))

        state.finishClearDone(ActionResult(ok: true, detail: ""),
                              currentBoard: current)
        XCTAssertEqual(state.clearDoneGate, .awaitingSnapshot(scope))
        state.reconcile(with: current)
        XCTAssertEqual(state.clearDoneGate, .awaitingSnapshot(scope))

        state.reconcile(with: board(done: 0, token: tokenB))
        XCTAssertEqual(state.clearDoneGate, .idle)
    }

    func testDoneTokenDecodesTolerantly() throws {
        let withToken = """
        {"counts":{"done":2},"done_clear_token":"\(tokenA)"}
        """.data(using: .utf8)!
        let withoutToken = """
        {"counts":{"done":2}}
        """.data(using: .utf8)!

        let current = try JSONDecoder().decode(Board.self, from: withToken)
        let old = try JSONDecoder().decode(Board.self, from: withoutToken)

        XCTAssertEqual(current.doneClearToken, tokenA)
        XCTAssertTrue(current.hasSafeDoneClearToken)
        XCTAssertEqual(old.doneClearToken, "")
        XCTAssertFalse(old.hasSafeDoneClearToken)
    }

    /// The clear gate reads the **membership** token and only that one. A view
    /// token that moved because a finished card was retitled must not become a
    /// second thing to confirm against, or every Clear Done would be refused
    /// after any edit.
    func testTheClearScopeIgnoresTheViewToken() {
        var a = board(done: 2, token: tokenA)
        a.doneViewToken = tokenB
        var b = board(done: 2, token: tokenA)
        b.doneViewToken = String(repeating: "c", count: 64)
        XCTAssertEqual(BulkClearDoneScope(board: a),
                       BulkClearDoneScope(board: b))
    }

    /// Cards spliced in from the on-demand archive are content, not scope: the
    /// count and the token both come from the store-wide fields the daemon
    /// publishes, so a longer `cards` list confirms against the same thing.
    func testASplicedArchiveDoesNotChangeTheConfirmedScope() {
        let bare = board(done: 3, token: tokenA)
        var spliced = bare
        var card = BoardCard()
        card.id = "archived"
        card.column = "done"
        spliced.cards = DoneArchive.splice(bare.cards, archive: [card])
        XCTAssertEqual(spliced.cards.count, 1)
        XCTAssertEqual(BulkClearDoneScope(board: spliced),
                       BulkClearDoneScope(board: bare))
    }

    func testCanonicalCountsSeeDoneOutsideThePreview() {
        let board = board(done: 4, token: tokenA)
        XCTAssertTrue(board.cards.isEmpty)
        XCTAssertEqual(board.doneCount, 4)
        XCTAssertFalse(board.isStoreEmpty)
    }
}
