import XCTest
@testable import BobPhone

/// The agent sheet's swipe rules and the router's `replaceTop`, as the phone
/// runs them. The rule table also runs on the host under `swiftc`
/// (`host/tests/test_phone_sheet_swipe.py`).
@MainActor
final class AgentSheetSwipeTests: XCTestCase {
    override func tearDown() {
        for id in ["agent/s1", "agent/s2"] { _ = PhonePlaceStore.shared.takeOrphanDraft(for: id) }
        super.tearDown()
    }

    private func agent(_ id: String) -> Agent {
        var a = Agent()
        a.sessionId = id
        return a
    }

    private func card(_ id: String) -> PhoneSheet {
        var card = BoardCard(); card.id = id; card.title = "Card"
        return .card(card)
    }

    func testDirection() {
        XCTAssertEqual(AgentSheetSwipe.direction(dx: -80, dy: 5), .next)
        XCTAssertEqual(AgentSheetSwipe.direction(dx: 80, dy: -10), .previous)
        XCTAssertNil(AgentSheetSwipe.direction(dx: -40, dy: 0))
        XCTAssertNil(AgentSheetSwipe.direction(dx: -80, dy: 50))
        XCTAssertNil(AgentSheetSwipe.direction(dx: 0, dy: -120))
        XCTAssertEqual(AgentSheetSwipe.direction(dx: -120, dy: -60), .next)
    }

    func testNeighbour() {
        let order = ["a", "b", "c"]
        XCTAssertEqual(AgentSheetSwipe.neighbour(of: "b", in: order, .next), "c")
        XCTAssertEqual(AgentSheetSwipe.neighbour(of: "b", in: order, .previous), "a")
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "c", in: order, .next))
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "a", in: order, .previous))
        XCTAssertEqual(AgentSheetSwipe.neighbour(of: "zz", in: order, .next), "a")
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "zz", in: order, .previous))
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "a", in: [], .next))
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "a", in: [], .previous))
    }

    func testOrder() {
        XCTAssertEqual(AgentSheetSwipe.order([["a", "b"], ["c"]]) { $0 }, ["a", "b", "c"])
        XCTAssertEqual(AgentSheetSwipe.order([["a", "skip", "a"], ["d"]]) { $0 == "skip" ? nil : $0 },
                       ["a", "d"])
    }

    func testReplaceTopOnAnEmptyRouterIsANoOp() {
        let router = PhoneSheetRouter()
        router.replaceTop(.agent(agent("s1"), .waiting))
        XCTAssertTrue(router.stack.isEmpty)
        XCTAssertNil(router.presentation)
    }

    func testReplaceTopKeepsTheTrailAndRemounts() {
        let router = PhoneSheetRouter()
        router.show(card("one"))
        router.show(.agent(agent("s1"), .waiting))
        let before = router.topState?.id
        let presentation = router.presentation?.id
        router.replaceTop(.agent(agent("s2"), .waiting))
        XCTAssertEqual(router.stack.count, 2)
        XCTAssertEqual(router.top?.id, "agent/s2")
        XCTAssertNotEqual(router.topState?.id, before)
        XCTAssertEqual(router.presentation?.id, presentation)
        router.back()
        XCTAssertEqual(router.top?.id, "card/one")
    }

    func testReplaceTopOfTheEqualSubjectIsARefresh() {
        let router = PhoneSheetRouter()
        router.show(.agent(agent("s1"), .waiting))
        let before = router.topState?.id
        router.replaceTop(.agent(agent("s1"), .waiting))
        XCTAssertEqual(router.topState?.id, before)
        XCTAssertEqual(router.stack.count, 1)
    }

    func testAHalfTypedReplySurvivesSwipingAwayAndBack() {
        let router = PhoneSheetRouter()
        router.show(.agent(agent("s1"), .waiting))
        router.topState!.replyDraft(for: "s1").text = "half"
        router.replaceTop(.agent(agent("s2"), .waiting))
        router.replaceTop(.agent(agent("s1"), .waiting))
        XCTAssertEqual(router.topState!.replyDraft(for: "s1").text, "half")
    }

    // MARK: - the position mark

    func testThePositionIsOneBasedAndNeedsTwoWaiters() {
        let o = ["a", "b", "c"]
        XCTAssertEqual(AgentSheetSwipe.position(of: "a", in: o), .init(index: 1, count: 3))
        XCTAssertEqual(AgentSheetSwipe.position(of: "b", in: o), .init(index: 2, count: 3))
        XCTAssertEqual(AgentSheetSwipe.position(of: "c", in: o), .init(index: 3, count: 3))
        XCTAssertNil(AgentSheetSwipe.position(of: "a", in: ["a"]))
        XCTAssertNil(AgentSheetSwipe.position(of: "a", in: []))
    }

    func testAnAbsentAgentHasNoPosition() {
        XCTAssertNil(AgentSheetSwipe.position(of: "zz", in: ["a", "b", "c"]))
        XCTAssertEqual(AgentSheetSwipe.position(of: "a", in: ["a", "b", "a"]), .init(index: 1, count: 3))
    }

    func testTheMarkAndItsSpokenForm() {
        XCTAssertEqual(AgentSheetSwipe.mark(.init(index: 2, count: 5)), "2 of 5")
        XCTAssertEqual(AgentSheetSwipe.mark(.init(index: 1, count: 2)), "1 of 2")
        XCTAssertEqual(AgentSheetSwipe.spokenMark(.init(index: 2, count: 5)), "2 of 5 waiting")
    }

    func testTheMarkYieldsAtAccessibilitySizes() {
        let p = AgentSheetSwipe.Position(index: 2, count: 5)
        XCTAssertTrue(AgentSheetSwipe.showsMark(p, accessibilitySize: false))
        XCTAssertFalse(AgentSheetSwipe.showsMark(p, accessibilitySize: true))
        XCTAssertFalse(AgentSheetSwipe.showsMark(nil, accessibilitySize: false))
        XCTAssertFalse(AgentSheetSwipe.showsMark(nil, accessibilitySize: true))
    }
}
