import SwiftUI

/// `/api/history`, decoded.
///
/// **A judgment recorded here has been reversed, on purpose.** This file used
/// to decode four things — one figure row, one trend, one breakdown, one table
/// — and said so: the model mix, the context-pressure histogram, time blocked
/// on a human, the historical limit windows and the hourly profile were left
/// undecoded as "curiosities nobody acted on twice", and what survived was
/// called "the honest minimum". That reasoning was sound for the product as it
/// then was, and it is kept here rather than deleted, because a note that
/// vanishes teaches nobody.
///
/// It no longer holds. Watching how the agents perform is now a goal of Dark
/// Army rather than a curiosity about it: the question "is this kind of helper
/// worth what it costs" needs the mix of models, how close sessions ran to
/// their limits, how long work sat waiting on a person and the daily rhythm,
/// all beside each other. So the withheld sections are decoded, and two new
/// ones join them — `by_agent` / `top_dispatches` (what each kind of helper
/// cost) and `effectiveness` (whether the work it did was accepted). The old
/// judgment is superseded, not mistaken.
///
/// Same tolerant decoding as `Snapshot`: a missing key must never blank the
/// view. Swift's synthesized `Decodable` throws on an absent key even where
/// the property has a default, so every field below goes through `c.value`.
struct HistoryReport: Decodable {
    var available = false
    var range = ""
    var totals = HistoryTotals()
    var daily: [HistoryDay] = []
    var byProject: [HistoryProject] = []
    var sessions: [HistorySession] = []
    var byModel: [HistoryModel] = []
    var waiting = HistoryWaiting()
    var limits = HistoryLimits()
    var hourly: [HistoryHour] = []
    var context = HistoryContext()
    var byAgent: [HistoryAgent] = []
    var topDispatches: [HistoryDispatch] = []
    var effectiveness = HistoryEffectiveness()
    /// Leftover turns, beside `daily` and never mixed into it. Absent — the
    /// effectiveness half could not be computed — decodes as none, and the
    /// ledger then draws no other line.
    var otherDays: [HistoryOtherDay] = []

    enum CodingKeys: String, CodingKey {
        case available, range, totals, daily, sessions, waiting, limits
        case hourly, context, effectiveness
        case byProject = "by_project"
        case byModel = "by_model"
        case byAgent = "by_agent"
        case topDispatches = "top_dispatches"
        case otherDays = "other_days"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        range = c.value(.range, "")
        totals = c.value(.totals, HistoryTotals())
        daily = c.value(.daily, [])
        byProject = c.value(.byProject, [])
        sessions = c.value(.sessions, [])
        byModel = c.value(.byModel, [])
        waiting = c.value(.waiting, HistoryWaiting())
        limits = c.value(.limits, HistoryLimits())
        hourly = c.value(.hourly, [])
        context = c.value(.context, HistoryContext())
        byAgent = c.value(.byAgent, [])
        topDispatches = c.value(.topDispatches, [])
        effectiveness = c.value(.effectiveness, HistoryEffectiveness())
        otherDays = c.value(.otherDays, [])
    }

    init() {}
}

struct HistoryTotals: Decodable {
    var turns = 0
    var sessions = 0
    var outputTokens = 0
    var costUsd: Double = 0
    /// How much of `costUsd` was measured rather than estimated. The split is
    /// kept because a total that silently mixes the two overstates its own
    /// precision — `unpricedSessions` is what the footnote is drawn from.
    var measuredCostUsd: Double = 0
    var measuredSessions = 0
    var unpricedSessions = 0
    var claudeCostUsd: Double = 0
    var grokCostUsd: Double = 0
    var claudeSessions = 0
    var grokSessions = 0
    var claudeTurns = 0
    var grokTurns = 0
    var claudeOutputTokens = 0
    var grokOutputTokens = 0
    var claudeMeasuredSessions = 0
    var grokMeasuredSessions = 0
    var claudeUnpricedSessions = 0
    var grokUnpricedSessions = 0

