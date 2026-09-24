import XCTest
@testable import BobPanel

/// The importance number as the panel sees it: a tolerant decode, the fold of
/// `""` onto `"0"`, and the term's place in `cardOrder` — below the Done
/// review clause and above `position`.
///
/// `ReviewPinTests.swift`'s shape, and for its reason: `cardOrder` is static
/// and pure, so the whole rule is testable with four cards and no view.
final class CardPriorityOrderTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func make(_ id: String, priority: String = "",
                      position: Double = 0, createdAt: Double = 0,
                      closedBy: String = "",
                      reviewedAt: Double? = nil) -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.priority = priority
        c.position = position
        c.createdAt = createdAt
        c.closedBy = closedBy
        c.reviewedAt = reviewedAt
        return c
    }

    // MARK: - Decoding

    /// A daemon from before the column ships sends no key at all, and Swift's
    /// synthesized `Decodable` throws on a missing key even where the property
    /// has a default. Absent must read as unscored.
    func testAnAbsentPriorityDecodesAsUnscored() throws {
        let old = try card(#"{"id":"a","title":"t"}"#)
        XCTAssertEqual(old.priority, "")
        XCTAssertEqual(old.priorityValue, 0)
        let scored = try card(#"{"id":"b","title":"t","priority":"75"}"#)
        XCTAssertEqual(scored.priority, "75")
        XCTAssertEqual(scored.priorityValue, 75)
    }

    /// The same fold SQLite's `CAST(priority AS INTEGER)` does: `""` and
    /// `"0"` compare equal in the sort, and differ only on the card face.
    func testUnscoredAndZeroFoldTogetherInTheSortAndNotOnTheFace() {
        let unscored = make("a")
        let zero = make("b", priority: "0")
        XCTAssertEqual(unscored.priorityValue, zero.priorityValue)
        XCTAssertTrue(unscored.priority.isEmpty)
        XCTAssertFalse(zero.priority.isEmpty)
    }

    /// Nonsense that somehow reached the wire is not a score. The store
    /// refuses it, so this is belt to that braces.
    func testAnUnparsableNumberIsTreatedAsUnscored() {
        XCTAssertEqual(make("a", priority: "high").priorityValue, 0)
    }

    // MARK: - The order

    /// Highest first, in every column, and above `position` — so a drag
    /// cannot lift a card over one that is more important.
    func testHigherPrioritiesLeadEveryColumn() {
        let ninety = make("z", priority: "90", position: 99)
        let fifty = make("a", priority: "50", position: 1)
        for column: BoardColumn in [.prep, .backlog, .inProgress, .done] {
            XCTAssertTrue(BoardView.cardOrder(ninety, fifty, column: column))
            XCTAssertFalse(BoardView.cardOrder(fifty, ninety, column: column))
        }
    }

    /// Cards that share a number fall to `position` — which is what makes a
    /// drag meaningful between them, and only between them.
    func testASharedNumberFallsToPositionThenCreatedAtThenId() {
        let first = make("b", priority: "50", position: 1)
        let second = make("a", priority: "50", position: 2)
        XCTAssertTrue(BoardView.cardOrder(first, second, column: .backlog))
        XCTAssertFalse(BoardView.cardOrder(second, first, column: .backlog))

        let early = make("b", priority: "50", createdAt: 1)
        let late = make("a", priority: "50", createdAt: 2)
        XCTAssertTrue(BoardView.cardOrder(early, late, column: .backlog))

        let tieA = make("a", priority: "50"), tieB = make("b", priority: "50")
        XCTAssertTrue(BoardView.cardOrder(tieA, tieB, column: .backlog))
    }

    /// An unscored card sits with the zeroes at the bottom of its column, and
    /// the two are ordered between themselves by the drag.
    func testUnscoredCardsSitWithTheZeroesAndAreDragOrderedBetweenThemselves() {
        let scored = make("a", priority: "1", position: 99)
        let unscored = make("b", position: 1)
        let zero = make("c", priority: "0", position: 2)
        XCTAssertTrue(BoardView.cardOrder(scored, unscored, column: .backlog))
        XCTAssertTrue(BoardView.cardOrder(unscored, zero, column: .backlog))
        XCTAssertFalse(BoardView.cardOrder(zero, unscored, column: .backlog))
    }

    /// **The Done review clause stays first.** A high-priority Done card
    /// ahead of an unacknowledged agent close would reintroduce exactly the
    /// scroll-below-the-fold failure `ReviewPinTests` exists to prevent.
    func testTheDoneReviewClauseStillBeatsAHigherPriority() {
        let pending = make("z", priority: "0", closedBy: "s1")
        let important = make("a", priority: "100", closedBy: "s2",
                             reviewedAt: 5)
        XCTAssertTrue(BoardView.cardOrder(pending, important, column: .done))
        XCTAssertFalse(BoardView.cardOrder(important, pending, column: .done))
        // …and outside Done the clause buys nothing: the number decides.
        XCTAssertTrue(BoardView.cardOrder(important, pending,
                                          column: .backlog))
    }

    /// A board nobody has scored orders exactly as it does today.
    func testAnAllEmptySetOrdersAsItDidBefore() {
        let a = make("a", position: 1)
        let b = make("b", position: 2, createdAt: 2)
        let c = make("c", position: 2, createdAt: 1)
        for column: BoardColumn in [.prep, .backlog, .inProgress, .done] {
            XCTAssertTrue(BoardView.cardOrder(a, b, column: column))
            // Same position, so `createdAt` decides — oldest first.
            XCTAssertTrue(BoardView.cardOrder(c, b, column: column))
        }
    }
}
