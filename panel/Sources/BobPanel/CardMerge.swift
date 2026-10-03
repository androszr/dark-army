import Foundation

/// Which review-and-merge controls a Done card offers, and the words on them
/// — the same rule on the Mac and the phone.
///
/// `plans/2026-10-03-review-and-merge-done-card.md`. A Done card whose branch
/// the daemon remembers shows **MERGE** (armed, then confirmed), **Fix** where
/// a merge stopped on a conflict or failed checks, and **Run review**; the
/// daemon decides everything else (the hand-check, the folder, the main
/// checkout) at the press and re-checks it when each verb fires, so these
/// functions only hide a control that cannot succeed. `mergeState` is the
/// card's published `merge_state` (`""`, `merging`, `blocked`, `conflict`,
/// `checks_failed`, `merged`); `manualDue` is the daemon's own
/// `manual_check_due`. **Pure**: Foundation only, column ids as the daemon's
/// plain strings. Copied byte-equal into `ios/BobPhone/CardMerge.swift` and
/// pinned by `host/tests/test_card_merge_rule.py`; tabled in
/// `panel/Tests/BobPanelTests/BoardMergeVerbTests.swift`.
enum CardMerge {
    /// The two states the Fix press answers.
    static let fixStates: Set<String> = ["conflict", "checks_failed"]
    /// The states in which MERGE is offered: nothing yet, or a stop.
    static let openStates: Set<String> = ["", "blocked", "conflict", "checks_failed"]

    /// MERGE: a Done card with a recorded branch, not merged, not mid-merge,
    /// no hand-check due, **and the daemon's own fact** (`merge_offered` on
    /// the card: the gate without its busy rungs, which alone knows a Failed
    /// hand-check). A daemon that publishes no such key reads as `false`, so
    /// nothing is offered against an older one.
    static func offered(column: String, branch: String, mergeState: String,
                        manualDue: Bool, daemonOffers: Bool) -> Bool {
        daemonOffers && column == "done" && !branch.isEmpty && !manualDue
            && openStates.contains(mergeState)
    }

    /// Fix: only where a merge stopped on a conflict or failed checks, and
    /// the daemon would still take the card (`daemonOffers`).
    static func fixOffered(mergeState: String, daemonOffers: Bool) -> Bool {
        daemonOffers && fixStates.contains(mergeState)
    }

    /// Run review: a Done card with a recorded branch, no review running,
    /// no hand-check due and the daemon's fact.
    static func reviewOffered(column: String, branch: String,
                              reviewRunning: Bool, manualDue: Bool,
                              daemonOffers: Bool) -> Bool {
        daemonOffers && column == "done" && !branch.isEmpty && !reviewRunning
            && !manualDue
    }

    /// The MERGE button's words: inert `MERGING…` while the daemon works,
    /// the confirming question once armed.
    static func mergeLabel(mergeState: String, armed: Bool) -> String {
        if mergeState == "merging" { return "MERGING…" }
        return armed ? "Merge into main?" : "MERGE"
    }

    /// The Fix button's words.
    static func fixLabel(armed: Bool) -> String {
        armed ? "Fix the merge?" : "Fix"
    }

    /// The review line under the card: running, or the verdict with the
    /// stale suffix where the branch moved since the version judged. `nil`
    /// for no review at all.
    static func verdictLine(verdict: String, running: Bool, current: Bool) -> String? {
        if running { return "Review running…" }
        let word: String
        switch verdict {
        case "ship": word = "SHIP"
        case "stop": word = "STOP"
        default: return nil
        }
        return "Review: " + word + (current ? "" : " — reviewed at an earlier version")
    }
}
