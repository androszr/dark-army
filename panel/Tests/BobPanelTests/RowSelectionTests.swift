import XCTest
@testable import BobPanel

/// `RowSelection`, the rule behind a board row's select mode, as tables, plus
/// the `BoardState` half that keeps a selection honest against snapshots.
/// `plans/2026-09-25-batch-refine-prep-cards.md`.
final class RowSelectionTests: XCTestCase {
    private func chrome(dispatch: Bool = true) -> BoardChrome {
        var board = Board()
        board.dispatchEnabled = dispatch
        return BoardChrome(board)
    }

    private func prep(_ id: String, root: String = "/p/one") -> BoardCard {
        var card = BoardCard()
        card.id = id
        card.column = BoardColumn.prep.rawValue
        card.root = root
        card.tool = "claude"
        return card
    }

    /// Every one of today's seven Refine terms, each broken alone.
    func testTickableMirrorsTheSevenTermsOfRefine() {
        let ok = prep("a")
        XCTAssertTrue(RowSelection.tickable(.prep, card: ok, chrome: chrome()))

        XCTAssertFalse(RowSelection.tickable(.prep, card: ok,
                                             chrome: chrome(dispatch: false)))
        var moved = ok; moved.column = BoardColumn.backlog.rawValue
        var planned = ok; planned.planPath = "/p/one/plans/x.md"
        var scout = ok; scout.kind = "scout"
        var refining = ok; refining.refineState = "live"
        var dispatchingRefine = ok; dispatchingRefine.refineState = "dispatching"
        var bound = ok; bound.sessionId = "s1"
        var starting = ok; starting.linkState = "dispatching"
        for card in [moved, planned, scout, refining, dispatchingRefine,
                     bound, starting] {
            XCTAssertFalse(RowSelection.tickable(.prep, card: card,
                                                 chrome: chrome()), card.id)
        }
        // An ended refinement may be refined again, as Refine allows.
        var ended = ok; ended.refineState = "ended"
        XCTAssertTrue(RowSelection.tickable(.prep, card: ended, chrome: chrome()))
    }

    func testNoOtherRowTicksAnythingYet() {
        let card = prep("a")
        for column in [BoardColumn.inProgress, .done] {
            XCTAssertFalse(RowSelection.tickable(column, card: card,
                                                 chrome: chrome()))
            XCTAssertEqual(RowSelection.verb(column, count: 3), "")
        }
    }

    func testAdmitsRefusesASecondProjectFolder() {
        let first = prep("a", root: "/p/one")
        let same = prep("b", root: "/p/one")
        let other = prep("c", root: "/p/two")
        XCTAssertTrue(RowSelection.admits(.prep, card: first, given: [],
                                          chrome: chrome()))
        XCTAssertTrue(RowSelection.admits(.prep, card: same, given: [first],
                                          chrome: chrome()))
        XCTAssertFalse(RowSelection.admits(.prep, card: other, given: [first],
                                           chrome: chrome()))
        var busy = same; busy.refineState = "live"
        XCTAssertFalse(RowSelection.admits(.prep, card: busy, given: [first],
                                           chrome: chrome()))
    }

    func testVerbAndMinimum() {
        XCTAssertEqual(RowSelection.minimum, 2)
        XCTAssertEqual(RowSelection.verb(.prep, count: 3), "REFINE 3 TOGETHER")
        XCTAssertFalse(RowSelection.offersSelect(.prep, cards: [prep("a")],
                                                 chrome: chrome()))
        XCTAssertTrue(RowSelection.offersSelect(.prep,
                                                cards: [prep("a"), prep("b")],
                                                chrome: chrome()))
        XCTAssertFalse(RowSelection.offersSelect(.prep,
                                                 cards: [prep("a"), prep("b")],
                                                 chrome: chrome(dispatch: false)))
    }

