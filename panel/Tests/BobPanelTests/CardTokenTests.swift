import XCTest
@testable import BobPanel

/// The `#card <hex>` search phrase: the one query that names a card rather
/// than looking for words.
///
/// The temp `root` swap happens in `setUp` *before* the first `BoardState()`
/// — `init` reads `PanelPlacement.boardProjects()`, and none of that may
/// touch the real `~/.dark-army/panel-position.json` (`BoardFoldTests`'
/// pattern).
final class CardTokenTests: XCTestCase {
    private var originalRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalRoot = PanelPlacement.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-cardtoken-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot,
                                                withIntermediateDirectories: true)
        PanelPlacement.root = tempRoot
    }

    override func tearDownWithError() throws {
        PanelPlacement.root = originalRoot
        try? FileManager.default.removeItem(at: tempRoot)
    }

    private func card(id: String,
                      title: String = "",
                      summary: String = "",
                      prompt: String = "",
                      column: String = "backlog") -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = title
        c.summary = summary
        c.prompt = prompt
        c.column = column
        c.project = "dark-army"
        return c
    }

    private func board(_ cards: [BoardCard]) -> Board {
        var b = Board()
        b.available = true
        b.cards = cards
        return b
    }

    // MARK: - The parser

    func testParsesAValidPhrase() {
        XCTAssertEqual(CardToken.parse("#card ab12cd34"), "ab12cd34")
    }

    func testMarkerIsCaseInsensitiveAndTheTokenIsLowercased() {
        XCTAssertEqual(CardToken.parse("#CARD AB12"), "ab12")
        XCTAssertEqual(CardToken.parse("  #Card  Ff00  "), "ff00")
    }

    func testBareMarkerIsOrdinaryText() {
        XCTAssertNil(CardToken.parse("#card"))
        XCTAssertNil(CardToken.parse("#card   "))
    }

    func testExtraWordsAreOrdinaryText() {
        XCTAssertNil(CardToken.parse("#card ab12 foo"))
        XCTAssertNil(CardToken.parse("find #card ab12"))
    }

    func testNonHexIsOrdinaryText() {
        XCTAssertNil(CardToken.parse("#card zzzz"))
        XCTAssertNil(CardToken.parse("#card ab-12"))
    }

    func testOrdinaryTextIsOrdinaryText() {
        XCTAssertNil(CardToken.parse(""))
        XCTAssertNil(CardToken.parse("narrow the board"))
        XCTAssertNil(CardToken.parse("card ab12"))
    }

    // MARK: - Matching

    func testMatchesByPrefixAndRejectsASibling() {
        XCTAssertTrue(CardToken.matches(token: "ab12cd34", cardId: "ab12cd34ef567890"))
        XCTAssertTrue(CardToken.matches(token: "AB12CD34".lowercased(),
                                        cardId: "AB12CD34EF567890"))
        XCTAssertFalse(CardToken.matches(token: "ab12cd34", cardId: "ab12cd350000"))
    }

    func testQueryRoundTripsAndNamesItsOwnCardAlone() {
        let mine = card(id: "0123456789abcdef0123456789abcdef")
        let sibling = card(id: "0123456700000000000000000000000f")
        let phrase = CardToken.query(for: mine)
        XCTAssertEqual(phrase, "#card 01234567")
        let token = try? XCTUnwrap(CardToken.parse(phrase))
        XCTAssertEqual(token, "01234567")
        // Eight characters is the convention, and these two share them: the
        // guard against that is the id space, not the parser. A nine-char
        // divergence must separate them.
        let far = card(id: "fedcba9876543210fedcba9876543210")
        XCTAssertTrue(CardToken.matches(token: token ?? "", cardId: mine.id))
        XCTAssertTrue(CardToken.matches(token: token ?? "", cardId: sibling.id))
        XCTAssertFalse(CardToken.matches(token: token ?? "", cardId: far.id))
    }

    // MARK: - The search predicate

    @MainActor
    func testStateMatchesAdmitsTheTargetAndRejectsALiteralTextDecoy() {
        let target = card(id: "aaaa1111bbbb2222", title: "the real one")
        // A card whose *words* carry the phrase must not match: the token
        // branch skips the text scan entirely.
        let decoy = card(id: "cccc3333dddd4444",
                         title: "#card aaaa1111",
                         summary: "aaaa1111",
                         prompt: "see #card aaaa1111")
        let state = BoardState()
        state.query = "#card aaaa1111"
        XCTAssertTrue(state.matches(target))
        XCTAssertFalse(state.matches(decoy))
    }

    @MainActor
    func testOrdinaryTextSearchIsUnchanged() {
        let state = BoardState()
        let a = card(id: "aaaa", title: "Narrow the board")
        let b = card(id: "bbbb", title: "Something else", summary: "narrow")
        let c = card(id: "cccc", title: "Unrelated")
        state.query = "narrow"
        XCTAssertTrue(state.matches(a))
        XCTAssertTrue(state.matches(b))
        XCTAssertFalse(state.matches(c))
        state.query = ""
        XCTAssertTrue(state.matches(c))
    }

    // MARK: - The narrowing never lifts itself

    @MainActor
    func testQuerySurvivesAColumnMove() {
        let state = BoardState()
        let pinned = card(id: "aaaa1111bbbb2222", title: "pinned")
        state.query = CardToken.query(for: pinned)
        var moved = pinned
        moved.column = "in_progress"
        state.reconcile(with: board([moved]))
        XCTAssertEqual(state.query, "#card aaaa1111")
        XCTAssertTrue(state.matches(moved))
    }

    @MainActor
    func testQuerySurvivesTheCardVanishing() {
        let state = BoardState()
        let pinned = card(id: "aaaa1111bbbb2222", title: "pinned")
        state.query = CardToken.query(for: pinned)
        state.reconcile(with: board([card(id: "ffff9999eeee8888", title: "other")]))
        XCTAssertEqual(state.query, "#card aaaa1111")
    }

    // MARK: - The chip

    func testChipLabelNamesTheCardOrSaysItIsGone() {
        XCTAssertEqual(CardToken.chipLabel(resolvedTitle: "Narrow the board"),
                       "⌗ Narrow the board")
        XCTAssertEqual(CardToken.chipLabel(resolvedTitle: nil),
                       "⌗ card not on the board")
        XCTAssertEqual(CardToken.chipLabel(resolvedTitle: "   "),
                       "⌗ card not on the board")
    }
}
