import Foundation

// The Review section's rules — the state words, whether a press may be
// drawn enabled, the one line each finding, step and ledger entry reads, the
// Needs you entry a run waiting on picks makes, and the fingerprint material
// its Dismiss is checked against. Foundation only, and **byte-pinned to the
// panel** from the marker line down (`panel/Sources/BobPanel/ReviewRules.swift`,
// `host/tests/test_review_rules.py`): edit both copies together, never one.
enum ReviewRules {
    /// The provider the form starts on; Codex and Grok are the other two.
    static let defaultProvider = "claude"

    /// What a Mac that predates the section is called, and the sentence the
    /// section draws for it.
    static let olderMac = "older Mac"
    static let olderMacNote =
        "This Mac's Dark Army is older than the Review section — update it to review from here."

    /// The words a run's state is drawn as (`review_run.STATES`). An unknown
    /// state reads as itself rather than as a guess.
    static func stateWord(_ state: String) -> String {
        switch state {
        case "reviewing": return "reviewing"
        case "picks": return "pick the fixes"
        case "fixing": return "fixing and shipping"
        case "done": return "finished"
        case "exited": return "terminal ended"
        case "ended": return "ended"
        default: return state
        }
    }

    /// Whether Start is drawn enabled: a Mac that serves the section, the
    /// launcher switched on, a project and a provider chosen.
    static func canStart(root: String, tool: String, supported: Bool,
                         dispatchEnabled: Bool) -> Bool {
        supported && dispatchEnabled && !root.isEmpty && !tool.isEmpty
    }

    /// Continue is the second deliberate press, drawn only while the run
    /// waits on the person's picks.
    static func canContinue(state: String) -> Bool {
        state == "picks"
    }

    /// End is drawn for a run still holding its terminal.
    static func showsEnd(state: String) -> Bool {
        state == "reviewing" || state == "picks" || state == "fixing"
    }

    /// Whether the run's ticks are enabled for editing.
    static func picksEditable(state: String) -> Bool {
        state == "picks"
    }

    /// One finding as a row reads it: the grade, then the daemon's line.
    static func findingLine(grade: String, line: String) -> String {
        grade.isEmpty ? line : "\(grade) · \(line)"
    }

    /// One after-step as the section draws it: the label, then where it
    /// stands (nothing yet reads as `waiting`).
    static func stepLine(label: String, status: String, words: String) -> String {
        let state = status.isEmpty ? "waiting" : status
        return words.isEmpty ? "\(label) — \(state)" : "\(label) — \(state): \(words)"
    }

    /// One ledger line as the run's report reads it.
    static func ledgerLine(id: String, status: String, words: String) -> String {
        words.isEmpty ? "\(id): \(status)" : "\(id): \(status) — \(words)"
    }

    /// The Needs you entry for a run: `(key, detail)`, or nil unless the run
    /// is waiting on picks. The key is `r:<run id>`, the detail the scope
    /// line plus the finding count. Both clients read this and derive
    /// nothing else.
    static func picksEntry(runId: String, state: String, scopeLine: String,
                           findingCount: Int) -> (key: String, detail: String)? {
        guard state == "picks", !runId.isEmpty else { return nil }
        let n = findingCount == 1 ? "1 finding" : "\(findingCount) findings"
        let head = scopeLine.isEmpty ? "" : "\(scopeLine) — "
        return (key: "r:" + runId, detail: head + n + " — pick the fixes")
    }

    /// What Dismiss's fingerprint is made of: the run id and the whole second
    /// its findings landed, spelled as the daemon spells it
    /// (`inbox_ack.review_picks_material`).
    static func fingerprintMaterial(runId: String, findingsAt: Double) -> String {
        "\(runId):\(Int(findingsAt))"
    }

    /// Whether a fleet row is a review run's terminal, so a plain wait on it
    /// is not listed a second time under Needs you: the run's own entry
    /// carries the decision. A permission or a question on it still lists.
    static func isReviewRow(sessionId: String, runSessions: [String]) -> Bool {
        !sessionId.isEmpty && runSessions.contains(sessionId)
    }

    /// The run states whose terminal's plain wait the run's entry covers: the
    /// decision it waits on (`picks`) and the finished report (`done`). While
    /// it is `fixing` a wait is real news and is listed.
    static let coveredStates: Set<String> = ["picks", "done"]

    /// The ticked finding numbers, ascending, as the wire carries them.
    static func fixList(_ picks: Set<Int>) -> [Int] {
        picks.sorted()
    }
}
