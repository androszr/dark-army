import Foundation

/// The cards the board draws, per row, worked out once per redraw.
///
/// `BoardView` used to ask each row's question — `board.cards(in:)`, the
/// project filter, the search, the column's order — from the empty test, the
/// four headings and the four tile grids: up to twelve full filter-and-sort
/// passes per body. This is the one pass, and it reads nothing but the board
/// and `BoardState`'s filter and query, so it is a plain testable value.
struct BoardVisible: Equatable {
    /// Every row's cards, in the column's order (`cardOrder`), after the
    /// project filter and the search. Folding is not applied here — that is
    /// `BoardState.rowFolded`'s, and a folded row still has its cards.
    var byColumn: [BoardColumn: [BoardCard]] = [:]
    /// Whether any row has a card to draw.
    var anyVisible = false
    /// Whether the project filter or the search is narrowing the board —
    /// the one condition under which the store-wide Done count stops being
    /// the number the Done heading should say.
    var narrowed = false

    /// One row's cards; empty for a row with none.
    func cards(_ column: BoardColumn) -> [BoardCard] {
        byColumn[column] ?? []
    }

    /// The number a row's heading says, or nil when it cannot yet be known.
    ///
    /// Prep, Backlog and In progress arrive whole on every frame, so their
    /// heading is simply the row's visible cards. Done does not: the frame
    /// carries a bounded preview and the rest is `DoneArchive`'s. With
    /// nothing narrowing the board the heading is the store's own count,
    /// complete before the archive lands. Under a project filter or a search
    /// that count is every project's pile — the heading that read 47 on a
    /// project holding 3 — so it counts the visible Done cards instead, and
    /// only once the archive is in hand: counted off the preview it would
    /// be a smaller wrong number, so until then it says nothing.
    func headingCount(_ column: BoardColumn, storeDone: Int,
                      archiveReady: Bool) -> Int? {
        guard column == .done else { return cards(column).count }
        guard narrowed else { return storeDone }
        return archiveReady ? cards(.done).count : nil
    }

    /// The order in the column is the priority. Project still *filters*; it
    /// does not regroup.
    @MainActor
    static func compute(board: Board, state: BoardState) -> BoardVisible {
        var out = BoardVisible()
        out.narrowed = !state.projectFilter.isEmpty
            || !state.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        for column in BoardColumn.allCases {
            // The selecting row draws its ticked cards first, so a batch
            // being gathered sits together at the top of Prep or Backlog.
            let ticked = state.selectingRow == column
                ? state.rowSelection : []
            let cards = board.cards(in: column.rawValue)
                .filter { state.showsProject($0.project) }
                .filter { state.matches($0) }
                .sorted { a, b in
                    let ta = ticked.contains(a.id), tb = ticked.contains(b.id)
                    if ta != tb { return ta }
                    return cardOrder(a, b, column: column)
                }
            out.byColumn[column] = cards
            if !cards.isEmpty { out.anyVisible = true }
        }
        return out
    }

    /// The column's order as one testable term. In `.done` an unacknowledged
    /// agent close leads — a "FINISHED · REVIEW" banner that could scroll
    /// below the fold is a banner that can be missed, which is the failure it
    /// exists to prevent — and that clause stays **first**, above every
    /// number: a high-priority Done card ahead of an unacknowledged agent
    /// close would reintroduce exactly that failure.
    ///
    /// Then the importance number, highest first, which is the store's own
    /// first ordering term restated so the two cannot disagree.
    /// `priorityValue` folds `""` and `"0"` together exactly as SQLite's
    /// `CAST(priority AS INTEGER)` does, so an unscored card sits with the
    /// zeroes at the bottom of its column.
    ///
    /// Then the ordinary terms, byte-identical to what they were: `position`,
    /// then `createdAt` (the tie-break for cards a daemon that predates
    /// `position` still sends at 0), then `id`, because Swift's sort promises
    /// no stability. A drag therefore still decides the order between cards
    /// that share a number, and only between those.
    static func cardOrder(_ a: BoardCard, _ b: BoardCard,
                          column: BoardColumn) -> Bool {
        if column == .done && a.awaitsReview != b.awaitsReview {
            return a.awaitsReview
        }
        if a.priorityValue != b.priorityValue {
            return a.priorityValue > b.priorityValue
        }
        if a.position != b.position { return a.position < b.position }
        if a.createdAt != b.createdAt { return a.createdAt < b.createdAt }
        return a.id < b.id
    }

    /// Where `BoardView` keeps the pass its `body` just made, for the row
    /// functions to read. Those functions' signatures are pinned
    /// (`test_board_rows.py`), so the value cannot ride in as a parameter; a
    /// reference the view holds, written at the top of every `body` and read
    /// by the rows that body builds, carries it instead. Never observed and
    /// never a source of truth: it holds exactly what `compute` returned for
    /// the redraw in progress.
    @MainActor
    final class Slot {
        var value = BoardVisible()
    }
}
