import AppKit
import SwiftUI

/// The settings page: a search field over headed groups, every group and every
/// entry read off `SettingsMenuModel.rows(...)` through `SettingsSearch`. Kind
/// → control: a switch is a switch, a pick is an exclusive chip row, an action
/// is a chip button, a fact is a line of text, a divider is a hairline, and a
/// disabled row is dimmed rather than missing. `Theme` tokens throughout.
struct SettingsWindowRoot: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: SettingsWindowState
    let actions: SettingsActions

    var body: some View {
        let groups = SettingsSearch.filter(SettingsSearch.groups(actions.rows()),
                                           query: state.query)
        VStack(spacing: 0) {
            SettingsSearchField(state: state)
            Rectangle().fill(Theme.rule).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if groups.isEmpty {
                        Text("Nothing matches that")
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.faint)
                    }
                    ForEach(groups) { group in
                        SettingsGroupView(group: group, actions: actions, state: state)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        // Escape: clear the query first, then close — the panel's own
        // precedent for a window's cancel action. Zero-size so it draws nothing.
        .background(
            Button("") { state.escape() }
                .keyboardShortcut(.cancelAction)
                .frame(width: 0, height: 0)
                .opacity(0)
                .accessibilityHidden(true)
        )
    }
}

/// The search box. Takes the caret on appear and whenever `focusRequest`
/// steps, because the window is reused across presents.
struct SettingsSearchField: View {
    @ObservedObject var state: SettingsWindowState
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            Text(">")
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphor)
            TextField("search settings", text: $state.query)
                .textFieldStyle(.plain)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphorBright)
                .focused($focused)
            if !state.query.isEmpty {
                Button {
                    state.query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.dim)
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("Clear the search")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(Theme.well)
        .onAppear { focused = true }
        .onChange(of: state.focusRequest) { _, _ in focused = true }
    }
}

/// One headed group and its entries.
struct SettingsGroupView: View {
    let group: SettingsGroup
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(group.title.uppercased())
                .font(Theme.mono(11, weight: .semibold))
                .foregroundStyle(Theme.dim)
            ForEach(SettingsSearch.items(SettingsSearch.visible(
                group.entries, open: state.openBlocks, query: state.query))) { item in
                switch item {
                case .single(let entry):
                    SettingsEntryView(entry: entry, actions: actions, state: state)
                        .padding(.leading, entry.block.isEmpty ? 0 : 14)
                case .picks(let action, let entries):
                    SettingsPickRow(action: action, entries: entries, actions: actions)
                        .padding(.leading, (entries.first?.block.isEmpty ?? true) ? 0 : 14)
                }
            }
        }
    }
}

/// One non-pick entry, drawn by kind.
struct SettingsEntryView: View {
    let entry: SettingsEntry
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    private var row: SettingsRow { entry.row }

    var body: some View {
        Group {
            switch row.kind {
            case .toggle(let action, let isOn):
                SettingsToggleRow(row: row, action: action, modelOn: isOn,
                                  actions: actions, state: state)
            case .send(let action, let value):
                SettingsActionRow(row: row) {
                    actions.sendAction(action: action, value: value)
                }
            case .custom(let custom):
                SettingsActionRow(row: row) {
                    actions.handleCustom(custom)
                }
            case .info:
                SettingsInfoRow(row: row)
            case .divider:
                SettingsRule()
            case .submenu:
                SettingsBlockHeading(
                    row: row,
                    folded: SettingsSearch.foldable(row)
                        ? !state.openBlocks.contains(row.id) : nil,
                    toggle: { state.toggleBlock(row.id) })
            case .pick:
                // Runs of picks are drawn by `SettingsPickRow`; a lone one
                // reaching here (a filtered survivor) draws as a single chip.
                SettingsPickRow(action: pickAction(row.kind), entries: [entry],
                                actions: actions)
            }
        }
    }
}

private func pickAction(_ kind: SettingsRow.Kind) -> String {
    if case .pick(let action, _, _) = kind { return action }
    return ""
}

