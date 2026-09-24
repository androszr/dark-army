import XCTest
@testable import BobPanel

/// The switcher's pure rule (`ProviderChoice`): which tiles are drawn, which
/// one is lit, when the row hands back to an inert word chip, what the group
/// tells a screen reader, and where the keyboard cursor may go. The phone's
/// copy of the rule is the same bytes (`host/tests/test_provider_switcher.py`),
/// so these cases hold for both surfaces.
final class ProviderSwitchTests: XCTestCase {

    private let three = ["claude", "codex", "grok"]

    // MARK: - tiles

    func testEveryOfferedAssistantGetsATileAndOnlyTheChosenOneIsLit() {
        let tiles = ProviderChoice.tiles(tools: three, selected: "grok", showsNobody: false)
        XCTAssertEqual(tiles, [
            ProviderChoice.Tile(provider: "claude", hasMark: true, selected: false, installed: true),
            ProviderChoice.Tile(provider: "codex", hasMark: true, selected: false, installed: true),
            ProviderChoice.Tile(provider: "grok", hasMark: true, selected: true, installed: true),
        ])
    }

    func testAnUnassignedCardLightsNothing() {
        let tiles = ProviderChoice.tiles(tools: three, selected: "", showsNobody: false)
        XCTAssertEqual(tiles.count, 3)
        XCTAssertTrue(tiles.allSatisfy { !$0.selected })
    }

    func testAnAssistantWithNoLogoIsANameTileNotAChip() {
        let tiles = ProviderChoice.tiles(tools: ["claude", "cursor"], selected: "claude",
                                         showsNobody: false)
        XCTAssertEqual(tiles, [
            ProviderChoice.Tile(provider: "claude", hasMark: true, selected: true, installed: true),
            ProviderChoice.Tile(provider: "cursor", hasMark: false, selected: false, installed: true),
        ])
        XCTAssertFalse(ProviderChoice.wordChipOnly(tools: ["claude", "cursor"],
                                                   selected: "claude"))
    }

    func testShowsNobodyAppendsTheEmptyTileSelectedOnlyWhenNobodyIsChosen() {
        let unchosen = ProviderChoice.tiles(tools: three, selected: "", showsNobody: true)
        XCTAssertEqual(unchosen.last,
                       ProviderChoice.Tile(provider: "", hasMark: false, selected: true, installed: true))
        XCTAssertEqual(unchosen.count, 4)
        let chosen = ProviderChoice.tiles(tools: three, selected: "codex", showsNobody: true)
        XCTAssertEqual(chosen.last,
                       ProviderChoice.Tile(provider: "", hasMark: false, selected: false, installed: true))
        XCTAssertEqual(chosen.filter(\.selected).map(\.provider), ["codex"])
    }

    // MARK: - the word chip

    func testNothingOfferedIsAWordChip() {
        XCTAssertTrue(ProviderChoice.wordChipOnly(tools: [], selected: "claude"))
        XCTAssertTrue(ProviderChoice.wordChipOnly(tools: [], selected: ""))
    }

    func testASelectionTheRowCannotShowIsAWordChip() {
        XCTAssertTrue(ProviderChoice.wordChipOnly(tools: ["claude", "grok"],
                                                  selected: "codex"))
    }

    func testTheOrdinaryThreeWithNobodyChosenDrawTheRow() {
        XCTAssertFalse(ProviderChoice.wordChipOnly(tools: three, selected: ""))
    }

    // MARK: - what the group says

    func testTheGroupValueIsTheChosenNameOrNotChosen() {
        let name: (String) -> String = { $0.uppercased() }
        XCTAssertEqual(ProviderChoice.value(selected: "grok", name: name), "GROK")
        XCTAssertEqual(ProviderChoice.value(selected: "", name: name), "not chosen")
        XCTAssertEqual(ProviderChoice.unchosen, "not chosen")
    }

    func testTheGroupIsCalledAssistant() {
        XCTAssertEqual(ProviderChoice.groupLabel, "Assistant")
        XCTAssertEqual(ProviderChoice.nobody, "Nobody yet")
    }

    // MARK: - the keyboard cursor

