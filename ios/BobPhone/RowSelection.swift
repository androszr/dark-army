import Foundation

/// The phone Board tab's select mode on the Prep row and the Backlog row:
/// which cards may be ticked for a batch Refine or a batch Start, and the
/// words each row's batch button wears. The Prep row's press is queued; the
/// Backlog row's is synchronous (`docs/phone-contract.md`).
///
/// The Mac's twin is `panel/Sources/BobPanel/RowSelection.swift`. This is a
/// **typed twin, not a byte copy**: the Mac's rule is typed on `BoardColumn`
/// and `BoardChrome`, which the phone does not have, so the column here is
/// the daemon's own id as a plain string and the card is the phone's
/// `BoardCard`. The parity is the table, not the bytes: the same terms, in
/// both test suites (`host/tests/test_phone_batch_refine.py`,
/// `ios/BobPhoneTests/RowSelectionTests.swift`).
///
/// `tickable("prep", …)` is also what the card screen's Refine button reads
/// (`CardDetailView.canRefine`), so a card the board lets you tick is
/// exactly a card whose own screen offers Refine, and the two can never
/// disagree. The Mac re-checks every card under its own Refine guards at
/// the press; this only hides a tick that could not succeed.
///
/// Foundation only, so `swiftc` can run it from the host suite.
enum PhoneRowSelection {
    /// The fewest ticks the batch button fires on — one card is the card
    /// screen's own Refine.
    static let minimum = 2

    /// The one scope the batch press, its queue mark and its note are keyed
    /// on (the press queue's `enqueue(scope:)`), so the three never drift.
    static let scope = "batch:refine"

    /// The Backlog row's synchronous press's `post` scope, so a double-tap
    /// while the press is out is `stillSendingRefusal`; no queue mark and no
    /// note ride it.
    static let startScope = "batch:start"

    /// How one card's box is drawn in select mode.
    enum Tick: Equatable {
        /// Selected for the batch.
        case ticked
        /// May be ticked now.
        case open
        /// Cannot be ticked: not refinable, or another project than the
        /// first tick.
        case barred
    }

    /// Whether a card may be ticked in this row. Prep's answer is the card
    /// screen's Refine rule, term for term: Dark Army's launcher on, the
    /// card in Prep with no plan, not a scout, no refinement dispatching
    /// or live, no session and no start dispatching. Backlog's is the Mac's
    /// batch Start rule, the panel's `RowSelection.tickable(.backlog, …)`
    /// terms: the launcher on, the card in Backlog with a plan and an
    /// assistant, not a scout, never started, not refining, not queued and
    /// not already in a batch. `linkState.isEmpty` stands where the panel
    /// says `!isDispatching`: a live or ended link is a started card. Every
    /// other row answers false.
    static func tickable(_ column: String, card: BoardCard,
                         dispatchEnabled: Bool) -> Bool {
        switch column {
        case "prep":
            return dispatchEnabled
                && card.column == "prep"
                && card.planPath.isEmpty
                && !card.isScout
                && card.refineState != "dispatching"
                && card.refineState != "live"
                && card.sessionId.isEmpty
                && card.linkState != "dispatching"
        case "backlog":
            return dispatchEnabled
                && card.column == "backlog"
                && !card.planPath.isEmpty
                && !card.isScout
                && !card.tool.isEmpty
                && card.sessionId.isEmpty
                && card.linkState.isEmpty
                && !card.isRefining
                && card.queueState.isEmpty
                && card.batch == nil
        default:
            return false
        }
    }

    /// Whether a card may join a selection already made: tickable, and in
    /// the same project folder (`root`) as the first ticked card that is
    /// not this one. One planning session runs in one folder.
    static func admits(_ column: String, card: BoardCard, given selected: [BoardCard],
                       dispatchEnabled: Bool) -> Bool {
        guard tickable(column, card: card, dispatchEnabled: dispatchEnabled) else {
            return false
        }
        guard let first = selected.first(where: { $0.id != card.id }) else {
            return true
        }
        return card.root == first.root
    }

    /// The assistant every ticked card names, or `""` when they disagree
    /// (or none is ticked) — the tile the batch's assistant row draws
    /// selected. A batch runs in one session, so the Mac refuses a mixed set.
    static func sharedTool(_ selected: [BoardCard]) -> String {
        guard let first = selected.first?.tool,
              selected.allSatisfy({ $0.tool == first }) else { return "" }
        return first
    }

    /// The ticked cards a pick of `tool` has to write, in tick order: only
    /// those naming something else, so a card already on it is not rewritten
    /// (a tool write also clears the card's model).
    static func retoolIds(_ selected: [BoardCard], to tool: String) -> [String] {
        selected.filter { $0.tool != tool }.map(\.id)
    }

    /// The batch button's resting words.
    static func verb(_ column: String, count: Int) -> String {
        switch column {
        case "prep": return "REFINE \(count) TOGETHER"
        case "backlog": return "START \(count) TOGETHER"
        default: return ""
        }
    }

    /// The batch button once armed — the second press sends.
    static func armedVerb(_ column: String, count: Int) -> String {
        switch column {
        case "prep": return "Really refine \(count)?"
        case "backlog": return "Really start \(count)?"
        default: return ""
        }
    }

    /// Whether the row offers Select at all: at least `minimum` of the
    /// cards drawn could be ticked. Absent, never inert.
    static func offersSelect(_ column: String, cards: [BoardCard],
                             dispatchEnabled: Bool) -> Bool {
        var found = 0
        for card in cards where tickable(column, card: card,
                                         dispatchEnabled: dispatchEnabled) {
            found += 1
            if found >= minimum { return true }
        }
        return false
    }

    /// The selected ids in the board's own order, never tick order — the
    /// list the press sends and the arm is keyed on, computed the same way
    /// on both presses.
    static func orderedIds(selected: [String], in cards: [BoardCard]) -> [String] {
        let chosen = Set(selected)
        return cards.map(\.id).filter { chosen.contains($0) }
    }

    /// The selection after a new board: every id no longer listed in the
    /// row, or no longer tickable, is dropped; tick order is kept.
    static func prune(_ column: String, selected: [String], in cards: [BoardCard],
                      dispatchEnabled: Bool) -> [String] {
        let still = Set(cards.filter {
            tickable(column, card: $0, dispatchEnabled: dispatchEnabled)
        }.map(\.id))
        return selected.filter { still.contains($0) }
    }
}
