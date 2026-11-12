import XCTest
@testable import BobPhone

/// The Board tab's row order as a rule, and the router's `replaceTop` on a
/// card rung. The table also runs on the host under `swiftc`
/// (`host/tests/test_phone_card_sheet_swipe.py`).
@MainActor
final class CardSheetSwipeTests: XCTestCase {
    override func tearDown() {
        for id in ["card/zero", "card/one", "card/two"] { _ = PhonePlaceStore.shared.takeOrphanDraft(for: id) }
        super.tearDown()
    }

    private func card(_ id: String, project: String = "") -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = "Card \(id)"
        c.project = project
        c.column = "in_progress"
        return c
    }

    private func names(_ groups: [CardSheetSwipe.Group<BoardCard>]) -> [String] { groups.map(\.name) }

    func testOneProjectDrawsFlat() {
        let groups = CardSheetSwipe.drawnGroups([card("a1", project: "a"), card("a2", project: "a")], project: \.project)
        XCTAssertEqual(names(groups), [""])
        XCTAssertEqual(groups[0].cards.map(\.id), ["a1", "a2"])
    }

    func testSeveralProjectsGroupSortedWithTheUnnamedLast() {
        let cards = [card("b1", project: "b"), card("a2", project: "a"), card("u3"), card("b4", project: "b")]
        let groups = CardSheetSwipe.drawnGroups(cards, project: \.project)
        XCTAssertEqual(names(groups), ["a", "b", "—"])
        XCTAssertEqual(groups.map { $0.cards.map(\.id) }, [["a2"], ["b1", "b4"], ["u3"]])
    }

    func testUnnamedOnlyAndOneNamedPlusUnnamedDrawFlat() {
        let u = CardSheetSwipe.drawnGroups([card("u1"), card("u2")], project: \.project)
        XCTAssertEqual(names(u), [""])
        let mixed = CardSheetSwipe.drawnGroups([card("a1", project: "a"), card("u2")], project: \.project)
        XCTAssertEqual(names(mixed), [""])
        XCTAssertEqual(mixed[0].cards.map(\.id), ["a1", "u2"])
    }

    func testNoCardsIsOneEmptyGroup() {
        let groups = CardSheetSwipe.drawnGroups([BoardCard](), project: \.project)
        XCTAssertEqual(names(groups), [""])
        XCTAssertTrue(groups[0].cards.isEmpty)
        XCTAssertEqual(CardSheetSwipe.ungroupedName, "—")
    }

    func testOrderFlattensDedupesAndEndsStop() {
        let cards = [card("b1", project: "b"), card("a2", project: "a"), card("u3"), card("b4", project: "b")]
        let order = CardSheetSwipe.order(cards, project: \.project, id: \.id)
        XCTAssertEqual(order, ["a2", "b1", "b4", "u3"])
        XCTAssertEqual(CardSheetSwipe.order([card("a1"), card("a1")], project: \.project, id: \.id), ["a1"])
        XCTAssertEqual(CardSheetSwipe.order([BoardCard](), project: \.project, id: \.id), [])
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "u3", in: order, .next))
        XCTAssertNil(AgentSheetSwipe.neighbour(of: "a2", in: order, .previous))
        XCTAssertEqual(AgentSheetSwipe.neighbour(of: "a2", in: order, .next), "b1")
    }

    func testReplaceTopSwapsTheCardWithoutClosing() {
        let router = PhoneSheetRouter()
        router.show(.card(card("one")))
        let before = router.topState?.id
        router.replaceTop(.card(card("two")))
        XCTAssertEqual(router.stack.count, 1)
        XCTAssertEqual(router.top?.id, "card/two")
        XCTAssertNotNil(router.presentation)
        XCTAssertNotEqual(router.topState?.id, before)
    }

    func testBackAfterASwipeReturnsToWhereTheCardWasOpenedFrom() {
        let router = PhoneSheetRouter()
        router.show(.card(card("zero")))
        router.show(.card(card("one")))
        router.replaceTop(.card(card("two")))
        router.back()
        XCTAssertEqual(router.top?.id, "card/zero")
    }

    func testReplaceTopOfTheEqualCardKeepsTheState() {
        let router = PhoneSheetRouter()
        router.show(.card(card("one")))
        let before = router.topState?.id
        router.replaceTop(.card(card("one")))
        XCTAssertEqual(router.topState?.id, before)
    }

    func testATypedMessageSurvivesSwipingAwayAndBack() {
        let router = PhoneSheetRouter()
        router.show(.card(card("one")))
        router.topState!.cardDraft(for: "one").messageText = "half"
        router.replaceTop(.card(card("two")))
        router.replaceTop(.card(card("one")))
        XCTAssertEqual(router.topState!.cardDraft(for: "one").messageText, "half")
    }
}
