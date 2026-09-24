import XCTest
@testable import BobPanel

/// The panel's half of plan approval: an older daemon's frame must decode
/// with no approval and no capability, and the gate's two refusals must be
/// told apart by their opening words.
final class PlanApprovalTests: XCTestCase {

    private func decodeCard(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func decodeBoard(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    /// The documented trap: Swift's synthesized `Decodable` throws on a
    /// missing key even where the property has a default. A daemon that has
    /// never heard of the approval must not blank the card.
    func testACardWithNoneOfTheNewKeysStillDecodes() throws {
        let card = try decodeCard("""
        {"id": "c1", "title": "t", "plan_path": "plans/p.md"}
        """)
        XCTAssertEqual(card.id, "c1")
        XCTAssertEqual(card.planPath, "plans/p.md")
        XCTAssertEqual(card.planApproved, "")
        XCTAssertEqual(card.planApprovedAt, 0)
    }

    func testAnApprovedCardCarriesTheDigestAndTheTime() throws {
        let card = try decodeCard("""
        {"id": "c1", "plan_approved": "abc123", "plan_approved_at": 1700.5}
        """)
        XCTAssertEqual(card.planApproved, "abc123")
        XCTAssertEqual(card.planApprovedAt, 1700.5)
    }

    /// Both matchers are prefix tests, so a refusal must fire exactly one of
    /// them: two firing on one sentence would silently turn the changed-plan
    /// confirmation back into the unplanned one.
    func testTheTwoRefusalsAreToldApart() {
        let gate = ActionResult(
            ok: false,
            detail: "this card has no plan yet — refine it first, or confirm "
                  + "starting without one")
        XCTAssertTrue(gate.isPlanGateRefusal)
        XCTAssertFalse(gate.isPlanChangedRefusal)
        XCTAssertTrue(gate.isPlanConfirmable)

        let changed = ActionResult(
            ok: false,
            detail: "this card's plan has changed since it was approved — "
                  + "read it again, or confirm starting anyway")
        XCTAssertTrue(changed.isPlanChangedRefusal)
        XCTAssertFalse(changed.isPlanGateRefusal)
        XCTAssertTrue(changed.isPlanConfirmable)
    }

    func testAnOrdinaryRefusalIsNeither() {
        let other = ActionResult(ok: false, detail: "that card moved")
        XCTAssertFalse(other.isPlanConfirmable)
    }

    /// The held move now says which rung raised it, so the dialog can word
    /// itself. Default false — every route that holds *before* sending is the
    /// unplanned one.
    func testPendingMoveDefaultsToTheUnplannedRung() {
        let held = PendingMove(cardId: "c1", column: "in_progress",
                               beforeId: nil, dispatches: true)
        XCTAssertFalse(held.planChanged)
        let stale = PendingMove(cardId: "c1", column: "in_progress",
                                beforeId: nil, dispatches: true,
                                planChanged: true)
        XCTAssertTrue(stale.planChanged)
    }

    /// The digest the panel echoes is the daemon's own: SHA-256 over the
    /// plan's UTF-8 bytes, lowercase hex. Disagree and every approval is
    /// refused.
    func testTheDigestMatchesTheDaemons() {
        // sha256("hello\n")
        XCTAssertEqual(
            BoardCardSheet.digest("hello\n"),
            "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03")
    }
}
