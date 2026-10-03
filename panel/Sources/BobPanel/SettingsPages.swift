import SwiftUI

/// One section's page: its title and lede, then its groups as cards. Every
/// entry drawn comes out of the page's groups — `SettingsSearch.groups` over
/// `SettingsMenuModel.rows` — and `rows` (the same tree) is read only to split
/// Projects into its list and detail. Dependents of a switch are indented and
/// dimmed on the switch's *drawn* state, and never disabled.
struct SettingsPageView: View {
    let page: SettingsPage
    let rows: [SettingsRow]
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(page.title)
                            .font(Theme.prose(20, weight: .semibold))
                            .foregroundStyle(Theme.text)
                            .accessibilityAddTraits(.isHeader)
                        Text(page.id.subtitle)
                            .font(Theme.prose(13))
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    pageBody
                }
                .padding(SettingsWindowMetrics.pagePadding)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // Keyed on the jump, not the mark: a second jump to the row still
            // marked scrolls again. `initial` covers a page switched in by it.
            .onChange(of: state.jumpSerial, initial: true) { _, _ in
                guard let mark = state.highlightedRowId else { return }
                // One turn later, so a page that has just switched in is laid
                // out before it is scrolled.
                DispatchQueue.main.async {
                    Motion.animate(.easeInOut(duration: 0.25), reduced: reduceMotion) {
                        proxy.scrollTo(mark, anchor: .center)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var pageBody: some View {
        switch page.id {
        case .board: boardBody
        case .models: modelsBody
        case .projects: projectsBody
        case .devices: devicesBody
        default: plainBody
        }
    }

    // MARK: - The drawn state of a master switch

    /// The master's drawn (optimistic) state, read off its row in `entries`.
    private func masterOn(_ masterId: String, in entries: [SettingsEntry]) -> Bool {
        guard let entry = entries.first(where: { $0.id == masterId }),
              case .toggle(let action, let isOn) = entry.row.kind else { return true }
        return actions.toggleIsOn(action: action, model: isOn)
    }

    // MARK: - General, Sessions, Security, Advanced

    private var plainBody: some View {
        VStack(alignment: .leading, spacing: 18) {
            ForEach(page.groups) { group in
                groupBlock(group)
            }
        }
    }

    /// A group as eyebrow + card. The eyebrow is left off where it would
    /// repeat the page's title, and where the group is one pick run drawn
    /// as a single row under its own title (Panel size).
    @ViewBuilder
    private func groupBlock(_ group: SettingsGroup) -> some View {
        let lines = Self.groupLines(group)
        let singleRun = lines.count == 1 && {
            if case .picks = lines[0] { return true }
            return false
        }()
        VStack(alignment: .leading, spacing: 8) {
            if group.title != page.title && !singleRun {
                SettingsGroupHeading(title: group.title)
            }
            SettingsLinesCard(lines: lines, actions: actions, state: state)
        }
    }

    /// A group's lines; a group that is one pick run takes the group's own
    /// title and hover text as its row.
    static func groupLines(_ group: SettingsGroup) -> [SettingsLine] {
        let lines = SettingsControls.lines(group.entries)
        if lines.count == 1, case .picks(nil, let action, let run) = lines[0] {
            return [.picks(heading: SettingsControls.groupHeading(group),
                           action: action, entries: run)]
        }
        return lines
    }

    // MARK: - Board

    /// One card: the loose board switches with Pipeline's rows spliced under
    /// "Dark Army may start sessions", indented and dimmed while it is off.
    private var boardBody: some View {
        let master = "toggle:set_board_dispatch"
        let loose = page.groups.first { $0.id == "loose:Board" }
        let spliced = page.groups.filter {
            SettingsSections.dependents[master]?.contains($0.id) == true
        }
        let others = page.groups.filter { $0.id != loose?.id && !spliced.contains($0) }
        var lines: [SettingsLine] = []
        var dependentIds: Set<String> = []
        for line in loose.map({ SettingsControls.lines($0.entries) }) ?? [] {
            lines.append(line)
            if line.id == master {
                for group in spliced {
                    let heading = SettingsLine.heading(SettingsControls.groupHeading(group))
                    let inner = [heading] + SettingsControls.lines(group.entries)
                    lines.append(contentsOf: inner)
                    dependentIds.formUnion(inner.map(\.id))
                }
            }
        }
        // Without the master (never today), the dependents still draw.
        if loose == nil || !lines.contains(where: { $0.id == master }) {
            for group in spliced where !lines.contains(where: { $0.id == "heading:" + group.id }) {
                lines.append(.heading(SettingsControls.groupHeading(group)))
                lines.append(contentsOf: SettingsControls.lines(group.entries))
            }
        }
        let on = masterOn(master, in: page.groups.flatMap(\.entries))
        return VStack(alignment: .leading, spacing: 18) {
            SettingsLinesCard(lines: lines, actions: actions, state: state,
                              indentIds: dependentIds,
                              dimIds: on ? [] : dependentIds)
            ForEach(others) { group in
                groupBlock(group)
            }
        }
    }

    // MARK: - Devices

    private var devicesBody: some View {
        let entries = page.groups.flatMap(\.entries)
        let master = "toggle:set_lan_access"
        let dependents = Set(SettingsSections.dependents[master] ?? [])
        let accessIds = dependents.union([master, "info:lan-exposure"])
        let split = Self.splitBlocks(entries)
        let access = split.loose.filter { accessIds.contains($0.id) }
        let rest = split.loose.filter { !accessIds.contains($0.id) }
        let devices = split.blocks.filter { $0.head.id.hasPrefix("submenu:device:") }
        let otherBlocks = split.blocks.filter { !$0.head.id.hasPrefix("submenu:device:") }
        let accessLines = SettingsControls.lines(access)
        let on = masterOn(master, in: entries)
        let dependentLines = Set(accessLines.map(\.id).filter { dependents.contains($0) })
        let restLines = SettingsControls.lines(rest)
        let facts = restLines.filter { if case .entry(let e) = $0, case .info = e.row.kind { return true }; return false }
        let verbs = restLines.filter { !facts.contains($0) }
        return VStack(alignment: .leading, spacing: 18) {
            if !accessLines.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    SettingsGroupHeading(title: "Access")
                    SettingsLinesCard(lines: accessLines, actions: actions, state: state,
                                      indentIds: dependentLines,
                                      dimIds: on ? [] : dependentLines)
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                SettingsGroupHeading(title: "Paired devices")
                if !facts.isEmpty {
                    SettingsLinesCard(lines: facts, actions: actions, state: state)
                }
                ForEach(devices, id: \.head.id) { block in
                    SettingsLinesCard(lines: [.heading(block.head)]
                                          + SettingsControls.lines(block.entries),
                                      actions: actions, state: state)
                }
            }
            ForEach(otherBlocks, id: \.head.id) { block in
                VStack(alignment: .leading, spacing: 8) {
                    SettingsGroupHeading(title: block.head.row.title)
                    SettingsLinesCard(lines: SettingsControls.lines(block.entries),
                                      actions: actions, state: state)
                }
            }
            ForEach(verbs) { line in
                SettingsLineView(line: line, actions: actions, state: state)
            }
        }
    }

    struct Block: Equatable {
        var head: SettingsEntry
        var entries: [SettingsEntry]
    }

    /// A group's entries split at its top level: the loose rows (`block`
    /// empty, not a submenu) and each nested block — its heading entry plus
    /// everything under it — in inventory order.
    static func splitBlocks(_ entries: [SettingsEntry]) -> (loose: [SettingsEntry], blocks: [Block]) {
        var loose: [SettingsEntry] = []
        var blocks: [Block] = []
        for entry in entries {
            let isSubmenu: Bool = {
                if case .submenu = entry.row.kind { return true }
                return false
            }()
            if entry.block.isEmpty {
                if isSubmenu {
                    blocks.append(Block(head: entry, entries: []))
                } else {
                    loose.append(entry)
                }
            } else if !blocks.isEmpty {
                blocks[blocks.count - 1].entries.append(entry)
            } else {
                loose.append(entry)
            }
        }
        return (loose, blocks)
    }

    // MARK: - Models

    @ViewBuilder
    private var modelsBody: some View {
        if let group = page.groups.first {
            let table = SettingsModelsTable.build(entries: group.entries)
            VStack(alignment: .leading, spacing: 12) {
                if !group.tooltip.isEmpty {
                    Text(group.tooltip)
                        .font(Theme.prose(12.5))
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                SettingsModelsGrid(table: table, actions: actions, state: state)
            }
        } else {
            Text("This menu bar sends no model table.")
                .font(Theme.prose(13))
                .foregroundStyle(Theme.muted)
        }
    }

    // MARK: - Projects

    @ViewBuilder
    private var projectsBody: some View {
        if let projectsRow = rows.first(where: { $0.id == "submenu:projects" }) {
            let layout = SettingsProjectsPage.build(projectsRow: projectsRow,
                                                    selectedRoot: state.selectedProjectRoot)
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top, spacing: SettingsModelsGridMetrics.projectListSpacing) {
                    SettingsProjectList(layout: layout, actions: actions, state: state)
                        .frame(width: SettingsModelsGridMetrics.projectListWidth)
                    if let selected = layout.selected {
                        SettingsProjectDetail(item: selected, actions: actions, state: state)
                            .id(selected.root)
                    } else {
                        Spacer(minLength: 0)
                    }
                }
                // The project's model table and Un-enrol take the page's
                // full width: beside the list the detail pane is too narrow
                // for three assistants' columns
                // (`SettingsModelsGridMetrics`).
                if let selected = layout.selected {
                    SettingsProjectFoot(item: selected, actions: actions, state: state)
                        .id("foot:" + selected.root)
                }
            }
        }
    }
}

