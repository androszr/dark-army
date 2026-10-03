import XCTest
@testable import BobPanel

/// Rebuild & restart on the Mac: the agent's `rebuild_offered` flag decodes
/// false when absent (an older daemon draws no button), is independent of the
/// Low priority flag, the context's rebuild keys default empty, and the words
/// the top bar and the agent bar wear are pinned.
final class RebuildReachTests: XCTestCase {

    private func decodeAgent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    func testRebuildOfferedTrue() throws {
        let agent = try decodeAgent(#"{"session_id":"s1","rebuild_offered":true}"#)
        XCTAssertTrue(agent.rebuildOffered)
    }

    func testRebuildOfferedFalse() throws {
        let agent = try decodeAgent(#"{"session_id":"s1","rebuild_offered":false}"#)
        XCTAssertFalse(agent.rebuildOffered)
    }

    func testRebuildOfferedAbsentIsFalse() throws {
        let agent = try decodeAgent(#"{"session_id":"s1"}"#)
        XCTAssertFalse(agent.rebuildOffered)
    }

    func testRebuildOfferedIsIndependentOfCanLowPriority() throws {
        let agent = try decodeAgent(
            #"{"session_id":"s1","can_low_priority":true,"rebuild_offered":false}"#)
        XCTAssertTrue(agent.canLowPriority)
        XCTAssertFalse(agent.rebuildOffered)
    }

    func testRebuildOfferedWrongTypeDoesNotBlankTheRow() throws {
        let agent = try decodeAgent(#"{"session_id":"s1","rebuild_offered":"yes"}"#)
        XCTAssertFalse(agent.rebuildOffered)
        XCTAssertEqual(agent.sessionId, "s1")
    }

    func testPanelContextRebuildKeysDefaultEmpty() {
        let ctx = DaemonClient.PanelContext()
        XCTAssertEqual(ctx.rebuildOutcome, "")
        XCTAssertEqual(ctx.rebuildError, "")
        XCTAssertNil(ctx.rebuildStartedAt)
        XCTAssertFalse(ctx.canRebuild)
    }

    func testTheStaleNoteSaysTheBuildIsOlderThanTheSource() {
        XCTAssertTrue(RebuildControl.staleNote.contains("older than the source"))
    }

    func testTitlePassesTheLabelThrough() {
        XCTAssertEqual(
            RebuildControl.title(label: "Rebuild & Reload", rebuilding: false, outcome: ""),
            "Rebuild & Reload")
        XCTAssertEqual(
            RebuildControl.title(label: "Rebuild & Deploy", rebuilding: false, outcome: ""),
            "Rebuild & Deploy")
    }

    func testRebuildingWinsOverAFailure() {
        XCTAssertEqual(
            RebuildControl.title(label: "Rebuild & Reload", rebuilding: true, outcome: "failed"),
            "Rebuilding…")
    }

    func testAFailureSaysSoUntilTheNextAttempt() {
        XCTAssertEqual(
            RebuildControl.title(label: "Rebuild & Reload", rebuilding: false, outcome: "failed"),
            "Rebuild failed — try again")
    }

    func testAnUnknownOutcomeKeepsTheLabel() {
        XCTAssertEqual(
            RebuildControl.title(label: "Rebuild & Reload", rebuilding: false, outcome: "ok"),
            "Rebuild & Reload")
    }
}
