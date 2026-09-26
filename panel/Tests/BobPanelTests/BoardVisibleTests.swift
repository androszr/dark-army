import XCTest
@testable import BobPanel

/// `BoardVisible.compute` — the board's one filter-and-sort pass per redraw —
/// must answer exactly what the per-row calls it replaced answered: each
/// column's cards, admitted by the project filter and the search, in
/// `cardOrder`.
@MainActor
final class BoardVisibleTests: XCTestCase {
    private func card(_ id: String, _ column: BoardColumn, project: String = "alpha",
                      title: String = "", priority: String = "",
                      position: Double = 0, createdAt: Double = 0) -> BoardCard {
        var card = BoardCard()
        card.id = id
        card.column = column.rawValue
        card.project = project
        card.title = title.isEmpty ? "Card \(id)" : title
        card.priority = priority
        card.position = position
        card.createdAt = createdAt
        return card
    }

    /// A state with nothing narrowing it — the habit file is not consulted.
    private func openState() -> BoardState {
        let state = BoardState()
        state.projectFilter = []
        state.query = ""
        return state
    }

    private var board: Board {
        var board = Board()
        board.available = true
        board.cards = [
            card("b3", .backlog, priority: "10", position: 2),
            card("p1", .prep, project: "beta"),
            card("b1", .backlog, priority: "90"),
            card("b2", .backlog, priority: "10", position: 1),
            card("i1", .inProgress, project: "beta", title: "Wire the feed"),
            card("d1", .done, createdAt: 5),
            card("d2", .done, createdAt: 1),
        ]
        return board
    }

    func testEachColumnIsSortedByCardOrder() {
        let visible = BoardVisible.compute(board: board, state: openState())
        for column in BoardColumn.allCases {
            let expected = board.cards(in: column.rawValue)
                .sorted { BoardVisible.cardOrder($0, $1, column: column) }
                .map(\.id)
            XCTAssertEqual(visible.cards(column).map(\.id), expected, column.rawValue)
        }
        XCTAssertEqual(visible.cards(.backlog).map(\.id), ["b1", "b2", "b3"])
        XCTAssertEqual(visible.cards(.done).map(\.id), ["d2", "d1"])
        XCTAssertTrue(visible.anyVisible)
    }

    func testTheForwardingOrderIsTheSameRule() {
        let a = card("a", .backlog, priority: "50")
        let b = card("b", .backlog, priority: "20")
        XCTAssertEqual(BoardView.cardOrder(a, b, column: .backlog),
                       BoardVisible.cardOrder(a, b, column: .backlog))
        XCTAssertEqual(BoardView.cardOrder(b, a, column: .backlog),
                       BoardVisible.cardOrder(b, a, column: .backlog))
    }

    func testTheProjectFilterIsApplied() {
        let state = openState()
        state.projectFilter = ["beta"]
        let visible = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(visible.cards(.prep).map(\.id), ["p1"])
        XCTAssertEqual(visible.cards(.inProgress).map(\.id), ["i1"])
        XCTAssertTrue(visible.cards(.backlog).isEmpty)
        XCTAssertTrue(visible.cards(.done).isEmpty)
        XCTAssertTrue(visible.anyVisible)
    }

    func testTheSearchIsApplied() {
        let state = openState()
        state.query = "wire the"
        let visible = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(visible.byColumn.values.flatMap { $0 }.map(\.id), ["i1"])
        // And a `#card` token names exactly one card by id.
        state.query = CardToken.query(for: card("b2", .backlog))
        let token = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(token.byColumn.values.flatMap { $0 }.map(\.id), ["b2"])
    }

    func testAnyVisibleIsFalseExactlyWhenEveryColumnIsEmpty() {
        let state = openState()
        state.query = "nothing on this board says this"
        let none = BoardVisible.compute(board: board, state: state)
        XCTAssertFalse(none.anyVisible)
        for column in BoardColumn.allCases {
            XCTAssertTrue(none.cards(column).isEmpty, column.rawValue)
        }
        // One card in one column is enough.
        state.query = "Card d2"
        let one = BoardVisible.compute(board: board, state: state)
        XCTAssertTrue(one.anyVisible)
        XCTAssertEqual(one.cards(.done).map(\.id), ["d2"])
        // An empty board has nothing visible.
        XCTAssertFalse(BoardVisible.compute(board: Board(), state: openState()).anyVisible)
    }

    func testAFoldDoesNotNarrowTheCards() {
        let state = openState()
        state.rowFlips = []                    // Done folded by default
        XCTAssertTrue(state.rowFolded(.done))
        let folded = BoardVisible.compute(board: board, state: state)
        state.rowFlips = ["done"]              // Done flipped open
        XCTAssertFalse(state.rowFolded(.done))
        let open = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(folded, open, "folding is rowFolded's rule, never this one's")
        XCTAssertEqual(folded.cards(.done).map(\.id), ["d2", "d1"])
    }

    /// The selecting row draws its ticked cards first, in the column's own
    /// order among themselves; another row's order is untouched.
    func testTickedCardsLeadTheSelectingRow() {
        let state = openState()
        state.enterRowSelection(.backlog)
        state.rowSelection = ["b3", "b2"]
        let visible = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(visible.cards(.backlog).map(\.id), ["b2", "b3", "b1"])
        XCTAssertEqual(visible.cards(.done).map(\.id), ["d2", "d1"])
        state.exitRowSelection()
        let plain = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(plain.cards(.backlog).map(\.id), ["b1", "b2", "b3"])
    }

    /// The heading that read 47 on a project holding 3: Done's heading was
    /// the store-wide count whatever the project filter said.
    func testTheDoneHeadingFollowsTheProjectFilterOnceTheArchiveIsIn() {
        var board = self.board
        board.counts = ["done": 47]
        let state = openState()
        let open = BoardVisible.compute(board: board, state: state)
        XCTAssertFalse(open.narrowed)
        XCTAssertEqual(open.headingCount(.done, storeDone: 47, archiveReady: false), 47)
        XCTAssertEqual(open.headingCount(.done, storeDone: 47, archiveReady: true), 47)

        state.projectFilter = ["beta"]
        let beta = BoardVisible.compute(board: board, state: state)
        XCTAssertTrue(beta.narrowed)
        XCTAssertEqual(beta.headingCount(.done, storeDone: 47, archiveReady: true), 0)
        XCTAssertNil(beta.headingCount(.done, storeDone: 47, archiveReady: false),
                     "counted off the preview it would be a smaller wrong number")
        XCTAssertEqual(beta.headingCount(.prep, storeDone: 47, archiveReady: false), 1)
        XCTAssertEqual(beta.headingCount(.backlog, storeDone: 47, archiveReady: false), 0)

        state.projectFilter = ["alpha"]
        let alpha = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(alpha.headingCount(.done, storeDone: 47, archiveReady: true), 2)
        XCTAssertEqual(alpha.headingCount(.backlog, storeDone: 47, archiveReady: true), 3)
    }

    func testASearchNarrowsTheDoneHeadingToo() {
        let state = openState()
        state.query = "   "
        XCTAssertFalse(BoardVisible.compute(board: board, state: state).narrowed,
                       "blank text is no search — matches() admits everything")
        state.query = "Card d2"
        let found = BoardVisible.compute(board: board, state: state)
        XCTAssertEqual(found.headingCount(.done, storeDone: 47, archiveReady: true), 1)
    }
}
