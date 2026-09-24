import Foundation

/// The panel's flight recorder.
///
/// **It writes to stderr rather than to a file of its own**, because the menu
/// bar already drains the panel's stderr into the app log
/// (`panel_process._read_errors`) — and that log is the only place a panel line
/// can be read directly beneath the daemon event that should have caused it.
/// That adjacency is the whole point: the question this exists to answer is
/// whether a snapshot the daemon pushed ever reached the panel and what was in
/// it, and a private log file would have to be merged by hand against
/// timestamps from a second clock.
///
/// Every line leads with `trace` so the menu bar can file it as information;
/// anything else the panel writes to stderr is still logged as a warning, as it
/// always was.
///
/// **What is traced is the data path, not the drawing.** A SwiftUI `body` runs
/// whenever the framework feels like it, so a line emitted from one says
/// nothing about what is on screen and plenty about what is not. Traced
/// instead: what arrived, when, and the row fields the caption rules are pure
/// functions of — from those the drawing is derivable, and the trace does not
/// perturb the thing it measures.
enum Trace {
    /// Whether the two *per-frame* lines ("snapshot …", "snapshot duplicate
    /// skipped …") are written at all. Read once from `BOB_PANEL_TRACE` —
    /// which the panel inherits from the menu bar, so
    /// `launchctl setenv BOB_PANEL_TRACE 1` plus a relaunch turns the pair
    /// back on (with `BOB_COMPANION_LOG_LEVEL DEBUG` for the daemon's half;
    /// unset both and relaunch to quiet it again). Off by default because
    /// those two lines, with the daemon's matching `SSE push:`, were 86% of a
    /// day's log. Every *rare* line — `sse open/ended/dropped`, `REJECTED`,
    /// not-utf8 — stays unconditional: those are the diagnostics.
    static let verbose: Bool = {
        let raw = ProcessInfo.processInfo.environment["BOB_PANEL_TRACE"] ?? ""
        return !raw.isEmpty && raw != "0"
    }()

    /// Serialised and off the caller's thread: lines come from the SSE task, the
    /// run loop and the key handler, and interleaved writes to one descriptor is
    /// how a log ends up with half-lines in it. The timestamp is taken *before*
    /// the hop, or the queue's own latency would be recorded as the event's.
    private static let queue = DispatchQueue(label: "com.bob-companion.panel.trace")

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    static func log(_ line: String) {
        let stamp = clock.string(from: Date())
        queue.async {
            FileHandle.standardError.write(Data("trace \(stamp) \(line)\n".utf8))
        }
    }
}

extension Snapshot {
    /// One line saying what arrived — enough to answer "did the panel hold this
    /// row, in this bucket, with its summary" without dumping a payload nobody
    /// would read.
    ///
    /// `age` is the snapshot's own age (`now - generated_at`), which separates
    /// the two ways a panel goes stale: a payload that took a long time to
    /// arrive, and a payload that arrived promptly and then stopped being
    /// replaced. Only the rows the caption path is about are named — a row with
    /// a `bob-tldr` summary or an open question — because those are the ones
    /// whose absence from the screen is worth explaining.
    var digest: String {
        let age = max(0, Date().timeIntervalSince1970 - generatedAt)
        var parts = [
            "run=\(agents.running.count)",
            "wait=\(agents.waiting.count)",
            "sleep=\(agents.sleeping.count)",
            "fin=\(agents.finished.count)",
            "aband=\(agents.abandoned.count)",
            "cards=\(notifications.count)",
            "prompts=\(permissions.count)",
            String(format: "age=%.1fs", age),
        ]
        let buckets: [(String, [Agent])] = [("wait", agents.waiting),
                                            ("run", agents.running),
                                            ("sleep", agents.sleeping)]
        let says = buckets.flatMap { label, rows in
            rows.filter { !$0.lastSummary.isEmpty || !$0.question.text.isEmpty }
                .map { row -> String in
                    let who = row.nickname.isEmpty ? String(row.sessionId.prefix(8))
                                                   : row.nickname
                    return "\(who):\(label)/\(Int(row.idleSeconds))s"
                        + (row.lastSummary.isEmpty ? "" : "/tldr")
                        + (row.question.text.isEmpty ? "" : "/q")
                        + (row.currentTool.isEmpty ? "" : "/\(row.currentTool)")
                }
        }
        if !says.isEmpty { parts.append("says=[" + says.joined(separator: " ") + "]") }
        return parts.joined(separator: " ")
    }
}
