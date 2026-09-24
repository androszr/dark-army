import Foundation

/// What to do with Prepare's opinion about which project a card belongs to:
/// whether it names a listed project the picker is not already on, and — if
/// so — which project to offer as the way back once it has been applied.
///
/// Two pure statics rather than conditions inside the composer, so the
/// decision is testable without rendering a view. The daemon already narrows
/// its answer to the intersection of the board's `projects` and the roots
/// dispatch would accept; this is the second, independent guard — a newer
/// daemon talking to a panel holding an older snapshot can still name a root
/// this panel cannot select, and moving the picker onto a root it cannot show
/// is worse than leaving it alone.
enum ProjectSuggestion {

    /// The suggestion, applied: the picker's new value and, where there is
    /// one, the listed project to offer going back to.
    struct Decision: Equatable {
        /// The picker's new value.
        let apply: BoardProject
        /// The project the picker held before, to name on the one revert
        /// button — `nil` when there is nothing to go back to.
        let revertTo: BoardProject?
    }

    /// The listed project the suggestion names, or `nil` for "do nothing".
    ///
    /// Nothing for an empty `root` (no opinion), for a `root` the draft is
    /// already filed against (there is nothing to change), or for a `root` no
    /// listed project matches (unselectable). Matching is on the root, which
    /// is what the picker's tags are, never on the name.
    static func offer(root: String, current: String,
                      projects: [BoardProject]) -> BoardProject? {
        let wanted = root.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !wanted.isEmpty else { return nil }
        guard wanted != current.trimmingCharacters(in: .whitespacesAndNewlines)
        else { return nil }
        return projects.first { $0.root == wanted }
    }

    /// Apply, and offer a way back. `nil` exactly where `offer` is `nil`;
    /// otherwise the project to move the picker onto and the listed project
    /// the picker held before. `revertTo` is `nil` for an empty `current`
    /// (the picker's `—`) **and** for a `current` no listed project matches:
    /// a revert button has to name a project, and a root the picker cannot
    /// show has no name to put on it.
    static func decide(root: String, current: String,
                       projects: [BoardProject]) -> Decision? {
        guard let apply = offer(root: root, current: current,
                                projects: projects) else { return nil }
        let held = current.trimmingCharacters(in: .whitespacesAndNewlines)
        let revertTo = held.isEmpty
            ? nil : projects.first { $0.root == held }
        return Decision(apply: apply, revertTo: revertTo)
    }
}