/// The Models table's widths. The page and the Projects page both draw it
/// at the page's content width, and `minimumWidth` has to fit that at the
/// window's smallest and first sizes (`SettingsModelsGridMetricsTests`).
enum SettingsModelsGridMetrics {
    /// The card's inset around the grid.
    static let padding: CGFloat = 14
    static let columnSpacing: CGFloat = 10
    /// The slot label wraps inside these.
    static let labelMinWidth: CGFloat = 72
    static let labelMaxWidth: CGFloat = 112
    /// A pop-up truncates its title below its ideal width, never below this.
    static let cellMinWidth: CGFloat = 88
    static let cellMaxWidth: CGFloat = 170
    /// The Projects page's list pane, beside the selected project's rows.
    static let projectListWidth: CGFloat = 190
    static let projectListSpacing: CGFloat = 16

    /// The narrowest the table draws with every column: the widest label,
    /// then each assistant's column at its floor.
    static func minimumWidth(columns: Int) -> CGFloat {
        2 * padding + labelMaxWidth + CGFloat(columns) * (columnSpacing + cellMinWidth)
    }

    /// What the Projects detail pane gets beside the list, in a window this
    /// wide — why the project's table is not drawn there.
    static func projectDetailWidth(windowWidth: CGFloat) -> CGFloat {
        SettingsWindowMetrics.pageContentWidth(windowWidth: windowWidth)
            - projectListWidth - projectListSpacing
    }
}

