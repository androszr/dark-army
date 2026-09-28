import XCTest
@testable import BobPanel

/// The phone's week is the Mac's week. `LedgerWeek` is the one fold both
/// clients carry byte for byte (`host/tests/test_ledger_week.py` pins the
/// copies); these tests prove the Mac's History *calls* it — the seven-day
/// spent columns `LedgerFold` draws equal what `LedgerWeek.picture` gives
/// the phone over the same cards — and that its dollar is `Format.usd`'s.
final class LedgerWeekTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_758_000_000)

    func testTheSharedDollarIsTheMacsDollar() {
        for value in [0, 0.004, 0.05, 1, 9.99, 10, 123.456, 4819.2, 0.009999, -1] {
            XCTAssertEqual(LedgerWeek.usd(value), Format.usd(value), "\(value)")
        }
    }

    func testTheMacsSevenDaysAreThePhonesSevenDays() {
        var partly = card("p", provider: "grok", token: 2, reported: 0.5, daysAgo: 1)
        partly.sessions[0].tokenUnpricedTurns = 2
        var split = card("s", provider: "codex", token: 3, daysAgo: 2)
        split.sessions[0].claudeTokenCostUsd = 2
        split.sessions[0].codexTokenCostUsd = 1
        let cards = [
            card("c", token: 4, reported: 6, daysAgo: 0),
            card("g", provider: "grok", token: 2, reported: 1.5, daysAgo: 3),
            card("x", provider: "codex", token: 1, daysAgo: 5),
            card("u", daysAgo: 4),
            card("old", token: 0.75, daysAgo: 9),
            partly, split,
        ]
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(LedgerFold.timestamp(daysAgo: 2, now: now))
        other.claudeTokenCostUsd = 0.3
        other.codexTokenCostUsd = 0.2
        other.claudeReportedCostUsd = 0
        other.grokTokenUnpricedSessions = 1
        other.grokTokenUnpricedSessionIds = ["lg"]
        var early = HistoryOtherDay()
        early.day = LedgerFold.localDay(LedgerFold.timestamp(daysAgo: 8, now: now))
        early.grokTokenCostUsd = 0.11
        early.grokReportedCostUsd = 0.2

        let mac = LedgerFold.picture(
            report: report(cards: cards, other: [other, early]), projects: [],
            range: "7d", lens: .spent, claude: true, grok: true, person: "",
            selectedDay: "", openCard: "", now: now)
        let phone = LedgerWeek.picture(
            cards: cards.map(weekCard), others: [other, early].map(weekOther), now: now)

        XCTAssertEqual(mac.days.count, 7)
        XCTAssertEqual(phone.days.count, 7)
        for (m, p) in zip(mac.days, phone.days) {
            XCTAssertEqual(m.day, p.day)
            XCTAssertEqual(m.figure, p.figure, m.day)
            XCTAssertEqual(m.reportedMark, p.reportedMark, m.day)
            XCTAssertEqual(m.unknown, p.unknown, m.day)
            XCTAssertEqual(m.claudeToken, p.claude, m.day)
            XCTAssertEqual(m.grokToken, p.grok, m.day)
            XCTAssertEqual(m.codexToken, p.codex, m.day)
            XCTAssertEqual(LedgerWeekLayout.segments(m, lens: .spent, scale: 10).map(\.ink),
                           LedgerWeek.segments(p).map { ink($0.provider) }, m.day)
        }
        XCTAssertEqual(mac.spent, phone.total)
        XCTAssertEqual(mac.reported.map { "\($0) reported" }, phone.reported)
        XCTAssertEqual(mac.usual, phone.usual)
        XCTAssertTrue(mac.masthead.hasPrefix(phone.span))
        XCTAssertTrue(mac.aside.contains("1 cost unknown"))
        XCTAssertEqual(phone.unknownCards, 1)
        // The priced Grok card with unpriced turns, and the leftover Grok
        // session, each once.
        XCTAssertEqual(phone.unpricedSessions, 2)
        XCTAssertTrue(mac.aside.contains("2 sessions with turns not priced"))
    }

    func testAWeekWithNoPriceIsNotPricedOnThePhoneAndUnknownOnTheMac() {
        let cards = [card("u", daysAgo: 1)]
        let phone = LedgerWeek.picture(cards: cards.map(weekCard), others: [], now: now)
        XCTAssertEqual(phone.total, LedgerWeek.notPriced)
        XCTAssertNotEqual(phone.total, "$0.00")
        let mac = LedgerFold.picture(
            report: report(cards: cards), projects: [], range: "7d", lens: .spent,
            claude: true, grok: true, person: "", selectedDay: "", openCard: "",
            now: now)
        XCTAssertEqual(mac.spent, "cost unknown")
        XCTAssertEqual(mac.days.map(\.figure), phone.days.map(\.figure))
    }

    // MARK: - Fixtures

    private func ink(_ provider: String) -> LedgerInk {
        switch provider {
        case "grok": return .grok
        case "codex": return .codex
        default: return .claude
        }
    }

    private func report(cards: [HistoryOutcomeCard],
                        other: [HistoryOtherDay] = []) -> HistoryReport {
        var report = HistoryReport()
        report.available = true
        report.range = "7d"
        report.otherDays = other
        report.effectiveness.available = true
        report.effectiveness.root = ""
        report.effectiveness.cards = cards
        return report
    }

    private func card(_ id: String, provider: String = "claude",
                      token: Double? = nil, reported: Double? = nil,
                      daysAgo: Int = 0) -> HistoryOutcomeCard {
        var card = HistoryOutcomeCard()
        card.cardId = id
        card.title = id
        var session = HistoryOutcomeSession()
        session.sessionId = "s-\(id)"
        session.provider = provider
        session.phase = "implementation"
        session.boundAt = LedgerFold.timestamp(daysAgo: daysAgo, now: now)
        session.known = token != nil
        session.tokenCostUsd = token
        session.reportedCostUsd = reported
        card.sessions = [session]
        return card
    }

    /// The phone's input, built field for field the way `HistoryWeek.swift`
    /// maps the sealed read.
    private func weekCard(_ card: HistoryOutcomeCard) -> LedgerWeek.Card {
        LedgerWeek.Card(id: card.cardId, sessions: card.sessions.map { session in
            LedgerWeek.Session(
                sessionId: session.sessionId, provider: session.provider,
                phase: session.phase, boundAt: session.boundAt, known: session.known,
                tokenCostUsd: session.tokenCostUsd,
                claudeTokenCostUsd: session.claudeTokenCostUsd,
                grokTokenCostUsd: session.grokTokenCostUsd,
                codexTokenCostUsd: session.codexTokenCostUsd,
                tokenUnpricedTurns: session.tokenUnpricedTurns,
                reportedCostUsd: session.reportedCostUsd)
        })
    }

    private func weekOther(_ row: HistoryOtherDay) -> LedgerWeek.OtherDay {
        LedgerWeek.OtherDay(
            day: row.day, claude: row.claudeTokenCostUsd, grok: row.grokTokenCostUsd,
            codex: row.codexTokenCostUsd,
            claudeReported: row.claudeReportedCostUsd,
            grokReported: row.grokReportedCostUsd,
            claudeUnpriced: row.claudeTokenUnpricedSessions,
            grokUnpriced: row.grokTokenUnpricedSessions,
            codexUnpriced: row.codexTokenUnpricedSessions,
            claudeUnpricedIds: row.claudeTokenUnpricedSessionIds,
            grokUnpricedIds: row.grokTokenUnpricedSessionIds,
            codexUnpricedIds: row.codexTokenUnpricedSessionIds)
    }
}