    enum CodingKeys: String, CodingKey {
        case turns, sessions
        case outputTokens = "output_tokens"
        case costUsd = "cost_usd"
        case measuredCostUsd = "measured_cost_usd"
        case measuredSessions = "measured_sessions"
        case unpricedSessions = "unpriced_sessions"
        case claudeCostUsd = "claude_cost_usd"
        case grokCostUsd = "grok_cost_usd"
        case claudeSessions = "claude_sessions"
        case grokSessions = "grok_sessions"
        case claudeTurns = "claude_turns"
        case grokTurns = "grok_turns"
        case claudeOutputTokens = "claude_output_tokens"
        case grokOutputTokens = "grok_output_tokens"
        case claudeMeasuredSessions = "claude_measured_sessions"
        case grokMeasuredSessions = "grok_measured_sessions"
        case claudeUnpricedSessions = "claude_unpriced_sessions"
        case grokUnpricedSessions = "grok_unpriced_sessions"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        turns = c.value(.turns, 0)
        sessions = c.value(.sessions, 0)
        outputTokens = c.value(.outputTokens, 0)
        costUsd = c.value(.costUsd, 0)
        measuredCostUsd = c.value(.measuredCostUsd, 0)
        measuredSessions = c.value(.measuredSessions, 0)
        unpricedSessions = c.value(.unpricedSessions, 0)
        claudeCostUsd = c.value(.claudeCostUsd, 0)
        grokCostUsd = c.value(.grokCostUsd, 0)
        claudeSessions = c.value(.claudeSessions, 0)
        grokSessions = c.value(.grokSessions, 0)
        claudeTurns = c.value(.claudeTurns, 0)
        grokTurns = c.value(.grokTurns, 0)
        claudeOutputTokens = c.value(.claudeOutputTokens, 0)
        grokOutputTokens = c.value(.grokOutputTokens, 0)
        claudeMeasuredSessions = c.value(.claudeMeasuredSessions, 0)
        grokMeasuredSessions = c.value(.grokMeasuredSessions, 0)
        claudeUnpricedSessions = c.value(.claudeUnpricedSessions, 0)
        grokUnpricedSessions = c.value(.grokUnpricedSessions, 0)
    }

    init() {}

    /// Slice the combined totals down to the providers the user has on.
    /// Both on is the payload as served; one on is that provider's columns.
    func sliced(claude: Bool, grok: Bool) -> HistoryTotals {
        if claude && grok { return self }
        var t = HistoryTotals()
        if claude {
            t.costUsd = claudeCostUsd
            t.sessions = claudeSessions
            t.turns = claudeTurns
            t.outputTokens = claudeOutputTokens
            t.measuredSessions = claudeMeasuredSessions
            t.unpricedSessions = claudeUnpricedSessions
        } else if grok {
            t.costUsd = grokCostUsd
            t.sessions = grokSessions
            t.turns = grokTurns
            t.outputTokens = grokOutputTokens
            t.measuredSessions = grokMeasuredSessions
            t.unpricedSessions = grokUnpricedSessions
        }
        return t
    }
}

struct HistoryDay: Decodable, Identifiable {
    var id: String { day }
    var day = ""
    var costUsd: Double = 0
    var turns = 0
    var claudeCostUsd: Double = 0
    var grokCostUsd: Double = 0
    var claudeTurns = 0
    var grokTurns = 0
    /// Decoded for completeness. The ledger does not add this on top of
    /// the cards: `daily` mixes an estimate into `costUsd`.
    var outputTokens: Int?

