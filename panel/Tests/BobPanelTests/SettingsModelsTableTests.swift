import XCTest
@testable import BobPanel

/// The Models table is a regrouping of the Agent models rows the tree
/// already emits: one row per slot, one cell per assistant, each cell the
/// slot's own pick run. Driven from `SettingsMenuModelTests.modelsRaw`.
final class SettingsModelsTableTests: XCTestCase {

    private let root = "/Users/me/proj"

    private func modelSettings() -> DaemonClient.Settings {
        DaemonClient.Settings(SettingsMenuModelTests.modelsRaw, base: DaemonClient.Settings())
    }

    private func tree(enrolled: Bool = false) -> [SettingsRow] {
        var enrollment = Enrollment()
        if enrolled {
            enrollment.available = true
            enrollment.enrolled = [EnrolledProject(root: root, label: "proj")]
        }
        return SettingsMenuModel.rows(
            settings: modelSettings(), context: DaemonClient.PanelContext(),
            enrollment: enrollment, recordingShortcut: false, unenrolArmed: nil)
    }

    private func machineWide() -> SettingsModelsTable.Table {
        let group = SettingsSearch.groups(tree())
            .first { $0.id == SettingsMenuModel.agentModelsRowId }!
        return SettingsModelsTable.build(entries: group.entries)
    }

    private func projectTable() -> SettingsModelsTable.Table {
        let block = SettingsMenuModel.flattened(tree(enrolled: true))
            .first { $0.id == SettingsMenuModel.projectAgentModelsPrefix + root }!
        guard case .submenu(_, let nested) = block.kind else {
            XCTFail("the project's Agent models row is not a submenu")
            return SettingsModelsTable.Table(providers: [], rows: [])
        }
        return SettingsModelsTable.build(rows: nested)
    }

    private func model(_ entry: SettingsEntry?) -> String? {
        guard let entry, case .pick(_, .map(let record), _) = entry.row.kind else { return nil }
        return record["model"]
    }

    func testThreeAssistantsAcrossAndThreeSlotsDown() {
        let table = machineWide()
        XCTAssertEqual(table.providers, DaemonClient.AgentModels.providers)
        XCTAssertEqual(table.rows.map(\.id), ["main", "planner", "card-preparer"])
        XCTAssertEqual(table.rows.map(\.label),
                       ["main", "planner", "card-preparer"].map(SettingsMenuModel.slotLabel))
        for row in table.rows {
            XCTAssertEqual(Set(row.cells.keys), Set(DaemonClient.AgentModels.providers), row.id)
        }
    }

    func testEachCellIsTheSlotsRunWithDefaultFirst() {
        let models = modelSettings().agentModels
        for row in machineWide().rows {
            for (provider, cell) in row.cells {
                XCTAssertEqual(cell.action, SettingsMenuModel.agentModelAction)
                XCTAssertEqual(cell.choices.map(\.row.title),
                               [SettingsMenuModel.defaultModelLabel]
                                   + models.allowed(provider: provider, slot: row.id))
                for choice in cell.choices {
                    guard case .pick(_, .map(let record), _) = choice.row.kind else {
                        return XCTFail("\(choice.id) is not a map pick")
                    }
                    XCTAssertEqual(record["provider"], provider)
                    XCTAssertEqual(record["slot"], row.id)
                    XCTAssertNil(record["root"])
                }
            }
        }
    }

    func testTheSelectedChoiceIsTheResolvedTables() {
        let rows = Dictionary(uniqueKeysWithValues: machineWide().rows.map { ($0.id, $0) })
        XCTAssertEqual(rows["planner"]?.cells["claude"]?.selected?.row.title, "opus")
        XCTAssertEqual(model(rows["main"]?.cells["claude"]?.selected), "")
        XCTAssertEqual(rows["main"]?.cells["claude"]?.selected?.row.title,
                       SettingsMenuModel.defaultModelLabel)
        XCTAssertEqual(rows["main"]?.cells["grok"]?.selected?.row.title, "grok-4.5")
        XCTAssertEqual(rows["planner"]?.cells["codex"]?.selected?.row.title, "gpt-6-astra")
    }

    func testAnUndrawableSlotYieldsNoRow() {
        XCTAssertFalse(machineWide().rows.contains { $0.id == "integration-reviewer" })
        XCTAssertFalse(projectTable().rows.contains { $0.id == "integration-reviewer" })
    }

