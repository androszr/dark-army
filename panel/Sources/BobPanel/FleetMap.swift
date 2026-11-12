import Foundation

/// The fleet activity map's rules, Foundation-only and table-tested
/// (`FleetMapRulesTests`). The daemon decides every fact on the map (the
/// bucket, the station of a call, how a call ended); these functions only
/// fold what it sent for drawing. Nothing here reads a clock: flight timing
/// is a pure function of the pane's own arrival stamp.
/// Contract: `docs/fleet-map-contract.md`.

/// The rail's two views of the selected project's agents.
enum FleetRailView: String, CaseIterable {
    case table, map

    /// Only `"map"` is ever stored; absent or any other word is the table.
    static func fromStored(_ word: String?) -> FleetRailView {
        word == FleetRailView.map.rawValue ? .map : .table
    }
}

/// One line of the feed under the map.
struct FleetFeedRow: Equatable {
    var sessionId: String
    var name: String
    var station: String
    var tool: String
    var end: FleetCallEnd
    /// Further finished calls folded into this row ("+2").
    var extra: Int
    var seq: Int

    var symbol: String { FleetMap.symbol(for: end) }
}

enum FleetMap {
    /// Hues an agent can wear. Amber and red are never members: they mean
    /// "waiting" and "failed" on this map.
    static let paletteSize = 8

    /// How long one pulse takes to fly from a portrait to its station.
    static let flight: Double = 0.6

    /// An agent's colour slot: its index in the tab's `byStart` order.
    static func colorIndex(sessionId: String, in ordered: [String]) -> Int {
        guard let at = ordered.firstIndex(of: sessionId) else { return 0 }
        return at % paletteSize
    }

    /// Running calls per station across the tab's sessions: two agents in
    /// Files at once read 2. Sessions outside `sessionIds` count for nothing.
    static func stationCounts(calls: [String: [FleetCall]],
                              in sessionIds: [String]) -> [String: Int] {
        var out: [String: Int] = [:]
        for sid in sessionIds {
            for call in calls[sid] ?? [] where call.end == .running {
                out[call.station, default: 0] += 1
            }
        }
        return out
    }

    /// Failed calls per station for sessions whose error count is still
    /// standing (it clears on that agent's next prompt).
    static func failedCounts(sessions: [String: FleetSessionActivity],
                             in sessionIds: [String]) -> [String: Int] {
        var out: [String: Int] = [:]
        for sid in sessionIds {
            guard let activity = sessions[sid], activity.errors > 0 else { continue }
            for (station, n) in activity.errorsByStation where n > 0 {
                out[station, default: 0] += n
            }
        }
        return out
    }

    /// Groups of two or more sessions bound to one card, each group sorted by
    /// the order of `rows`; a group of one draws nothing. The card is the
    /// board's binding (`card(forSession:)`), else the row's `originCard`.
    static func ties(rows: [Agent], board: Board) -> [[String]] {
        var order: [String] = []
        var groups: [String: [String]] = [:]
        for agent in rows {
            let bound = board.card(forSession: agent.sessionId)?.id ?? ""
            let key = bound.isEmpty ? agent.originCard : bound
            guard !key.isEmpty else { continue }
            if groups[key] == nil { order.append(key) }
            groups[key, default: []].append(agent.sessionId)
        }
        return order.compactMap { groups[$0] }.filter { $0.count >= 2 }
    }

    static func symbol(for end: FleetCallEnd) -> String {
        switch end {
        case .running: return "●"
        case .done: return "·"
        case .failed: return "✕"
        }
    }

    /// The feed: every running call (the current actions) then the newest
    /// finished rows, at most `limit` of those, newest first. A run of
    /// consecutive finished calls by one agent at one station folds into one
    /// row carrying `extra`. Folding is per agent, so another agent's call in
    /// between never breaks a run.
    static func feedRows(sessions: [String: [FleetCall]],
                         names: [String: String],
                         in sessionIds: [String],
                         limit: Int = 16) -> [FleetFeedRow] {
        var rows: [FleetFeedRow] = []
        // Only the tab's own sessions: another project's calls, or a ring
        // whose session is no longer a live row, never reach this tab's feed.
        for sid in sessionIds {
            let calls = sessions[sid] ?? []
            var folded: [FleetFeedRow] = []
            // Newest first within the agent.
            for call in calls.sorted(by: { $0.seq > $1.seq }) {
                if call.end == .done, var last = folded.last,
                   last.end == .done, last.station == call.station {
                    last.extra += 1
                    folded[folded.count - 1] = last
                    continue
                }
                folded.append(FleetFeedRow(
                    sessionId: sid, name: names[sid] ?? String(sid.prefix(6)),
                    station: call.station, tool: call.tool, end: call.end,
                    extra: 0, seq: call.seq))
            }
            rows.append(contentsOf: folded)
        }
        rows.sort { $0.seq > $1.seq }
        let running = rows.filter { $0.end == .running }
        let finished = rows.filter { $0.end != .running }.prefix(max(0, limit))
        return Array(running) + Array(finished)
    }

    /// 0 at arrival, 1 once landed; a stamp of 0 (seen before the pane was
    /// watching) is always landed.
    static func pulseProgress(seenAt: Double, now: Double,
                              flight: Double = FleetMap.flight) -> Double {
        guard seenAt > 0, flight > 0 else { return 1 }
        return min(1, max(0, (now - seenAt) / flight))
    }

    /// Path width for an agent's use of one station: log-scaled, 1...4pt.
    static func thickness(count: Int) -> Double {
        guard count > 0 else { return 1 }
        return min(4, max(1, 1 + log2(Double(count) + 1) * 0.45))
    }

    /// A waiting agent parks a steady line on You and draws no motion.
    static func waitingLine(category: Category) -> Bool {
        category == .waiting
    }

    /// The calls that may fly: none for a waiting agent.
    static func pulsingCalls(category: Category, calls: [FleetCall]) -> [FleetCall] {
        waitingLine(category: category) ? [] : calls
    }

    /// One expanding ring when a turn ends (running to anything else).
    static func turnEndedRing(previous: Category?, current: Category) -> Bool {
        previous == .running && current != .running
    }

    // MARK: - Geometry (a fixed square, never measured)

    /// A station's centre: `count` points on a ring, the first at the top.
    static func stationPoint(index: Int, count: Int, side: Double) -> CGPoint {
        let radius = side / 2 - 40
        let angle = 2 * Double.pi * Double(index) / Double(max(1, count)) - Double.pi / 2
        return CGPoint(x: side / 2 + radius * cos(angle),
                       y: side / 2 + radius * sin(angle))
    }

    /// Where `count` portraits sit in the middle: one at the centre, more on
    /// a small circle.
    static func portraitCenters(count: Int, side: Double) -> [CGPoint] {
        guard count > 0 else { return [] }
        let mid = CGPoint(x: side / 2, y: side / 2)
        if count == 1 { return [mid] }
        let radius = min(side * 0.2, 40 + Double(count) * 6)
        return (0..<count).map { i in
            let angle = 2 * Double.pi * Double(i) / Double(count) - Double.pi / 2
            return CGPoint(x: mid.x + radius * cos(angle),
                           y: mid.y + radius * sin(angle))
        }
    }

    /// Portrait edge: shrinks as the tab fills, so the middle stays legible.
    static func portraitSize(count: Int) -> Double {
        switch count {
        case ...3: return 48
        case 4...6: return 40
        default: return 32
        }
    }
}
