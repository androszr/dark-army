import XCTest
@testable import BobPhone

final class CatchUpTests: XCTestCase {
    @MainActor
    func testLockOrPairingStopDiscardsAnAlreadyRunningHistoryResponse() async {
        let requests = CatchUpRequests()
        var continuation: CheckedContinuation<CatchUpPage?, Never>?
        let fetch = Task {
            await requests.run {
                await withCheckedContinuation { continuation = $0 }
            }
        }
        while continuation == nil { await Task.yield() }
        requests.cancel()
        var page = CatchUpPage(); page.available = true
        continuation?.resume(returning: page)
        let result = await fetch.value
        XCTAssertNil(result)
        let fresh = await requests.run { page }
        XCTAssertEqual(fresh?.available, true)
    }
    func testTolerantDecoding() throws {
        let page = try JSONDecoder().decode(CatchUpPage.self, from: Data("{}".utf8))
        XCTAssertFalse(page.available)
        XCTAssertEqual(page.items.count, 0)
    }
    func testOnlyCompleteGlobalSinceWindowCanAdvance() {
        var state = CatchUpState()
        var page = CatchUpPage(); page.available = true; page.upperCursor = 12; page.nextCursor = 5
        state.append(page)
        XCTAssertNil(state.checkpoint(project: "", timeFilter: "since"))
        page.nextCursor = nil; state.append(page)
        XCTAssertEqual(state.checkpoint(project: "", timeFilter: "since"), 12)
        XCTAssertNil(state.checkpoint(project: "/a", timeFilter: "since"))
        XCTAssertNil(state.checkpoint(project: "", timeFilter: "week"))
        state.subset = true
        XCTAssertNil(state.checkpoint(project: "", timeFilter: "since"))
    }
    func testNewArrivalsCannotMoveDisplayedWatermark() {
        var state = CatchUpState()
        var page = CatchUpPage(); page.available = true; page.upperCursor = 12
        state.append(page)
        page.upperCursor = 13; state.append(page)
        XCTAssertEqual(state.upperCursor, 12)
    }
    func testMissingAndNewerEpisodeHaveNoLiveTarget() throws {
        var item = DecisionItem(); item.id = "old"; item.sessionId = "s"; item.status = "open"
        var snapshot = Snapshot()
        XCTAssertNil(item.liveAgent(in: snapshot))
        var agent = try JSONDecoder().decode(Agent.self, from: Data("{}".utf8)); agent.sessionId = "s"; agent.decisionEpisodeId = "new"
        snapshot.agents.waiting = [agent]
        XCTAssertNil(item.liveAgent(in: snapshot))
        agent.decisionEpisodeId = "old"; snapshot.agents.waiting = [agent]
        XCTAssertNotNil(item.liveAgent(in: snapshot))
        item.status = "delivered_unconfirmed"
        XCTAssertNil(item.liveAgent(in: snapshot))
    }
    func testUnresolvedFirstAndRevisionsDeduplicate() {
        var old = DecisionItem(); old.id = "a"; old.cursor = 1; old.status = "open"
        var latest = old; latest.cursor = 3; latest.status = "answered_observed"
        var open = DecisionItem(); open.id = "b"; open.cursor = 2; open.status = "open"
        var page = CatchUpPage(); page.available = true; page.items = [old, open, latest]
        var state = CatchUpState(); state.append(page)
        XCTAssertEqual(state.items.map(\.id), ["b", "a"])
    }
}
