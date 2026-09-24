import XCTest
@testable import BobPanel

/// `BoardCard.deleteClosesTerminal` — the panel approximation that switches
/// the Delete armed label. The daemon's provider check is authoritative;
/// this only has to agree with the labels the two delete surfaces read.
final class DeleteClosesTerminalTests: XCTestCase {

    private func card(column: String = "backlog",
                      linkState: String = "",
                      sessionId: String = "",
                      tool: String = "claude",
                      refineState: String = "",
                      refineSessionId: String = "") -> BoardCard {
        var c = BoardCard()
        c.column = column
        c.linkState = linkState
        c.sessionId = sessionId
        c.tool = tool
        c.refineState = refineState
        c.refineSessionId = refineSessionId
        return c
    }

    func testALiveBoundCardClosesTheTerminal() {
        XCTAssertTrue(card(linkState: "live", sessionId: "s1").deleteClosesTerminal)
    }

    func testACardWithNobodyWorkingDoesNot() {
        XCTAssertFalse(card().deleteClosesTerminal)
    }

    func testADoneLiveCardClosesTheTerminal() {
        XCTAssertTrue(card(column: "done",
                           linkState: "live",
                           sessionId: "s1").deleteClosesTerminal)
    }

    func testALiveCodexCardDoesNot() {
        XCTAssertFalse(card(linkState: "live",
                            sessionId: "s1",
                            tool: "codex").deleteClosesTerminal)
    }

    func testALiveRefinementClosesTheInterview() {
        XCTAssertTrue(card(column: "prep",
                           refineState: "live",
                           refineSessionId: "rsid").deleteClosesTerminal)
    }

    func testDispatchingClosesWhetherBacklogOrDone() {
        XCTAssertTrue(card(linkState: "dispatching").deleteClosesTerminal)
        XCTAssertTrue(card(column: "done",
                           linkState: "dispatching").deleteClosesTerminal)
    }

    func testEndedDoesNot() {
        XCTAssertFalse(card(linkState: "ended",
                            sessionId: "s1").deleteClosesTerminal)
    }

    func testADispatchingCodexCardDoesNot() {
        XCTAssertFalse(card(linkState: "dispatching",
                            tool: "codex").deleteClosesTerminal)
    }
}
