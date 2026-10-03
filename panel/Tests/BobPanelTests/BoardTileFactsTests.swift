import XCTest
@testable import BobPanel

/// `BoardTileFacts` is each tile's slice of `BoardState`: a press about one
/// card must change that card's facts and leave every other card's equal,
/// which is what lets `.equatable()` skip redrawing the rest of the board.
@MainActor
final class BoardTileFactsTests: XCTestCase {
    private func card(_ id: String, _ column: BoardColumn = .prep) -> BoardCard {
        var card = BoardCard()
        card.id = id
        card.column = column.rawValue
        card.project = "alpha"
        card.title = "Card \(id)"
        return card
    }

    private func openState() -> BoardState {
        let state = BoardState()
        state.projectFilter = []
        state.query = ""
        return state
    }

    func testAPressAboutOneCardLeavesTheOthersEqual() {
        let state = openState()
        let chrome = BoardChrome(Board())
        let a = card("a"), b = card("b")
        let before = BoardTileFacts(card: b, state: state, chrome: chrome)
        state.armed = "a"
        state.starting.insert("a")
        state.refusals["a"] = "no"
        state.dropTarget = .card("a")
        XCTAssertEqual(BoardTileFacts(card: b, state: state, chrome: chrome), before)
        let facts = BoardTileFacts(card: a, state: state, chrome: chrome)
        XCTAssertTrue(facts.armed)
        XCTAssertTrue(facts.starting)
        XCTAssertTrue(facts.dropTargeted)
        XCTAssertEqual(facts.refusal, "no")
    }

    /// MERGE and Fix arm on the state and reach the face only through the
    /// facts; a tile that held a stale copy would never show the confirm.
    func testArmingMergeOrFixReachesOnlyThatTile() {
        let state = openState()
        let chrome = BoardChrome(Board())
        let a = card("a", .done), b = card("b", .done)
        let before = BoardTileFacts(card: b, state: state, chrome: chrome)
        state.armMerge("a")
        XCTAssertTrue(BoardTileFacts(card: a, state: state, chrome: chrome).mergeArmed)
        XCTAssertEqual(BoardTileFacts(card: b, state: state, chrome: chrome), before)
        state.armFix("a")
        let fixed = BoardTileFacts(card: a, state: state, chrome: chrome)
        XCTAssertTrue(fixed.fixArmed)
        XCTAssertFalse(fixed.mergeArmed, "arming Fix disarms MERGE")
        XCTAssertEqual(BoardTileFacts(card: b, state: state, chrome: chrome), before)
    }

    func testSelectModeReachesOnlyItsOwnRow() {
        let state = openState()
        let chrome = BoardChrome(Board())
        let prep = card("p"), backlog = card("b", .backlog)
        state.enterRowSelection(.prep)
        let p = BoardTileFacts(card: prep, state: state, chrome: chrome)
        XCTAssertEqual(p.selectingRow, .prep)
        XCTAssertFalse(p.ticked)
        XCTAssertNil(BoardTileFacts(card: backlog, state: state, chrome: chrome).selectingRow)
        state.rowSelection = ["p"]
        let ticked = BoardTileFacts(card: prep, state: state, chrome: chrome)
        XCTAssertTrue(ticked.ticked)
        XCTAssertTrue(ticked.tickAdmitted, "a ticked card may always be unticked")
        XCTAssertNotEqual(ticked, p)
    }
}
