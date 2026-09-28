import SwiftUI

/// Rules the settings rows share, keyed on ids and kinds — never a list of rows.
enum SettingsControls {
    /// A pick run of up to this many choices is a segmented control; a longer
    /// one is a pop-up.
    static let segmentedLimit = 4

    /// `custom:away-days:<device>:<days>`: a phone's away-access lengths,
    /// drawn as one segmented control ticked from `SettingsRow.checked`.
    static let awayDaysPrefix = "custom:away-days:"

    /// `custom:bot-access:<device>:<side>:<mode>`: one side of the bot's
    /// access, drawn as its own segmented control per side.
    static let botAccessPrefix = "custom:bot-access:"

    /// What VoiceOver says for one segment: its title, except for the bot's
    /// access segments, whose tooltip names the side ("Read access: 1
    /// hour") so the two runs of identical lengths are told apart.
    static func spokenName(_ row: SettingsRow) -> String {
        row.id.hasPrefix(botAccessPrefix) && !row.tooltip.isEmpty
            ? row.tooltip : row.title
    }

    /// The id with its last `:` component removed — what a run of one
    /// segmented control shares.
    static func runKey(_ id: String) -> String {
        guard let cut = id.lastIndex(of: ":") else { return id }
        return String(id[..<cut])
    }

    /// Facts that are sentences rather than values: drawn in the prose face,
    /// every other fact in the fixed-width one.
    static func isProseFact(_ rowId: String) -> Bool {
        switch rowId {
        case "info:enrollment", "info:devices", "info:lan-exposure",
             "info:away-needs-lan", "info:macwhisper":
            return true
        default:
            return rowId.hasPrefix("info:pack-self:")
        }
    }

    /// Destructive verbs drawn in the alarm ink on this window. Additive: the
    /// title already says what the press does, and the tree's own `danger`
    /// flag (the kill switch alone) is honoured too.
    static func drawsDanger(_ row: SettingsRow) -> Bool {
        row.danger
            || row.id.hasPrefix("custom:unpair:")
            || row.id.hasPrefix("custom:unenrol:")
    }

    static func isOnePickRun(_ rows: [SettingsRow]) -> Bool {
        guard let first = rows.first, case .pick(let action, _, _) = first.kind else {
            return false
        }
        return rows.allSatisfy {
            if case .pick(let other, _, _) = $0.kind { return other == action }
            return false
        }
    }

    static func isCustomChoices(_ rows: [SettingsRow]) -> Bool {
        !rows.isEmpty && rows.allSatisfy {
            if case .custom = $0.kind { return true }
            return false
        }
    }

    /// A group's entries as drawn lines: a pick run becomes one line (headed
    /// by its submenu where it has one), a submenu of verbs one line of
    /// buttons, a phone's away lengths one segmented line; dividers go —
    /// the card draws hairlines between every line.
    static func lines(_ entries: [SettingsEntry]) -> [SettingsLine] {
        let items = SettingsSearch.items(entries.filter { !$0.isDivider })
        var out: [SettingsLine] = []
        var pendingHeading: SettingsEntry?
        var index = 0
        while index < items.count {
            switch items[index] {
            case .picks(let action, let run):
                out.append(.picks(heading: pendingHeading, action: action, entries: run))
                pendingHeading = nil
                index += 1
            case .single(let entry):
                if let heading = pendingHeading {
                    out.append(.heading(heading))
                    pendingHeading = nil
                }
                if case .submenu(_, let nested) = entry.row.kind {
                    if isOnePickRun(nested) {
                        pendingHeading = entry
                        index += 1
                        continue
                    }
                    if isCustomChoices(nested) {
                        let ids = Set(nested.map(\.id))
                        var run: [SettingsEntry] = []
                        var next = index + 1
                        while next < items.count, case .single(let candidate) = items[next],
                              ids.contains(candidate.id) {
                            run.append(candidate)
                            next += 1
                        }
                        out.append(.choices(heading: entry, entries: run, segmented: false))
                        index = next
                        continue
                    }
                    out.append(.heading(entry))
                    index += 1
                    continue
                }
                if entry.id.hasPrefix(awayDaysPrefix) {
                    var run = [entry]
                    var next = index + 1
                    while next < items.count, case .single(let candidate) = items[next],
                          candidate.id.hasPrefix(awayDaysPrefix) {
                        run.append(candidate)
                        next += 1
                    }
                    out.append(.choices(heading: nil, entries: run, segmented: true))
                    index = next
                    continue
                }
                if entry.id.hasPrefix(botAccessPrefix) {
                    // One run per side: the fold keys on the id minus its
                    // mode, so Read and Write are two controls, never one
                    // of ten with two ticks.
                    let key = runKey(entry.id)
                    var run = [entry]
                    var next = index + 1
                    while next < items.count, case .single(let candidate) = items[next],
                          candidate.id.hasPrefix(botAccessPrefix),
                          runKey(candidate.id) == key {
                        run.append(candidate)
                        next += 1
                    }
                    out.append(.choices(heading: nil, entries: run, segmented: true))
                    index = next
                    continue
                }
                out.append(.entry(entry))
                index += 1
            }
        }
        if let heading = pendingHeading { out.append(.heading(heading)) }
        return out
    }

