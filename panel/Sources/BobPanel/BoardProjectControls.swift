import SwiftUI

/// The two per-project controls the board's row headings carry when the
/// project picker has named **one** project: the `RUN n/m` capacity picker on
/// the In progress heading and START PROJECT on the Backlog heading. Both
/// used to live on the rail's pipeline band, which drew the same cards the
/// board already draws; the cards went, the two controls moved here.
///
/// **Absent, never inert.** With zero or several projects ticked there is no
/// folder to aim a press at, so neither control is drawn; a heading with a
/// dial that sets nothing would be a promise this app cannot keep.
enum BoardProjectControls {
    /// `RUN 2/3`: the count is what is being worked on, the limit the daemon's
    /// own published number; `max(1,)` is the same floor the decode applies,
    /// restated here because a heading is the one place a zero would be read
    /// as a rule rather than as a missing field.
    static func runHeading(count: Int, limit: Int) -> String {
        "RUN \(count)/\(max(1, limit))"
    }

    /// How many agents may work at once in the project these cards belong
    /// to. **The daemon's own answer, picked up rather than computed**: every
    /// card carries the resolved figure, the first one that has it wins, and
    /// the scalar is used only where every card reads 0. Nothing here knows
    /// the override map, the root canonicaliser or the clamp.
    static func resolvedLimit(cards: [BoardCard], fallback: Int) -> Int {
        for card in cards where card.parallelLimit > 0 {
            return card.parallelLimit
        }
        return max(1, fallback)
    }

    /// The folder a press would aim at: the first of these cards that names
    /// one. Empty means nothing to aim at, which is what makes the control
    /// absent rather than inert.
    static func projectRoot(cards: [BoardCard]) -> String {
        for card in cards where !card.root.isEmpty { return card.root }
        return ""
    }

    /// Whether the RUN heading is drawn at all. Its job is spare capacity and
    /// why the queue waits; a project with nothing running or queued has
    /// neither question to answer.
    static func showsRunHeading(running: Int, queued: Int) -> Bool {
        running > 0 || queued > 0
    }
}

/// The `RUN n/m` heading as a picker: Default, then 1…4. Choosing a rung
/// sends `set_board_parallel_root` on the panel's stdin/stdout channel —
/// `preferences.json` is the menu-bar app's to write.
struct RunLimitPicker: View {
    let count: Int
    let limit: Int
    let defaultLimit: Int
    let overridden: Bool
    let onSetLimit: (Int) -> Void

    var body: some View {
        Menu {
            Button {
                onSetLimit(0)
            } label: {
                Self.rungLabel("Default (\(max(1, defaultLimit)))",
                               ticked: !overridden)
            }
            ForEach(1...4, id: \.self) { rung in
                Button {
                    onSetLimit(rung)
                } label: {
                    Self.rungLabel("\(rung)", ticked: overridden && rung == limit)
                }
            }
        } label: {
            Text(BoardProjectControls.runHeading(count: count, limit: limit))
                .font(Theme.mono(10, weight: .medium))
                .tracking(0.6)
                .foregroundStyle(Theme.phosphor)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .clickable()
        .help("How many agents may work at once in this project")
    }

    /// The tick is drawn by *omitting* the image rather than by asking for
    /// an empty symbol name: `Image(systemName: "")` is not a symbol, and
    /// AppKit draws a hole where the glyph should be.
    @ViewBuilder
    private static func rungLabel(_ title: String, ticked: Bool) -> some View {
        if ticked {
            Label(title, systemImage: "checkmark")
        } else {
            Text(title)
        }
    }
}

/// One press for the whole project, with the daemon's report of the last
/// press drawn verbatim beneath it.
struct StartProjectButton: View {
    let report: String
    let fire: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Button(action: fire) {
                Text("START PROJECT")
                    .font(Theme.mono(10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .clickable()
            .help("Start every planned card in this project, one after another")
            if !report.isEmpty {
                Text(report)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A row's select mode, drawn under its heading's caption —
/// `StartProjectButton`'s shape. Not selecting: one `SELECT` button (the
/// caller draws the control only where the row has at least
/// `RowSelection.minimum` tickable cards and Dark Army may start sessions).
/// Selecting: the row's batch verb (`RowSelection.verb`, disabled below the
/// minimum with a line saying why), `CANCEL`, and the daemon's words for a
/// refused press drawn verbatim beneath.
///
/// No tooltips (the panel's `.help` sites are a closed allowlist,
/// `test_panel_tooltips.py`): what a tooltip would say is spoken as an
/// accessibility hint, and the one thing a sighted person needs — why the
/// verb is dim — is a line on screen.
struct RowBatchControl: View {
    let column: BoardColumn
    let count: Int
    let selecting: Bool
    let refusal: String
    let onSelect: () -> Void
    let onCancel: () -> Void
    let onFire: () -> Void
    /// The row's batch verb has been pressed once and waits for its
    /// confirmation (Backlog). The label then says what a second press does
    /// (`RowSelection.confirm`). Default off, so a row that fires at once
    /// passes nothing.
    var armed: Bool = false

    private var ready: Bool { count >= RowSelection.minimum }

    private var fireLabel: String {
        let confirm = RowSelection.confirm(column, count: count)
        return armed && !confirm.isEmpty ? confirm
                                         : RowSelection.verb(column, count: count)
    }

    private var fireHint: String {
        column == .backlog
            ? "One session for every ticked card, worked one at a time; each still closes as its own card"
            : "One planning session for every ticked card; each still gets its own plan"
    }

    private var selectHint: String {
        column == .backlog
            ? "Tick several planned cards and start them in one session"
            : "Tick several cards and refine them in one session"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                if selecting {
                    Button(action: onFire) {
                        Self.label(fireLabel, lit: ready)
                    }
                    .buttonStyle(.plain)
                    .disabled(!ready)
                    .clickable(ready)
                    .accessibilityHint(fireHint)
                    Button(action: onCancel) {
                        Self.label("CANCEL", lit: false)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    .accessibilityHint("Leaves select mode; nothing is sent")
                } else {
                    Button(action: onSelect) {
                        Self.label("SELECT", lit: true)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    .accessibilityHint(selectHint)
                }
            }
            if selecting && !ready {
                Text("tick at least \(RowSelection.minimum) cards of one project")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if selecting && !refusal.isEmpty {
                Text(refusal)
                    .font(Theme.mono(10))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private static func label(_ text: String, lit: Bool) -> some View {
        Text(text)
            .font(Theme.mono(10, weight: .semibold))
            .tracking(0.8)
            .foregroundStyle(lit ? Theme.phosphor : Theme.dim)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
            .contentShape(Rectangle())
    }
}