/// The Models table: roles down, assistants across, one pop-up per cell.
struct SettingsModelsGrid: View {
    let table: SettingsModelsTable.Table
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    private typealias Metrics = SettingsModelsGridMetrics

    // Anchors and flashes sit on cells, never on a `GridRow`: a modified
    // `GridRow` is no longer a row to the `Grid`, which would draw it as one
    // view spanning every column.
    var body: some View {
        SettingsCard {
            Grid(alignment: .leading, horizontalSpacing: Metrics.columnSpacing,
                 verticalSpacing: 10) {
                GridRow {
                    Text("")
                    ForEach(table.providers, id: \.self) { provider in
                        Text(provider)
                            .font(Theme.mono(12, weight: .semibold))
                            .foregroundStyle(Theme.muted)
                            .settingsMarked(table.providerIds[provider].map { [$0] } ?? [],
                                            state: state)
                    }
                }
                ForEach(table.rows) { slot in
                    let ids = slot.infoIds + table.providers.flatMap { provider in
                        (slot.cells[provider]?.choices.map(\.id) ?? [])
                            + (slot.effortCells[provider]?.choices.map(\.id) ?? [])
                    }
                    GridRow {
                        Text(slot.label)
                            .font(Theme.prose(13.5))
                            .foregroundStyle(Theme.text)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(minWidth: Metrics.labelMinWidth,
                                   maxWidth: Metrics.labelMaxWidth, alignment: .leading)
                            .settingsMarked(ids, state: state)
                        ForEach(table.providers, id: \.self) { provider in
                            if let cell = slot.cells[provider] {
                                // The effort pop-up stacks under the model's, so
                                // the column keeps the width the window's
                                // minimum is measured against; absent where the
                                // menu bar lists no levels for the slot.
                                VStack(alignment: .leading, spacing: 4) {
                                    SettingsPopUp(cell: cell, actions: actions)
                                        .id("\(slot.id)|\(provider)|"
                                            + (cell.choices.first.flatMap(SettingsSearch.runKey) ?? ""))
                                    if let effort = slot.effortCells[provider] {
                                        SettingsPopUp(cell: effort, actions: actions)
                                            .id("\(slot.id)|\(provider)|effort|"
                                                + (effort.choices.first.flatMap(SettingsSearch.runKey) ?? ""))
                                    }
                                }
                            } else {
                                Text("—").foregroundStyle(Theme.muted)
                            }
                        }
                    }
                }
            }
            .padding(Metrics.padding)
        }
    }
}

