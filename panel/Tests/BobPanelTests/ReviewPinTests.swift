import XCTest
@testable import BobPanel

/// The review acknowledgement as the panel sees it: a tolerant decode of
/// `reviewed_at`, the rule that a pending close is an agent close nobody has
/// acknowledged, and the Done column's pin — pending closes lead, everything
/// else keeps today's order byte for byte.
final class ReviewPinTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func make(_ id: String, position: Double = 0,
                      createdAt: Double = 0, closedBy: String = "",
                      reviewedAt: Double? = nil) -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.position = position
        c.createdAt = createdAt
        c.closedBy = closedBy
        c.reviewedAt = reviewedAt
        return c
    }

    // MARK: - Decoding

    /// A daemon from before `reviewed_at` shipped sends cards with no key at
    /// all. Swift's synthesized `Decodable` throws on a missing key even where
    /// the property has a default — one absent field must never blank the
    /// board (Models.swift's documented trap) — and on such a daemon an
    /// agent-closed card reads as pending, which is the stated degradation:
    /// the banner draws, and the press fails into the refusal line.
    func testAnAbsentReviewedAtPendsOnlyAnAgentClose() throws {
        let closed = try card(#"{"id":"a","title":"t","closed_by":"s1"}"#)
        XCTAssertNil(closed.reviewedAt)
        XCTAssertTrue(closed.awaitsReview)
        // A hand-dragged card has no close to acknowledge: `awaitsReview` is
        // a property of the close, not of the column.
        let dragged = try card(#"{"id":"b","title":"t"}"#)
        XCTAssertFalse(dragged.awaitsReview)
    }

    func testAPresentReviewedAtDropsTheBanner() throws {
        let row = try card(
            #"{"id":"a","title":"t","closed_by":"s1","reviewed_at":1000.5}"#)
        XCTAssertEqual(row.reviewedAt, 1000.5)
        XCTAssertFalse(row.awaitsReview)
    }

    /// The close signature is untouched by the acknowledgement: pressing
    /// Reviewed drops the banner, never the name or the note.
    func testTheSignatureSurvivesTheDecodeEitherWay() throws {
        let row = try card(
            #"{"id":"a","title":"t","closed_by":"s1","close_note":"tests pass","reviewed_at":5}"#)
        XCTAssertEqual(row.closedBy, "s1")
        XCTAssertEqual(row.closeNote, "tests pass")
    }

    // MARK: - The Done pin

    /// A pending close leads the Done column regardless of the ordinary
    /// terms — a banner that can scroll below the fold is a banner that can
    /// be missed, which is the failure it exists to prevent.
    func testPendingClosesLeadTheDoneColumn() {
        let pending = make("z", position: 99, closedBy: "s1")
        let reviewed = make("a", position: 1, closedBy: "s2", reviewedAt: 5)
        let dragged = make("b", position: 2)
        XCTAssertTrue(BoardView.cardOrder(pending, reviewed, column: .done))
        XCTAssertFalse(BoardView.cardOrder(reviewed, pending, column: .done))
        XCTAssertTrue(BoardView.cardOrder(pending, dragged, column: .done))
        XCTAssertFalse(BoardView.cardOrder(dragged, pending, column: .done))
    }

    /// Two pending closes fall back to the ordinary order — the pin is one
    /// leading term, not a second sort.
    func testTwoPendingClosesKeepTheOrdinaryOrder() {
        let older = make("a", position: 1, closedBy: "s1")
        let newer = make("b", position: 2, closedBy: "s2")
        XCTAssertTrue(BoardView.cardOrder(older, newer, column: .done))
        XCTAssertFalse(BoardView.cardOrder(newer, older, column: .done))
    }

    /// Every other column is byte-identical to today: `position`, then
    /// `createdAt`, then `id` — a pending close buys nothing outside Done.
    func testOrdinaryOrderIsPreservedOutsideDone() {
        let pending = make("z", position: 99, closedBy: "s1")
        let plain = make("a", position: 1)
        for column: BoardColumn in [.prep, .backlog, .inProgress] {
            XCTAssertTrue(BoardView.cardOrder(plain, pending, column: column))
            XCTAssertFalse(BoardView.cardOrder(pending, plain, column: column))
        }
        // And the tie-breaks are unchanged: createdAt, then id.
        let early = make("b", createdAt: 1)
        let late = make("a", createdAt: 2)
        XCTAssertTrue(BoardView.cardOrder(early, late, column: .backlog))
        let tieA = make("a"), tieB = make("b")
        XCTAssertTrue(BoardView.cardOrder(tieA, tieB, column: .backlog))
    }
}