    func testAProjectsTableLeadsWithInheritCarriesTheRootAndInheritsWhereUnset() {
        let table = projectTable()
        XCTAssertEqual(table.providers, DaemonClient.AgentModels.providers)
        XCTAssertEqual(table.rows.map(\.id), ["main", "planner", "card-preparer"])
        for row in table.rows {
            for (provider, cell) in row.cells {
                XCTAssertEqual(cell.choices.first?.row.title, SettingsMenuModel.inheritLabel)
                XCTAssertEqual(cell.choices.dropFirst().first?.row.title,
                               SettingsMenuModel.defaultModelLabel)
                for choice in cell.choices {
                    guard case .pick(_, .map(let record), _) = choice.row.kind else {
                        return XCTFail("\(choice.id) is not a map pick")
                    }
                    XCTAssertEqual(record["root"], root, choice.id)
                    XCTAssertEqual(record["provider"], provider)
                }
                if row.id == "planner" && provider == "claude" {
                    XCTAssertEqual(cell.selected?.row.title, "sonnet")
                } else {
                    // No override: Inherit is the ticked choice.
                    XCTAssertEqual(cell.selected?.row.title, SettingsMenuModel.inheritLabel,
                                   "\(provider)/\(row.id)")
                }
            }
        }
        let rows = Dictionary(uniqueKeysWithValues: table.rows.map { ($0.id, $0) })
        XCTAssertEqual(rows["main"]?.cells["claude"]?.selected?.row.title,
                       SettingsMenuModel.inheritLabel)
    }

    func testNoTableIsAnEmptyTable() {
        let table = SettingsModelsTable.build(entries: [])
        XCTAssertTrue(table.isEmpty)
        XCTAssertTrue(table.providers.isEmpty)
    }

    func testTheInfoIdParsesProviderAndSlotWithOrWithoutARoot() {
        XCTAssertEqual(SettingsModelsTable.parseInfoId("info:agent-model:codex:planner")?.provider,
                       "codex")
        let withRoot = SettingsModelsTable.parseInfoId("info:agent-model:claude:main:/a:b/c")
        XCTAssertEqual(withRoot?.provider, "claude")
        XCTAssertEqual(withRoot?.slot, "main")
        XCTAssertNil(SettingsModelsTable.parseInfoId("info:build"))
        XCTAssertNil(SettingsModelsTable.parseInfoId("info:agent-model:claude"))
    }

    // MARK: - The effort beside the model

    /// `modelsRaw` plus the effort keys: levels only for `planner`.
    private func effortSettings() -> DaemonClient.Settings {
        var raw = SettingsMenuModelTests.modelsRaw
        raw["agent_efforts"] = [
            "claude": ["planner": "low"], "codex": ["planner": "high"],
            "grok": ["planner": ""],
        ]
        raw["agent_efforts_by_root"] = ["/Users/me/proj": ["claude": ["planner": "max"]]]
        raw["agent_effort_options"] = [
            "claude": ["planner": ["low", "medium", "high", "xhigh", "max"]],
            "codex": ["planner": ["low", "medium", "high", "xhigh", "max"]],
            "grok": ["planner": ["minimal", "low", "medium", "high", "xhigh"]],
        ]
        raw["agent_effort_slots"] = ["main", "planner"]
        return DaemonClient.Settings(raw, base: DaemonClient.Settings())
    }

    private func effortTree(enrolled: Bool = false) -> [SettingsRow] {
        var enrollment = Enrollment()
        if enrolled {
            enrollment.available = true
            enrollment.enrolled = [EnrolledProject(root: root, label: "proj")]
        }
        return SettingsMenuModel.rows(
            settings: effortSettings(), context: DaemonClient.PanelContext(),
            enrollment: enrollment, recordingShortcut: false, unenrolArmed: nil)
    }

    private func effortMachineWide() -> SettingsModelsTable.Table {
        let group = SettingsSearch.groups(effortTree())
            .first { $0.id == SettingsMenuModel.agentModelsRowId }!
        return SettingsModelsTable.build(entries: group.entries)
    }

    func testEffortCellsAppearOnlyForSlotsTheOptionsList() {
        let rows = Dictionary(uniqueKeysWithValues: effortMachineWide().rows.map { ($0.id, $0) })
        XCTAssertEqual(Set(rows["planner"]?.effortCells.keys ?? [:].keys),
                       Set(DaemonClient.AgentModels.providers))
        // `main` is a drawable effort slot but lists no levels here, and the
        // preparer is no effort slot at all.
        XCTAssertTrue(rows["main"]?.effortCells.isEmpty ?? false)
        XCTAssertTrue(rows["card-preparer"]?.effortCells.isEmpty ?? false)
    }

    func testAnOlderMenuBarWithNoEffortKeysDrawsNoEffortCell() {
        for row in machineWide().rows { XCTAssertTrue(row.effortCells.isEmpty, row.id) }
    }

