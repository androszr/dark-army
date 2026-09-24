import AppKit
import SwiftUI

/// What the pointer is over during a drag. One slot, four kinds — so a
/// hover cannot be a column move and a dependency mark at once. Innermost
/// wins at drop time via `BoardState.takeDrop`, not via a platform promise.
/// `card` is the blocked-by gesture. `heading` is the row's pinned heading:
/// it resolves to the same move `column` does, but it is **its own value**,
/// because the heading and the row's tiles are two views writing one shared
/// slot, and `noteHover`'s ownership test (a leave clears the slot only
/// while it still holds the leaver's value) cannot tell a late leave from
/// one apart from an enter on the other when both write the same value —
/// dragging from the heading down into the row fired tiles-enter then
/// heading-leave, and the leave wiped the tint the tiles had just set.
enum BoardDropTarget: Equatable {
    case column(BoardColumn)
    case heading(BoardColumn)
    case gap(BoardColumn, beforeId: String)
    case card(String)

    /// The row a target sits in, whichever of its views the pointer is
    /// over — what both the heading and the tiles read to draw the row's
    /// tint, so hovering either lights the whole row as one target.
    var row: BoardColumn? {
        switch self {
        case .column(let column), .heading(let column):
            return column
        case .gap, .card:
            return nil
        }
    }
}

/// A drop into In progress held back by the plan gate, waiting on the
/// confirmation dialog. An arm slot is the wrong shape for a drag — the
/// gesture is already released when the question arises, and `armed`'s whole
/// design is a first press aiming a second — so the move is *held* instead:
/// nothing was sent, the card visibly stays put (the daemon moves columns and
/// has not been asked), and confirm replays it through the same
/// drop-resolution code with the skip flag set.
struct PendingMove: Equatable {
    let cardId: String
    let column: String
    /// nil for a plain column drop; a gap drop carries its slot ("" appends).
    let beforeId: String?
    /// Whether confirming starts an assistant (launcher on) or merely moves
    /// the card (launcher off, which the dialog's button label says).
    let dispatches: Bool
    /// Which of the plan gate's two rungs raised this. `false` — the default,
    /// and every held-before-sending route — is "no plan at all"; `true` is
    /// "the approved plan has been edited since", which the dialog has to say
    /// in its own words, because confirming "Start without a plan" on a card
    /// that has one would be a button lying about what pressing it agrees to.
    var planChanged: Bool = false
    /// Whether confirming should spawn Dark Army's own terminal. Carried
    /// through the plan-gate dialog so START HERE does not fall back to
    /// the editor on the replay. Default false — every drop and ordinary
    /// Start leave the preference in charge.
    var ownTerminal: Bool = false
}