    @MainActor
    func testStateTicksOnlyWhatAdmitsAndLeavesCleanly() {
        let state = BoardState()
        let one = prep("a", root: "/p/one")
        let two = prep("b", root: "/p/one")
        let far = prep("c", root: "/p/two")
        // Nothing ticks outside select mode.
        state.toggleRowSelection(one, chrome: chrome())
        XCTAssertTrue(state.rowSelection.isEmpty)

        state.enterRowSelection(.prep)
        state.toggleRowSelection(one, chrome: chrome())
        state.toggleRowSelection(two, chrome: chrome())
        state.toggleRowSelection(far, chrome: chrome())
        XCTAssertEqual(state.rowSelection, ["a", "b"])
        state.toggleRowSelection(one, chrome: chrome())
        XCTAssertEqual(state.rowSelection, ["b"])

        state.rowBatchRefusal = "refused"
        state.exitRowSelection()
        XCTAssertNil(state.selectingRow)
        XCTAssertTrue(state.rowSelection.isEmpty)
        XCTAssertEqual(state.rowBatchRefusal, "")
    }

    @MainActor
    func testReconcilePrunesACardThatStoppedBeingTickable() {
        let state = BoardState()
        state.enterRowSelection(.prep)
        let one = prep("a"), two = prep("b"), three = prep("c")
        for card in [one, two, three] {
            state.toggleRowSelection(card, chrome: chrome())
        }
        var board = Board()
        board.dispatchEnabled = true
        var refining = two; refining.refineState = "live"
        board.cards = [one, refining]          // `c` is gone altogether
        state.reconcile(with: board)
        XCTAssertEqual(state.rowSelection, ["a"])
        XCTAssertEqual(state.rowSelectionCards.map(\.id), ["a"])
    }
}

extension RowSelectionTests {
    /// Ticks survive looking elsewhere: a search, another project or a
    /// folded row keeps select mode and the ticked cards; each disarms a
    /// waiting batch Start, because the confirmed board is out of sight.
    @MainActor
    func testNarrowingOrFoldingTheBoardKeepsTheSelection() {
        let state = BoardState()
        var board = Board()
        board.dispatchEnabled = true
        let chrome = BoardChrome(board)
        var one = BoardCard(); one.id = "a"; one.column = "prep"; one.root = "/p"
        var two = one; two.id = "b"

        state.enterRowSelection(.prep)
        state.toggleRowSelection(one, chrome: chrome)
        state.toggleRowSelection(two, chrome: chrome)
        state.rowBatchArmed = true
        state.query = "b"
        XCTAssertEqual(state.selectingRow, .prep)
        XCTAssertEqual(state.rowSelection, ["a", "b"])
        XCTAssertFalse(state.rowBatchArmed)

        state.rowBatchArmed = true
        state.projectFilter = state.projectFilter.union(["elsewhere"])
        XCTAssertEqual(state.selectingRow, .prep)
        XCTAssertEqual(state.rowSelection, ["a", "b"])
        XCTAssertFalse(state.rowBatchArmed)

        state.toggleRowFold(.prep)
        state.toggleRowFold(.prep)
        XCTAssertEqual(state.selectingRow, .prep)
        XCTAssertEqual(state.rowSelection, ["a", "b"])
    }
}

/// The Backlog row's select mode — START n TOGETHER, one session worked one
/// card at a time. `plans/2026-09-25-batch-implement-backlog-cards.md`.
extension RowSelectionTests {
    private func planned(_ id: String, root: String = "/p/one") -> BoardCard {
        var card = BoardCard()
        card.id = id
        card.column = BoardColumn.backlog.rawValue
        card.root = root
        card.tool = "claude"
        card.planPath = "\(root)/plans/\(id).md"
        return card
    }

    private func dispatchOn() -> BoardChrome {
        var board = Board()
        board.dispatchEnabled = true
        return BoardChrome(board)
    }

    func testBacklogTicksAPlannedCardAndRefusesEachTermBrokenAlone() {
        let ok = planned("a")
        XCTAssertTrue(RowSelection.tickable(.backlog, card: ok, chrome: dispatchOn()))

        var off = Board(); off.dispatchEnabled = false
        XCTAssertFalse(RowSelection.tickable(.backlog, card: ok,
                                             chrome: BoardChrome(off)))
        var unplanned = ok; unplanned.planPath = ""
        var scout = ok; scout.kind = "scout"
        var queued = ok; queued.queueState = "queued"
        var waiting = ok
        waiting.batch = BatchMark(rank: 2, size: 3, state: "waiting")
        var bound = ok; bound.sessionId = "s1"
        var starting = ok; starting.linkState = "dispatching"
        var toolless = ok; toolless.tool = ""
        var inPrep = ok; inPrep.column = BoardColumn.prep.rawValue
        for card in [unplanned, scout, queued, waiting, bound, starting,
                     toolless, inPrep] {
            XCTAssertFalse(RowSelection.tickable(.backlog, card: card,
                                                 chrome: dispatchOn()), card.id)
        }
        // A Prep card is never a Backlog tick, and the other way round.
        XCTAssertFalse(RowSelection.tickable(.prep, card: ok, chrome: dispatchOn()))
    }

