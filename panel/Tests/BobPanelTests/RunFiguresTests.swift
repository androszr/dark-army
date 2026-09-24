import XCTest
@testable import BobPanel

/// The card's cost-and-time line: the one wording rule and the tolerant
/// decode of the object the daemon stamps as `run_figures`.
final class RunFiguresTests: XCTestCase {

    private func figures(cost: Double? = 1.4, coverage: String = "complete",
                         seconds: Int? = 23 * 60, ticking: Bool = true,
                         ctx: Int? = 61, attempts: Int = 1) -> RunFigures.Figures {
        RunFigures.Figures(costUsd: cost, costCoverage: coverage,
                           activeSeconds: seconds, activeTicking: ticking,
                           ctxPercent: ctx, attempts: attempts)
    }

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    // MARK: - Wording

    /// nil in, nil out: a card with no figures draws no line, not a dash.
    func testNoFiguresIsNoLine() {
        XCTAssertNil(RunFigures.line(nil))
    }

    func testCompleteLine() {
        XCTAssertEqual(RunFigures.line(figures()), "$1.40 · 23 min · 61% ctx")
    }

    func testPartialCostSaysSo() {
        XCTAssertEqual(RunFigures.line(figures(coverage: "partial", ctx: nil)),
                       "$1.40 (partial) · 23 min")
    }

    /// A cost Dark Army could not measure is said in words — never a made-up `$0`.
    func testUnknownCostIsWords() {
        XCTAssertEqual(RunFigures.line(figures(cost: nil, coverage: "unknown", ctx: nil)),
                       "cost unknown · 23 min")
        // A figure without a coverage behind it is still unknown.
        XCTAssertEqual(RunFigures.line(figures(cost: 0.5, coverage: "unknown", ctx: nil)),
                       "cost unknown · 23 min")
    }

    func testTwoAttemptsTrail() {
        XCTAssertEqual(RunFigures.line(figures(ctx: nil, attempts: 2)),
                       "$1.40 · 23 min · 2 attempts")
        XCTAssertEqual(RunFigures.line(figures(ctx: nil, attempts: 1)),
                       "$1.40 · 23 min")
    }

    func testUnderAMinute() {
        XCTAssertEqual(RunFigures.line(figures(seconds: 45, ctx: nil)),
                       "$1.40 · under a minute")
    }

    func testHoursAndMinutes() {
        XCTAssertEqual(RunFigures.line(figures(seconds: 3900, ctx: nil)),
                       "$1.40 · 1 h 5 min")
    }

    /// Refined but never started: a run row, no execution span, so the
    /// line is the planner's spend alone — never "under a minute".
    func testNoExecutionSpanDrawsNoTimePart() {
        XCTAssertEqual(RunFigures.line(figures(cost: 0.31, seconds: nil, ctx: nil)), "$0.31")
        XCTAssertEqual(RunFigures.line(figures(cost: nil, coverage: "unknown", seconds: nil, ctx: nil)),
                       "cost unknown")
        XCTAssertEqual(RunFigures.line(figures(seconds: 0, ctx: nil)),
                       "$1.40 · under a minute")
    }

    /// `AgentFacts.money`'s rule (`AgentDetailPane.swift`), restated so the phone can share it.
    func testMoneyMatchesTheDetailPane() {
        for usd in [0.0, 0.42, 9.99, 10.0, 123.45] {
            XCTAssertEqual(RunFigures.money(usd), AgentFacts.money(usd))
        }
        XCTAssertEqual(RunFigures.line(figures(cost: 12.34, ctx: nil)),
                       "$12.3 · 23 min")
    }

    // MARK: - Decode

    /// A card JSON without `run_figures` decodes to **nil**: a card nobody
    /// has run, or an older daemon, draws no line.
    func testAbsentFiguresDecodeNil() throws {
        XCTAssertNil(try card("{}").runFigures)
        XCTAssertNil(try card(#"{"id":"c1","title":"x","work_record":{}}"#).runFigures)
        XCTAssertNil(RunFigures.line(try card("{}").runFigures))
    }

    /// `{}` and `{"cost_usd": null}` decode without throwing, optionals nil
    /// and counts zero — the ragged shapes the daemon actually sends.
    func testRaggedFiguresDecodeTolerantly() throws {
        let empty = try card(#"{"run_figures":{}}"#).runFigures
        XCTAssertNotNil(empty)
        XCTAssertNil(empty?.costUsd)
        XCTAssertNil(empty?.ctxPercent)
        XCTAssertEqual(empty?.attempts, 0)
        XCTAssertNil(empty?.activeSeconds)
        XCTAssertEqual(empty?.costCoverage, "unknown")
        XCTAssertFalse(empty?.activeTicking ?? true)
        XCTAssertEqual(RunFigures.line(empty), "cost unknown")

        let nulls = try card(#"{"run_figures":{"cost_usd":null,"ctx_percent":null,"cost_coverage":"unknown","active_seconds":120,"attempts":2}}"#).runFigures
        XCTAssertNil(nulls?.costUsd)
        XCTAssertNil(nulls?.ctxPercent)
        XCTAssertEqual(nulls?.activeSeconds, 120)
        XCTAssertEqual(RunFigures.line(nulls), "cost unknown · 2 min · 2 attempts")

        let noTime = try card(#"{"run_figures":{"cost_usd":0.31,"cost_coverage":"complete","active_seconds":null,"attempts":0}}"#).runFigures
        XCTAssertNil(noTime?.activeSeconds)
        XCTAssertEqual(RunFigures.line(noTime), "$0.31")
    }

    func testFullFiguresDecode() throws {
        let f = try card(#"{"run_figures":{"cost_usd":1.4,"cost_coverage":"complete","active_seconds":1380,"active_ticking":true,"ctx_percent":61,"attempts":1}}"#).runFigures
        XCTAssertEqual(f, figures())
        XCTAssertEqual(RunFigures.line(f), "$1.40 · 23 min · 61% ctx")
    }

    /// The panel decodes no version markers (`Board` carries none): the
    /// figures ride the card alone, and a `Board` without any marker still
    /// decodes its cards' figures.
    func testBoardWithoutMarkerStillCarriesFigures() throws {
        let board = try JSONDecoder().decode(Board.self, from: Data(#"{"cards":[{"id":"c1","run_figures":{"active_seconds":60}}]}"#.utf8))
        XCTAssertEqual(board.cards.first?.runFigures?.activeSeconds, 60)
        XCTAssertEqual(RunFigures.line(board.cards.first?.runFigures),
                       "cost unknown · 1 min")
    }
}
