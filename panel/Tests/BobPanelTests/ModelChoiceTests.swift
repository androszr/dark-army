import XCTest
@testable import BobPanel

/// The per-card model choice, panel side (30 Aug 2026,
/// `plans/2026-08-30-per-card-model-choice.md`).
///
/// Two things are pinned here and they are the two ways this can go wrong on
/// screen. **Ragged decode**: an older daemon sends neither the catalogue nor
/// the card's field, and Swift's synthesized `Decodable` throws on a missing
/// key even where the property has a default — one absent field must never
/// blank the board. And **absent, never empty**: every chooser is gated on a
/// non-empty catalogue, so an older daemon draws no model menu rather than a
/// menu whose only row is "Default".
final class ModelChoiceTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func board(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    // MARK: - Decoding an older daemon

    func testACardWithNoModelKeyDecodesToDefault() throws {
        let decoded = try card(#"{"id": "c1", "title": "t", "tool": "claude"}"#)
        XCTAssertEqual(decoded.model, "")
        XCTAssertEqual(decoded.tool, "claude")
    }

    func testACardCarriesTheModelWhenTheDaemonSendsOne() throws {
        let decoded = try card(
            #"{"id": "c1", "title": "t", "tool": "grok", "model": "grok-4.5"}"#)
        XCTAssertEqual(decoded.model, "grok-4.5")
    }

    func testABoardWithNoModelsKeyDecodesToAnEmptyCatalogue() throws {
        let decoded = try board(#"{"available": true, "tools": ["claude"]}"#)
        XCTAssertEqual(decoded.models, [:])
        // And the rest of the board is untouched — the whole point of the
        // tolerant helper.
        XCTAssertTrue(decoded.available)
        XCTAssertEqual(decoded.tools, ["claude"])
    }

    func testTheCatalogueDecodesWhenTheDaemonSendsIt() throws {
        let decoded = try board(
            #"{"available": true, "models": {"claude": ["opus", "sonnet"]}}"#)
        XCTAssertEqual(decoded.models["claude"], ["opus", "sonnet"])
    }

    // MARK: - Narrowing

    func testGPT6CatalogueKeepsRawOptionsAndOnlyAstraHasASpecialLabel() throws {
        let decoded = try board(
            #"{"models":{"codex":["gpt-6-astra","gpt-6-sol","gpt-6-luna","gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna","gpt-5.5"]}}"#)
        let options = Board.modelOptions(models: decoded.models, tool: "codex")
        XCTAssertEqual(options, ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                                 "gpt-5.6-sol", "gpt-5.6-terra",
                                 "gpt-5.6-luna", "gpt-5.5"])
        XCTAssertEqual(options.map(Board.modelLabel),
                       ["Astra 6", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"])
        for model in ["", "opus", "grok-4.6", "future-model", "GPT-6-Astra"] {
            XCTAssertEqual(Board.modelLabel(model), model)
        }
        for json in [#"{}"#, #"{"models":{}}"#, #"{"models":{"codex":[]}}"#] {
            let older = try board(json)
            XCTAssertTrue(Board.modelOptions(models: older.models, tool: "codex").isEmpty)
        }
    }

    func testAstraCardOpensInDraftWithItsRawSelection() throws {
        let decoded = try card(
            #"{"id":"astra","title":"try it","tool":"codex","model":"gpt-6-astra"}"#)
        let draft = BoardDraft(decoded)
        XCTAssertEqual(draft.tool, "codex")
        XCTAssertEqual(draft.model, "gpt-6-astra")
        XCTAssertEqual(Board.modelLabel(draft.model), "Astra 6")
    }

    @MainActor
    func testSolDraftBanksAndResumesWithItsRawSelection() throws {
        let originalRoot = CardDrafts.root
        let originalAttachments = CardAttachments.dir
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("sol-draft-\(UUID().uuidString)")
        CardDrafts.root = root
        CardAttachments.dir = root.appendingPathComponent("attachments")
        defer {
            CardDrafts.root = originalRoot
            CardAttachments.dir = originalAttachments
            try? FileManager.default.removeItem(at: root)
        }
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: ["codex"])
        state.draft.title = "Try Sol"
        state.draft.tool = "codex"
        state.draft.model = "gpt-6-sol"
        state.bankComposerDraft()
        CardDrafts.root = root // Force a fresh read of the banked file.
        let banked = try XCTUnwrap(CardDrafts.all().first)
        XCTAssertEqual(banked.model, "gpt-6-sol")
        state.resumeDraft(banked)
        XCTAssertEqual(state.draft.model, "gpt-6-sol")
        XCTAssertEqual(state.draft.tool, "codex")
        state.closeEditor()
    }

    func testModelOptionsNarrowsToTheNamedAssistant() {
        let models = ["claude": ["opus", "sonnet"], "grok": ["grok-4.6"]]
        XCTAssertEqual(Board.modelOptions(models: models, tool: "claude"),
                       ["opus", "sonnet"])
        XCTAssertEqual(Board.modelOptions(models: models, tool: "grok"),
                       ["grok-4.6"])
    }

    func testModelOptionsIsEmptyWithNoAssistantChosen() {
        let models = ["claude": ["opus"]]
        XCTAssertTrue(Board.modelOptions(models: models, tool: "").isEmpty)
    }

    func testModelOptionsIsEmptyForAnAssistantWithNoCatalogue() {
        let models = ["claude": ["opus"]]
        XCTAssertTrue(Board.modelOptions(models: models, tool: "codex").isEmpty)
    }

    func testModelOptionsIsEmptyForAnOlderDaemon() {
        XCTAssertTrue(Board.modelOptions(models: [:], tool: "claude").isEmpty)
    }

    // MARK: - The chip's label

    func testTheChipReadsToolAloneOnADefaultCard() {
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "grok", model: ""),
                       "grok")
    }

    func testTheChipReadsToolAndModelOnceOneIsChosen() {
        XCTAssertEqual(
            BoardCardView.toolChipLabel(tool: "grok", model: "grok-4.5"),
            "grok · grok-4.5")
    }

    func testTheChipDropsTheModelsRedundantToolPrefix() {
        // `claude · claude-opus-5[1m]` is the same word twice on a 260pt card.
        XCTAssertEqual(
            BoardCardView.toolChipLabel(tool: "claude",
                                        model: "claude-opus-5[1m]"),
            "claude · opus-5[1m]")
    }

    func testTheChipKeepsAPrefixWhoseRemainderIsOnlyAVersion() {
        // `grok · 4.5` names nothing. Repeating the word beats losing it.
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "grok",
                                                   model: "grok-4.5"),
                       "grok · grok-4.5")
    }

    func testTheChipLeavesAnAliasAlone() {
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "claude",
                                                   model: "opus"),
                       "claude · opus")
    }

    func testAnUnassignedCardSaysSoRatherThanNamingAModel() {
        XCTAssertEqual(BoardCardView.toolChipLabel(tool: "", model: "opus"),
                       "assign…")
    }

    // MARK: - The draft

    func testADraftOpenedOnACardCopiesItsModel() throws {
        let decoded = try card(
            #"{"id": "c1", "title": "t", "tool": "grok", "model": "grok-4.5"}"#)
        let draft = BoardDraft(decoded)
        XCTAssertEqual(draft.model, "grok-4.5")
        XCTAssertEqual(draft.tool, "grok")
    }

    func testAFreshDraftIsDefault() {
        XCTAssertEqual(BoardDraft().model, "")
    }
}