    func testBacklogRefusesASecondProjectFolder() {
        let first = planned("a", root: "/p/one")
        let same = planned("b", root: "/p/one")
        let other = planned("c", root: "/p/two")
        XCTAssertTrue(RowSelection.admits(.backlog, card: same, given: [first],
                                          chrome: dispatchOn()))
        XCTAssertFalse(RowSelection.admits(.backlog, card: other, given: [first],
                                           chrome: dispatchOn()))
    }

    func testBacklogVerbAndConfirmLabels() {
        XCTAssertEqual(RowSelection.verb(.backlog, count: 3), "START 3 TOGETHER")
        XCTAssertEqual(RowSelection.confirm(.backlog, count: 3), "Really start 3?")
        XCTAssertEqual(RowSelection.confirm(.prep, count: 3), "")
        XCTAssertTrue(RowSelection.offersSelect(
            .backlog, cards: [planned("a"), planned("b")], chrome: dispatchOn()))
        XCTAssertFalse(RowSelection.offersSelect(
            .backlog, cards: [planned("a")], chrome: dispatchOn()))
    }

    @MainActor
    func testBacklogBatchArmsFirstAndLeavingSelectModeDisarms() {
        let state = BoardState()
        state.enterRowSelection(.backlog)
        state.toggleRowSelection(planned("a"), chrome: dispatchOn())
        state.toggleRowSelection(planned("b"), chrome: dispatchOn())
        XCTAssertEqual(state.rowSelection, ["a", "b"])
        var board = Board()
        board.dispatchEnabled = true
        board.cards = [planned("a"), planned("b")]
        let client = DaemonClient()
        state.pressStartSelected(board: board, client: client)
        XCTAssertTrue(state.rowBatchArmed, "the first press only arms")
        XCTAssertEqual(state.rowSelection, ["a", "b"])
        state.exitRowSelection()
        XCTAssertFalse(state.rowBatchArmed)
        state.enterRowSelection(.backlog)
        state.rowBatchArmed = true
        state.toggleRowFold(.backlog)
        XCTAssertFalse(state.rowBatchArmed, "a fold disarms")
        state.toggleRowFold(.backlog)
    }

    @MainActor
    func testFoldingTheSelectingRowKeepsSelectMode() {
        let state = BoardState()
        state.enterRowSelection(.backlog)
        state.toggleRowSelection(planned("a"), chrome: dispatchOn())
        state.toggleRowFold(.prep)
        XCTAssertEqual(state.selectingRow, .backlog, "another row's fold leaves it alone")
        state.toggleRowFold(.prep)
        state.toggleRowFold(.backlog)
        XCTAssertEqual(state.selectingRow, .backlog,
                       "the folded heading still draws the count and CANCEL")
        XCTAssertEqual(state.rowSelection, ["a"])
        state.toggleRowFold(.backlog)
    }

    @MainActor
    func testChangingTheSelectionOrArmingACardDisarmsTheBatch() {
        let state = BoardState()
        state.enterRowSelection(.backlog)
        state.toggleRowSelection(planned("a"), chrome: dispatchOn())
        state.rowBatchArmed = true
        state.toggleRowSelection(planned("b"), chrome: dispatchOn())
        XCTAssertFalse(state.rowBatchArmed, "a changed selection is not the confirmed one")
        state.rowBatchArmed = true
        state.arm("a")
        XCTAssertFalse(state.rowBatchArmed, "one arm at a time")
        state.rowBatchArmed = true
        state.armHere("a")
        XCTAssertFalse(state.rowBatchArmed)
    }
}
