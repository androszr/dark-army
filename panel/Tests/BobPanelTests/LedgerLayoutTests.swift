import XCTest
@testable import BobPanel

/// The seven-column History picture's layout rules, with no window. Every
/// figure and word going in is `LedgerFold`'s; these only check where it
/// sits, in what order and how tall.
final class LedgerLayoutTests: XCTestCase {
    private func column(claude: Double = 0, grok: Double = 0, codex: Double = 0,
                        magnitude: Double? = nil) -> LedgerDayColumn {
        var column = LedgerDayColumn()
        column.day = "2026-09-15"
        column.claudeToken = claude
        column.grokToken = grok
        column.codexToken = codex
        column.token = claude + grok + codex
        column.anyToken = column.token > 0
        column.magnitude = magnitude ?? column.token
        return column
    }

    func testSegmentsStackClaudeOverGrokOverCodexWithNoEstimate() {
        var day = column(claude: 4, grok: 2, codex: 1)
        day.reported = 50
        day.reportedMark = "$50.00 reported"
        let segments = LedgerWeekLayout.segments(day, lens: .spent, scale: 8)
        XCTAssertEqual(segments.map(\.ink), [.claude, .grok, .codex])
        let full = LedgerWeekLayout.segmentMax
        XCTAssertEqual(segments[0].height, full / 2, accuracy: 0.0001)
        XCTAssertEqual(segments[1].height, full / 4, accuracy: 0.0001)
        XCTAssertEqual(segments[2].height, full / 8, accuracy: 0.0001)
        // A reported dollar is never a segment of the stack.
        XCTAssertFalse(segments.contains { $0.ink == .reported })
    }

    func testAZeroValueIsOmittedAndASmallOneIsAtLeastFourPoints() {
        let segments = LedgerWeekLayout.segments(
            column(claude: 10, grok: 0, codex: 0.01), lens: .spent, scale: 10)
        XCTAssertEqual(segments.map(\.ink), [.claude, .codex])
        XCTAssertEqual(segments[0].height, LedgerWeekLayout.segmentMax, accuracy: 0.0001)
        XCTAssertEqual(segments[1].height, LedgerWeekLayout.segmentMin)
        XCTAssertEqual(LedgerWeekLayout.segmentMin, 4)
    }

    func testTokensLensIsOneSegment() {
        let tokens = LedgerWeekLayout.segments(
            column(claude: 4, grok: 2, codex: 1, magnitude: 500), lens: .tokens, scale: 1000)
        XCTAssertEqual(tokens, [LedgerWeekLayout.Segment(
            ink: .claude, height: LedgerWeekLayout.segmentMax / 2)])
        let time = LedgerWeekLayout.segments(
            column(magnitude: 60), lens: .time, scale: 60)
        XCTAssertEqual(time.map(\.ink), [.claude])
    }

    func testNoBodyIsNoSegments() {
        XCTAssertEqual(LedgerWeekLayout.segments(column(), lens: .spent, scale: 5), [])
        XCTAssertEqual(LedgerWeekLayout.segments(column(), lens: .tokens, scale: 5), [])
        XCTAssertEqual(LedgerWeekLayout.segments(
            column(claude: 3), lens: .spent, scale: 0), [])
    }

    func testUsualLiftIsSpentLensOnlyAndClamped() {
        XCTAssertNil(LedgerWeekLayout.usualLift(usual: 2, lens: .tokens, scale: 4))
        XCTAssertNil(LedgerWeekLayout.usualLift(usual: 2, lens: .time, scale: 4))
        XCTAssertNil(LedgerWeekLayout.usualLift(usual: nil, lens: .spent, scale: 4))
        XCTAssertNil(LedgerWeekLayout.usualLift(usual: 2, lens: .spent, scale: 0))
        XCTAssertEqual(LedgerWeekLayout.usualLift(usual: 9, lens: .spent, scale: 4),
                       LedgerWeekLayout.segmentMax)
        XCTAssertEqual(LedgerWeekLayout.usualLift(usual: 4, lens: .spent, scale: 4),
                       LedgerWeekLayout.segmentMax)
        XCTAssertEqual(LedgerWeekLayout.usualLift(usual: 2, lens: .spent, scale: 4), 32)
        XCTAssertEqual(LedgerWeekLayout.usualLift(usual: -1, lens: .spent, scale: 4), 0)
    }

    func testFigureLineJoinsTokenCostReportedAndDashOnOneLine() {
        var day = LedgerDayColumn()
        day.figure = "$6.20"
        day.reportedMark = "$0.60 reported"
        day.unknown = true
        XCTAssertEqual(LedgerWeekLayout.figureLine(day), [
            .init(text: "$6.20", ink: .claude),
            .init(text: "$0.60 reported", ink: .reported),
            .init(text: LedgerFold.dash, ink: .unknown),
        ])
        XCTAssertFalse(LedgerWeekLayout.figureLine(day).contains { $0.text.hasPrefix("~") })

        var zero = LedgerDayColumn()
        zero.figure = "$3.00"
        zero.reportedMark = "$0.00 reported"
        XCTAssertEqual(LedgerWeekLayout.figureLine(zero).map(\.text),
                       ["$3.00", "$0.00 reported"])

        var empty = LedgerDayColumn()
        empty.figure = ""
        XCTAssertEqual(LedgerWeekLayout.figureLine(empty), [])

        var unpriced = LedgerDayColumn()
        unpriced.figure = LedgerFold.dash
        unpriced.unknown = true
        XCTAssertEqual(LedgerWeekLayout.figureLine(unpriced),
                       [.init(text: LedgerFold.dash, ink: .claude)])

        var plain = LedgerDayColumn()
        plain.figure = "$1.00"
        XCTAssertEqual(LedgerWeekLayout.figureLine(plain).map(\.text), ["$1.00"])
    }

    func testTileMetaJoinsFigureAndTurns() {
        var card = LedgerCardLine()
        card.figure = "$1.84"
        card.turns = 31
        XCTAssertEqual(LedgerWeekLayout.tileMeta(card), "$1.84 · 31 turns")
        card.figure = LedgerFold.dash
        card.turns = 0
        XCTAssertEqual(LedgerWeekLayout.tileMeta(card), "— · 0 turns")
    }

    func testAcceptanceSplitBoldsTheThreeCountsAndLeavesAReasonWhole() {
        let sentence = "4 accepted · 6 submitted · rework 15% · 1 manual check outstanding"
        let split = LedgerWeekLayout.acceptanceSplit(sentence)
        XCTAssertEqual(split.lead, "4 accepted · 6 submitted · rework 15%")
        XCTAssertEqual(split.rest, " · 1 manual check outstanding")
        XCTAssertEqual(split.lead + split.rest, sentence)

        let reason = "A total across the Mac is not computed."
        let whole = LedgerWeekLayout.acceptanceSplit(reason)
        XCTAssertEqual(whole.lead, "")
        XCTAssertEqual(whole.rest, reason)

        let none = LedgerWeekLayout.acceptanceSplit("No submitted work in this period.")
        XCTAssertEqual(none.lead, "")
        XCTAssertEqual(none.rest, "No submitted work in this period.")
    }
}