    enum CodingKeys: String, CodingKey {
        case day, turns
        case costUsd = "cost_usd"
        case claudeCostUsd = "claude_cost_usd"
        case grokCostUsd = "grok_cost_usd"
        case claudeTurns = "claude_turns"
        case grokTurns = "grok_turns"
        case outputTokens = "output_tokens"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.value(.day, "")
        turns = c.value(.turns, 0)
        costUsd = c.value(.costUsd, 0)
        claudeCostUsd = c.value(.claudeCostUsd, 0)
        grokCostUsd = c.value(.grokCostUsd, 0)
        claudeTurns = c.value(.claudeTurns, 0)
        grokTurns = c.value(.grokTurns, 0)
        outputTokens = c.maybe(.outputTokens)
    }

    func cost(claude: Bool, grok: Bool) -> Double {
        if claude && grok { return costUsd }
        return (claude ? claudeCostUsd : 0) + (grok ? grokCostUsd : 0)
    }

    func turnCount(claude: Bool, grok: Bool) -> Int {
        if claude && grok { return turns }
        return (claude ? claudeTurns : 0) + (grok ? grokTurns : 0)
    }

    /// Just the day number, for the sparkline's foot.
    var shortLabel: String { String(day.suffix(2)) }
}

struct HistoryProject: Decodable, Identifiable {
    var id: String { project }
    var project = ""
    var sessions = 0
    var costUsd: Double = 0
    var claudeCostUsd: Double = 0
    var grokCostUsd: Double = 0
    var claudeSessions = 0
    var grokSessions = 0

    enum CodingKeys: String, CodingKey {
        case project, sessions
        case costUsd = "cost_usd"
        case claudeCostUsd = "claude_cost_usd"
        case grokCostUsd = "grok_cost_usd"
        case claudeSessions = "claude_sessions"
        case grokSessions = "grok_sessions"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        project = c.value(.project, "")
        sessions = c.value(.sessions, 0)
        costUsd = c.value(.costUsd, 0)
        claudeCostUsd = c.value(.claudeCostUsd, 0)
        grokCostUsd = c.value(.grokCostUsd, 0)
        claudeSessions = c.value(.claudeSessions, 0)
        grokSessions = c.value(.grokSessions, 0)
    }

    func cost(claude: Bool, grok: Bool) -> Double {
        if claude && grok { return costUsd }
        return (claude ? claudeCostUsd : 0) + (grok ? grokCostUsd : 0)
    }
}

struct HistorySession: Decodable, Identifiable {
    var id: String { sessionId }
    var sessionId = ""
    var project = ""
    var title = ""
    var primaryModel = ""
    var costUsd: Double = 0
    /// `"measured"` or an estimate. An estimated cost is drawn with a tilde
    /// rather than silently sitting in the same column as a real figure.
    var costSource = ""
    var turns = 0
    var lastSeen: Double = 0
    var endReason = ""
    var provider = ""

    enum CodingKeys: String, CodingKey {
        case project, title, turns, provider
        case sessionId = "session_id"
        case primaryModel = "primary_model"
        case costUsd = "cost_usd"
        case costSource = "cost_source"
        case lastSeen = "last_seen"
        case endReason = "end_reason"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.value(.sessionId, "")
        project = c.value(.project, "")
        title = c.value(.title, "")
        primaryModel = c.value(.primaryModel, "")
        costUsd = c.value(.costUsd, 0)
        costSource = c.value(.costSource, "")
        turns = c.value(.turns, 0)
        lastSeen = c.value(.lastSeen, 0)
        endReason = c.value(.endReason, "")
        provider = c.value(.provider, "")
    }

    var estimated: Bool { !costSource.isEmpty && costSource != "measured" }
}

/// One model's share of the window. `costUsd` is optional on purpose: the
/// daemon sends `null` where nothing in the group could be priced, and a `0`
/// in that cell would read as free.
struct HistoryModel: Decodable, Identifiable {
    var id: String { model }
    var model = ""
    var turns = 0
    var inputTokens = 0
    var outputTokens = 0
    var cacheRead = 0
    var cacheCreation = 0
    var costUsd: Double?
    var unpricedTurns = 0

