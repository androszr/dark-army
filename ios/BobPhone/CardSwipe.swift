import Foundation

/// The Board tab's sideways swipe on a Prep or Backlog card: which verb the
/// first button wears, the words of the buttons,
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

    /// The swipe's buttons send on the first press (4 Oct 2026): a person who
    /// slid the card aside and pressed a verb has already said it twice. The
    /// card screen keeps its own armed confirmations.
    static let startLabel = "START"
    static let refineLabel = "Refine"
    static let deleteLabel = "Delete"
    static let deleteSpoken = "Delete card"
    static let deleteIcon = "trash"

    // MARK: - The slide

    /// How far a drag travels before it counts as a drag, so a tap never
    /// registers as one.
    static let minimumDrag: CGFloat = 20
    /// How far past rest a drag must travel to open or close the row.
    static let revealTravel: CGFloat = 56
    /// The width of the buttons the card slides off.
    static let actionsWidth: CGFloat = 148
    /// A flick this fast (points a second) opens or shuts the row whatever
    /// its travel, the way the system's own swipe rows answer a quick swipe.
    static let flickSpeed: CGFloat = 300

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

    /// `revealAfter(translation:revealed:)` with the finger's speed at the
    /// lift: a leftward flick opens, a rightward one shuts, a slow drag
    /// reads the travel alone.
    static func revealAfter(translation: CGFloat, velocity: CGFloat, revealed: Bool) -> Bool {
        if velocity <= -flickSpeed && translation < 0 { return true }
        if velocity >= flickSpeed && translation > 0 { return false }
        return revealAfter(translation: translation, revealed: revealed)
    }
}
