import Foundation

/// Which of the board's four rows are folded, as one pure rule.
///
/// The board is four stacked rows — Prep, Backlog, In progress, Done — and
/// each folds to its heading line. What is remembered is **not** the folded
/// set but the rows whose state *differs from the default* (`flipped`): an
/// absent record means the defaults (Done folded, the rest open), and a
/// person who opens Done and folds Backlog is stored as `["backlog",
/// "done"]`. That keeps the stored value small and lets the default change
/// later without rewriting anybody's habit file.
///
/// Two rules borrowed from the rail (`PanelView.isCollapsed`): **a search
/// overrides a fold**, because a match hidden in a folded row is
/// indistinguishable from no match; and **a reveal is a search** — the
/// panel's `reveal(_:)` writes the `#card` query, so the revealed card's row
/// draws open with no reveal-specific fold code, and the folds come back
/// when the search is cleared.
///
/// Foundation only, and the rows are plain strings rather than
/// `BoardColumn`: the phone carries a byte-equal copy of this block at the
/// bottom of its `BoardView.swift` (`host/tests/test_board_rows.py` pins
/// the two equal), and the phone has no `BoardColumn`.
enum BoardRowFold {
    /// The four rows, top to bottom. The column ids the daemon speaks.
    static let rows = ["prep", "backlog", "in_progress", "done"]

    /// Done starts folded; the three working rows start open.
    static func defaultFolded(_ id: String) -> Bool {
        id == "done"
    }

    /// Folded right now, given the rows a person has flipped away from the
    /// default. XOR: flipping a row that starts folded opens it.
    static func folded(_ id: String, flipped: Set<String>) -> Bool {
        defaultFolded(id) != flipped.contains(id)
    }

    /// What is drawn: a search overrides every fold, so a matching card is
    /// never hidden behind a heading.
    static func drawnFolded(_ id: String, flipped: Set<String>, searching: Bool) -> Bool {
        !searching && folded(id, flipped: flipped)
    }

    /// The set after a press on this row's heading — symmetric difference
    /// on `id`. An id that names no row is a no-op, so a stray value can
    /// never be written into the habit file.
    static func toggled(_ id: String, flipped: Set<String>) -> Set<String> {
        guard rows.contains(id) else { return flipped }
        var out = flipped
        if out.contains(id) {
            out.remove(id)
        } else {
            out.insert(id)
        }
        return out
    }

    /// A stored array back to the flipped set. Members of `rows` only —
    /// anything else is dropped, and nil (never saved) is the defaults.
    static func decode(_ raw: [String]?) -> Set<String> {
        Set((raw ?? []).filter { rows.contains($0) })
    }

    /// The flipped set as an array in `rows` order, so the stored value is
    /// stable across runs. Empty for the defaults.
    static func encode(_ flipped: Set<String>) -> [String] {
        rows.filter { flipped.contains($0) }
    }

    /// The comma-joined spelling the phone's `@AppStorage` string holds.
    static func decode(joined: String) -> Set<String> {
        decode(joined.split(separator: ",").map(String.init))
    }

    static func encode(joined flipped: Set<String>) -> String {
        encode(flipped).joined(separator: ",")
    }
}