    enum CodingKeys: String, CodingKey {
        case model, turns
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheRead = "cache_read"
        case cacheCreation = "cache_creation"
        case costUsd = "cost_usd"
        case unpricedTurns = "unpriced_turns"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        model = c.value(.model, "")
        turns = c.value(.turns, 0)
        inputTokens = c.value(.inputTokens, 0)
        outputTokens = c.value(.outputTokens, 0)
        cacheRead = c.value(.cacheRead, 0)
        cacheCreation = c.value(.cacheCreation, 0)
        costUsd = c.maybe(.costUsd)
        unpricedTurns = c.value(.unpricedTurns, 0)
    }

    init() {}
}

/// How long the agents sat blocked on a person.
struct HistoryWaiting: Decodable {
    var totalSeconds: Double = 0
    var medianSeconds: Double = 0
    var longestSeconds: Double = 0
    var spans = 0
    var openNow = 0
    var byDay: [HistoryWaitingDay] = []

    enum CodingKeys: String, CodingKey {
        case spans
        case totalSeconds = "total_seconds"
        case medianSeconds = "median_seconds"
        case longestSeconds = "longest_seconds"
        case openNow = "open_now"
        case byDay = "by_day"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        totalSeconds = c.value(.totalSeconds, 0)
        medianSeconds = c.value(.medianSeconds, 0)
        longestSeconds = c.value(.longestSeconds, 0)
        spans = c.value(.spans, 0)
        openNow = c.value(.openNow, 0)
        byDay = c.value(.byDay, [])
    }

    init() {}
}

struct HistoryWaitingDay: Decodable, Identifiable {
    var id: String { day }
    var day = ""
    var spans = 0
    var totalSeconds: Double = 0

    enum CodingKeys: String, CodingKey {
        case day, spans
        case totalSeconds = "total_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.value(.day, "")
        spans = c.value(.spans, 0)
        totalSeconds = c.value(.totalSeconds, 0)
    }
}

/// The rate-limit windows as they were, not as they are: the live bars are the
/// footer's job, and this is the history of how close the machine ran.
struct HistoryLimits: Decodable {
    var series: [HistoryLimitPoint] = []
    var burnPctPerHour: Double?

    enum CodingKeys: String, CodingKey {
        case series
        case burnPctPerHour = "burn_pct_per_hour"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        series = c.value(.series, [])
        burnPctPerHour = c.maybe(.burnPctPerHour)
    }

    init() {}
}

struct HistoryLimitPoint: Decodable, Identifiable {
    var id: Double { ts }
    var ts: Double = 0
    /// **`nil` is "nobody measured this", and it is a real state on the
    /// wire.** `limits_report`'s series admits a bucket where *either*
    /// window has a reading (`five_hour_pct IS NOT NULL OR
    /// seven_day_pct IS NOT NULL`), so `MAX(five_hour_pct)` comes back JSON
    /// `null` for a bucket the seven-day window alone reported. Defaulting
    /// that to `0` drew an unmeasured sample as a zero-height bar whose
    /// hover read "5h 0%" — a measurement nobody made, on the one tab where
    /// every other absent figure draws a dash.
    var fiveHourPct: Double?
    var sevenDayPct: Double?

    enum CodingKeys: String, CodingKey {
        case ts
        case fiveHourPct = "five_hour_pct"
        case sevenDayPct = "seven_day_pct"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ts = c.value(.ts, 0)
        fiveHourPct = c.maybe(.fiveHourPct)
        sevenDayPct = c.maybe(.sevenDayPct)
    }
}

/// One hour of the day, averaged over the range.
struct HistoryHour: Decodable, Identifiable {
    var id: Int { hour }
    var hour = 0
    var turns = 0
    var outputTokens = 0

    enum CodingKeys: String, CodingKey {
        case hour, turns
        case outputTokens = "output_tokens"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hour = c.value(.hour, 0)
        turns = c.value(.turns, 0)
        outputTokens = c.value(.outputTokens, 0)
    }
}

