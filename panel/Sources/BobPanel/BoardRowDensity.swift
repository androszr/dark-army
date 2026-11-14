import Foundation

/// Which board rows draw their cards as one line each, as one pure rule.
///
/// A tile shows a card's whole summary, which is the right face for a row
/// of a handful and the wrong one for a pile: with 48 cards in Backlog,
/// deciding what is worth starting meant scrolling through paragraphs
/// (13 Nov 2026). A compact row draws every card as a single line in the
/// column's own order — importance first — and a click opens that one card
/// in place as the tile it always was, across the row's whole width.
///
/// **One choice for the whole board, made in Settings** (Board ▸ Compact
/// cards, the `board_compact` preference, on by default): every row draws
/// lines or every row draws tiles. A row-by-row switch on the headings was
/// tried the same day and taken back — a board half lines and half tiles
/// has two reading rhythms.
///
/// Foundation only. The phone draws lines too, always and with no choice,
/// by its own smaller rule (`PhoneCardLine`): there is nothing to pin.
enum BoardRowDensity {
    /// The rows that draw as lines while the choice is on: all four the
    /// daemon speaks. An id it does not speak draws tiles.
    static let rows = ["prep", "backlog", "in_progress", "done"]

    /// Drawn as lines right now, given the Settings choice.
    static func compact(_ id: String, enabled: Bool) -> Bool {
        enabled && rows.contains(id)
    }

    /// How one card is drawn.
    enum Face: Equatable {
        /// The full tile, in a row that draws tiles.
        case tile
        /// One line, in a compact row.
        case line
        /// The full tile inside a compact row: the one card a person opened.
        case opened
    }

    /// The face of one card. **One card open at a time** — `opened` is a
    /// single id, so opening another closes the first and the list never
    /// grows back into a wall. A revealed card draws opened for as long as
    /// it is revealed: a reveal that landed on a closed line would point at
    /// a title and nothing else.
    static func face(compact: Bool, card: String, opened: String?,
                     revealed: String?) -> Face {
        guard compact else { return .tile }
        return (card == opened || card == revealed) ? .opened : .line
    }

    /// The open card after a press on `card`'s line or on its opened
    /// tile's chevron: pressing the open one closes it.
    static func toggledOpen(_ card: String, opened: String?) -> String? {
        opened == card ? nil : card
    }

    /// What a press on a line does.
    enum Press: Equatable {
        /// Open or close the card in place.
        case toggle
        /// Open the card window, a double click's meaning on every tile.
        case window
    }

    /// The first click of a double click has already toggled the card by
    /// the time the second arrives, so the second opens the window rather
    /// than toggling back. A key press reports no clicks and toggles.
    static func press(clickCount: Int) -> Press {
        clickCount >= 2 ? .window : .toggle
    }
}
