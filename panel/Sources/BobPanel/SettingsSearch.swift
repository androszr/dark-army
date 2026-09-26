import Foundation

/// One line of the settings page: a descriptor row plus the nested block it
/// sits in. `block` is the nested submenu's title (`"iPhone"`, `"Agents per
/// project"`, a project label; two levels deep joined with ` › `) or `""` at
/// the group's own top level. The row is the row `SettingsMenuModel` emitted —
/// nothing here is retitled or reordered.
struct SettingsEntry: Equatable, Identifiable {
    var row: SettingsRow
    var block: String = ""
    /// The id of the foldable heading this entry sits under, or nil. Set by
    /// `groups` for every entry inside a `SettingsSearch.foldable` submenu
    /// (the heading itself carries nil: it is what you press). Read by
    /// `visible`, never by `items`.
    var foldedUnder: String?

    var id: String { row.id }

    var isDivider: Bool {
        if case .divider = row.kind { return true }
        return false
    }
}

/// A headed section of the settings page. A top-level submenu is one group; the
/// loose top-level rows are gathered under headings `SettingsSearch.looseSection`
/// names. `id` is the submenu row's id, or `loose:<title>`.
struct SettingsGroup: Equatable, Identifiable {
    var id: String
    var title: String
    var entries: [SettingsEntry]
    /// The submenu row's own hover text (Panel size's, Agent models'), drawn
    /// as the group's description. A loose group has none.
    var tooltip: String = ""
}

/// Pure grouping and filtering over the descriptor tree `SettingsMenuModel.rows`
/// produces. **There is no second inventory here**: every entry drawn on the
/// settings page came out of that one function; the only table this file adds
/// is `looseSection`, which names *headings*, never rows.
enum SettingsSearch {
    /// The heading a loose row lands under when `looseSection` does not know
    /// it. A test pins that today's inventory never reaches it, so a new loose
    /// row fails the suite instead of landing untitled.
    static let fallbackSection = "General"

    /// Which heading a top-level row that is not a submenu belongs under.
    /// Headings, not rows: the row itself, its title and its order come from
    /// the descriptor tree.
    static func looseSection(rowId: String) -> String? {
        switch rowId {
        case SettingsMenuModel.killRowId:
            return "Kill switch"
        case "toggle:set_channel", "toggle:set_auto_compact":
            return "Sessions"
        case "toggle:set_board_dispatch", "toggle:set_board_close_terminal":
            return "Board"
        case "send:restart", "send:quit_app":
            return "This app"
        default:
            return nil
        }
    }

    /// Walk the top level in inventory order. A `.submenu` is its own group,
    /// its children flattened with `block` set from any nested submenu's title
    /// (the nested submenu row itself is kept as the block's heading entry, so
    /// its tooltip — `Agents per project`'s is the only place the parallel
    /// explanation is written — stays searchable). A loose row takes its group
    /// from `looseSection`; loose rows sharing a heading gather under the first
    /// occurrence of it, and a `.divider` between two *consecutive* rows of one
    /// heading is kept as an entry inside it. A divider between groups is
    /// dropped — the heading is the separator now.
    static func groups(_ rows: [SettingsRow]) -> [SettingsGroup] {
        var out: [SettingsGroup] = []
        var pendingDividers: [SettingsRow] = []
        var lastLooseGroup: Int?
        for row in rows {
            switch row.kind {
            case .submenu(_, let nested):
                out.append(SettingsGroup(id: row.id, title: row.title,
                                         entries: flatten(nested, block: ""),
                                         tooltip: row.tooltip))
                pendingDividers = []
                lastLooseGroup = nil
            case .divider:
                pendingDividers.append(row)
            default:
                let title = looseSection(rowId: row.id) ?? fallbackSection
                let groupId = "loose:\(title)"
                if let index = out.firstIndex(where: { $0.id == groupId }) {
                    if lastLooseGroup == index {
                        out[index].entries.append(
                            contentsOf: pendingDividers.map { SettingsEntry(row: $0) })
                    }
                    out[index].entries.append(SettingsEntry(row: row))
                    lastLooseGroup = index
                } else {
                    out.append(SettingsGroup(id: groupId, title: title,
                                             entries: [SettingsEntry(row: row)]))
                    lastLooseGroup = out.count - 1
                }
                pendingDividers = []
            }
        }
        return out
    }