/// How hard the context window was pushed, per project. Two sources with two
/// denominators, which is why the daemon never adds them up and neither does
/// this.
struct HistoryContext: Decodable {
    var byProject: [HistoryContextProject] = []
    var compactions = 0
    var manualCompactions = 0
    var sessionsOver90 = 0
    var sessionsSampled = 0

    enum CodingKeys: String, CodingKey {
        case compactions
        case byProject = "by_project"
        case manualCompactions = "manual_compactions"
        case sessionsOver90 = "sessions_over_90"
        case sessionsSampled = "sessions_sampled"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        byProject = c.value(.byProject, [])
        compactions = c.value(.compactions, 0)
        manualCompactions = c.value(.manualCompactions, 0)
        sessionsOver90 = c.value(.sessionsOver90, 0)
        sessionsSampled = c.value(.sessionsSampled, 0)
    }

    init() {}
}

struct HistoryContextProject: Decodable, Identifiable {
    var id: String { project }
    var project = ""
    /// `nil` where no session of this project was ever sampled — a dash, not
    /// a 0%, which would read as "it never got close".
    var peakPct: Double?
    var over90 = 0
    var compactions = 0
    var manual = 0
    var sessions = 0

    enum CodingKeys: String, CodingKey {
        case project, compactions, sessions, manual
        case peakPct = "peak_pct"
        case over90 = "over_90"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        project = c.value(.project, "")
        peakPct = c.maybe(.peakPct)
        over90 = c.value(.over90, 0)
        compactions = c.value(.compactions, 0)
        manual = c.value(.manual, 0)
        sessions = c.value(.sessions, 0)
    }
}

/// One kind of helper: what it cost and how it ran. `costUsd` and
/// `cacheHitRatio` are optional because the daemon sends `null` for a figure
/// it cannot vouch for, and a `0` would read as a measurement.
struct HistoryAgent: Decodable, Identifiable {
    var id: String { name }
    var name = ""
    var role = ""
    var dispatches = 0
    var sessions = 0
    var turns = 0
    var inputTokens = 0
    var outputTokens = 0
    var cacheRead = 0
    var durationMs = 0
    var toolCalls = 0
    var costUsd: Double?
    var cacheHitRatio: Double?
    var unpricedTurns = 0
    /// The models this kind of helper ran on, busiest first. Membership is
    /// what the model filter asks about — "which helpers touched this model".
    var models: [String] = []

    enum CodingKeys: String, CodingKey {
        case name, role, dispatches, sessions, turns, models
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheRead = "cache_read"
        case durationMs = "duration_ms"
        case toolCalls = "tool_calls"
        case costUsd = "cost_usd"
        case cacheHitRatio = "cache_hit_ratio"
        case unpricedTurns = "unpriced_turns"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.value(.name, "")
        role = c.value(.role, "")
        dispatches = c.value(.dispatches, 0)
        sessions = c.value(.sessions, 0)
        turns = c.value(.turns, 0)
        inputTokens = c.value(.inputTokens, 0)
        outputTokens = c.value(.outputTokens, 0)
        cacheRead = c.value(.cacheRead, 0)
        durationMs = c.value(.durationMs, 0)
        toolCalls = c.value(.toolCalls, 0)
        costUsd = c.maybe(.costUsd)
        cacheHitRatio = c.maybe(.cacheHitRatio)
        unpricedTurns = c.value(.unpricedTurns, 0)
        models = c.value(.models, [])
    }

    init() {}
}

/// One individual dispatch — a single spawned helper.
struct HistoryDispatch: Decodable, Identifiable {
    var id: String { agentId }
    var agentId = ""
    var name = ""
    var sessionId = ""
    var turns = 0
    var durationMs = 0
    var toolCalls = 0
    var costUsd: Double?
    var model = ""

