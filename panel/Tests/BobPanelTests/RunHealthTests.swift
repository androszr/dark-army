import XCTest
@testable import BobPanel

/// The run-health line: the exact strings `RunHealthLine` spells from the
/// daemon's `run_health` object, and the tolerant decode of that object off
/// a card. Nothing here re-derives a figure — every number is the daemon's.
final class RunHealthTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    /// The plan's fixture: a large run, two attempts, sent back once, two
    /// fix rounds.
    private var fixture: RunHealth {
        var h = RunHealth()
        h.sizeClass = "large"
        h.basis = "project"
        h.turns = 84
        h.tokensK = 2100
        h.ctxPct = 65
        h.asks = 3
        h.refusals = 1
        h.attempts = 2
        h.returns = 1
        h.fixRounds = 2
        return h
    }

    // MARK: - text

    func testTheLineReadsTheSizeWordFirstThenEveryCount() {
        XCTAssertEqual(RunHealthLine.text(fixture),
                       "LARGE · 84t · 2.1M · ctx 65% · asks 3 · refusals 1 · attempt 2 · back 1 · fixes 2")
    }

    func testNoReturnsDropsTheBackPart() {
        var h = fixture
        h.returns = 0
        XCTAssertEqual(RunHealthLine.text(h),
                       "LARGE · 84t · 2.1M · ctx 65% · asks 3 · refusals 1 · attempt 2 · fixes 2")
    }

    func testAnAbsentFixCountDrawsADashNotAZero() {
        // A Codex run: nobody counted, so `fixes 0` would be a lie.
        var h = fixture
        h.fixRounds = nil
        XCTAssertTrue(RunHealthLine.text(h).hasSuffix(" · fixes –"))
        XCTAssertFalse(RunHealthLine.text(h).contains("fixes 0"))
    }

    func testAnUnsizedRunSaysSo() {
        var h = fixture
        h.sizeClass = ""
        XCTAssertTrue(RunHealthLine.text(h).hasPrefix("UNSIZED · "))
        XCTAssertEqual(RunHealthLine.sizeWord("worrying"), "WORRYING")
        XCTAssertEqual(RunHealthLine.sizeWord("typical"), "TYPICAL")
    }

    func testUnknownFiguresAreLeftOutRatherThanDrawnAsZero() {
        var h = fixture
        h.turns = nil
        h.tokensK = nil
        h.ctxPct = nil
        XCTAssertEqual(RunHealthLine.text(h),
                       "LARGE · asks 3 · refusals 1 · attempt 2 · back 1 · fixes 2")
    }

    func testTokensReadInThousandsUnderAMillionAndMillionsFromThere() {
        XCTAssertEqual(RunHealthLine.tokens(840), "840k")
        XCTAssertEqual(RunHealthLine.tokens(999), "999k")
        XCTAssertEqual(RunHealthLine.tokens(1000), "1.0M")
        XCTAssertEqual(RunHealthLine.tokens(2100), "2.1M")
        XCTAssertEqual(RunHealthLine.tokens(0), "0k")
    }

    // MARK: - spoken

    func testTheSpokenFormNamesTheBasisAndEveryFact() {
        XCTAssertEqual(RunHealthLine.spoken(fixture),
                       "Large run, judged against this project, 84 turns, 2.1 million tokens, "
                       + "context 65 percent full, 3 asks, 1 refusal, attempt 2, sent back once, "
                       + "2 fix rounds.")
    }

    func testTheSpokenFormAgainstDefaultsAndWithNothingCounted() {
        var h = fixture
        h.basis = "default"
        h.fixRounds = nil
        h.returns = 3
        let spoken = RunHealthLine.spoken(h)
        XCTAssertTrue(spoken.hasPrefix("Large run, judged against defaults, "))
        XCTAssertTrue(spoken.contains("sent back 3 times"))
        XCTAssertTrue(spoken.hasSuffix("fix rounds not counted."))
        var unsized = h
        unsized.sizeClass = ""
        XCTAssertTrue(RunHealthLine.spoken(unsized).hasPrefix("Unsized run, "))
    }

    // MARK: - decode

    func testAPresentObjectDecodesOffTheCard() throws {
        let c = try card("""
            {"id":"c1","run_health":{"class":"worrying","basis":"default",
             "turns":130,"tokens_k":5600,"ctx_pct":90,"asks":0,"refusals":2,
             "attempts":1,"returns":0,"fix_rounds":3,"attention":true,
             "live":true}}
            """)
        let h = try XCTUnwrap(c.runHealth)
        XCTAssertEqual(h.sizeClass, "worrying")
        XCTAssertEqual(h.basis, "default")
        XCTAssertEqual(h.turns, 130)
        XCTAssertEqual(h.tokensK, 5600)
        XCTAssertEqual(h.ctxPct, 90)
        XCTAssertEqual(h.refusals, 2)
        XCTAssertEqual(h.fixRounds, 3)
        XCTAssertTrue(h.attention)
        XCTAssertTrue(h.live)
        XCTAssertEqual(RunHealthLine.text(h),
                       "WORRYING · 130t · 5.6M · ctx 90% · asks 0 · refusals 2 · attempt 1 · fixes 3")
    }

    func testAnAbsentObjectDecodesToNil() throws {
        // A card nobody has started must draw no line, so the optional is
        // the fact, never an empty value.
        let c = try card(#"{"id":"c1","title":"t"}"#)
        XCTAssertNil(c.runHealth)
    }

    func testAPartialObjectDecodesWithoutThrowing() throws {
        // Swift's synthesized Decodable throws on a missing key even where
        // the property has a default; one absent field must never blank
        // the board. The optionals stay nil rather than zero.
        let c = try card(#"{"id":"c1","run_health":{"class":"typical","asks":1}}"#)
        let h = try XCTUnwrap(c.runHealth)
        XCTAssertEqual(h.sizeClass, "typical")
        XCTAssertEqual(h.asks, 1)
        XCTAssertNil(h.turns)
        XCTAssertNil(h.tokensK)
        XCTAssertNil(h.ctxPct)
        XCTAssertNil(h.fixRounds)
        XCTAssertFalse(h.attention)
        XCTAssertFalse(h.live)
    }

    func testAnUndecodableObjectThrowsOnItsOwnAndNeverBlanksTheCard() throws {
        // The struct itself refuses a value that is not an object at all …
        XCTAssertThrowsError(
            try JSONDecoder().decode(RunHealth.self, from: Data("5".utf8)))
        // … and on the card that refusal lands as "no reading" through
        // `maybe`, so the rest of the card still draws.
        let c = try card(#"{"id":"c1","title":"t","run_health":5}"#)
        XCTAssertEqual(c.title, "t")
        XCTAssertNil(c.runHealth)
    }
}
