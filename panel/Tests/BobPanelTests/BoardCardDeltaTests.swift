import XCTest
@testable import BobPanel

/// The panel's half of `?cards=delta`: the pure merge that rebuilds the
/// board's card list from the daemon's order, and `DaemonClient.apply`
/// running it before the carry-forward, so no view ever meets a delta.
@MainActor
final class BoardCardDeltaTests: XCTestCase {
    private func card(_ id: String, title: String = "") -> BoardCard {
        var card = BoardCard()
        card.id = id
        card.title = title.isEmpty ? id : title
        return card
    }

    // MARK: - merge

    func testOrderIsTheDaemons() {
        let held = [card("a"), card("b"), card("c")]
        let out = BoardCardDelta.merge(order: ["c", "a", "b"], changed: [],
                                       held: held)
        XCTAssertEqual(out.cards.map(\.id), ["c", "a", "b"])
        XCTAssertEqual(out.missing, [])
    }

    func testAChangedCardReplacesTheHeldOne() {
        let held = [card("a", title: "old"), card("b")]
        let out = BoardCardDelta.merge(order: ["a", "b"],
                                       changed: [card("a", title: "new")],
                                       held: held)
        XCTAssertEqual(out.cards.map(\.id), ["a", "b"])
        XCTAssertEqual(out.cards.first?.title, "new")
        XCTAssertEqual(out.missing, [])
    }

    func testAnIdAbsentFromTheOrderIsDropped() {
        let held = [card("a"), card("b"), card("c")]
        let out = BoardCardDelta.merge(order: ["a", "c"], changed: [],
                                       held: held)
        XCTAssertEqual(out.cards.map(\.id), ["a", "c"])
        XCTAssertEqual(out.missing, [])
    }

    func testAnIdKnownToNeitherListIsReportedMissing() {
        let out = BoardCardDelta.merge(order: ["a", "z", "b"],
                                       changed: [card("b")], held: [card("a")])
        XCTAssertEqual(out.cards.map(\.id), ["a", "b"])
        XCTAssertEqual(out.missing, ["z"])
    }

    func testAnEmptyOrderYieldsAnEmptyList() {
        let out = BoardCardDelta.merge(order: [], changed: [card("a")],
                                       held: [card("b")])
        XCTAssertEqual(out.cards, [])
        XCTAssertEqual(out.missing, [])
    }

    // MARK: - apply

    private func feed(_ client: DaemonClient, _ text: String,
                      file: StaticString = #filePath, line: UInt = #line) async throws {
        let prepared = await DaemonClient.prepareFrame(text, lastApplied: "", via: "test")
        let frame = try XCTUnwrap(prepared, "frame rejected", file: file, line: line)
        client.apply(frame.snapshot, normalized: frame.normalized, via: "test")
    }

    func testADeltaFrameIsMergedIntoTheHeldBoardBeforeAnyoneReadsIt() async throws {
        let client = DaemonClient()
        try await feed(client, """
        {"generated_at": 1, "board": {"available": true, "cards": [
          {"id": "c1", "title": "one"}, {"id": "c2", "title": "two"},
          {"id": "c3", "title": "three"}]}}
        """)
        // A later slim frame: the board present, one card moved, one gone.
        try await feed(client, """
        {"generated_at": 2, "board": {"available": true, "cards_delta": true,
          "card_order": ["c3", "c1"],
          "cards": [{"id": "c1", "title": "one, ticked"}]}}
        """)
        let board = client.snapshot.board
        XCTAssertEqual(board.cards.map(\.id), ["c3", "c1"])
        XCTAssertEqual(board.cards.last?.title, "one, ticked")
        XCTAssertFalse(board.cardsDelta, "no view downstream ever meets a delta")
        XCTAssertEqual(board.cardOrder, [])
        // And the carried board is whole: an agents-only frame keeps it.
        try await feed(client, """
        {"generated_at": 3, "counts": {"working": 1}}
        """)
        XCTAssertEqual(client.snapshot.board.cards.map(\.id), ["c3", "c1"])
    }

    func testAWholeBoardIsNeverTreatedAsADelta() async throws {
        let client = DaemonClient()
        try await feed(client, """
        {"generated_at": 1, "board": {"cards": [{"id": "c1"}, {"id": "c2"}]}}
        """)
        // A present short board with no marker replaces the held one.
        try await feed(client, """
        {"generated_at": 2, "board": {"cards": [{"id": "c2"}]}}
        """)
        XCTAssertEqual(client.snapshot.board.cards.map(\.id), ["c2"])
    }
}