    enum CodingKeys: String, CodingKey {
        case name, turns, model
        case agentId = "agent_id"
        case sessionId = "session_id"
        case durationMs = "duration_ms"
        case toolCalls = "tool_calls"
        case costUsd = "cost_usd"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        agentId = c.value(.agentId, "")
        name = c.value(.name, "")
        sessionId = c.value(.sessionId, "")
        turns = c.value(.turns, 0)
        durationMs = c.value(.durationMs, 0)
        toolCalls = c.value(.toolCalls, 0)
        costUsd = c.maybe(.costUsd)
        model = c.value(.model, "")
    }
}

/// The effectiveness half: the board's own acceptance arithmetic, joined to
/// the sessions that did the work. Nothing here is re-derived on this side —
/// `board_outcomes.project_summary` is the one place that arithmetic is
/// written, and this decodes its answer.
struct HistoryEffectiveness: Decodable {
    var supported = false
    var available = false
    var reason = ""
    var outcomesAvailable = false
    var outcomesReason = ""
    var sessionsTruncated = false
    /// The project this half is about, `""` for every project.
    var root = ""
    /// `"project"` or `"machine"` — what the helper table beside it counts.
    /// Stated by the daemon rather than inferred here: with a project chosen
    /// the rollup is narrowed to that project's own sessions, and a table
    /// that silently changed scope would be read as the same figures twice.
    var agentScope = ""
    /// The helper table **as narrowed by `root`**, which is the only list
    /// that may be drawn under a project caption.
    ///
    /// `HistoryReport.byAgent` is always machine-wide: `/api/history`'s own
    /// `collect()` calls `by_agent(days)` with no session filter, whatever
    /// `?root=` said. The narrowed fold is computed beside the acceptance
    /// half and arrives here, so drawing `byAgent` under "this project" —
    /// which the desk did until this was decoded — states every helper on
    /// the Mac as one project's.
    var agents: [HistoryAgent] = []
    var cards: [HistoryOutcomeCard] = []
    var summary = HistoryEffectivenessSummary()

    enum CodingKeys: String, CodingKey {
        case supported, available, reason, cards, summary, root, agents
        case outcomesAvailable = "outcomes_available"
        case outcomesReason = "outcomes_reason"
        case sessionsTruncated = "sessions_truncated"
        case agentScope = "agent_scope"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        reason = c.value(.reason, "")
        outcomesAvailable = c.value(.outcomesAvailable, false)
        outcomesReason = c.value(.outcomesReason, "")
        sessionsTruncated = c.value(.sessionsTruncated, false)
        root = c.value(.root, "")
        agentScope = c.value(.agentScope, "")
        agents = c.value(.agents, [])
        cards = c.value(.cards, [])
        summary = c.value(.summary, HistoryEffectivenessSummary())
    }

    init() {}

    /// Whether this half has anything to say.
    ///
    /// Drawn as a sibling of the helper table rather than inside it: the two
    /// answer different questions, and nesting meant a model filter that
    /// emptied the helper rows also hid the acceptance figures, which no
    /// model filter has any bearing on.
    var speaks: Bool {
        agentScope == "project" || outcomesAvailable
            || !outcomesReason.isEmpty || !reason.isEmpty
    }
}

struct HistoryEffectivenessSummary: Decodable {
    var acceptedOutcomes = 0
    var submittedCards = 0
    var reworkedCards = 0
    /// `nil` for an empty denominator — never `0`, which reads as "nothing
    /// came back" when the truth is "nothing was submitted".
    var reworkRate: Double?
    var observedCardHours: Double = 0
    var manualChecksOutstanding = 0
    var joinedCards = 0
    var joinedSessions = 0
    var sessionsWithHistory = 0
    var partial = false