    /// A heading drawn for a whole group (Panel size's own title and hover
    /// text over its pick run, or Pipeline's sub-heading inside Board's card).
    /// A drawing aid carrying the group's own id and words — not a row.
    static func groupHeading(_ group: SettingsGroup) -> SettingsEntry {
        SettingsEntry(row: SettingsRow(
            id: group.id, title: group.title,
            kind: .submenu(title: group.title, rows: []), tooltip: group.tooltip))
    }
}

/// One drawn line of a settings card.
enum SettingsLine: Identifiable, Equatable {
    /// A switch, a verb, a fact.
    case entry(SettingsEntry)
    /// A nested block's own heading (a phone, a project's pack).
    case heading(SettingsEntry)
    /// A pick run, headed by its submenu where it has one.
    case picks(heading: SettingsEntry?, action: String, entries: [SettingsEntry])
    /// Verbs that stand in a choice, ticked from `SettingsRow.checked`.
    case choices(heading: SettingsEntry?, entries: [SettingsEntry], segmented: Bool)

    var id: String {
        switch self {
        case .entry(let entry): return entry.id
        case .heading(let entry): return "heading:" + entry.id
        case .picks(let heading, let action, let entries):
            return heading?.id ?? "picks:\(action):\(entries.first?.id ?? "")"
        case .choices(let heading, let entries, _):
            return heading?.id ?? "choices:\(entries.first?.id ?? "")"
        }
    }

    /// Every entry the line draws — the anchors a search jump scrolls to.
    var entryIds: [String] {
        switch self {
        case .entry(let entry), .heading(let entry): return [entry.id]
        case .picks(let heading, _, let entries), .choices(let heading, let entries, _):
            return (heading.map { [$0.id] } ?? []) + entries.map(\.id)
        }
    }
}

// MARK: - The row shape

/// Every settings row: the label, its explanation always drawn under it (the
/// hover text stays too), and the control on the right. `dimmed` is drawn at
/// 0.45 and never disables anything — a dependent stays pressable.
struct SettingsRowShell<Control: View>: View {
    var title: String
    var description: String
    var tooltip: String
    var indent: Bool
    var dimmed: Bool
    var highlighted: Bool
    var onFlashEnd: () -> Void
    let control: Control

    init(title: String = "", description: String = "", tooltip: String = "",
         indent: Bool = false, dimmed: Bool = false, highlighted: Bool = false,
         onFlashEnd: @escaping () -> Void = {},
         @ViewBuilder control: () -> Control) {
        self.title = title
        self.description = description
        self.tooltip = tooltip
        self.indent = indent
        self.dimmed = dimmed
        self.highlighted = highlighted
        self.onFlashEnd = onFlashEnd
        self.control = control()
    }

