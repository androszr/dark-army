import XCTest
@testable import BobPanel

/// `Board.card(forSession:)` — which card a session's readout links back to.
///
/// The rule is a two-rung ladder over the daemon's own bookkeeping: the card
/// the session is *executing* first, the Prep card it is *refining* second.
final class CardRevealTests: XCTestCase {
    private func card(id: String,
                      session: String = "",
                      refine: String = "",
                      title: String = "",
                      project: String = "dark-army") -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = title.isEmpty ? id : title
        c.project = project
        c.sessionId = session
        c.refineSessionId = refine
        // A refine link is only a link while the refinement is running; the
        // field outlives it as an audit trail. Fixtures that mean "refining"
        // must say so, exactly as the daemon does.
        c.refineState = refine.isEmpty ? "" : "live"
        return c
    }

    private func board(_ cards: [BoardCard]) -> Board {
        var b = Board()
        b.available = true
        b.cards = cards
        return b
    }

    func testExecutingBindingOutranksRefining() {
        let b = board([
            card(id: "refining", refine: "S"),
            card(id: "executing", session: "S"),
        ])
        XCTAssertEqual(b.card(forSession: "S")?.id, "executing")
    }

    func testRefiningBindingResolvesWhenNothingIsExecuting() {
        let b = board([
            card(id: "other", session: "T"),
            card(id: "refining", refine: "S"),
        ])
        XCTAssertEqual(b.card(forSession: "S")?.id, "refining")
    }

    func testNoBindingResolvesToNothing() {
        let b = board([card(id: "a"), card(id: "b", session: "T")])
        XCTAssertNil(b.card(forSession: "S"))
    }

    func testEmptySessionIdNeverMatchesAnUnstartedCard() {
        let b = board([card(id: "unstarted"), card(id: "unrefined")])
        XCTAssertNil(b.card(forSession: ""))
    }

    func testSnapshotOrderBreaksATie() {
        let b = board([
            card(id: "first", session: "S"),
            card(id: "second", session: "S"),
        ])
        XCTAssertEqual(b.card(forSession: "S")?.id, "first")
    }

    /// The bug this gate exists for. `attach_plan` clears `refine_state` and
    /// leaves `refine_session_id` standing, so the session that wrote the plan
    /// still names a card that has since been dispatched to another agent.
    /// It must not claim it.
    func testAFinishedRefinementNoLongerClaimsTheCard() {
        var stale = card(id: "planned", session: "OTHER", refine: "S")
        stale.refineState = ""          // attach_plan cleared it; the id stayed.
        XCTAssertNil(board([stale]).card(forSession: "S"))
    }

    /// And the same card must still resolve for the agent actually on it.
    func testTheExecutingSessionStillResolvesAStalelyRefinedCard() {
        var stale = card(id: "planned", session: "OTHER", refine: "S")
        stale.refineState = ""
        XCTAssertEqual(board([stale]).card(forSession: "OTHER")?.id, "planned")
    }

    func testEmptyBoardResolvesToNothing() {
        XCTAssertNil(Board().card(forSession: "S"))
    }
}

/// `CardReveal.outcome` — what the reverse jump from VS Code should do against
/// the board that is on screen at the moment the request lands.
///
/// The interesting half is the waiting: a request routinely beats the snapshot
/// that would carry its card, and a request that is never consumed would yank
/// the board an hour later when some card happens to bind to that session.
final class CardRevealOutcomeTests: XCTestCase {
    private func card(id: String, session: String = "",
                      refine: String = "") -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = id
        c.project = "dark-army"
        c.sessionId = session
        c.refineSessionId = refine
        c.refineState = refine.isEmpty ? "" : "live"
        return c
    }

    func testAnUnavailableBoardHoldsTheRequest() {
        var b = Board()          // `available` defaults false: no board yet.
        b.cards = []
        XCTAssertEqual(CardReveal.outcome(session: "S", board: b), .wait)
    }

    func testARealBoardWithNoCardDropsTheRequest() {
        var b = Board()
        b.available = true
        b.cards = [card(id: "other", session: "T")]
        XCTAssertEqual(CardReveal.outcome(session: "S", board: b), .drop)
    }

    func testTheExecutingCardIsRevealed() {
        var b = Board()
        b.available = true
        b.cards = [card(id: "refining", refine: "S"),
                   card(id: "executing", session: "S")]
        guard case .reveal(let hit) = CardReveal.outcome(session: "S", board: b)
        else { return XCTFail("expected a reveal") }
        XCTAssertEqual(hit.id, "executing")
    }

    func testAnEmptySessionIdDropsRatherThanRevealingAnUnstartedCard() {
        var b = Board()
        b.available = true
        b.cards = [card(id: "unstarted")]
        XCTAssertEqual(CardReveal.outcome(session: "", board: b), .drop)
    }

    func testEditorJumpOpensTheBoardWhenTheSessionHasACard() {
        var b = Board()
        b.available = true
        b.cards = [card(id: "bound", session: "S")]
        let hit = CardReveal.outcome(session: "S", board: b)
        XCTAssertTrue(CardReveal.openBoard(wantsCard: true, outcome: hit))
        XCTAssertFalse(CardReveal.holdFocus(wantsCard: true, outcome: hit))
        // A banner tap never asked for the card.
        XCTAssertFalse(CardReveal.openBoard(wantsCard: false, outcome: hit))
    }

    func testEditorJumpWithoutACardOpensTheAgent() {
        var b = Board()
        b.available = true
        b.cards = [card(id: "other", session: "T")]
        let miss = CardReveal.outcome(session: "S", board: b)
        XCTAssertFalse(CardReveal.openBoard(wantsCard: true, outcome: miss))
        XCTAssertFalse(CardReveal.holdFocus(wantsCard: true, outcome: miss))
    }

    func testEditorJumpWaitsForABoardThatHasNotArrived() {
        let pending = CardReveal.outcome(session: "S", board: Board())
        XCTAssertTrue(CardReveal.holdFocus(wantsCard: true, outcome: pending))
        XCTAssertFalse(CardReveal.openBoard(wantsCard: true, outcome: pending))
        XCTAssertFalse(CardReveal.holdFocus(wantsCard: false, outcome: pending))
    }
}