    enum CodingKeys: String, CodingKey {
        case partial
        case acceptedOutcomes = "accepted_outcomes"
        case submittedCards = "submitted_cards"
        case reworkedCards = "reworked_cards"
        case reworkRate = "rework_rate"
        case observedCardHours = "observed_card_hours"
        case manualChecksOutstanding = "manual_checks_outstanding"
        case joinedCards = "joined_cards"
        case joinedSessions = "joined_sessions"
        case sessionsWithHistory = "sessions_with_history"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        acceptedOutcomes = c.value(.acceptedOutcomes, 0)
        submittedCards = c.value(.submittedCards, 0)
        reworkedCards = c.value(.reworkedCards, 0)
        reworkRate = c.maybe(.reworkRate)
        observedCardHours = c.value(.observedCardHours, 0)
        manualChecksOutstanding = c.value(.manualChecksOutstanding, 0)
        joinedCards = c.value(.joinedCards, 0)
        joinedSessions = c.value(.joinedSessions, 0)
        sessionsWithHistory = c.value(.sessionsWithHistory, 0)
        partial = c.value(.partial, false)
    }

    init() {}
}

/// One bound session on a card, plus the history figures when the store
/// still has the turns. Every money and token field is optional: a missing
/// key is unknown, and a zero default would draw as a measured nothing.
struct HistoryOutcomeSession: Decodable, Identifiable {
    var id: String { sessionId }
    var provider = ""
    var sessionId = ""
    var phase = ""
    var boundAt: Double?
    var known = false
    var model: String?
    var measuredCostUsd: Double?
    var estimatedCostUsd: Double?
    var outputTokens: Int?
    var inputTokens: Int?
    var cacheRead: Int?
    var cacheHitRatio: Double?
    var durationMs: Int?

    enum CodingKeys: String, CodingKey {
        case provider, phase, known, model
        case sessionId = "session_id"
        case boundAt = "bound_at"
        case measuredCostUsd = "measured_cost_usd"
        case estimatedCostUsd = "estimated_cost_usd"
        case outputTokens = "output_tokens"
        case inputTokens = "input_tokens"
        case cacheRead = "cache_read"
        case cacheHitRatio = "cache_hit_ratio"
        case durationMs = "duration_ms"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = c.value(.provider, "")
        sessionId = c.value(.sessionId, "")
        phase = c.value(.phase, "")
        boundAt = c.maybe(.boundAt)
        known = c.value(.known, false)
        model = c.maybe(.model)
        measuredCostUsd = c.maybe(.measuredCostUsd)
        estimatedCostUsd = c.maybe(.estimatedCostUsd)
        outputTokens = c.maybe(.outputTokens)
        inputTokens = c.maybe(.inputTokens)
        cacheRead = c.maybe(.cacheRead)
        cacheHitRatio = c.maybe(.cacheHitRatio)
        durationMs = c.maybe(.durationMs)
    }

    init() {}
}

/// Turns whose session is not on a named card. Zeros here are "nothing
/// spent", and `unpricedSessions` is what says the day is not fully priced.
struct HistoryOtherDay: Decodable, Identifiable {
    var id: String { day }
    var day = ""
    var claudeMeasuredCostUsd: Double = 0
    var grokMeasuredCostUsd: Double = 0
    var claudeEstimatedCostUsd: Double = 0
    var grokEstimatedCostUsd: Double = 0
    /// Claude plus Grok estimates. The picture adds only the assistants
    /// that are on, from the split fields, and does not read this sum.
    var estimatedCostUsd: Double = 0
    var unpricedSessions = 0
    var claudeUnpricedSessions = 0
    var grokUnpricedSessions = 0
    var claudeOutputTokens = 0
    var grokOutputTokens = 0