    var body: some View {
        HStack(alignment: .center, spacing: 16) {
            if title.isEmpty && description.isEmpty {
                // A verb with nothing to explain: the button alone, leading.
                control
                Spacer(minLength: 0)
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    if !title.isEmpty {
                        Text(title)
                            .font(Theme.prose(14))
                            .foregroundStyle(Theme.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !description.isEmpty {
                        Text(description)
                            .font(Theme.prose(12.5))
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                control
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .padding(.leading, indent ? 20 : 0)
        .contentShape(Rectangle())
        .help(tooltip)
        .opacity(dimmed ? 0.45 : 1)
        .modifier(SettingsFlash(active: highlighted, onEnd: onFlashEnd))
    }
}

/// The search jump's mark: an accent wash fading over 1.2 s, or — with
/// Reduce Motion on — a still 1pt outline for the same 1.2 s. Either way it
/// ends by calling `onEnd`, which clears the highlight it was drawn for.
struct SettingsFlash: ViewModifier {
    let active: Bool
    let onEnd: () -> Void
    static let seconds: Double = 1.2

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var wash: Double = 0
    @State private var outlined = false

    func body(content: Content) -> some View {
        content
            .background(Theme.accent.opacity(wash))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.controlRadius)
                    .stroke(Theme.accent, lineWidth: outlined ? 1 : 0))
            .task(id: active) {
                guard active else {
                    wash = 0
                    outlined = false
                    return
                }
                if reduceMotion {
                    outlined = true
                } else {
                    wash = 0.18
                    try? await Task.sleep(nanoseconds: 30_000_000)
                    Motion.animate(.easeOut(duration: Self.seconds), reduced: reduceMotion) { wash = 0 }
                }
                try? await Task.sleep(nanoseconds: UInt64(Self.seconds * 1_000_000_000))
                outlined = false
                wash = 0
                // A cancelled flash (the row went, or another jump moved the
                // mark) clears nothing: the mark may be somebody else's now.
                guard !Task.isCancelled else { return }
                onEnd()
            }
    }
}

extension View {
    /// Anchors for `ids` plus the flash when the jump's mark is one of them;
    /// the view clears the mark when its flash ends. For every drawn target a
    /// search hit can land on that is not a `SettingsLineView` (which does
    /// both itself). `SettingsWindowState.jump(to:)` clears the mark too, so a
    /// target that is never drawn cannot strand it.
    func settingsMarked(_ ids: [String], state: SettingsWindowState) -> some View {
        settingsAnchors(ids).modifier(SettingsFlash(
            active: state.highlightedRowId.map { ids.contains($0) } ?? false,
            onEnd: {
                if let mark = state.highlightedRowId, ids.contains(mark) {
                    state.highlightedRowId = nil
                }
            }))
    }

    /// Zero-size anchors, one per entry id, so `ScrollViewReader.scrollTo`
    /// finds a row by the id of any entry the line draws.
    func settingsAnchors(_ ids: [String]) -> some View {
        background(
            VStack(spacing: 0) {
                ForEach(ids, id: \.self) { id in
                    Color.clear.frame(width: 0, height: 0).id(id)
                }
            }
            .accessibilityHidden(true))
    }
}

/// A group's card: `Theme.surface`, a 1pt `Theme.line` edge, hairlines
/// between the lines it is handed.
struct SettingsCard<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.surface)
        .clipShape(RoundedRectangle(cornerRadius: Theme.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius)
            .stroke(Theme.line, lineWidth: 1))
    }
}

/// The hairline between two lines of a card.
struct SettingsHairline: View {
    var body: some View {
        Rectangle().fill(Theme.line).frame(height: 1)
    }
}

/// A group's uppercase label over its card.
struct SettingsGroupHeading: View {
    let title: String

    var body: some View {
        Text(title.uppercased())
            .font(Theme.prose(11, weight: .semibold))
            .foregroundStyle(Theme.muted)
            .accessibilityAddTraits(.isHeader)
    }
}

// MARK: - Controls

/// A switch. The binding's setter sends the wanted state and writes the
/// optimistic overlay; the getter reads the overlay ahead of the model.
struct SettingsToggleRow: View {
    let row: SettingsRow
    let action: String
    let modelOn: Bool
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState
    var indent = false
    var dimmed = false
    var highlighted = false
    var onFlashEnd: () -> Void = {}

