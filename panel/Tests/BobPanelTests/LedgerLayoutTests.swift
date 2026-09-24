import XCTest
@testable import BobPanel

/// The seven-column History picture's layout rules, with no window. Every
/// figure and word going in is `LedgerFold`'s; these only check where it
/// sits, in what order and how tall.
final class LedgerLayoutTests: XCTestCase {
    private func column(claude: Double = 0, grok: Double = 0, estimated: Double = 0,
                        magnitude: Double? = nil) -> LedgerDayColumn {
        var column = LedgerDayColumn()
        column.day = "2026-09-15"
        column.claudeMeasured = claude
        column.grokMeasured = grok
        column.estimated = estimated
        column.measured = claude + grok
        column.magnitude = magnitude ?? (claude + grok + estimated)
        return column
    }

    func testSegmentsStackClaudeOverGrokOverEstimate() {
        let segments = LedgerWeekLayout.segments(
            column(claude: 4, grok: 2, estimated: 1), lens: .spent, scale: 8)
        XCTAssertEqual(segments.map(\.ink), [.claude, .grok, .estimated])
        let full = LedgerWeekLayout.segmentMax
        XCTAssertEqual(segments[0].height, full / 2, accuracy: 0.0001)
        XCTAssertEqual(segments[1].height, full / 4, accuracy: 0.0001)
        XCTAssertEqual(segments[2].height, full / 8, accuracy: 0.0001)
    }

    func testAZeroValueIsOmittedAndASmallOneIsAtLeastFourPoints() {
        let segments = LedgerWeekLayout.segments(
            column(claude: 10, grok: 0, estimated: 0.01), lens: .spent, scale: 10)
        XCTAssertEqual(segments.map(\.ink), [.claude, .estimated])
        XCTAssertEqual(segments[0].height, LedgerWeekLayout.segmentMax, accuracy: 0.0001)
        XCTAssertEqual(segments[1].height, LedgerWeekLayout.segmentMin)
        XCTAssertEqual(LedgerWeekLayout.segmentMin, 4)
    }

    func testTokensLensIsOneSegment() {
        let tokens = LedgerWeekLayout.segments(
            column(claude: 4, grok: 2, estimated: 1, magnitude: 500), lens: .tokens, scale: 1000)
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

    func testFigureLineJoinsFigureTildeAndDashOnOneLine() {
        var day = LedgerDayColumn()
        day.figure = "$6.20"
        day.estimatedMark = "~$0.60"
        day.unknown = true
        XCTAssertEqual(LedgerWeekLayout.figureLine(day), [
            .init(text: "$6.20", ink: .claude),
            .init(text: "~$0.60", ink: .estimated),
            .init(text: LedgerFold.dash, ink: .unknown),
        ])

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
