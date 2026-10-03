import Foundation

/// The run-health line, drawn verbatim wherever a card with a run is shown:
/// the card tile, the card window's status section and the phone's card
/// screen. The daemon decides every figure (`run_health.py`); this file only
/// spells them, so both clients read the same words from the same object.
///
/// **Byte-copied to `ios/BobPhone/RunHealth.swift`** and pinned equal by
/// `host/tests/test_run_health.py`. Foundation only, no view, no clock.
///
/// The word comes first — TYPICAL, LARGE, WORRYING, or UNSIZED where the
/// daemon could not size the run — so the line can be understood without
/// colour; `attention` is a second signal, never the only one.
enum RunHealthLine {
    /// `LARGE · 84t · 2.1M · ctx 65% · asks 3 · refusals 1 · attempt 2 ·
    /// back 1 · fixes 2 · effort high`. Unknown figures are left out rather
    /// than drawn as zero; an absent fix count draws `fixes –`, because a run
    /// nobody counted is not a run with no fix rounds. The effort is the one
    /// the session itself reported, last, and absent while unknown.
    static func text(_ h: RunHealth) -> String {
        var parts: [String] = []
        parts.append(sizeWord(h.sizeClass))
        if let turns = h.turns { parts.append("\(turns)t") }
        if let k = h.tokensK { parts.append(tokens(k)) }
        if let pct = h.ctxPct { parts.append("ctx \(pct)%") }
        parts.append("asks \(h.asks)")
        parts.append("refusals \(h.refusals)")
        parts.append("attempt \(h.attempts)")
        if h.returns > 0 { parts.append("back \(h.returns)") }
        if let fixes = h.fixRounds {
            parts.append("fixes \(fixes)")
        } else {
            parts.append("fixes –")
        }
        if !h.effort.isEmpty { parts.append("effort \(h.effort)") }
        return parts.joined(separator: " · ")
    }

    /// The same facts as one sentence, for VoiceOver, with the basis the
    /// daemon judged the size against.
    static func spoken(_ h: RunHealth) -> String {
        var parts: [String] = []
        parts.append(sizeSentence(h.sizeClass, basis: h.basis))
        if let turns = h.turns { parts.append(plural(turns, "turn")) }
        if let k = h.tokensK { parts.append(spokenTokens(k)) }
        if let pct = h.ctxPct { parts.append("context \(pct) percent full") }
        parts.append(plural(h.asks, "ask"))
        parts.append(plural(h.refusals, "refusal"))
        parts.append("attempt \(h.attempts)")
        if h.returns > 0 {
            parts.append(h.returns == 1 ? "sent back once"
                                        : "sent back \(h.returns) times")
        }
        if let fixes = h.fixRounds {
            parts.append(plural(fixes, "fix round"))
        } else {
            parts.append("fix rounds not counted")
        }
        if !h.effort.isEmpty { parts.append("effort \(h.effort)") }
        return parts.joined(separator: ", ") + "."
    }

    // MARK: - pieces

    static func sizeWord(_ sizeClass: String) -> String {
        sizeClass.isEmpty ? "UNSIZED" : sizeClass.uppercased()
    }

    static func sizeSentence(_ sizeClass: String, basis: String) -> String {
        let judged = basis == "project" ? "judged against this project"
                                        : "judged against defaults"
        switch sizeClass {
        case "typical": return "Typical run, \(judged)"
        case "large": return "Large run, \(judged)"
        case "worrying": return "Worrying run, \(judged)"
        default: return "Unsized run"
        }
    }

    /// `840k` under a million tokens, `2.1M` from there.
    static func tokens(_ k: Int) -> String {
        if k < 1000 { return "\(k)k" }
        return String(format: "%.1fM", Double(k) / 1000)
    }

    static func spokenTokens(_ k: Int) -> String {
        if k < 1000 { return "\(k) thousand tokens" }
        return String(format: "%.1f million tokens", Double(k) / 1000)
    }

    static func plural(_ n: Int, _ noun: String) -> String {
        n == 1 ? "1 \(noun)" : "\(n) \(noun)s"
    }
}