/// A switch. The binding's setter sends the wanted state and writes the
/// optimistic overlay; the getter reads the overlay ahead of the model.
struct SettingsToggleRow: View {
    let row: SettingsRow
    let action: String
    let modelOn: Bool
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    var body: some View {
        HStack(spacing: 10) {
            Text(row.title)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
            Spacer(minLength: 8)
            Toggle("", isOn: Binding(
                get: { actions.toggleIsOn(action: action, model: modelOn) },
                set: { actions.sendToggle(action: action, wanted: $0) }))
                .labelsHidden()
                .toggleStyle(.switch)
                .tint(Theme.phosphor)
                .accessibilityLabel(row.title)
        }
        .help(row.tooltip)
        .disabled(row.disabled)
        .opacity(row.disabled ? 0.45 : 1)
    }
}

/// An exclusive row of chips, one per `.pick` entry sharing an action.
/// Drawn through `WrapLayout`, not an `HStack`: the Agent models runs are
/// up to thirteen chips wide and a 460pt window has no horizontal scroll,
/// so a run that does not fit wraps rather than truncating every title.
struct SettingsPickRow: View {
    let action: String
    let entries: [SettingsEntry]
    let actions: SettingsActions

    var body: some View {
        WrapLayout(spacing: 6, lineSpacing: 6) {
            ForEach(entries) { entry in
                if case .pick(_, let value, let selected) = entry.row.kind {
                    SettingsChoiceChip(title: entry.row.title, on: selected,
                                       disabled: entry.row.disabled) {
                        actions.sendPick(action: action, value: value)
                    }
                    .help(entry.row.tooltip)
                }
            }
        }
    }
}

/// A `.send` or `.custom` row: one chip button.
struct SettingsActionRow: View {
    let row: SettingsRow
    let press: () -> Void

    var body: some View {
        SettingsChoiceChip(title: row.title, on: false, disabled: row.disabled,
                           danger: row.danger, action: press)
            .help(row.tooltip)
    }
}

/// `ProjectSwitchChip`'s shape and tokens, with a disabled state.
struct SettingsChoiceChip: View {
    let title: String
    let on: Bool
    var disabled = false
    /// A destructive verb: alarm ink and an alarm edge. The title says what
    /// the press does either way — this only makes it hard to miss.
    var danger = false
    let action: () -> Void

    private var ink: Color {
        if danger { return Theme.alarm }
        return on ? Theme.phosphor : Theme.faint
    }

    private var edge: Color {
        if danger { return Theme.alarm }
        return on ? Theme.hair : Theme.rule
    }

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(Theme.mono(11, weight: on ? .semibold : .regular))
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: Theme.corner)
                        .fill(on ? Theme.card : Color.clear)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.corner)
                        .stroke(edge)
                )
                .foregroundStyle(ink)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable(!disabled)
        .disabled(disabled)
        .opacity(disabled ? 0.45 : 1)
    }
}

/// A fact: the build line, a project root, a device id. Selectable so it can
/// be copied.
struct SettingsInfoRow: View {
    let row: SettingsRow

    var body: some View {
        Text(row.title)
            .font(Theme.mono(11))
            .foregroundStyle(Theme.faint)
            .textSelection(.enabled)
            .help(row.tooltip)
    }
}

/// A nested submenu's heading inside a group — a project, a phone, the agent
/// dial — with its tooltip on hover. `folded` non-nil makes it a disclosure
/// (`SettingsSearch.foldable`): a chevron says which way it is, and a press
/// toggles the block under it. The glyph is never the only signal — a closed
/// block simply has nothing under its heading.
struct SettingsBlockHeading: View {
    let row: SettingsRow
    var folded: Bool? = nil
    var toggle: () -> Void = {}

    var body: some View {
        if let folded {
            Button(action: toggle) {
                HStack(spacing: 6) {
                    Text(folded ? "▸" : "▾")
                        .font(Theme.mono(11, weight: .semibold))
                        .foregroundStyle(Theme.dim)
                    label
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .clickable()
            .accessibilityLabel(row.title)
            .accessibilityValue(folded ? "collapsed" : "expanded")
        } else {
            label
        }
    }

    private var label: some View {
        Text(row.title)
            .font(Theme.mono(11, weight: .semibold))
            .foregroundStyle(row.disabled ? Theme.faint : Theme.phosphor)
            .padding(.top, 4)
            .help(row.tooltip)
            .opacity(row.disabled ? 0.45 : 1)
    }
}

struct SettingsRule: View {
    var body: some View {
        Rectangle().fill(Theme.rule).frame(height: 1)
    }
}
