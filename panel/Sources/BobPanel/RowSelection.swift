import Foundation

/// The rule behind a board row's select mode: which cards may be ticked, which
/// may join a selection already started, and what the row's one batch button
/// says.
///
/// **Keyed on the row, not on the verb.** A select mode belongs to a
/// `BoardColumn`; the verb it fires is that row's own. Prep refines several
/// cards in one planning session (`board_refine_batch`,
/// `plans/2026-09-25-batch-refine-prep-cards.md`); Backlog starts several
/// planned cards in one session, worked one at a time (`board_start_batch`,
/// `plans/2026-09-25-batch-implement-backlog-cards.md`). Every other row
/// ticks nothing, so a select control there is absent rather than inert.
///
/// Foundation only, so the whole rule is a table in `RowSelectionTests`. The
/// daemon re-checks every term against the store at the moment of the press;
/// this only hides a tick that could not succeed.
enum RowSelection {
    /// A batch is at least two cards: one card is the row's ordinary verb.
    static let minimum = 2

    /// Whether this card may be ticked in `column`'s select mode at all.
    ///
    /// `.prep` is today's Refine button, term for term — the tile's own
    /// `canRefine` delegates here, so the tick and the button can never
    /// disagree about a card.
    static func tickable(_ column: BoardColumn, card: BoardCard,
                         chrome: BoardChrome) -> Bool {
        switch column {
        case .prep:
            return chrome.dispatchEnabled
                && card.column == BoardColumn.prep.rawValue
                && card.planPath.isEmpty
                && !card.isScout
                && !card.isRefining
                && card.sessionId.isEmpty
                && !card.isDispatching
        case .backlog:
            // Start's own terms plus what a batch needs: a plan (the batch
            // asks no plan-gate confirmation), not a scout, not queued and
            // not already waiting in another batch.
            return chrome.dispatchEnabled
                && card.column == BoardColumn.backlog.rawValue
                && !card.planPath.isEmpty
                && !card.isScout
                && !card.tool.isEmpty
                && card.sessionId.isEmpty
                && !card.isDispatching
                && !card.isRefining
                && card.queueState.isEmpty
                && card.batch == nil
        case .inProgress, .done:
            return false
        }
    }

    /// Whether this card may join a selection that already holds `selected`:
    /// tickable, and in the same project folder as the first card ticked —
    /// one session works in one folder.
    static func admits(_ column: BoardColumn, card: BoardCard,
                       given selected: [BoardCard], chrome: BoardChrome) -> Bool {
        guard tickable(column, card: card, chrome: chrome) else { return false }
        guard let first = selected.first(where: { $0.id != card.id }) else {
            return true
        }
        return card.root == first.root
    }

    /// The label on the row's batch button while it is selecting.
    static func verb(_ column: BoardColumn, count: Int) -> String {
        switch column {
        case .prep:
            return "REFINE \(count) TOGETHER"
        case .backlog:
            return "START \(count) TOGETHER"
        case .inProgress, .done:
            return ""
        }
    }

    /// The armed label of a row's batch button, where the row arms before
    /// it fires (Backlog: a batch Start opens a terminal and spends money,
    /// the single START's argument). Empty where the row does not arm.
    static func confirm(_ column: BoardColumn, count: Int) -> String {
        switch column {
        case .backlog:
            return "Really start \(count)?"
        case .prep, .inProgress, .done:
            return ""
        }
    }

    /// Whether a row offers select mode at all: some verb, and at least
    /// `minimum` cards that could be ticked right now.
    static func offersSelect(_ column: BoardColumn, cards: [BoardCard],
                             chrome: BoardChrome) -> Bool {
        cards.filter { tickable(column, card: $0, chrome: chrome) }.count
            >= minimum
    }
}
