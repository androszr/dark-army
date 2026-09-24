import Foundation

/// The picks of a multi-question dialog, held outside any one view.
///
/// Two screens draw an `AnswerBox` for the same waiting agent — the "Needs
/// you" list row and the agent detail — and each SwiftUI view identity kept
/// its own `@State`, so picks made on one screen never showed on the other.
/// This store is the one copy both boxes read and write, keyed by
/// session id + question id. The key itself carries the reset rule: a *new*
/// question id starts blank (its key holds nothing yet), and the same id
/// keeps its picks across views and screens.
final class AnswerDrafts: ObservableObject {
    struct Entry: Equatable {
        /// The picks per question position. A set on every question: a
        /// pick-one question holds at most one (a pick replaces), a
        /// multi-select question holds every ticked box (a pick toggles) —
        /// one shape, because the pick-one group is the degenerate case and
        /// a parallel field would be two truths the send has to reconcile.
        var choices: [Int: Set<Int>] = [:]
        var confirming = false
    }

    @Published private var entries: [String: Entry] = [:]

    static func key(sessionId: String, questionId: String) -> String {
        sessionId + "|" + questionId
    }

    func entry(_ key: String) -> Entry { entries[key] ?? Entry() }

    /// Record a press on `option` of `question`: toggled in and out of the
    /// set when `multi` (a tick box), else the set becomes that one option
    /// (a radio mark).
    func pick(_ key: String, question: Int, option: Int, multi: Bool) {
        var entry = entries[key] ?? Entry()
        if multi {
            var set = entry.choices[question] ?? []
            set.formSymmetricDifference([option])
            entry.choices[question] = set
        } else {
            entry.choices[question] = [option]
        }
        entries[key] = entry
    }

    func setConfirming(_ key: String, _ value: Bool) {
        var entry = entries[key] ?? Entry()
        entry.confirming = value
        entries[key] = entry
    }

    func clear(_ key: String) {
        guard entries[key] != nil else { return }
        entries[key] = nil
    }

    /// A new dialog replaced the old one on this session: drop the session's
    /// other entries so the next dialog starts blank, keeping the current
    /// one's — the old `onChange(of: agent.question.id)` reset, store-side.
    func reset(sessionId: String, keepingQuestionId id: String) {
        let keep = Self.key(sessionId: sessionId, questionId: id)
        let prefix = sessionId + "|"
        let stale = entries.keys.filter { $0.hasPrefix(prefix) && $0 != keep }
        guard !stale.isEmpty else { return }
        for key in stale { entries[key] = nil }
    }

    /// Drop every entry whose question is no longer shown anywhere. Called
    /// with the keys the current snapshot still draws; the guard keeps a
    /// no-op prune from republishing on every poll.
    func prune(keeping keys: Set<String>) {
        let stale = entries.keys.filter { !keys.contains($0) }
        guard !stale.isEmpty else { return }
        for key in stale { entries[key] = nil }
    }
}
