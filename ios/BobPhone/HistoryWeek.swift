import Foundation

/// The sealed `history_week` read: the Mac's last seven days, machine-wide,
/// already cut by the Mac to the fields the week fold reads — card ids and
/// their sessions' money, and the leftover days — and nothing a person
/// reads (no titles, names or project folders).
///
/// Every key decodes tolerantly. Money decodes through `maybe`: an absent
/// token cost is nil, never 0 (a 0 would price a day at $0.00), and a
/// reported `0` stays 0 — a real report of nothing.
struct HistoryWeekReport: Decodable {
    var available = false
    var reason = ""
    var rangeDays = 7
    /// The week is a floor: the Mac's join was capped.
    var partial = false
    /// Codex's journals are still being read, so its share is a floor.
    var codexHistoryPartial = false
    /// The Mac cut cards to fit one answer; the total is lower than the week.
    var truncated = false
    var cards: [HistoryWeekCard] = []
    var otherDays: [HistoryWeekOtherDay] = []
    /// Claude's limit pressure over the week. An older Mac sends none, which
    /// decodes empty and draws nothing.
    var limits = HistoryWeekLimits()

    enum CodingKeys: String, CodingKey {
        case available, reason, partial, truncated, cards
        case limits
        case rangeDays = "range_days"
        case codexHistoryPartial = "codex_history_partial"
        case otherDays = "other_days"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        reason = c.value(.reason, "")
        rangeDays = c.value(.rangeDays, 7)
        partial = c.value(.partial, false)
        codexHistoryPartial = c.value(.codexHistoryPartial, false)
        truncated = c.value(.truncated, false)
        cards = c.value(.cards, [])
        otherDays = c.value(.otherDays, [])
        limits = c.value(.limits, HistoryWeekLimits())
    }

    /// The week as `LedgerWeek` folds it — the same input the Mac's History
    /// builds from its own report.
    func asLedgerInput() -> (cards: [LedgerWeek.Card], others: [LedgerWeek.OtherDay]) {
        (cards.map { $0.ledger }, otherDays.map { $0.ledger })
    }
}

struct HistoryWeekCard: Decodable {
    var cardId = ""
    var sessions: [HistoryWeekSession] = []

    enum CodingKeys: String, CodingKey {
        case sessions
        case cardId = "card_id"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cardId = c.value(.cardId, "")
        sessions = c.value(.sessions, [])
    }

    var ledger: LedgerWeek.Card {
        LedgerWeek.Card(id: cardId, sessions: sessions.map { $0.ledger })
    }
}

struct HistoryWeekSession: Decodable {
    var sessionId = ""
    var provider = ""
    var phase = ""
    var boundAt: Double?
    var known = false
    var tokenCostUsd: Double?
    var claudeTokenCostUsd: Double?
    var grokTokenCostUsd: Double?
    var codexTokenCostUsd: Double?
    var tokenUnpricedTurns: Int?
    var reportedCostUsd: Double?

    enum CodingKeys: String, CodingKey {
        case provider, phase, known
        case sessionId = "session_id"
        case boundAt = "bound_at"
        case tokenCostUsd = "token_cost_usd"
        case claudeTokenCostUsd = "claude_token_cost_usd"
        case grokTokenCostUsd = "grok_token_cost_usd"
        case codexTokenCostUsd = "codex_token_cost_usd"
        case tokenUnpricedTurns = "token_unpriced_turns"
        case reportedCostUsd = "reported_cost_usd"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.value(.sessionId, "")
        provider = c.value(.provider, "")
        phase = c.value(.phase, "")
        boundAt = c.maybe(.boundAt)
        known = c.value(.known, false)
        tokenCostUsd = c.maybe(.tokenCostUsd)
        claudeTokenCostUsd = c.maybe(.claudeTokenCostUsd)
        grokTokenCostUsd = c.maybe(.grokTokenCostUsd)
        codexTokenCostUsd = c.maybe(.codexTokenCostUsd)
        tokenUnpricedTurns = c.maybe(.tokenUnpricedTurns)
        reportedCostUsd = c.maybe(.reportedCostUsd)
    }

    var ledger: LedgerWeek.Session {
        LedgerWeek.Session(
            sessionId: sessionId, provider: provider, phase: phase,
            boundAt: boundAt, known: known, tokenCostUsd: tokenCostUsd,
            claudeTokenCostUsd: claudeTokenCostUsd,
            grokTokenCostUsd: grokTokenCostUsd,
            codexTokenCostUsd: codexTokenCostUsd,
            tokenUnpricedTurns: tokenUnpricedTurns,
            reportedCostUsd: reportedCostUsd)
    }
}

/// One local day of turns on no named card. Token costs are "nothing spent"
/// at 0; the unpriced counts say what could not be priced; a reported dollar
/// is nil where none was reported.
struct HistoryWeekOtherDay: Decodable {
    var day = ""
    var claudeTokenCostUsd: Double = 0
    var grokTokenCostUsd: Double = 0
    var codexTokenCostUsd: Double = 0
    var claudeReportedCostUsd: Double?
    var grokReportedCostUsd: Double?
    var claudeTokenUnpricedSessions = 0
    var grokTokenUnpricedSessions = 0
    var codexTokenUnpricedSessions = 0
    var claudeTokenUnpricedSessionIds: [String] = []
    var grokTokenUnpricedSessionIds: [String] = []
    var codexTokenUnpricedSessionIds: [String] = []

