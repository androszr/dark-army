import Foundation

/// What a card has cost so far and how long its assistant has actually
/// worked — the one wording rule, shared with the phone.
///
/// **Byte-pinned with `ios/BobPhone/RunFigures.swift` from the
/// `enum RunFigures {` line down** (`host/tests/test_run_figures.py`), the
/// `Markdown` / `Specialists` discipline: edit both copies together. Only
/// this header may differ. Foundation only, so the phone can carry it
/// unchanged.
///
/// The daemon composes the figures (`run_figures.py`) at minute, cent and
/// whole-percent granularity and buys the frame that draws a moved one;
/// neither client ages anything — the line is drawn as it arrived, and no
/// clock on either surface depends on the frame's own stamp.
enum RunFigures {
    /// The wire object, decoded tolerantly: every member through `value` /
    /// `maybe`, so `{}` and `{"cost_usd": null}` decode without throwing.
    /// `costUsd`, `activeSeconds` and `ctxPercent` are **optionals, not
    /// zeros** — a cost Dark Army could not measure is said in words, never as
    /// `$0`, and a card with no execution span at all (refined, never
    /// started) carries no time part rather than "under a minute".
    struct Figures: Decodable, Equatable {
        var costUsd: Double?
        var costCoverage = "unknown"
        var activeSeconds: Int?
        var activeTicking = false
        var ctxPercent: Int?
        var attempts = 0

        enum CodingKeys: String, CodingKey {
            case attempts
            case costUsd = "cost_usd"
            case costCoverage = "cost_coverage"
            case activeSeconds = "active_seconds"
            case activeTicking = "active_ticking"
            case ctxPercent = "ctx_percent"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            costUsd = c.maybe(.costUsd)
            costCoverage = c.value(.costCoverage, "unknown")
            activeSeconds = c.maybe(.activeSeconds)
            activeTicking = c.value(.activeTicking, false)
            ctxPercent = c.maybe(.ctxPercent)
            attempts = c.value(.attempts, 0)
        }

        init(costUsd: Double? = nil, costCoverage: String = "unknown",
             activeSeconds: Int? = nil, activeTicking: Bool = false,
             ctxPercent: Int? = nil, attempts: Int = 0) {
            self.costUsd = costUsd
            self.costCoverage = costCoverage
            self.activeSeconds = activeSeconds
            self.activeTicking = activeTicking
            self.ctxPercent = ctxPercent
            self.attempts = attempts
        }
    }

    /// The daemon's own words for a cost it could not measure
    /// (`run_figures.COST_UNKNOWN`).
    static let costUnknown = "cost unknown"

    static let separator = " · "

    /// The one line: `"$1.40 · 23 min · 61% ctx"`, `"$1.40 (partial) · 23
    /// min"`, `"cost unknown · 23 min · 2 attempts"`, `"$0.31"` for a card
    /// refined but never started. **nil in, nil out** — a card with no
    /// figures draws no line, not a dash.
    static func line(_ f: Figures?) -> String? {
        guard let f else { return nil }
        var parts: [String] = []
        if let usd = f.costUsd, f.costCoverage != "unknown" {
            parts.append(money(usd) + (f.costCoverage == "partial" ? " (partial)" : ""))
        } else {
            parts.append(costUnknown)
        }
        if let seconds = f.activeSeconds {
            parts.append(duration(seconds))
        }
        if let pct = f.ctxPercent {
            parts.append("\(pct)% ctx")
        }
        if f.attempts > 1 {
            parts.append("\(f.attempts) attempts")
        }
        return parts.joined(separator: separator)
    }

    /// `AgentDetailPane.money`'s rule, restated here so the phone can share
    /// it: two decimals under ten dollars, one above.
    static func money(_ usd: Double) -> String {
        usd < 10 ? String(format: "$%.2f", usd) : String(format: "$%.1f", usd)
    }

    /// Minutes, because the daemon publishes minutes: under one is said in
    /// words, an hour and up is `h` and `min`.
    static func duration(_ seconds: Int) -> String {
        let total = max(seconds, 0)
        if total < 60 { return "under a minute" }
        let minutes = total / 60
        if total < 3600 { return "\(minutes) min" }
        return "\(minutes / 60) h \(minutes % 60) min"
    }
}