    func testEffortCellsAreTheSlotsRunOnTheEffortActionWithDefaultFirst() {
        let planner = effortMachineWide().rows.first { $0.id == "planner" }!
        for (provider, cell) in planner.effortCells {
            XCTAssertEqual(cell.action, SettingsMenuModel.agentEffortAction)
            XCTAssertEqual(cell.choices.first?.row.title, SettingsMenuModel.defaultModelLabel)
            for choice in cell.choices {
                guard case .pick(_, .map(let record), _) = choice.row.kind else {
                    return XCTFail("\(choice.id) is not a map pick")
                }
                XCTAssertEqual(record["provider"], provider)
                XCTAssertEqual(record["slot"], "planner")
                XCTAssertNotNil(record["effort"])
                XCTAssertNil(record["model"])
            }
        }
        XCTAssertEqual(planner.effortCells["claude"]?.selected?.row.title, "low")
        XCTAssertEqual(planner.effortCells["codex"]?.selected?.row.title, "high")
        XCTAssertEqual(planner.effortCells["grok"]?.selected?.row.title,
                       SettingsMenuModel.defaultModelLabel)
    }

    func testTheModelCellsAreUnchangedByTheEffortRows() {
        let with = effortMachineWide()
        let without = machineWide()
        XCTAssertEqual(with.rows.map(\.id), without.rows.map(\.id))
        for (a, b) in zip(with.rows, without.rows) {
            XCTAssertEqual(a.cells.mapValues { $0.choices.map(\.row.title) },
                           b.cells.mapValues { $0.choices.map(\.row.title) }, a.id)
            XCTAssertEqual(a.cells.mapValues { $0.selected?.row.title },
                           b.cells.mapValues { $0.selected?.row.title }, a.id)
        }
    }

    func testAProjectsEffortCellLeadsWithInheritAndCarriesTheRoot() {
        let block = SettingsMenuModel.flattened(effortTree(enrolled: true))
            .first { $0.id == SettingsMenuModel.projectAgentModelsPrefix + root }!
        guard case .submenu(_, let nested) = block.kind else {
            return XCTFail("the project's Agent models row is not a submenu")
        }
        let planner = SettingsModelsTable.build(rows: nested).rows.first { $0.id == "planner" }!
        for (_, cell) in planner.effortCells {
            XCTAssertEqual(cell.choices.first?.row.title, SettingsMenuModel.inheritLabel)
            XCTAssertEqual(cell.choices.dropFirst().first?.row.title,
                           SettingsMenuModel.defaultModelLabel)
            for choice in cell.choices {
                guard case .pick(_, .map(let record), _) = choice.row.kind else {
                    return XCTFail("\(choice.id) is not a map pick")
                }
                XCTAssertEqual(record["root"], root)
            }
        }
        XCTAssertEqual(planner.effortCells["claude"]?.selected?.row.title, "max")
        XCTAssertEqual(planner.effortCells["codex"]?.selected?.row.title,
                       SettingsMenuModel.inheritLabel)
    }

    func testAProjectsEffortCellOffersThatProjectsLevelsNotTheMachineWideOnes() {
        var raw = SettingsMenuModelTests.modelsRaw
        raw["agent_effort_options"] = [
            "codex": ["planner": ["low", "high", "max"]],
        ]
        raw["agent_effort_slots"] = ["planner"]
        // This project's planner model has no `max`; a root with no entry
        // falls back to the machine-wide list.
        raw["agent_effort_options_by_root"] = [
            "/Users/me/proj": ["codex": ["planner": ["low", "high"]]],
        ]
        let settings = DaemonClient.Settings(raw, base: DaemonClient.Settings())
        let models = settings.agentModels
        XCTAssertEqual(models.allowedEfforts(provider: "codex", slot: "planner"),
                       ["low", "high", "max"])
        XCTAssertEqual(models.allowedEfforts(provider: "codex", slot: "planner",
                                             root: "/Users/me/proj"), ["low", "high"])
        XCTAssertEqual(models.allowedEfforts(provider: "codex", slot: "planner",
                                             root: "/elsewhere"), ["low", "high", "max"])
        let rows = SettingsMenuModel.agentModelRows(models, root: "/Users/me/proj")
        let titles = SettingsMenuModel.flattened(rows).filter {
            if case .pick(let action, _, _) = $0.kind {
                return action == SettingsMenuModel.agentEffortAction
            }
            return false
        }.map(\.title)
        XCTAssertFalse(titles.contains("max"))
        XCTAssertTrue(titles.contains("high"))
        // A reverse case: the project's list may offer a level the global lacks.
        raw["agent_effort_options_by_root"] = [
            "/Users/me/proj": ["codex": ["planner": ["low", "high", "max", "xhigh"]]],
        ]
        let wider = DaemonClient.Settings(raw, base: DaemonClient.Settings()).agentModels
        XCTAssertTrue(wider.allowedEfforts(provider: "codex", slot: "planner",
                                           root: "/Users/me/proj").contains("xhigh"))
    }

