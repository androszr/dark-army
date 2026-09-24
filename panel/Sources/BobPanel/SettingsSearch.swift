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
                                         entries: flatten(nested, block: "")))
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
}