    private static func flatten(_ rows: [SettingsRow], block: String,
                                fold: String? = nil) -> [SettingsEntry] {
        var out: [SettingsEntry] = []
        for row in rows {
            out.append(SettingsEntry(row: row, block: block, foldedUnder: fold))
            if case .submenu(let title, let nested) = row.kind {
                let inner = block.isEmpty ? title : "\(block) › \(title)"
                // The outermost fold wins: a provider block nested inside a
                // project's Agent models block folds with the project's.
                let innerFold = fold ?? (foldable(row) ? row.id : nil)
                out.append(contentsOf: flatten(nested, block: inner, fold: innerFold))
            }
        }
        return out
    }

    /// Which nested submenus draw closed behind their heading until pressed.
    /// Exactly the per-project Agent models blocks today: each is three
    /// provider blocks of seven chip runs, and an enrolled project's rows
    /// would otherwise be twenty-one headings deep between the pack rows
    /// and Un-enrol. A rule on the id, not a flag on the row, so the
    /// descriptor tree stays the one inventory.
    static func foldable(_ row: SettingsRow) -> Bool {
        if case .submenu = row.kind {
            return row.id.hasPrefix(SettingsMenuModel.projectAgentModelsPrefix)
        }
        return false
    }

    /// The entries drawn: everything, minus those folded under a heading
    /// that is not in `open`. A non-empty query overrides every fold — the
    /// board's `BoardRowFold` rule — so a search can never hide its own hit.
    static func visible(_ entries: [SettingsEntry], open: Set<String>,
                        query: String) -> [SettingsEntry] {
        let searching = !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if searching { return entries }
        return entries.filter { entry in
            guard let fold = entry.foldedUnder else { return true }
            return open.contains(fold)
        }
    }

    /// Everything a query is matched against: the title, the hover text, the
    /// block label and the group heading, one per line.
    static func haystack(_ entry: SettingsEntry, groupTitle: String) -> String {
        [entry.row.title, entry.row.tooltip, entry.block, groupTitle]
            .joined(separator: "\n")
    }

    /// An empty or whitespace-only query returns `groups` unchanged, dividers
    /// included. Otherwise every whitespace-separated token must
    /// `localizedStandardContains` the entry's haystack (case- and
    /// diacritic-insensitive, `BoardState.matches`' choice); dividers are
    /// dropped while filtering; a group with no surviving entry is omitted;
    /// survivors keep inventory order and their heading.
    static func filter(_ groups: [SettingsGroup], query: String) -> [SettingsGroup] {
        let tokens = query.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        guard !tokens.isEmpty else { return groups }
        var out: [SettingsGroup] = []
        for group in groups {
            let kept = group.entries.filter { entry in
                guard !entry.isDivider else { return false }
                let hay = haystack(entry, groupTitle: group.title)
                return tokens.allSatisfy { hay.localizedStandardContains($0) }
            }
            guard !kept.isEmpty else { continue }
            var survivor = group
            survivor.entries = kept
            out.append(survivor)
        }
        return out
    }

    /// How a group's entries are drawn: runs of `.pick` entries that share an
    /// action become one exclusive chip row; everything else is drawn alone.
    enum Item: Equatable, Identifiable {
        case single(SettingsEntry)
        case picks(action: String, entries: [SettingsEntry])

        var id: String {
            switch self {
            case .single(let entry): return entry.id
            case .picks(let action, let entries):
                return "picks:\(action):\(entries.first?.id ?? "")"
            }
        }
    }