    func testTheCursorStepsByOneAndClampsAtBothEnds() {
        XCTAssertEqual(ProviderChoice.moved(cursor: 0, count: 3, step: 1), 1)
        XCTAssertEqual(ProviderChoice.moved(cursor: 1, count: 3, step: 1), 2)
        XCTAssertEqual(ProviderChoice.moved(cursor: 2, count: 3, step: 1), 2)
        XCTAssertEqual(ProviderChoice.moved(cursor: 2, count: 3, step: -1), 1)
        XCTAssertEqual(ProviderChoice.moved(cursor: 0, count: 3, step: -1), 0)
    }

    func testTheCursorOnAnEmptyRowStaysAtZero() {
        XCTAssertEqual(ProviderChoice.moved(cursor: 0, count: 0, step: 1), 0)
        XCTAssertEqual(ProviderChoice.moved(cursor: 4, count: 0, step: -1), 0)
    }

    func testTheCursorStartsOnTheChosenTileElseTheFirst() {
        let chosen = ProviderChoice.tiles(tools: three, selected: "codex", showsNobody: false)
        XCTAssertEqual(ProviderChoice.startCursor(tiles: chosen), 1)
        let none = ProviderChoice.tiles(tools: three, selected: "", showsNobody: false)
        XCTAssertEqual(ProviderChoice.startCursor(tiles: none), 0)
        let nobody = ProviderChoice.tiles(tools: three, selected: "", showsNobody: true)
        XCTAssertEqual(ProviderChoice.startCursor(tiles: nobody), 3)
        XCTAssertEqual(ProviderChoice.startCursor(tiles: []), 0)
    }

    // MARK: - what the view keeps

    func testTheRecordMarkIsDrawnOnlyForAKnownAssistant() {
        XCTAssertEqual(ProviderSwitch.recordMark(selected: "codex"), "codex")
        XCTAssertNil(ProviderSwitch.recordMark(selected: "cursor"))
        XCTAssertNil(ProviderSwitch.recordMark(selected: ""))
    }

    func testTheKnownMarksAreTheThreeWeHaveArtworkFor() {
        XCTAssertEqual(ProviderChoice.knownMarks, ["claude", "codex", "grok"])
        XCTAssertEqual(ProviderSwitch.knownMarks, ProviderChoice.knownMarks)
    }

    // MARK: - who holds the keyboard

    func testAClaimAlwaysLandsAndOnlyTheHolderReleases() {
        XCTAssertEqual(BoardState.switcherFocus(after: nil, card: "a", focused: true), "a")
        // Focus moving straight from one face to another: whichever order the
        // two `onChange`s land in, card b ends up holding the slot.
        XCTAssertEqual(BoardState.switcherFocus(after: "a", card: "b", focused: true), "b")
        XCTAssertEqual(BoardState.switcherFocus(after: "b", card: "a", focused: false), "b")
        // Another card's tile disappearing under a snapshot releases nothing.
        XCTAssertEqual(BoardState.switcherFocus(after: "a", card: "b", focused: false), "a")
        XCTAssertNil(BoardState.switcherFocus(after: "a", card: "a", focused: false))
        XCTAssertNil(BoardState.switcherFocus(after: nil, card: "a", focused: false))
    }

    @MainActor
    func testTheStateAppliesTheSameRule() {
        let state = BoardState()
        XCTAssertNil(state.switcherFocused)
        state.noteSwitcherFocus(card: "a", focused: true)
        XCTAssertEqual(state.switcherFocused, "a")
        state.noteSwitcherFocus(card: "b", focused: false)
        XCTAssertEqual(state.switcherFocused, "a")
        state.noteSwitcherFocus(card: "a", focused: false)
        XCTAssertNil(state.switcherFocused)
    }

    func testTheLayoutConstantsAreThePinnedOnes() {
        XCTAssertEqual(ProviderSwitch.markSize, 10)
        XCTAssertEqual(ProviderSwitch.target, 16)
        XCTAssertEqual(ProviderSwitch.spacing, 3)
        XCTAssertEqual(ProviderSwitch.underline, 1)
    }
}