    var body: some View {
        SettingsRowShell(title: row.title, description: row.tooltip, tooltip: row.tooltip,
                         indent: indent, dimmed: dimmed || row.disabled,
                         highlighted: highlighted, onFlashEnd: onFlashEnd) {
            Toggle("", isOn: Binding(
                get: { actions.toggleIsOn(action: action, model: modelOn) },
                set: { actions.sendToggle(action: action, wanted: $0) }))
                .labelsHidden()
                .toggleStyle(.switch)
                .tint(Theme.accent)
                .disabled(row.disabled)
                .accessibilityLabel(row.title)
        }
    }
}

/// One bordered strip, one segment per choice; the chosen one raised and in
/// the accent. For short pick runs and a phone's away lengths.
struct SettingsSegmented: View {
    let entries: [SettingsEntry]
    let selectedId: String?
    let press: (SettingsEntry) -> Void

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                if index > 0 {
                    Rectangle().fill(Theme.control).frame(width: 1)
                }
                segment(entry, on: entry.id == selectedId)
            }
        }
        .fixedSize()
        .background(Theme.field)
        .clipShape(RoundedRectangle(cornerRadius: Theme.controlRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
            .stroke(Theme.control, lineWidth: 1))
    }

    private func segment(_ entry: SettingsEntry, on: Bool) -> some View {
        Button {
            press(entry)
        } label: {
            Text(entry.row.title)
                .font(Theme.prose(12.5, weight: on ? .semibold : .regular))
                .foregroundStyle(on ? Theme.accent : Theme.text)
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(on ? Theme.raised : Color.clear)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable(!entry.row.disabled)
        .disabled(entry.row.disabled)
        .help(entry.row.tooltip)
        .accessibilityLabel(SettingsControls.spokenName(entry.row))
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// A pop-up over one models-table cell (or a pick run too long to segment).
/// The selection is driven from the cell's ticked choice and a press sends
/// only a *different* choice, so a snapshot re-render never re-sends.
struct SettingsPopUp: View {
    let cell: SettingsModelsTable.Cell
    let actions: SettingsActions

    private var spoken: String { cell.choices.first?.row.tooltip ?? "" }

    var body: some View {
        Picker("", selection: Binding(
            get: { cell.selected?.id ?? "" },
            set: { chosen in
                guard chosen != cell.selected?.id,
                      let choice = cell.choices.first(where: { $0.id == chosen }),
                      case .pick(_, let value, _) = choice.row.kind else { return }
                actions.sendPick(action: cell.action, value: value)
            })) {
            if cell.selected == nil {
                Text("—").tag("")
            }
            ForEach(cell.choices) { choice in
                if choice.row.title == SettingsMenuModel.inheritLabel {
                    Text(choice.row.title).italic().foregroundStyle(Theme.muted)
                        .tag(choice.id)
                } else {
                    Text(choice.row.title).tag(choice.id)
                }
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(minWidth: SettingsModelsGridMetrics.cellMinWidth,
               maxWidth: SettingsModelsGridMetrics.cellMaxWidth)
        .help(spoken)
        .accessibilityLabel(spoken)
    }
}

/// A verb. `Theme.raised` on a `Theme.control` edge, so it never looks like an
/// unchosen option; `danger` is alarm ink on a clear fill; `armed` fills the
/// alarm (the title already says what the second press does); `checked`
/// takes the accent edge.
struct SettingsBorderedButton: View {
    let title: String
    var danger = false
    var armed = false
    var checked = false
    var disabled = false
    var fullWidth = false
    let press: () -> Void

    private var fill: Color {
        if armed { return Theme.danger }
        return danger ? Color.clear : Theme.raised
    }

    private var ink: Color {
        if armed { return Theme.canvas }
        return danger ? Theme.danger : Theme.text
    }

    private var edge: Color {
        if armed || danger { return Theme.danger }
        return checked ? Theme.accent : Theme.control
    }

    var body: some View {
        Button(action: press) {
            Text(title)
                .font(Theme.prose(13, weight: .medium))
                .foregroundStyle(ink)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: !fullWidth, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 5)
                .frame(maxWidth: fullWidth ? .infinity : nil)
                .background(RoundedRectangle(cornerRadius: Theme.controlRadius).fill(fill))
                .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
                    .stroke(edge, lineWidth: 1))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable(!disabled)
        .disabled(disabled)
        .opacity(disabled ? 0.45 : 1)
        .accessibilityAddTraits(checked ? .isSelected : [])
    }
}

/// A fact: a path, an id, the build line — fixed-width and selectable so it
/// can be copied. A sentence (`SettingsControls.isProseFact`) is prose.
struct SettingsFactRow: View {
    let row: SettingsRow
    var indent = false
    var dimmed = false
    var highlighted = false
    var onFlashEnd: () -> Void = {}

    var body: some View {
        Text(row.title)
            .font(SettingsControls.isProseFact(row.id) ? Theme.prose(12.5) : Theme.mono(12.5))
            .foregroundStyle(Theme.muted)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 9)
            .padding(.horizontal, 14)
            .padding(.leading, indent ? 20 : 0)
            .help(row.tooltip)
            .opacity(dimmed ? 0.45 : 1)
            .modifier(SettingsFlash(active: highlighted, onEnd: onFlashEnd))
    }
}

/// The search box. Takes the caret on appear and whenever `focusRequest`
/// steps, because the window is reused across presents.
struct SettingsSearchField: View {
    @ObservedObject var state: SettingsWindowState
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 11))
                .foregroundStyle(Theme.muted)
                .accessibilityHidden(true)
            TextField("Search settings", text: $state.query)
                .textFieldStyle(.plain)
                .font(Theme.prose(13))
                .foregroundStyle(Theme.text)
                .focused($focused)
            if !state.query.isEmpty {
                Button {
                    state.query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.muted)
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("Clear the search")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Theme.well)
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
            .stroke(focused ? Theme.accent : Theme.control, lineWidth: 1))
        .onAppear { focused = true }
        .onChange(of: state.focusRequest) { _, _ in focused = true }
    }
}

