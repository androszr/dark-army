import Foundation

/// The rules that decide what the workspace's two panes show and how a press
/// moves between them — written as plain functions so `RailLayoutTests` can
/// table them and the view cannot quietly drift.
///
/// Four decisions live here: whether the wide left pane is the board or the
/// selected agent's detail (`leftPane`); which rung Escape takes
/// (`escapeRung`); whether a click on a row selects or unselects it
/// (`selectionAfterTap`); whether a changed row list is allowed to re-aim the
/// selection (`reaimAfterListChange`). Two more are the arithmetic behind the
/// message/terminal divider in `AgentDetailPane` (`splitHeights`,
/// `draggedFraction`). Nothing here reads a window.
enum RailLayout {
    /// What the wide pane draws.
    enum LeftPane: Equatable {
        case board
        case detail(String)
        /// The board with Mission Control's terminal column beside it —
        /// the Comm tab. The board stays mounted and merely narrows.
        case mission
        /// History, drawn across the board's pane. The board stays mounted
        /// underneath, the same way the agent's detail does.
        case history
    }

    /// History takes the board's place whatever is selected. The detail
    /// takes it only on the Agents tab with a row selected. The Comm tab
    /// draws the board beside Mission Control's terminal column. Every
    /// other tab shows the board alone.
    static func leftPane(tab: PanelView.Tab, selected: String?) -> LeftPane {
        if tab == .history { return .history }
        if tab == .comm { return .mission }
        if tab == .agents, let selected { return .detail(selected) }
        return .board
    }

    /// The rungs Escape climbs down, innermost first.
    enum EscapeRung: Equatable {
        /// Give up the text box the caret is in (reply, terminal input,
        /// filter) — or clear a filter that still holds text.
        case endEditing
        /// Close the run written out under the ledger.
        case closeHistoryRun
        /// Close the day the ledger is describing.
        case closeHistoryDay
        /// Leave History and bring the board back.
        case leaveHistory
        /// Close the selected agent's detail and bring the board back.
        case deselect
        /// Pop the project drill-in.
        case back
        /// Hide the panel.
        case hide
    }

    /// A focused text box is given up first — a picture under a caret must
    /// never close from beneath it — then an open History run, then a
    /// chosen History day, then History itself, then the detail, then the
    /// drill, then the panel. The History flags default off so a caller
    /// that does not know about them still compiles.
    static func escapeRung(editing: Bool, filterActive: Bool,
                           detailOpen: Bool, drilledIn: Bool,
                           historyRun: Bool = false,
                           historyDay: Bool = false,
                           historyOpen: Bool = false) -> EscapeRung {
        if editing || filterActive { return .endEditing }
        if historyRun { return .closeHistoryRun }
        if historyDay { return .closeHistoryDay }
        if historyOpen { return .leaveHistory }
        if detailOpen { return .deselect }
        if drilledIn { return .back }
        return .hide
    }

    /// A click on the selected row unselects it (the board comes back); a
    /// click anywhere else selects that row.
    static func selectionAfterTap(selected: String?, tapped: String) -> String? {
        tapped == selected ? nil : tapped
    }

    /// Only a *pruned* selection re-aims: a row that left the list takes the
    /// highlight with it and the top waiter is picked instead. An empty
    /// selection — the reader closed the detail on purpose — stays empty
    /// until they open the panel again or pick a row.
    static func reaimAfterListChange(selected: String?, listed: Set<String>) -> Bool {
        guard let selected else { return false }
        return !listed.contains(selected)
    }

    /// Whether the first snapshot after a deliberate open may aim the
    /// selection at whoever needs you. `hide()` stops the SSE client, so the
    /// open's own aim (`FocusRouter.shown`) runs against a frozen snapshot
    /// and a waiter that appeared while the panel was hidden is only listed
    /// by the attach frame ~100ms later — `pending` is the one-shot that
    /// carries the aim over to it. It fires only where that first aim found
    /// nothing (`selected` is nil: a selection already made — by the stale
    /// aim, a banner's focus request or a click — is left alone) and where no
    /// card reveal is holding the board (`revealedForCard`: the reverse jump
    /// and the `card ⌗` chip both ask for the *board*, and an aim would draw
    /// the top waiter's detail over the card they just revealed).
    static func aimAfterSnapshot(pending: Bool, selected: String?,
                                 revealedForCard: Bool) -> Bool {
        pending && selected == nil && !revealedForCard
    }

    /// Split `total` between the message region (top) and the terminal
    /// (bottom), the bottom taking `fraction`. Both halves are held at
    /// `minEach` where `total` allows; below `2 * minEach` the height is
    /// split evenly. Always sums to `total`.
    static func splitHeights(total: CGFloat, fraction: CGFloat,
                             minEach: CGFloat) -> (top: CGFloat, bottom: CGFloat) {
        let total = max(0, total)
        if total < 2 * minEach {
            let half = total / 2
            return (total - half, half)
        }
        let bottom = min(max(total * fraction, minEach), total - minEach)
        return (total - bottom, bottom)
    }

    /// The terminal's share after a drag of `delta` points (positive is
    /// downward, which shrinks the terminal) from the share the drag began
    /// on. Clamped to 0.2…0.8 so neither region can be dragged away; a
    /// `total` of zero or less leaves the share alone.
    static func draggedFraction(start: CGFloat, delta: CGFloat,
                                total: CGFloat) -> CGFloat {
        guard total > 0 else { return start }
        return min(0.8, max(0.2, start - delta / total))
    }

    /// The divider's share of the height, in `splitHeights`' terms, is what
    /// `draggedFraction` and `steppedFraction` both move.
    static let fractionStep: CGFloat = 0.1

    /// The terminal's share after one keyboard / assistive step: `up`
    /// grows the terminal by `fractionStep`, `down` shrinks it, under the
    /// same 0.2…0.8 clamp the drag has.
    static func steppedFraction(start: CGFloat, up: Bool) -> CGFloat {
        let next = up ? start + fractionStep : start - fractionStep
        return min(0.8, max(0.2, next))
    }
}