    /// What decides whether two consecutive picks are one run: the action,
    /// and for a `.map` value the record minus its `model` — so the Agent
    /// models chips of one slot join and those of the next slot do not, even
    /// when a search has dropped the `info` row that separated them.
    static func runKey(_ entry: SettingsEntry) -> String? {
        guard case .pick(let action, let value, _) = entry.row.kind else { return nil }
        if case .map(let record) = value {
            let facets = record.filter { $0.key != "model" }
            return action + "|" + facets.keys.sorted()
                .map { "\($0)=\(facets[$0] ?? "")" }.joined(separator: ";")
        }
        return action
    }

    static func items(_ entries: [SettingsEntry]) -> [Item] {
        var out: [Item] = []
        var lastKey: String?
        for entry in entries {
            if case .pick(let action, _, _) = entry.row.kind {
                let key = runKey(entry)
                if case .picks(_, let run)? = out.last, lastKey == key {
                    out[out.count - 1] = .picks(action: action, entries: run + [entry])
                } else {
                    out.append(.picks(action: action, entries: [entry]))
                }
                lastKey = key
            } else {
                out.append(.single(entry))
                lastKey = nil
            }
        }
        return out
    }

    // MARK: - Hits across every section

    /// One search result: the entry, where it lives, and the trail drawn over
    /// it. `section` nil is the sidebar's foot.
    struct Hit: Identifiable, Equatable {
        var section: SettingsSectionID?
        var crumb: String
        var entry: SettingsEntry
        /// The enrolled project the entry sits under, off the tree's
        /// `submenu:project:<root>` id — never off its label, which two
        /// projects may share.
        var projectRoot: String?

        var id: String { entry.id }
    }

    /// The crumb for the sidebar's foot.
    static let footerCrumb = "Sidebar"

    /// The trail over a hit: the section's title; then the group's, unless it
    /// says the same or the section holds only that group; then the entry's
    /// `block` where it has one. Joined with ` › `. `section` nil is the foot.
    static func crumb(section: SettingsPage?, group: SettingsGroup,
                      entry: SettingsEntry) -> String {
        guard let section else { return footerCrumb }
        var parts = [section.title]
        if group.title != section.title && section.groups.count > 1 {
            parts.append(group.title)
        }
        if !entry.block.isEmpty { parts.append(entry.block) }
        return parts.joined(separator: " › ")
    }

    /// Entry id → the enrolled root it sits under, walked in inventory order:
    /// a `submenu:project:<root>` entry opens a project, and the next entry at
    /// the group's own top level (`block` empty) closes it.
    static func projectRoots(_ entries: [SettingsEntry]) -> [String: String] {
        let prefix = "submenu:project:"
        var out: [String: String] = [:]
        var current: String?
        for entry in entries {
            if entry.id.hasPrefix(prefix) {
                current = String(entry.id.dropFirst(prefix.count))
            } else if entry.block.isEmpty {
                current = nil
            }
            if let current { out[entry.id] = current }
        }
        return out
    }

    /// Every section searched with the unchanged `filter`, page by page in
    /// page order, the foot last. An empty or whitespace query finds nothing:
    /// the window shows the selected page instead.
    static func hits(pages: [SettingsPage], footer: [SettingsEntry],
                     query: String) -> [Hit] {
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return []
        }
        var out: [Hit] = []
        for page in pages {
            for group in page.groups {
                let roots = projectRoots(group.entries)
                for survivor in filter([group], query: query) {
                    for entry in survivor.entries {
                        out.append(Hit(
                            section: page.id,
                            crumb: crumb(section: page, group: group, entry: entry),
                            entry: entry,
                            projectRoot: roots[entry.id]))
                    }
                }
            }
        }
        let foot = SettingsGroup(id: "footer", title: footerCrumb, entries: footer)
        for survivor in filter([foot], query: query) {
            for entry in survivor.entries {
                out.append(Hit(section: nil, crumb: footerCrumb, entry: entry,
                               projectRoot: nil))
            }
        }
        return out
    }
}