    enum CodingKeys: String, CodingKey {
        case day
        case claudeMeasuredCostUsd = "claude_measured_cost_usd"
        case grokMeasuredCostUsd = "grok_measured_cost_usd"
        case claudeEstimatedCostUsd = "claude_estimated_cost_usd"
        case grokEstimatedCostUsd = "grok_estimated_cost_usd"
        case estimatedCostUsd = "estimated_cost_usd"
        case unpricedSessions = "unpriced_sessions"
        case claudeUnpricedSessions = "claude_unpriced_sessions"
        case grokUnpricedSessions = "grok_unpriced_sessions"
        case claudeOutputTokens = "claude_output_tokens"
        case grokOutputTokens = "grok_output_tokens"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.value(.day, "")
        claudeMeasuredCostUsd = c.value(.claudeMeasuredCostUsd, 0)
        grokMeasuredCostUsd = c.value(.grokMeasuredCostUsd, 0)
        claudeEstimatedCostUsd = c.value(.claudeEstimatedCostUsd, 0)
        grokEstimatedCostUsd = c.value(.grokEstimatedCostUsd, 0)
        estimatedCostUsd = c.value(.estimatedCostUsd, 0)
        unpricedSessions = c.value(.unpricedSessions, 0)
        claudeUnpricedSessions = c.value(.claudeUnpricedSessions, 0)
        grokUnpricedSessions = c.value(.grokUnpricedSessions, 0)
        claudeOutputTokens = c.value(.claudeOutputTokens, 0)
        grokOutputTokens = c.value(.grokOutputTokens, 0)
    }

    init() {}
}

struct HistoryOutcomeCard: Decodable, Identifiable {
    var id: String { cardId }
    var cardId = ""
    var title = ""
    var root = ""
    var who = ""
    var sessions: [HistoryOutcomeSession] = []
    /// `nil` where acceptance was never asked for — the report was over every
    /// project, and the board's acceptance arithmetic is per project. A
    /// `false` there would read as "somebody rejected it".
    var accepted: Bool?
    var reworkCount: Int?
    var manualCheckOutstanding = false
    var turns = 0
    var durationMs = 0
    var costUsd: Double?
    var partial = false

    enum CodingKeys: String, CodingKey {
        case title, root, accepted, turns, partial, who, sessions
        case cardId = "card_id"
        case reworkCount = "rework_count"
        case manualCheckOutstanding = "manual_check_outstanding"
        case durationMs = "duration_ms"
        case costUsd = "cost_usd"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cardId = c.value(.cardId, "")
        title = c.value(.title, "")
        root = c.value(.root, "")
        who = c.value(.who, "")
        sessions = c.value(.sessions, [])
        accepted = c.maybe(.accepted)
        reworkCount = c.maybe(.reworkCount)
        manualCheckOutstanding = c.value(.manualCheckOutstanding, false)
        turns = c.value(.turns, 0)
        durationMs = c.value(.durationMs, 0)
        costUsd = c.maybe(.costUsd)
        partial = c.value(.partial, false)
    }

    init() {}
}

/// Formatting the report's own "I cannot vouch for this" cases. A dash is the
/// one thing that must never be confused with a zero.
enum HistoryFormat {
    /// A dash where the daemon sent no figure; the figure otherwise, zero
    /// included — a genuine zero is a measurement.
    static func optionalUsd(_ value: Double?) -> String {
        guard let value else { return "—" }
        return Format.usd(value)
    }

    /// "62%" or a dash. Never "0%" for an absent ratio.
    static func percent(_ ratio: Double?) -> String {
        guard let ratio else { return "—" }
        return "\(Int((ratio * 100).rounded()))%"
    }

    /// "62%" or a dash, for a figure that is **already** in percentage
    /// points (`five_hour_pct` and friends), as against `percent(_:)`'s
    /// 0..1 ratio. Never "0%" for an absent reading.
    static func percentPoints(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))%"
    }

    /// Milliseconds as a duration, or a dash when nothing was recorded.
    static func duration(ms: Int) -> String {
        guard ms > 0 else { return "—" }
        return Format.duration(Double(ms) / 1000)
    }
}

/// What the history request is doing right now. The report takes **seconds** —
/// it is a full scan of `history.db` — so the view cannot pretend it is
/// instant: a blank panel for twenty seconds is indistinguishable from a broken
/// one, and this is the state that keeps them apart.
enum HistoryLoad {
    case idle
    case loading
    case loaded(HistoryReport)
    case failed(String)
}
