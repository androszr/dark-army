import XCTest
@testable import BobPanel

/// `AgentDetailHeader.cardLine(card:sessionId:)` — the detail header's lead
/// line, and how it tells writing a plan from doing the work.
///
/// The rule is one sentence: a card *bound* to this session reads as its bare
/// title, a card only being *refined* under this session says "Planning:"
/// first. The bound rung wins, because a session that refined a card and was
/// later dispatched on it is working it, not planning it.
final class AgentCardLineTests: XCTestCase {
    private func card(id: String,
                      title: String,
                      session: String = "",
                      refine: String = "",
                      refining: Bool = true) -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = title
        c.project = "dark-army"
        c.sessionId = session
        c.refineSessionId = refine
        // A refine link is only a link while the refinement is running; the
        // field outlives it as an audit trail.
        c.refineState = (refine.isEmpty || !refining) ? "" : "live"
        return c
    }

    func testBoundCardReadsAsItsBareTitle() {
        let c = card(id: "a", title: "Ship the thing", session: "S")
        XCTAssertEqual(
            AgentDetailHeader.cardLine(card: c, sessionId: "S"),
            "Ship the thing")
    }

    func testRefiningCardSaysPlanning() {
        let c = card(id: "a", title: "Ship the thing", refine: "S")
        XCTAssertEqual(
            AgentDetailHeader.cardLine(card: c, sessionId: "S"),
            "Planning: Ship the thing")
    }

    /// The same session in both fields is *working* the card. And a card whose
    /// refinement has finished is nobody's plan any more, so the board's own
    /// lookup hands back the bound card, not the stale link.
    func testTheBoundRungWinsOverAStaleRefineLink() {
        let bound = card(id: "bound", title: "Do it",
                         session: "S", refine: "S", refining: false)
        XCTAssertEqual(
            AgentDetailHeader.cardLine(card: bound, sessionId: "S"), "Do it")

        var b = Board()
        b.available = true
        b.cards = [card(id: "stale", title: "Old plan",
                        refine: "S", refining: false), bound]
        XCTAssertEqual(b.card(forSession: "S")?.id, "bound")
        XCTAssertEqual(
            AgentDetailHeader.cardLine(card: b.card(forSession: "S"),
                                       sessionId: "S"),
            "Do it")
    }

    func testNoCardAndAnEmptyTitleBothDrawNothing() {
        XCTAssertEqual(
            AgentDetailHeader.cardLine(card: nil, sessionId: "S"), "")
        let blank = card(id: "a", title: "   ", session: "S")
        XCTAssertEqual(
            AgentDetailHeader.cardLine(card: blank, sessionId: "S"), "")
    }
}
