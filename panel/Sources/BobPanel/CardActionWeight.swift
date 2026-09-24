import Foundation

/// Which one verb a board card outlines, chosen by its column — with one
/// kind-aware exception: a scout in Prep outlines START, because it has no
/// plan to refine.
///
/// `plans/2026-09-20-one-primary-button-per-card.md`. Two rules: a column
/// names exactly one primary verb — Refine in Prep, START in Backlog, Done in
/// In progress, none in Done — and a card whose column's primary is absent
/// (a Backlog card that already has a session, a Prep card already planned)
/// draws **no** primary rather than promoting another: `isPrimary` is false
/// for every other verb, so nothing else ever asks for the outline. HERE is
/// never primary. Foundation only, column ids as the daemon's plain strings,
/// so the phone compiles the same block with no panel column type in reach.
enum CardActionWeight {
    enum Verb: String { case refine, start, startHere, done }

    /// The one verb a column outlines. `nil` for done and any id the
    /// daemon does not speak.
    static func primary(column: String, kind: String = "") -> Verb? {
        switch column {
        case "prep": return kind == "scout" ? .start : .refine
        case "backlog": return .start
        case "in_progress": return .done
        default: return nil
        }
    }

    static func isPrimary(_ verb: Verb, column: String, kind: String = "") -> Bool {
        primary(column: column, kind: kind) == verb
    }
}
