import SwiftUI

/// `/api/history?session=` — one session's durable record, fetched on demand by
/// the card window (`DaemonClient.sessionRecord`). Same tolerant decoding as
/// `Snapshot`: a missing key must never blank the view.

/// One session's durable record from `GET /api/history?session=`.
///
/// Numeric fields Dark Army may not know (`costUsd`, `tokens`, the timestamps that
/// duration is computed from) decode as optionals: absent means a dash, never
/// a quiet zero. Tools arrive as `[name, count]` pairs.
struct SessionRecord: Decodable {
    var sessionId = ""
    var project = ""
    var title = ""
    var kind = ""
    var provider = ""
    var primaryModel = ""
    var firstSeen: Double?
    var lastSeen: Double?
    var endedAt: Double?
    var endReason = ""
    var costUsd: Double?
    var costSource = ""
    var turns = 0
    var tokens: Int?
    var subagents = 0
    var tools: [SessionTool] = []
    var attribution: [String] = []
    var compactions = 0

    enum CodingKeys: String, CodingKey {
        case project, title, kind, provider, turns, tokens, subagents
        case tools, attribution, compactions
        case sessionId = "session_id"
        case primaryModel = "primary_model"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
        case endedAt = "ended_at"
        case endReason = "end_reason"
        case costUsd = "cost_usd"
        case costSource = "cost_source"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.value(.sessionId, "")
        project = c.value(.project, "")
        title = c.value(.title, "")
        kind = c.value(.kind, "")
        provider = c.value(.provider, "")
        primaryModel = c.value(.primaryModel, "")
        firstSeen = c.maybe(.firstSeen)
        lastSeen = c.maybe(.lastSeen)
        endedAt = c.maybe(.endedAt)
        endReason = c.value(.endReason, "")
        costUsd = c.maybe(.costUsd)
        costSource = c.value(.costSource, "")
        turns = c.value(.turns, 0)
        tokens = c.maybe(.tokens)
        subagents = c.value(.subagents, 0)
        tools = c.value(.tools, [])
        attribution = c.value(.attribution, [])
        compactions = c.value(.compactions, 0)
    }
}

/// One `[name, count]` pair from `session_tools`.
struct SessionTool: Equatable {
    var name: String
    var count: Int

    init(name: String, count: Int) {
        self.name = name
        self.count = count
    }
}

extension SessionTool: Decodable {
    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        name = (try? c.decode(String.self)) ?? ""
        if let n = try? c.decode(Int.self) {
            count = n
        } else if let n = try? c.decode(Double.self) {
            count = Int(n)
        } else {
            count = 0
        }
    }
}

/// The `?session=` wrapper. `available: false` is "cannot be read";
/// `session: null` is "no record" — two different answers.
struct SessionRecordReport: Decodable {
    var available = false
    var live = false
    var reason = ""
    var session: SessionRecord?

    enum CodingKeys: String, CodingKey {
        case available, live, reason, session
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        live = c.value(.live, false)
        reason = c.value(.reason, "")
        session = c.maybe(.session)
    }
}

enum SessionRecordFormat {
    /// "Bash ×41 · Read ×23". Empty input is empty output, never "0 tools".
    static func toolsLine(_ tools: [SessionTool]) -> String {
        tools.filter { !$0.name.isEmpty && $0.count > 0 }
            .map { "\($0.name) ×\($0.count)" }
            .joined(separator: " · ")
    }

    /// From `first_seen` to (`ended_at` ?? `last_seen`). Dash when either
    /// end is missing or the span is inverted.
    static func durationText(firstSeen: Double?, endedAt: Double?,
                             lastSeen: Double?) -> String {
        guard let start = firstSeen else { return "—" }
        guard let end = endedAt ?? lastSeen, end >= start else { return "—" }
        return Format.duration(end - start)
    }

    /// Tilde when the figure is estimated, dash when Dark Army does not know.
    static func costText(usd: Double?, source: String) -> String {
        guard let usd else { return "—" }
        let estimated = !source.isEmpty && source != "measured"
        return (estimated ? "~" : "") + Format.usd(usd)
    }

    /// The line under WHAT RAN when there is no record body. nil means
    /// draw the figures. Loading is a line, never a heading over a blank pane.
    static func absenceLine(report: SessionRecordReport?,
                            fetchFailed: Bool) -> String? {
        if fetchFailed { return "Couldn't reach Dark Army for this run's record." }
        guard let report else { return "Loading this run's record…" }
        if !report.available { return "Dark Army's history database isn't available." }
        if report.session == nil { return "Dark Army has no durable record of this session." }
        return nil
    }
}