/// Projects' left pane: the enrolled folders, then the enrolment line and
/// **Enrol a folder…**.
struct SettingsProjectList: View {
    let layout: SettingsProjectsPage.Layout
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    var body: some View {
        SettingsCard {
            ForEach(Array(layout.projects.enumerated()), id: \.element.id) { index, item in
                if index > 0 { SettingsHairline() }
                let chosen = item.root == layout.selected?.root
                Button {
                    state.selectedProjectRoot = item.root
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.label)
                            .font(Theme.prose(13.5, weight: chosen ? .semibold : .regular))
                            .foregroundStyle(Theme.text)
                        if let profile = SettingsMenuModel.flattened(item.rows)
                            .first(where: \.checked)?.title {
                            Text(profile)
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
                    .padding(.horizontal, 12)
                    .background(chosen ? Theme.raised : Color.clear)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityAddTraits(chosen ? .isSelected : [])
                .settingsMarked([SettingsProjectsPage.projectPrefix + item.root], state: state)
            }
            let footer = SettingsControls.lines(layout.listFooter.map { SettingsEntry(row: $0) })
            ForEach(footer) { line in
                SettingsHairline()
                SettingsLineView(line: line, actions: actions, state: state)
            }
        }
    }
}

/// A project's rows split by where they are drawn: the root line, the agent
/// pack and the rest in the detail pane; its own model table and Un-enrol
/// under both panes, at full width. Every row lands in exactly one part.
struct SettingsProjectParts {
    var rootRow: SettingsRow?
    var pack: [SettingsRow]
    var rest: [SettingsRow]
    var models: SettingsRow?
    var unenrol: [SettingsRow]

    private static let packPrefixes = ["info:pack-", "submenu:pack:", "custom:stopPackSync:"]

    init(item: SettingsProjectsPage.ProjectItem) {
        rootRow = item.rows.first { $0.id.hasPrefix("info:project-root:") }
        pack = item.rows.filter { row in Self.packPrefixes.contains { row.id.hasPrefix($0) } }
        models = item.rows.first {
            $0.id.hasPrefix(SettingsMenuModel.projectAgentModelsPrefix)
        }
        unenrol = item.rows.filter { $0.id.hasPrefix("custom:unenrol:") }
        let placed = Set(([rootRow].compactMap { $0 } + pack + [models].compactMap { $0 }
                          + unenrol).map(\.id))
        rest = item.rows.filter { row in
            if case .divider = row.kind { return false }
            return !placed.contains(row.id)
        }
    }