// MARK: - Drawing a line

/// One entry that is not part of a run, drawn by kind: a switch is a switch,
/// a verb is a bordered button beside its explanation, a fact is a line of
/// text, a nested block's heading is a label with its explanation.
struct SettingsEntryView: View {
    let entry: SettingsEntry
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState
    var indent = false
    var dimmed = false
    var highlighted = false
    var onFlashEnd: () -> Void = {}

    private var row: SettingsRow { entry.row }

    var body: some View {
        switch row.kind {
        case .toggle(let action, let isOn):
            SettingsToggleRow(row: row, action: action, modelOn: isOn, actions: actions,
                              state: state, indent: indent, dimmed: dimmed,
                              highlighted: highlighted, onFlashEnd: onFlashEnd)
        case .send(let action, let value):
            verb { actions.sendAction(action: action, value: value) }
        case .custom(let custom):
            verb { actions.handleCustom(custom) }
        case .info:
            SettingsFactRow(row: row, indent: indent, dimmed: dimmed,
                            highlighted: highlighted, onFlashEnd: onFlashEnd)
        case .divider:
            EmptyView()
        case .submenu:
            SettingsRowShell(title: row.title, description: row.tooltip, tooltip: row.tooltip,
                             indent: indent, dimmed: dimmed || row.disabled,
                             highlighted: highlighted, onFlashEnd: onFlashEnd) {
                EmptyView()
            }
        case .pick(let action, let value, let selected):
            // A lone pick (a run of one) is a one-segment control.
            SettingsRowShell(indent: indent, dimmed: dimmed, highlighted: highlighted,
                             onFlashEnd: onFlashEnd) {
                SettingsSegmented(entries: [entry], selectedId: selected ? entry.id : nil) { _ in
                    actions.sendPick(action: action, value: value)
                }
            }
        }
    }

    private func verb(_ press: @escaping () -> Void) -> some View {
        SettingsRowShell(description: row.tooltip, tooltip: row.tooltip,
                         indent: indent, dimmed: dimmed,
                         highlighted: highlighted, onFlashEnd: onFlashEnd) {
            SettingsBorderedButton(
                title: row.title,
                danger: SettingsControls.drawsDanger(row),
                armed: (row.id == SettingsMenuModel.killRowId && state.killArmed)
                    || (row.id == SettingsMenuModel.deskTokenRowId && state.deskTokenArmed),
                checked: row.checked,
                disabled: row.disabled,
                press: press)
        }
    }
}