    func testTheEffortInfoIdParsesProviderAndSlotWithOrWithoutARoot() {
        XCTAssertEqual(SettingsModelsTable.parseEffortInfoId("info:agent-effort:codex:planner")?.slot,
                       "planner")
        let withRoot = SettingsModelsTable.parseEffortInfoId("info:agent-effort:claude:main:/a:b/c")
        XCTAssertEqual(withRoot?.provider, "claude")
        XCTAssertEqual(withRoot?.slot, "main")
        XCTAssertNil(SettingsModelsTable.parseEffortInfoId("info:agent-model:claude:main"))
        XCTAssertNil(SettingsModelsTable.parseInfoId("info:agent-effort:claude:main"))
    }

    // MARK: - Search targets and widths

    /// Every entry a Models search can land on is anchored by a drawn cell:
    /// a column header, a label cell (the slot's info rows and every pick in
    /// the row), so no jump leaves its mark on nothing.
    private func anchored(_ table: SettingsModelsTable.Table) -> Set<String> {
        var ids = Set(table.providerIds.values)
        for row in table.rows {
            ids.formUnion(row.infoIds)
            for cell in row.cells.values { ids.formUnion(cell.choices.map(\.id)) }
        }
        return ids
    }

    func testEveryMachineWideEntryHasADrawnAnchor() {
        let group = SettingsSearch.groups(tree())
            .first { $0.id == SettingsMenuModel.agentModelsRowId }!
        let table = machineWide()
        let ids = anchored(table)
        for entry in group.entries where entry.id != group.id {
            XCTAssertTrue(ids.contains(entry.id), entry.id)
        }
        XCTAssertEqual(table.providerIds,
                       Dictionary(uniqueKeysWithValues: DaemonClient.AgentModels.providers.map {
                           ($0, "submenu:agent-models:\($0)") }))
        for row in table.rows {
            XCTAssertEqual(row.infoIds, DaemonClient.AgentModels.providers.map {
                "info:agent-model:\($0):\(row.id)" })
        }
    }

    func testEveryProjectEntryHasADrawnAnchorAndCarriesTheRoot() {
        let block = SettingsMenuModel.flattened(tree(enrolled: true))
            .first { $0.id == SettingsMenuModel.projectAgentModelsPrefix + root }!
        guard case .submenu(_, let nested) = block.kind else {
            return XCTFail("the project's Agent models row is not a submenu")
        }
        let table = projectTable()
        let ids = anchored(table)
        for row in SettingsMenuModel.flattened(nested) {
            XCTAssertTrue(ids.contains(row.id), row.id)
        }
        XCTAssertEqual(table.providerIds["codex"], "submenu:agent-models:codex:\(root)")
    }

    func testTheProviderIdParsesWithOrWithoutARoot() {
        XCTAssertEqual(SettingsModelsTable.parseProviderId("submenu:agent-models:grok"), "grok")
        XCTAssertEqual(SettingsModelsTable.parseProviderId(
            "submenu:agent-models:grok:/Users/me/a:b"), "grok")
        XCTAssertNil(SettingsModelsTable.parseProviderId("submenu:agent-models:"))
        XCTAssertNil(SettingsModelsTable.parseProviderId("submenu:agent-models"))
    }

    /// Every assistant's column fits the page at the window's floor and at
    /// its first size, the Models page and the Projects page alike (the
    /// project's table is drawn at the page's width, under both panes).
    func testTheTableFitsThePageAtTheFloorAndTheFirstSize() {
        let columns = DaemonClient.AgentModels.providers.count
        let needed = SettingsModelsGridMetrics.minimumWidth(columns: columns)
        for width in [SettingsWindowMetrics.minWidth, SettingsWindowMetrics.defaultWidth] {
            let page = SettingsWindowMetrics.pageContentWidth(windowWidth: width)
            XCTAssertLessThanOrEqual(needed, page, "window \(width)")
        }
        XCTAssertEqual(SettingsWindowMetrics.pageContentWidth(windowWidth: 720), 461)
        XCTAssertEqual(needed, 434)
    }

    /// Why the project's table is not in the detail pane: beside the list,
    /// the pane is narrower than the table's floor even at the first size.
    func testTheDetailPaneBesideTheListIsTooNarrowForTheTable() {
        let needed = SettingsModelsGridMetrics.minimumWidth(
            columns: DaemonClient.AgentModels.providers.count)
        for width in [SettingsWindowMetrics.minWidth, SettingsWindowMetrics.defaultWidth] {
            XCTAssertLessThan(SettingsModelsGridMetrics.projectDetailWidth(windowWidth: width),
                              needed, "window \(width)")
        }
    }
}