    enum CodingKeys: String, CodingKey {
        case day
        case claudeTokenCostUsd = "claude_token_cost_usd"
        case grokTokenCostUsd = "grok_token_cost_usd"
        case codexTokenCostUsd = "codex_token_cost_usd"
        case claudeReportedCostUsd = "claude_reported_cost_usd"
        case grokReportedCostUsd = "grok_reported_cost_usd"
        case claudeTokenUnpricedSessions = "claude_token_unpriced_sessions"
        case grokTokenUnpricedSessions = "grok_token_unpriced_sessions"
        case codexTokenUnpricedSessions = "codex_token_unpriced_sessions"
        case claudeTokenUnpricedSessionIds = "claude_token_unpriced_session_ids"
        case grokTokenUnpricedSessionIds = "grok_token_unpriced_session_ids"
        case codexTokenUnpricedSessionIds = "codex_token_unpriced_session_ids"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.value(.day, "")
        claudeTokenCostUsd = c.value(.claudeTokenCostUsd, 0)
        grokTokenCostUsd = c.value(.grokTokenCostUsd, 0)
        codexTokenCostUsd = c.value(.codexTokenCostUsd, 0)
        claudeReportedCostUsd = c.maybe(.claudeReportedCostUsd)
        grokReportedCostUsd = c.maybe(.grokReportedCostUsd)
        claudeTokenUnpricedSessions = c.value(.claudeTokenUnpricedSessions, 0)
        grokTokenUnpricedSessions = c.value(.grokTokenUnpricedSessions, 0)
        codexTokenUnpricedSessions = c.value(.codexTokenUnpricedSessions, 0)
        claudeTokenUnpricedSessionIds = c.value(.claudeTokenUnpricedSessionIds, [])
        grokTokenUnpricedSessionIds = c.value(.grokTokenUnpricedSessionIds, [])
        codexTokenUnpricedSessionIds = c.value(.codexTokenUnpricedSessionIds, [])
    }

    var ledger: LedgerWeek.OtherDay {
        LedgerWeek.OtherDay(
            day: day, claude: claudeTokenCostUsd, grok: grokTokenCostUsd,
            codex: codexTokenCostUsd,
            claudeReported: claudeReportedCostUsd,
            grokReported: grokReportedCostUsd,
            claudeUnpriced: claudeTokenUnpricedSessions,
            grokUnpriced: grokTokenUnpricedSessions,
            codexUnpriced: codexTokenUnpricedSessions,
            claudeUnpricedIds: claudeTokenUnpricedSessionIds,
            grokUnpricedIds: grokTokenUnpricedSessionIds,
            codexUnpricedIds: codexTokenUnpricedSessionIds)
    }
}

/// Claude's five-hour and seven-day readings over the week, as the Mac's
/// `limits_report(7)` series, cut to the fields the chart reads. An absent
/// reading is nil, never 0: an unmeasured stretch is left empty.
struct HistoryWeekLimits: Decodable {
    var series: [HistoryWeekLimitPoint] = []
    var resets: [Double] = []
    var from: Double?
    var to: Double?

    enum CodingKeys: String, CodingKey {
        case series, resets, from, to
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        series = c.value(.series, [])
        resets = c.value(.resets, [])
        from = c.maybe(.from)
        to = c.maybe(.to)
    }

    /// The shared fold's picture (`LimitPressure`, byte-pinned with the Mac);
    /// nil where nothing was measured, so nothing is drawn.
    func pressure() -> LimitPressure.Picture? {
        LimitPressure.picture(
            points: series.map {
                LimitPressure.Point(ts: $0.ts, fiveHour: $0.fiveHourPct,
                                    sevenDay: $0.sevenDayPct)
            },
            resets: resets, from: from, to: to)
    }
}

struct HistoryWeekLimitPoint: Decodable {
    var ts: Double = 0
    var fiveHourPct: Double?
    var sevenDayPct: Double?

    enum CodingKeys: String, CodingKey {
        case ts
        case fiveHourPct = "five_hour_pct"
        case sevenDayPct = "seven_day_pct"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ts = c.value(.ts, 0)
        fiveHourPct = c.maybe(.fiveHourPct)
        sevenDayPct = c.maybe(.sevenDayPct)
    }
}

/// What the History screen says aloud for one column, composed from what
/// it draws: `Wed 24: $12.40, Claude $8.00, Grok $3.40, Codex $1.00, $5.00
/// reported`, and `not priced` where the figure is the dash.
enum HistoryWeekWords {
    static func spoken(_ column: LedgerWeek.Column) -> String {
        var parts: [String] = []
        if column.figure == LedgerWeek.dash {
            parts.append(LedgerWeek.notPriced)
        } else if column.figure.isEmpty {
            parts.append("nothing spent")
        } else {
            parts.append(column.figure)
        }
        for segment in LedgerWeek.segments(column) {
            parts.append("\(name(segment.provider)) \(LedgerWeek.usd(segment.value))")
        }
        if let mark = column.reportedMark { parts.append(mark) }
        if column.unknown, column.figure != LedgerWeek.dash {
            parts.append("some \(LedgerWeek.notPriced)")
        }
        return "\(column.weekday) \(column.dayNumber): " + parts.joined(separator: ", ")
    }

    static func name(_ provider: String) -> String {
        switch provider {
        case "grok": return "Grok"
        case "codex": return "Codex"
        default: return "Claude"
        }
    }
}