/// One line of a card: an entry, a heading, a pick run, or a run of verbs
/// standing in a choice. Carries an anchor for every entry it draws and
/// flashes when a search jump lands on any of them.
struct SettingsLineView: View {
    let line: SettingsLine
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState
    var indent = false
    var dimmed = false

    private var highlighted: Bool {
        guard let mark = state.highlightedRowId else { return false }
        return line.entryIds.contains(mark)
    }

    private func clearFlash() {
        if let mark = state.highlightedRowId, line.entryIds.contains(mark) {
            state.highlightedRowId = nil
        }
    }

    var body: some View {
        content.settingsAnchors(line.entryIds)
    }

    @ViewBuilder
    private var content: some View {
        switch line {
        case .entry(let entry):
            SettingsEntryView(entry: entry, actions: actions, state: state,
                              indent: indent, dimmed: dimmed,
                              highlighted: highlighted, onFlashEnd: clearFlash)
        case .heading(let entry):
            SettingsRowShell(title: entry.row.title, description: entry.row.tooltip,
                             tooltip: entry.row.tooltip, indent: indent, dimmed: dimmed,
                             highlighted: highlighted, onFlashEnd: clearFlash) {
                EmptyView()
            }
        case .picks(let heading, let action, let run):
            SettingsRowShell(title: heading?.row.title ?? "",
                             description: heading?.row.tooltip ?? "",
                             tooltip: heading?.row.tooltip ?? "",
                             indent: indent, dimmed: dimmed,
                             highlighted: highlighted, onFlashEnd: clearFlash) {
                pickControl(action: action, run: run)
            }
        case .choices(let heading, let run, let segmented):
            SettingsRowShell(title: heading?.row.title ?? "",
                             description: heading?.row.tooltip ?? "",
                             tooltip: heading?.row.tooltip ?? "",
                             indent: indent, dimmed: dimmed,
                             highlighted: highlighted, onFlashEnd: clearFlash) {
                if segmented {
                    SettingsSegmented(entries: run,
                                      selectedId: run.first(where: \.row.checked)?.id) { entry in
                        if case .custom(let custom) = entry.row.kind {
                            actions.handleCustom(custom)
                        }
                    }
                } else {
                    HStack(spacing: 6) {
                        ForEach(run) { entry in
                            SettingsBorderedButton(title: entry.row.title,
                                                   checked: entry.row.checked,
                                                   disabled: entry.row.disabled) {
                                if case .custom(let custom) = entry.row.kind {
                                    actions.handleCustom(custom)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func pickControl(action: String, run: [SettingsEntry]) -> some View {
        let selected = run.first { entry in
            if case .pick(_, _, let on) = entry.row.kind { return on }
            return false
        }
        if run.count <= SettingsControls.segmentedLimit {
            SettingsSegmented(entries: run, selectedId: selected?.id) { entry in
                if case .pick(_, let value, _) = entry.row.kind {
                    actions.sendPick(action: action, value: value)
                }
            }
        } else {
            SettingsPopUp(cell: SettingsModelsTable.Cell(action: action, choices: run,
                                                         selected: selected),
                          actions: actions)
        }
    }
}

/// A card of lines with hairlines between them. `indentIds` / `dimIds` name
/// the lines drawn indented or dimmed (by `SettingsLine.id`).
struct SettingsLinesCard: View {
    let lines: [SettingsLine]
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState
    var indentIds: Set<String> = []
    var dimIds: Set<String> = []

    var body: some View {
        SettingsCard {
            ForEach(Array(lines.enumerated()), id: \.element.id) { index, line in
                if index > 0 { SettingsHairline() }
                SettingsLineView(line: line, actions: actions, state: state,
                                 indent: indentIds.contains(line.id),
                                 dimmed: dimIds.contains(line.id))
            }
        }
    }
}
