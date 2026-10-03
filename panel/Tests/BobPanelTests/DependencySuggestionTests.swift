import XCTest
@testable import BobPanel

final class DependencySuggestionTests: XCTestCase {
    func testSuggestionNeverOverwritesAFilledBox() {
        let listed: Set<String> = ["a", "b"]
        XCTAssertNil(DependencySuggestion.decide(offer: ["a"], current: "b", listed: listed))
        XCTAssertNil(DependencySuggestion.decide(offer: ["a"], current: "b\n", listed: listed))
        XCTAssertEqual(DependencySuggestion.decide(offer: ["a"], current: "  \n", listed: listed), "a")
    }

    func testAnEmptyOfferOrAnEmptyListChangesNothing() {
        XCTAssertNil(DependencySuggestion.decide(offer: [], current: "", listed: ["a"]))
        XCTAssertNil(DependencySuggestion.decide(offer: ["a"], current: "", listed: []))
    }

    func testUnlistedIdsAreFilteredAndOfferOrderIsKept() {
        let got = DependencySuggestion.decide(
            offer: ["c", "gone", "a", "b"], current: "", listed: ["a", "b", "c"])
        XCTAssertEqual(got, "c\na\nb")
        XCTAssertNil(DependencySuggestion.decide(
            offer: ["gone"], current: "", listed: ["a"]))
    }

    func testDraftRoundTripsBlockedByAndAnOlderRowReadsEmpty() throws {
        let draft = CardDraft(id: "deps-only", blockedBy: "a\nb")
        let restored = try XCTUnwrap(CardDraft(id: draft.id, any: draft.body))
        XCTAssertEqual(restored.blockedBy, "a\nb")
        var active = BoardDraft()
        active.blockedBy = restored.blockedBy
        XCTAssertTrue(CardDrafts.worthKeeping(draft: active, staged: []))
        var old = draft.body
        old.removeValue(forKey: "blocked_by")
        XCTAssertEqual(try XCTUnwrap(CardDraft(id: "old", any: old)).blockedBy, "")
    }

    func testAFilledBoxHoldsTheProjectInPlace() {
        XCTAssertFalse(DependencySuggestion.allowsProjectMove(boxBeforePrepare: "a"))
        XCTAssertTrue(DependencySuggestion.allowsProjectMove(boxBeforePrepare: " \n"))
    }

    func testPrepareResultDefaultsToNoSuggestion() {
        let result = PrepareResult(ok: true, detail: "", prompt: "p", workflow: "")
        XCTAssertEqual(result.suggestedDependencies, [])
    }

    func testEditorOffersOnlySameProjectNotDoneNotListedNotSelf() {
        func card(_ id: String, _ root: String, _ column: String) -> BoardCard {
            var c = BoardCard()
            c.id = id; c.root = root; c.column = column
            return c
        }
        let cards = [card("a", "/p", "prep"), card("b", "/p", "done"),
                     card("c", "/q", "prep"), card("d", "/p", "backlog"),
                     card("me", "/p", "prep")]
        XCTAssertEqual(
            DependencyEditor.choices(root: "/p", selfId: "me", listed: ["a"], in: cards).map(\.id),
            ["d"])
        XCTAssertEqual(DependencyEditor.listedIds(root: "/p", in: cards), ["a", "d", "me"])
    }
}
