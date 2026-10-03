import Foundation

/// The Board tab's sideways swipe on a Prep or Backlog card: which verb the
/// first button wears, the words of the buttons and their two confirmations,
/// and the arithmetic of the slide. Every rule here is the card screen's own
/// (`PhoneCardDetailView`), moved or read, never copied: `canStart` is the
/// card screen's Start gate, read by both surfaces, and `primary` is the two
/// arms of `CardSections.nextAction` this view can reach, pinned equal to it
/// and to the Mac's `CardActionWeight.primary`
/// (`host/tests/test_phone_card_swipe.py`).
///
/// Foundation only, so `swiftc` can run it from the host suite.
/// Contract: `docs/phone-contract.md`, *A Prep or Backlog card is swiped on
/// the Board tab*.
enum PhoneCardSwipe {
    /// The rows whose cards swipe. In progress and Done answer from the card
    /// screen alone.
    static let rows = ["prep", "backlog"]

    /// The card screen's Start gate, moved verbatim so the screen and the
    /// swipe read one rule. `isBusy` is inlined: a Start or a Refine already
    /// dispatching or live.
    static func canStart(card: BoardCard, dispatchEnabled: Bool) -> Bool {
        dispatchEnabled
            && (card.column == "prep" || card.column == "backlog")
            && !card.tool.isEmpty
            && card.queueState != "queued"
            && card.sessionId.isEmpty
            && card.linkState != "dispatching"
            && card.linkState != "live"
            && card.refineState != "dispatching"
            && card.refineState != "live"
            // A card waiting its turn in a batch, or dragged out of the
            // line, is started by the batch or by Leave batch on the Mac,
            // not by this button. The Mac refuses a press made off a stale
            // picture with its batch-waiting refusal, which arrives on the
            // receipt and is drawn as any other refusal.
            && !card.holdsBatchMark
    }

    /// The one verb the first button offers.
    enum Primary: Equatable {
        case refine
        case start
    }

    /// Prep: Refine when the card may be refined, else Start when it may be
    /// started; Backlog: Start when it may be started; nil elsewhere.
    static func primary(column: String, canRefine: Bool, canStart: Bool) -> Primary? {
        switch column {
        case "prep":
            if canRefine { return .refine }
            return canStart ? .start : nil
        case "backlog":
            return canStart ? .start : nil
        default:
            return nil
        }
    }

    // MARK: - Words

    /// The card screen's `startLabel` words, minus the changed-plan one (the
    /// card screen is the one place that confirmation is answered).
    static func startLabel(armed: Bool, unplanned: Bool) -> String {
        if !armed { return "START" }
        return unplanned ? "Start unplanned?" : "Really start?"
    }

    static func refineLabel(armed: Bool) -> String {
        armed ? "Really refine?" : "Refine"
    }

    static let moreLabel = "⋯"
    static let moreSpoken = "More actions"
    static let deleteRow = "Delete card"
    static let deleteTitle = "Delete this card?"
    static let deleteMessage = "Are you sure? There is no undo."

    // MARK: - The slide

    /// How far a drag travels before it counts as a drag, so a tap never
    /// registers as one.
    static let minimumDrag: CGFloat = 20
    /// How far past rest a drag must travel to open or close the row.
    static let revealTravel: CGFloat = 56
    /// The width of the buttons the card slides off.
    static let actionsWidth: CGFloat = 148

    /// A drag that moves more sideways than up or down is a swipe; the rest
    /// belongs to the scroll view.
    static func dominantHorizontal(dx: CGFloat, dy: CGFloat) -> Bool {
        abs(dx) > abs(dy)
    }

    /// Where the card sits during a drag: its rest (open or shut) plus the
    /// finger's travel, never past the buttons and never past shut.
    static func offset(forTranslation translation: CGFloat, revealed: Bool) -> CGFloat {
        let rest: CGFloat = revealed ? -actionsWidth : 0
        return min(0, max(-actionsWidth, rest + translation))
    }

    /// Whether the row is open when the finger lifts: open past
    /// `-revealTravel`, shut past `+revealTravel`, otherwise as it was.
    static func revealAfter(translation: CGFloat, revealed: Bool) -> Bool {
        if translation <= -revealTravel { return true }
        if translation >= revealTravel { return false }
        return revealed
    }
}
