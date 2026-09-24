import AppKit
import SwiftUI

/// The board's four columns, in the order they are read.
///
/// **There was a fourth once before, and it was Ready.** It sat between Backlog
/// and In progress and meant "somebody has decided this should happen" — which
/// is what Backlog already means, so nobody could say which of the two a given
/// card belonged in, and the answer to "what is the difference?" was a
/// paragraph about dispatch mechanics rather than about work. What Ready was
/// actually doing was gating Start: a card had to be moved there first, and
/// that move was the deliberation. The gesture now carries that itself —
/// dragging a card into **In progress** starts the assistant it names — so the
/// column that existed to be a waiting room for a button is gone with the
/// button's ceremony.
///
/// **Prep is not Ready returned.** Ready had no verb of its own and no exit
/// but a hand; Prep has both — Refine dispatches a planning session onto the
/// card, and the plan that session attaches (`dark_army_attach_plan`) is what moves
/// the card out, into Backlog. So each pile answers a different one-line
/// question: Prep is "written down, not yet turned into a plan" (`planPath`
/// empty), Backlog is "planned, waiting to be picked up".
enum BoardColumn: String, CaseIterable, Identifiable {
    case prep, backlog, inProgress = "in_progress", done

    var id: String { rawValue }

    var title: String {
        switch self {
        case .prep: return "Prep"
        case .backlog: return "Backlog"
        case .inProgress: return "In progress"
        case .done: return "Done"
        }
    }

    /// One line under the heading saying what putting a card here *means*. Kept
    /// on screen rather than in a help sheet: the question this board could not
    /// answer was "what is the difference between these two columns", and a
    /// board that needs a manual to be read has already failed.
    ///
    /// Done's line used to promise that a person was the only thing that ever
    /// put a card in this column, which stopped being true the day an assistant
    /// could declare its own card finished. The promise the caption still has to
    /// keep is the honest half: a card gets here because *somebody said so*, and
    /// the card itself names who.
    var caption: String {
        switch self {
        case .prep: return "just written — refine it into a plan before anything starts"
        case .backlog: return "planned, nobody working on it yet"
        case .inProgress: return "drop here to start the assistant"
        case .done: return "finished — an assistant's close waits at the top until you press Reviewed"
        }
    }

    /// Whether landing here must forget the card's session.
    ///
    /// Backlog and Prep are columns a card is *started* from, and
    /// `dispatch.guard` refuses any card that still carries a `session_id`. A
    /// card put back here by a plain `board_update` keeps the id of the run
    /// that already ended and becomes permanently unstartable with nothing on
    /// screen able to undo it.
    /// See `BoardCardView.move(to:)`, which is the single place that acts on it.
    var clearsSessionLink: Bool {
        self == .backlog || self == .prep
    }
}

/// What the board's search bar draws right now. Pure so the test target can
/// pin it without a view: the clear chip belongs to the text you typed and
/// to nothing else.
enum BoardSearchBar {
    struct Chrome: Equatable {
        var showClear: Bool
    }

    static func chrome(queryEmpty: Bool) -> Chrome {
        Chrome(showClear: !queryEmpty)
    }

    /// The chip's words. Bright and bordered, because the faint glyph it
    /// replaced was easy to miss.
    static let clearLabel = "× CLEAR"

    /// The cap that keeps the chip against the search box. A `TextField` in
    /// an `HStack` is width-greedy: uncapped, it carries the chip all the way
    /// to the divider and recreates the adjacency this bar exists to remove.
    static let fieldMaxWidth: CGFloat = 420
}