    /// Rows (a pack submenu's children included) as drawn lines.
    static func lines(_ rows: [SettingsRow]) -> [SettingsLine] {
        SettingsControls.lines(SettingsMenuModel.flattened(rows).map { SettingsEntry(row: $0) })
    }
}

/// Projects' right pane: the project's name and root, its agent pack, and
/// its Knowledge and Checks.
struct SettingsProjectDetail: View {
    let item: SettingsProjectsPage.ProjectItem
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    var body: some View {
        let parts = SettingsProjectParts(item: item)
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.label)
                    .font(Theme.prose(16, weight: .semibold))
                    .foregroundStyle(Theme.text)
                if let rootRow = parts.rootRow {
                    Text(rootRow.title)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.muted)
                        .textSelection(.enabled)
                        .settingsMarked([rootRow.id], state: state)
                }
            }
            if !parts.pack.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    SettingsGroupHeading(title: "Agent pack")
                    SettingsLinesCard(lines: SettingsProjectParts.lines(parts.pack),
                                      actions: actions, state: state)
                }
            }
            if !parts.rest.isEmpty {
                SettingsLinesCard(lines: SettingsProjectParts.lines(parts.rest),
                                  actions: actions, state: state)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Under both Projects panes, at the page's full width: the selected
/// project's own model table (always open), then Un-enrol.
struct SettingsProjectFoot: View {
    let item: SettingsProjectsPage.ProjectItem
    let actions: SettingsActions
    @ObservedObject var state: SettingsWindowState

    var body: some View {
        let parts = SettingsProjectParts(item: item)
        VStack(alignment: .leading, spacing: 16) {
            if let models = parts.models, case .submenu(let title, let nested) = models.kind {
                VStack(alignment: .leading, spacing: 8) {
                    SettingsGroupHeading(title: title)
                        .settingsMarked([models.id], state: state)
                    if !models.tooltip.isEmpty {
                        Text(models.tooltip)
                            .font(Theme.prose(12.5))
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    SettingsModelsGrid(table: SettingsModelsTable.build(rows: nested),
                                       actions: actions, state: state)
                }
            }
            ForEach(SettingsProjectParts.lines(parts.unenrol)) { line in
                SettingsLineView(line: line, actions: actions, state: state)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// What a search finds, across every section: a count, then one row per hit
/// with its trail over it. A press opens the hit's section on its row.
struct SettingsResultsView: View {
    let hits: [SettingsSearch.Hit]
    let query: String
    @ObservedObject var state: SettingsWindowState

    private var tokens: [String] {
        query.split(whereSeparator: { $0.isWhitespace }).map(String.init)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(hits.count == 1 ? "1 match" : "\(hits.count) matches")
                    .font(Theme.prose(12.5))
                    .foregroundStyle(Theme.muted)
                if hits.isEmpty {
                    Text("Nothing matches that")
                        .font(Theme.prose(14))
                        .foregroundStyle(Theme.muted)
                } else {
                    SettingsCard {
                        ForEach(Array(hits.enumerated()), id: \.element.id) { index, hit in
                            if index > 0 { SettingsHairline() }
                            hitRow(hit)
                        }
                    }
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func hitRow(_ hit: SettingsSearch.Hit) -> some View {
        Button {
            state.jump(to: hit)
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text(hit.crumb)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.accent)
                Text(Self.emphasised(hit.entry.row.title, tokens: tokens,
                                     size: 14))
                    .foregroundStyle(Theme.text)
                if !hit.entry.row.tooltip.isEmpty {
                    Text(Self.emphasised(hit.entry.row.tooltip, tokens: tokens,
                                         size: 12.5))
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 9)
            .padding(.horizontal, 14)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityLabel("\(hit.crumb), \(hit.entry.row.title)")
    }

    /// The text with every matched token in the semibold weight.
    static func emphasised(_ text: String, tokens: [String], size: CGFloat) -> AttributedString {
        var out = AttributedString(text)
        out.font = Theme.prose(size)
        for token in tokens where !token.isEmpty {
            var from = text.startIndex
            while from < text.endIndex,
                  let found = text.range(of: token,
                                         options: [.caseInsensitive, .diacriticInsensitive],
                                         range: from..<text.endIndex) {
                if let range = Range(found, in: out) {
                    out[range].font = Theme.prose(size, weight: .semibold)
                }
                from = found.upperBound
            }
        }
        return out
    }
}
