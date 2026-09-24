import XCTest
@testable import BobPhone

/// The composer's 7d / 5h picker, over synthetic bars decoded through
/// the app's own `UsageBar` decoder so `group` and the `provider` default
/// are the real ones. The host-suite pin is
/// `host/tests/test_phone_composer_usage.py`.
final class ComposerUsageRuleTests: XCTestCase {
    private func bars(_ json: String) throws -> [UsageBar] {
        try JSONDecoder().decode([UsageBar].self, from: Data(json.utf8))
    }

    func testClaudePicksWeeklyAllAndSessionNeverWeeklyScoped() throws {
        let decoded = try bars("""
        [
          {"kind":"weekly_scoped","provider":"claude","title":"Current week (Fable)","percent":61,"stale":false},
          {"kind":"session","group":"session","provider":"claude","percent":18,"stale":false},
          {"kind":"weekly_all","group":"weekly","provider":"claude","percent":42,"stale":false}
        ]
        """)
        let rows = ComposerUsageRule.rows(tools: ["claude"], bars: decoded)
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].figures.map(\.window), ["7d", "5h"])
        XCTAssertEqual(rows[0].figures[0].percent, 42.0)
        XCTAssertEqual(rows[0].figures[1].percent, 18.0)
        XCTAssertFalse(rows[0].figures[0].stale)
        XCTAssertFalse(rows[0].figures[1].stale)
    }

    func testCodexPicksWeeklyGroupWhicheverSlot() throws {
        let decoded = try bars("""
        [
          {"kind":"codex_primary","group":"weekly","provider":"codex","label":"7d","percent":9,"stale":false},
          {"kind":"codex_secondary","group":"session","provider":"codex","label":"5h","percent":73,"stale":false}
        ]
        """)
        let rows = ComposerUsageRule.rows(tools: ["codex"], bars: decoded)
        XCTAssertEqual(rows[0].figures.count, 1)
        XCTAssertEqual(rows[0].figures[0].window, "7d")
        XCTAssertEqual(rows[0].figures[0].percent, 9.0)
        XCTAssertNil(ComposerUsageRule.fiveHour(for: "codex", in: decoded))
    }

    func testCodexFallsBackToLabelEndingInDWhenGroupIsAbsent() throws {
        let decoded = try bars("""
        [
          {"kind":"codex_primary","provider":"codex","label":"5h","percent":73,"stale":false},
          {"kind":"codex_secondary","provider":"codex","label":"7d","percent":11,"stale":false}
        ]
        """)
        XCTAssertEqual(decoded[1].group, "")
        let figure = ComposerUsageRule.sevenDay(for: "codex", in: decoded)
        XCTAssertEqual(figure?.percent, 11.0)
        XCTAssertEqual(ComposerUsageRule.rows(tools: ["codex"], bars: decoded)[0].figures[0].percent, 11.0)
    }

    func testGrokPicksGrokWeeklyAndGetsNoFiveHour() throws {
        let decoded = try bars("""
        [{"kind":"grok_weekly","group":"weekly","provider":"grok","label":"7d","percent":12,"stale":false}]
        """)
        let rows = ComposerUsageRule.rows(tools: ["grok"], bars: decoded)
        XCTAssertEqual(rows[0].figures.count, 1)
        XCTAssertEqual(rows[0].figures[0].window, "7d")
        XCTAssertEqual(rows[0].figures[0].percent, 12.0)
        XCTAssertNil(ComposerUsageRule.fiveHour(for: "grok", in: decoded))
    }

    func testAStaleBarIsNilPercentAndStale() throws {
        let decoded = try bars("""
        [{"kind":"grok_weekly","group":"weekly","provider":"grok","percent":55,"stale":true}]
        """)
        let figure = ComposerUsageRule.figure(
            window: "7d", bar: ComposerUsageRule.sevenDay(for: "grok", in: decoded))
        XCTAssertNil(figure.percent)
        XCTAssertTrue(figure.stale)
    }

    func testABarWithNoPercentIsNil() throws {
        let decoded = try bars("""
        [{"kind":"weekly_all","group":"weekly","provider":"claude","stale":false}]
        """)
        let figure = ComposerUsageRule.figure(
            window: "7d", bar: ComposerUsageRule.sevenDay(for: "claude", in: decoded))
        XCTAssertNil(figure.percent)
        XCTAssertFalse(figure.stale)
    }

    func testEmptyBarsGiveARowPerToolWithNilFigures() {
        let rows = ComposerUsageRule.rows(tools: ["claude", "codex", "grok"], bars: [])
        XCTAssertEqual(rows.map(\.provider), ["claude", "codex", "grok"])
        XCTAssertEqual(rows[0].figures.map(\.window), ["7d", "5h"])
        XCTAssertEqual(rows[1].figures.map(\.window), ["7d"])
        XCTAssertEqual(rows[2].figures.map(\.window), ["7d"])
        XCTAssertTrue(rows.allSatisfy { $0.figures.allSatisfy { $0.percent == nil && !$0.stale } })
    }

    func testRowsFollowTheToolsOrder() throws {
        let decoded = try bars("""
        [
          {"kind":"grok_weekly","group":"weekly","provider":"grok","percent":1,"stale":false},
          {"kind":"weekly_all","group":"weekly","provider":"claude","percent":2,"stale":false},
          {"kind":"codex_primary","group":"weekly","provider":"codex","label":"7d","percent":3,"stale":false}
        ]
        """)
        let rows = ComposerUsageRule.rows(tools: ["codex", "grok", "claude"], bars: decoded)
        XCTAssertEqual(rows.map(\.provider), ["codex", "grok", "claude"])
        XCTAssertEqual(rows.map { $0.figures[0].percent }, [3.0, 1.0, 2.0])
    }

    func testAFourthToolWithNoBarGetsOneNilSevenDay() {
        let rows = ComposerUsageRule.rows(tools: ["other"], bars: [])
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].provider, "other")
        XCTAssertEqual(rows[0].figures.count, 1)
        XCTAssertEqual(rows[0].figures[0].window, "7d")
        XCTAssertNil(rows[0].figures[0].percent)
        XCTAssertNil(ComposerUsageRule.fiveHour(for: "other", in: []))
    }

    func testSpokenForFullClaudeGrokAndStale() throws {
        let decoded = try bars("""
        [
          {"kind":"session","group":"session","provider":"claude","percent":18,"stale":false},
          {"kind":"weekly_all","group":"weekly","provider":"claude","percent":42,"stale":false},
          {"kind":"grok_weekly","group":"weekly","provider":"grok","percent":55,"stale":true}
        ]
        """)
        let claude = ComposerUsageRule.rows(tools: ["claude"], bars: decoded)[0]
        XCTAssertEqual(ComposerUsageRule.spoken(row: claude, name: "Claude"),
                       "Claude, 7 day 42 percent, 5 hour 18 percent")
        let grok = ComposerUsageRule.rows(tools: ["grok"], bars: decoded)[0]
        XCTAssertEqual(ComposerUsageRule.spoken(row: grok, name: "Grok"),
                       "Grok, 7 day no reading")
        let stale = ComposerUsageRule.Row(
            provider: "grok",
            figures: [ComposerUsageRule.Figure(window: "7d", percent: nil, stale: true)])
        XCTAssertEqual(ComposerUsageRule.spoken(row: stale, name: "Grok"),
                       "Grok, 7 day no reading")
    }
}
