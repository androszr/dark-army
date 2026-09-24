import SwiftUI

/// The agent efficiency report on the phone — the first long-range report the
/// phone has ever had.
///
/// Deliberately **not** a byte-pinned pair with the desk's `HistoryView`: the
/// Mac draws a wide table and a phone draws a stacked list, so a byte-identical
/// pair would be a lie. What is pinned instead is the wire agreement — the JSON
/// keys both sides name — by `host/tests/test_agent_report_surface.py`.
///
/// Tolerant decoding throughout: Swift's synthesized `Decodable` throws on a
/// missing key even where the property has a default, and one absent section
/// from an older Mac must never blank the Usage screen.

// MARK: - The wire

struct AgentReport: Decodable {
    var supported = false
    var available = false
    var reason = ""
    var outcomesAvailable = false
    var outcomesReason = ""
    var sessionsTruncated = false
    var rangeDays: Int?
    /// Set by the Mac when the report was too large for one sealed reply and
    /// was cut short. Present means **there is more**: a trimmed list that
    /// said nothing would read as a complete one, which is the whole failure
    /// this field exists to prevent.
    var nextOffset: Int?
    /// Whether the **helper list** was one of the collections the trim
    /// reached. `nextOffset` alone cannot answer that: the offset is shared
    /// between the helper list and the card list, `cards` is routinely the
    /// long one, and this screen never draws it — so a note keyed on
    /// `nextOffset` told the reader their helper list had been cut whenever a
    /// list they cannot see was. Absent means whole.
    var agentsTruncated = false
    /// `"project"` or `"machine"` — what the helper rows beside it count.
    /// Stated by the Mac, never inferred here.
    var agentScope = ""
    var agents: [AgentReportRow] = []
    var cards: [AgentReportCard] = []
    var summary = AgentReportSummary()

    enum CodingKeys: String, CodingKey {
        case supported, available, reason, agents, cards, summary
        case outcomesAvailable = "outcomes_available"
        case outcomesReason = "outcomes_reason"
        case sessionsTruncated = "sessions_truncated"
        case rangeDays = "range_days"
        case nextOffset = "next_offset"
        case agentsTruncated = "agents_truncated"
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
        rangeDays = c.maybe(.rangeDays)
        nextOffset = c.maybe(.nextOffset)
        agentsTruncated = c.value(.agentsTruncated, false)
        agentScope = c.value(.agentScope, "")
        agents = c.value(.agents, [])
        cards = c.value(.cards, [])
        summary = c.value(.summary, AgentReportSummary())
    }

    init() {}
}

/// One kind of helper. `costUsd` and `cacheHitRatio` are optional because the
/// Mac sends `null` for a figure it cannot vouch for, and a `0` in either cell
/// would read as a measurement.
struct AgentReportRow: Decodable, Identifiable {
    var id: String { name }
    var name = ""
    var role = ""
    var dispatches = 0
    var sessions = 0
    var turns = 0
    var durationMs = 0
    var toolCalls = 0
    var costUsd: Double?
    var cacheHitRatio: Double?
    var unpricedTurns = 0
    var models: [String] = []

    enum CodingKeys: String, CodingKey {
        case name, role, dispatches, sessions, turns, models
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
        durationMs = c.value(.durationMs, 0)
        toolCalls = c.value(.toolCalls, 0)
        costUsd = c.maybe(.costUsd)
        cacheHitRatio = c.maybe(.cacheHitRatio)
        unpricedTurns = c.value(.unpricedTurns, 0)
        models = c.value(.models, [])
    }
}

struct AgentReportCard: Decodable, Identifiable {
    var id: String { cardId }
    var cardId = ""
    var title = ""
    /// `nil` where acceptance was never asked for — a report over every
    /// project carries none, because the board's acceptance arithmetic is per
    /// project. A `false` here would read as "somebody rejected it".
    var accepted: Bool?
    var reworkCount: Int?
    var manualCheckOutstanding = false
    var turns = 0
    var durationMs = 0
    var costUsd: Double?
    var partial = false

    enum CodingKeys: String, CodingKey {
        case title, accepted, turns, partial
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
        accepted = c.maybe(.accepted)
        reworkCount = c.maybe(.reworkCount)
        manualCheckOutstanding = c.value(.manualCheckOutstanding, false)
        turns = c.value(.turns, 0)
        durationMs = c.value(.durationMs, 0)
        costUsd = c.maybe(.costUsd)
        partial = c.value(.partial, false)
    }
}

struct AgentReportSummary: Decodable {
    var acceptedOutcomes = 0
    var submittedCards = 0
    var reworkedCards = 0
    /// `nil` for an empty denominator, never `0` — nothing submitted is not
    /// the same fact as nothing came back.
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

/// Words for the figures Dark Army cannot vouch for. A dash is the one thing
/// that must never be confused with a zero.
enum AgentReportFormat {
    static func cost(_ value: Double?) -> String {
        guard let value else { return "no price" }
        return String(format: "$%.2f", value)
    }

    static func percent(_ ratio: Double?) -> String {
        guard let ratio else { return "–" }
        return "\(Int((ratio * 100).rounded()))%"
    }

    static func duration(ms: Int) -> String {
        guard ms > 0 else { return "–" }
        let seconds = ms / 1000
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        return "\(seconds / 3600)h"
    }

