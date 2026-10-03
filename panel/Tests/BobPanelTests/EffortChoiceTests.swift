import XCTest
@testable import BobPanel

/// The per-card effort choice, panel side (3 Oct 2026,
/// `plans/2026-10-03-card-and-role-effort-level.md`), `ModelChoiceTests`'
/// shape. **Ragged decode**: an older daemon sends neither the catalogue nor
/// the card's field, and one absent key must never blank the board. And
/// **absent, never empty**: the chooser is gated on a non-empty list, so an
/// older daemon draws no effort menu.
final class EffortChoiceTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func board(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    private let catalogue =
        #"{"efforts":{"codex":{"":["low","medium","high","xhigh","max"],"gpt-5.5":["low","medium","high","xhigh"]},"grok":{"":["minimal","low"]}}}"#

    // MARK: - Decoding an older daemon

    func testACardWithNoEffortKeyDecodesToDefault() throws {
        let decoded = try card(#"{"id": "c1", "title": "t", "tool": "claude"}"#)
        XCTAssertEqual(decoded.effort, "")
        XCTAssertEqual(decoded.tool, "claude")
    }

    func testACardCarriesTheEffortWhenTheDaemonSendsOne() throws {
        let decoded = try card(
            #"{"id": "c1", "title": "t", "tool": "claude", "effort": "high"}"#)
        XCTAssertEqual(decoded.effort, "high")
    }

    func testABoardWithNoEffortsKeyDecodesToAnEmptyCatalogue() throws {
        let decoded = try board(#"{"available": true, "tools": ["claude"]}"#)
        XCTAssertEqual(decoded.efforts, [:])
        XCTAssertTrue(decoded.available)
        XCTAssertEqual(decoded.tools, ["claude"])
    }

    func testTheCatalogueDecodesWhenTheDaemonSendsIt() throws {
        let decoded = try board(catalogue)
        XCTAssertEqual(decoded.efforts["codex"]?["gpt-5.5"],
                       ["low", "medium", "high", "xhigh"])
    }

    // MARK: - Narrowing

    func testOptionsNarrowByToolThenModelAndFallBackToTheToolsDefaultEntry() throws {
        let decoded = try board(catalogue)
        XCTAssertEqual(
            Board.effortOptions(efforts: decoded.efforts, tool: "codex", model: "gpt-5.5"),
            ["low", "medium", "high", "xhigh"])
        XCTAssertEqual(
            Board.effortOptions(efforts: decoded.efforts, tool: "codex", model: ""),
            ["low", "medium", "high", "xhigh", "max"])
        // A model the catalogue does not list takes the tool's `""` entry.
        XCTAssertEqual(
            Board.effortOptions(efforts: decoded.efforts, tool: "codex", model: "gpt-6-sol"),
            ["low", "medium", "high", "xhigh", "max"])
        XCTAssertEqual(
            Board.effortOptions(efforts: decoded.efforts, tool: "grok", model: "grok-4.5"),
            ["minimal", "low"])
    }

    func testOptionsAreEmptyForNoToolAnUnknownToolAndAnOlderDaemon() throws {
        let decoded = try board(catalogue)
        XCTAssertTrue(Board.effortOptions(efforts: decoded.efforts, tool: "", model: "").isEmpty)
        XCTAssertTrue(Board.effortOptions(efforts: decoded.efforts, tool: "gemini", model: "").isEmpty)
        XCTAssertTrue(Board.effortOptions(efforts: [:], tool: "claude", model: "opus").isEmpty)
    }

    // MARK: - The chip's label

    func testTheChipAppendsTheEffortAfterTheModel() {
        XCTAssertEqual(
            BoardCardView.toolChipLabel(tool: "claude", model: "opus", effort: "high"),
            "claude · opus · high")
        XCTAssertEqual(
            BoardCardView.toolChipLabel(tool: "grok", model: "", effort: "low"),
            "grok · low")
    }

    func testADefaultEffortLeavesTheChipExactlyAsItWas() {
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "claude", model: "opus", effort: ""),
                       "claude · opus")
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "claude", model: "opus"),
                       "claude · opus")
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "", model: "", effort: "high"),
                       "assign…")
    }

    // MARK: - The draft

    func testADraftOpenedOnACardCopiesItsEffort() throws {
        let decoded = try card(
            #"{"id":"c","title":"t","tool":"codex","model":"gpt-6-sol","effort":"max"}"#)
        let draft = BoardDraft(decoded)
        XCTAssertEqual(draft.effort, "max")
        XCTAssertEqual(BoardDraft().effort, "")
    }

    func testABankedDraftKeepsItsEffortAndAnOlderRowReadsDefault() {
        let banked = CardDraft(id: "d", tool: "claude", model: "opus", effort: "low")
        let again = CardDraft(id: "d", any: banked.body)
        XCTAssertEqual(again?.effort, "low")
        var older = banked.body
        older.removeValue(forKey: "effort")
        XCTAssertEqual(CardDraft(id: "d", any: older)?.effort, "")
    }

    // MARK: - The run-health line

    func testTheRunHealthLineEndsWithTheObservedEffortWhenKnown() throws {
        let health = try JSONDecoder().decode(
            RunHealth.self, from: Data(#"{"class":"typical","attempts":1,"effort":"high"}"#.utf8))
        XCTAssertTrue(RunHealthLine.text(health).hasSuffix(" · effort high"))
        XCTAssertTrue(RunHealthLine.spoken(health).contains("effort high"))
        let none = try JSONDecoder().decode(
            RunHealth.self, from: Data(#"{"class":"typical","attempts":1}"#.utf8))
        XCTAssertEqual(none.effort, "")
        XCTAssertFalse(RunHealthLine.text(none).contains("effort"))
    }
}
