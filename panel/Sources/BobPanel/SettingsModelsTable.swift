import Foundation

/// The Agent models rows regrouped as a table: one row per slot, one column
/// per assistant, one cell per (slot, assistant) holding that slot's pick run.
/// Pure and built from entries `SettingsMenuModel.agentModelRows` already
/// emitted — the machine-wide page's or one project's block — so nothing here
/// adds, drops or retitles a choice. A project's cells lead with `Inherit`
/// because the tree already emits it first.
enum SettingsModelsTable {
    struct Cell: Equatable {
        /// The pick action every choice sends (`set_agent_model`).
        var action: String
        /// The slot's pick run, in inventory order.
        var choices: [SettingsEntry]
        /// The ticked choice, or nil where none is.
        var selected: SettingsEntry?
    }

    struct SlotRow: Equatable, Identifiable {
        /// The slot (`main`, `planner`, …).
        var id: String
        /// What the slot is called — the `info` row's title,
        /// `SettingsMenuModel.slotLabel`.
        var label: String
        /// Assistant → its cell. An assistant with nothing to offer for a
        /// slot has no cell.
        var cells: [String: Cell]
        /// The slot's `info:agent-model:*` rows, one per assistant — search
        /// hits the table's label cell anchors and flashes.
        var infoIds: [String] = []
    }

    struct Table: Equatable {
        /// Assistants in `DaemonClient.AgentModels.providers` order, those
        /// with at least one cell.
        var providers: [String]
        /// Slots in inventory order.
        var rows: [SlotRow]
        /// Assistant → its `submenu:agent-models:<provider>[:<root>]` row, the
        /// hit the column's header anchors and flashes.
        var providerIds: [String: String] = [:]

        var isEmpty: Bool { rows.isEmpty }
    }

    static let infoPrefix = "info:agent-model:"
    static let providerPrefix = "submenu:agent-models:"

    /// `submenu:agent-models:<provider>[:<root>]` → the provider.
    static func parseProviderId(_ id: String) -> String? {
        guard id.hasPrefix(providerPrefix) else { return nil }
        let provider = id.dropFirst(providerPrefix.count)
            .split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false).first
        guard let provider, !provider.isEmpty else { return nil }
        return String(provider)
    }

    /// `info:agent-model:<provider>:<slot>[:<root>]` → provider and slot.
    static func parseInfoId(_ id: String) -> (provider: String, slot: String)? {
        guard id.hasPrefix(infoPrefix) else { return nil }
        let parts = id.dropFirst(infoPrefix.count)
            .split(separator: ":", maxSplits: 2, omittingEmptySubsequences: false)
        guard parts.count >= 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }
        return (String(parts[0]), String(parts[1]))
    }

    /// Walk the entries: each slot's `info` row opens a cell, and the pick run
    /// after it (one `SettingsSearch.runKey`) fills it. Anything else closes
    /// the cell, so a stray pick never joins the wrong slot.
    static func build(entries: [SettingsEntry]) -> Table {
        var slotOrder: [String] = []
        var labels: [String: String] = [:]
        var cells: [String: [String: Cell]] = [:]
        var current: (provider: String, slot: String)?
        var currentKey: String?
        var infoIds: [String: [String]] = [:]
        var providerIds: [String: String] = [:]

        for entry in entries {
            if let provider = parseProviderId(entry.id) {
                if providerIds[provider] == nil { providerIds[provider] = entry.id }
                current = nil
                currentKey = nil
                continue
            }
            if let parsed = parseInfoId(entry.id) {
                current = parsed
                currentKey = nil
                infoIds[parsed.slot, default: []].append(entry.id)
                if labels[parsed.slot] == nil {
                    slotOrder.append(parsed.slot)
                    labels[parsed.slot] = entry.row.title
                }
                continue
            }
            guard let open = current,
                  case .pick(let action, _, let selected) = entry.row.kind,
                  action == SettingsMenuModel.agentModelAction else {
                current = nil
                currentKey = nil
                continue
            }
            let key = SettingsSearch.runKey(entry)
            if let currentKey, currentKey != key {
                current = nil
                continue
            }
            currentKey = key
            var cell = cells[open.slot]?[open.provider]
                ?? Cell(action: action, choices: [], selected: nil)
            cell.choices.append(entry)
            if selected && cell.selected == nil { cell.selected = entry }
            cells[open.slot, default: [:]][open.provider] = cell
        }

        let rows = slotOrder.compactMap { slot -> SlotRow? in
            guard let byProvider = cells[slot], !byProvider.isEmpty else { return nil }
            return SlotRow(id: slot, label: labels[slot] ?? slot, cells: byProvider,
                           infoIds: infoIds[slot] ?? [])
        }
        let providers = DaemonClient.AgentModels.providers.filter { provider in
            rows.contains { $0.cells[provider] != nil }
        }
        return Table(providers: providers, rows: rows,
                     providerIds: providerIds.filter { providers.contains($0.key) })
    }

    /// The same, from a subtree of descriptor rows (one project's block).
    static func build(rows: [SettingsRow]) -> Table {
        build(entries: SettingsMenuModel.flattened(rows).map { SettingsEntry(row: $0) })
    }
}