    /// What one helper's row says out loud, as one sentence.
    static func spoken(_ row: AgentReportRow) -> String {
        var parts = ["\(row.name), \(row.dispatches) runs"]
        parts.append(row.costUsd == nil ? "no price recorded"
                                        : "\(cost(row.costUsd)) spent")
        if let ratio = row.cacheHitRatio {
            parts.append("\(Int((ratio * 100).rounded())) percent read from cache")
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: - The screen

/// A pushed screen inside Usage, fetch-on-appear. An older Mac draws no
/// link, so this view is never mounted there. The 30s floor re-asks only
/// while this screen is showing; popping stops it.
struct PhoneAgentReport: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @ObservedObject var client: PhoneClient
    @State private var root = ""
    @State private var fetch: Fetch = .idle
    @State private var lastFetch = Date.distantPast
    @State private var refreshing = false

    /// Failure, "still going" and "this Mac recorded no helper work" are three
    /// different facts, and holding the answer as a bare `AgentReport?` drew
    /// all three as the same empty box — the picker with nothing under it.
    /// The scan takes seconds, and the sealed read comes back `nil` for a
    /// relay that is down, a timeout or a background run, so the two cases
    /// that are not an answer say so in words.
    private enum Fetch {
        case idle
        case loading
        case failed
        case loaded(AgentReport, root: String)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if refreshing {
                    AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                     spoken: "Checking with the Mac")
                        .id("refresh")
                }
                PromptLine(path: "~/usage/agents")
                Picker("Project", selection: $root.decrypting(decryptFeedback)) {
                    Text("Every project").tag("")
                    ForEach(client.snapshot.board.projects, id: \.root) { project in
                        Text(project.name).tag(project.root)
                    }
                }
                .accessibilityLabel("Project")
                switch fetch {
                case .idle:
                    EmptyView()
                case .loading:
                    // No spinner: nothing spins on either client. A sentence
                    // that says what is happening does the same job and can be
                    // read aloud.
                    note("Asking Dark Army for the report… this can take a few "
                         + "seconds.")
                case .failed:
                    note("Dark Army could not be reached.")
                case .loaded(let report, _):
                    if report.available {
                        agentRows(report)
                    } else {
                        note(report.reason.isEmpty
                             ? "Dark Army could not produce this report."
                             : report.reason)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .font(Theme.mono(12))
        .background(Theme.bg)
        .navigationTitle("agents")
        .navigationBarTitleDisplayMode(.inline)
        .decryptSurface("PhoneAgentReport")
        .tint(Theme.phosphor)
        .refreshable {
            refreshing = true
            await load()
            refreshing = false
        }
        .task(id: root) { await load() }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            if Date().timeIntervalSince(lastFetch) >= 30 {
                Task { await load() }
            }
        }
    }

    /// One line of prose about the fetch itself — never clipped, and sized
    /// through `Theme.mono` like everything else here.
    private func note(_ text: String) -> some View {
        Text(text)
            .font(Theme.mono(12))
            .foregroundStyle(Theme.dim)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func agentRows(_ report: AgentReport) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(report.agents.prefix(12)) { row in
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.name)
                        .font(Theme.mono(13))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("\(row.dispatches) runs · \(AgentReportFormat.cost(row.costUsd)) · "
                         + "\(AgentReportFormat.duration(ms: row.durationMs)) · "
                         + "cache \(AgentReportFormat.percent(row.cacheHitRatio))")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(AgentReportFormat.spoken(row))
            }
            if report.agents.isEmpty {
                Text(report.agentScope == "project"
                     ? "No helper work recorded for this project in this period."
                     : "No helper work recorded in this period.")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            } else if report.agentScope == "project" {
                // What the narrowing actually is. The Mac filters on the
                // sessions `outcome_runs` recorded, so ad-hoc work in the
                // same folder is absent — calling them the project's
                // sessions outright claimed a completeness the store cannot
                // give.
                Text("These are the sessions this project's cards were worked by; ad-hoc work in the same folder is not counted.")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            outcomes(report)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func outcomes(_ report: AgentReport) -> some View {
        let summary = report.summary
        VStack(alignment: .leading, spacing: 2) {
            if report.outcomesAvailable {
                Text("\(summary.acceptedOutcomes) accepted · \(summary.submittedCards) submitted "
                     + "· rework \(AgentReportFormat.percent(summary.reworkRate))")
                Text("\(summary.manualChecksOutstanding) manual checks outstanding")
            } else if !report.outcomesReason.isEmpty {
                Text(report.outcomesReason)
            }
            if summary.partial {
                Text("Some of this work is older than the history Dark Army keeps, so the spend beside it is a floor.")
            }
            if report.sessionsTruncated {
                Text("Too many sessions to join in one report; these figures cover the earliest of them.")
            }
            if report.agentsTruncated {
                Text("This report was too long to send in one piece, so the list above stops early. Open it on the Mac to see all of it.")
            }
        }
        .font(Theme.mono(11))
        .foregroundStyle(Theme.dim)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func load() async {
        lastFetch = Date()
        let wanted = root
        // Only the *first* fetch for this project draws the waiting line: a
        // 30s refresh that replaced a drawn report with "asking…" would
        // blank a screen somebody is reading.
        if case .loaded(_, let shown) = fetch, shown == wanted {} else {
            fetch = .loading
        }
        let fresh = await client.agentReport(root: wanted)
        guard !Task.isCancelled, root == wanted else { return }
        fetch = fresh.map { Fetch.loaded($0, root: wanted) } ?? .failed
    }
}
