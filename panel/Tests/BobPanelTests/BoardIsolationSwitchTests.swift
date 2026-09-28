import XCTest
@testable import BobPanel

/// Card isolation on the board: the tolerant decode of a card's three
/// worktree keys and the board's `isolation_overrides`, and the one rule the
/// In progress heading's switch reads. `docs/card-worktrees.md`,
/// `plans/2026-09-28-card-worktree-isolation.md`.
final class BoardIsolationSwitchTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func board(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    func testACardWithoutTheKeysDecodesEmpty() throws {
        let decoded = try card(#"{"id":"a","title":"kept","column_name":"backlog"}"#)
        XCTAssertEqual(decoded.isolation, "")
        XCTAssertEqual(decoded.worktreeBranch, "")
        XCTAssertEqual(decoded.worktreeNote, "")
        XCTAssertEqual(decoded.title, "kept")
    }

    func testACardWithTheKeysDecodesThem() throws {
        let decoded = try card(#"""
            {"id":"a","isolation":"on","worktree_branch":"card/abcd1234-fix-it",
             "worktree_path":"/p/.worktrees/card-abcd1234",
             "worktree_note":"Dark Army is preparing this card's own branch"}
            """#)
        XCTAssertEqual(decoded.isolation, "on")
        XCTAssertEqual(decoded.worktreeBranch, "card/abcd1234-fix-it")
        XCTAssertEqual(decoded.worktreeNote,
                       "Dark Army is preparing this card's own branch")
    }

    func testAMalformedKeyNeverBlanksTheCard() throws {
        let decoded = try card(#"""
            {"id":"a","title":"kept","isolation":3,"worktree_branch":null,
             "worktree_note":{}}
            """#)
        XCTAssertEqual(decoded.title, "kept")
        XCTAssertEqual(decoded.isolation, "")
        XCTAssertEqual(decoded.worktreeBranch, "")
        XCTAssertEqual(decoded.worktreeNote, "")
    }

    func testABoardWithoutOverridesDecodesToAnEmptyMap() throws {
        let decoded = try board(#"{"cards":[],"parallel_limit":2}"#)
        XCTAssertEqual(decoded.isolationOverrides, [:])
        XCTAssertEqual(decoded.parallelLimit, 2)
    }

    func testABoardCarriesItsOverrides() throws {
        let decoded = try board(#"{"cards":[],"isolation_overrides":{"/p":false}}"#)
        XCTAssertEqual(decoded.isolationOverrides, ["/p": false])
    }

    func testIsolationStateIsTheFirstCardThatSaysOne() throws {
        let none = try card(#"{"id":"a"}"#)
        let off = try card(#"{"id":"b","isolation":"off"}"#)
        let on = try card(#"{"id":"c","isolation":"on"}"#)
        XCTAssertEqual(BoardProjectControls.isolationState(cards: []), "")
        XCTAssertEqual(BoardProjectControls.isolationState(cards: [none]), "")
        XCTAssertEqual(BoardProjectControls.isolationState(cards: [none, off, on]),
                       "off")
        XCTAssertEqual(BoardProjectControls.isolationState(cards: [on, off]), "on")
    }
}
