import XCTest
@testable import BobPanel

/// The settings page is grouped and filtered from `SettingsMenuModel.rows(...)`
/// by pure statics, so the whole inventory can be driven here with no window.
final class SettingsSearchTests: XCTestCase {

    private func rows(
        settings: DaemonClient.Settings = DaemonClient.Settings(),
        context: DaemonClient.PanelContext = DaemonClient.PanelContext(),
        enrollment: Enrollment = Enrollment(),
        devices: Devices = Devices()
    ) -> [SettingsRow] {
        SettingsMenuModel.rows(
            settings: settings, context: context, enrollment: enrollment,
            devices: devices, recordingShortcut: false, unenrolArmed: nil)
    }

    private func groups(_ rows: [SettingsRow]) -> [SettingsGroup] {
        SettingsSearch.groups(rows)
    }

    private func enrolledOne() -> Enrollment {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/Users/me/proj", label: "proj")]
        return enrollment
    }

    private func pairedOne() -> Devices {
        var devices = Devices()
        devices.available = true
        devices.lanEnabled = true
        devices.remoteStated = true
        devices.devices = [PairedDevice(id: "abc", name: "Kitchen")]
        return devices
    }

    // MARK: - Grouping

    func testGroupsAreTheSubmenusPlusTheLooseHeadingsInInventoryOrderAndNoneIsEmpty() {
        let tree = rows()
        let submenuTitles = tree.compactMap { row -> String? in
            if case .submenu = row.kind { return row.title }
            return nil
        }
        let grouped = groups(tree)
        let titles = grouped.map(\.title)
        // Every top-level submenu is a group, in the order the tree emits them.
        XCTAssertEqual(titles.filter { submenuTitles.contains($0) }, submenuTitles)
        // The loose rows land under headings of their own.
        for heading in ["Sessions", "Board", "This app"] {
            XCTAssertTrue(titles.contains(heading), "\(heading) heading missing")
        }
        XCTAssertFalse(titles.contains(SettingsSearch.fallbackSection))
        for group in grouped {
            XCTAssertFalse(group.entries.isEmpty, "\(group.title) is empty")
        }
        // Order across the whole page follows the inventory: the kill switch
        // leads, Sessions comes after Notifications, and This app is last.
        XCTAssertEqual(titles.first, "Kill switch")
        XCTAssertEqual(titles.dropFirst().first, "Notifications")
        XCTAssertEqual(titles.last, "This app")
        // Its heading holds exactly one row — the switch itself.
        XCTAssertEqual(grouped.first?.entries.map(\.id),
                       [SettingsMenuModel.killRowId])
    }

    func testEveryLooseRowResolvesToAHeadingAcrossFourPermutations() {
        var built = DaemonClient.PanelContext()
        built.build = "built 5 Sep 14:02"
        var rebuildable = DaemonClient.PanelContext()
        rebuildable.canRebuild = true
        rebuildable.build = "built 5 Sep 14:02"
        let permutations: [[SettingsRow]] = [
            rows(),
            rows(context: built),
            rows(context: rebuildable),
            rows(enrollment: enrolledOne(), devices: pairedOne()),
        ]
        for tree in permutations {
            for row in tree {
                switch row.kind {
                case .submenu, .divider: continue
                default:
                    XCTAssertNotNil(SettingsSearch.looseSection(rowId: row.id),
                                    "loose row \(row.id) has no heading")
                }
            }
        }
    }

    func testTheLooseRowsKeepTheirDividerAndSitUnderTheHeadingTheTableNames() {
        var rebuildable = DaemonClient.PanelContext()
        rebuildable.canRebuild = true
        rebuildable.build = "built"
        let grouped = groups(rows(context: rebuildable))
        let app = grouped.first { $0.title == "This app" }!
        XCTAssertEqual(app.entries.map(\.id), [
            "send:restart", "divider:before-quit", "send:quit_app",
        ])
        // The build line and Rebuild are Advanced's now, not loose rows.
        let advanced = grouped.first { $0.title == "Advanced" }!
        XCTAssertTrue(advanced.entries.map(\.id).contains("info:build"))
        XCTAssertTrue(advanced.entries.map(\.id).contains("send:rebuild"))
        let sessions = grouped.first { $0.title == "Sessions" }!
        XCTAssertTrue(sessions.entries.map(\.id).contains("toggle:set_channel"))
        XCTAssertTrue(sessions.entries.map(\.id).contains("toggle:set_auto_compact"))
        let board = grouped.first { $0.title == "Board" }!
        XCTAssertEqual(board.entries.map(\.id),
                       ["toggle:set_board_dispatch", "toggle:set_board_close_terminal"])
        XCTAssertEqual(grouped.filter { $0.title == "Board" }.count, 1)
    }

    func testANestedSubmenuIsKeptAsABlockHeadingAndItsChildrenCarryTheBlock() {
        let pipeline = groups(rows()).first { $0.id == "submenu:pipeline" }!
        let heading = pipeline.entries.first { $0.id == "submenu:agents-per-project" }!
        XCTAssertEqual(heading.block, "")
        let picks = pipeline.entries.filter { $0.id.hasPrefix("pick:set_board_parallel:") }
        XCTAssertEqual(picks.count, SettingsMenuModel.parallelOptions.count)
        for pick in picks {
            XCTAssertEqual(pick.block, "Agents per project")
        }
    }

    func testPickRunsCoalesceIntoOneItem() {
        let pipeline = groups(rows()).first { $0.id == "submenu:pipeline" }!
        let items = SettingsSearch.items(pipeline.entries)
        let runs = items.filter {
            if case .picks = $0 { return true }
            return false
        }
        XCTAssertEqual(runs.count, 1)
        if case .picks(let action, let entries) = runs[0] {
            XCTAssertEqual(action, "set_board_parallel")
            XCTAssertEqual(entries.count, SettingsMenuModel.parallelOptions.count)
        }
    }

    private func modelSettings() -> DaemonClient.Settings {
        DaemonClient.Settings(SettingsMenuModelTests.modelsRaw,
                              base: DaemonClient.Settings())
    }

    private func runs(_ items: [SettingsSearch.Item]) -> [(String, [SettingsEntry])] {
        items.compactMap {
            if case .picks(let action, let entries) = $0 { return (action, entries) }
            return nil
        }
    }

    func testEachAgentModelSlotIsItsOwnChipRun() {
        let grouped = groups(rows(settings: modelSettings()))
        let page = grouped.first { $0.id == SettingsMenuModel.agentModelsRowId }!
        let found = runs(SettingsSearch.items(page.entries))
        // Three providers × three drawable slots, one run each.
        XCTAssertEqual(found.count, 9)
        for (action, entries) in found {
            XCTAssertEqual(action, SettingsMenuModel.agentModelAction)
            let slots = Set(entries.compactMap { entry -> String? in
                if case .pick(_, .map(let record), _) = entry.row.kind { return record["slot"] }
                return nil
            })
            XCTAssertEqual(slots.count, 1, "a run mixes slots")
        }
    }

    func testASearchForAModelNameKeepsOneRunPerSlot() {
        // The `info` row per slot never matches `gpt-6-astra` and is
        // dropped; the runs must still split on the slot the chip sets,
        // never merge into one row reading the same name three times.
        let grouped = groups(rows(settings: modelSettings()))
        let found = SettingsSearch.filter(grouped, query: "gpt-6-astra")
        let hits = found.first { $0.id == SettingsMenuModel.agentModelsRowId }!
        let items = SettingsSearch.items(hits.entries)
        let picks = runs(items)
        XCTAssertEqual(picks.count, items.count, "a model-name search returned a non-chip row")
        XCTAssertEqual(picks.count, 3)
        for (action, entries) in picks {
            XCTAssertEqual(action, SettingsMenuModel.agentModelAction)
            XCTAssertEqual(entries.count, 1)
            XCTAssertEqual(entries.first?.row.title, "gpt-6-astra")
            XCTAssertEqual(entries.first?.block, "codex")
        }
    }

    func testASearchForASlotNameFindsItsChipsThroughTheTooltip() {
        let grouped = groups(rows(settings: modelSettings()))
        let found = SettingsSearch.filter(grouped, query: "claude planner")
        let hits = found.first { $0.id == SettingsMenuModel.agentModelsRowId }!
        let picks = runs(SettingsSearch.items(hits.entries))
        XCTAssertEqual(picks.count, 1)
        XCTAssertEqual(picks[0].1.map(\.row.title),
                       ["Default", "opus", "sonnet", "haiku"])
    }

    func testRunKeySplitsOnTheSlotAndJoinsWithinIt() {
        let grouped = groups(rows(settings: modelSettings()))
        let page = grouped.first { $0.id == SettingsMenuModel.agentModelsRowId }!
        let picks = page.entries.filter { SettingsSearch.runKey($0) != nil }
        let keys = picks.map { SettingsSearch.runKey($0)! }
        XCTAssertEqual(Set(keys).count, 9)
        // Ordinary int picks keep the action as their whole key.
        let pipeline = grouped.first { $0.id == "submenu:pipeline" }!
        let dial = pipeline.entries.compactMap(SettingsSearch.runKey)
        XCTAssertEqual(Set(dial), ["set_board_parallel"])
    }

    // MARK: - Folding

    func testOnlyThePerProjectAgentModelsBlockIsFoldable() {
        let tree = rows(settings: modelSettings(), enrollment: enrolledOne())
        let foldable = SettingsMenuModel.flattened(tree).filter(SettingsSearch.foldable)
        XCTAssertEqual(foldable.map(\.id),
                       [SettingsMenuModel.projectAgentModelsPrefix + "/Users/me/proj"])
    }

    func testAFoldedBlockHidesItsWholeSubtreeUntilOpenedOrSearched() {
        let tree = rows(settings: modelSettings(), enrollment: enrolledOne())
        let projects = groups(tree).first { $0.id == "submenu:projects" }!
        let fold = SettingsMenuModel.projectAgentModelsPrefix + "/Users/me/proj"
        let inside = projects.entries.filter { $0.foldedUnder == fold }
        // Three provider headings, and every info row and chip under them.
        XCTAssertEqual(inside.filter { if case .submenu = $0.row.kind { return true }; return false }.count, 3)
        XCTAssertTrue(inside.contains { SettingsSearch.runKey($0) != nil })
        // The heading itself is never folded under itself.
        let heading = projects.entries.first { $0.id == fold }!
        XCTAssertNil(heading.foldedUnder)
        // Nothing outside the block carries the fold.
        for entry in projects.entries where entry.foldedUnder != nil {
            XCTAssertEqual(entry.foldedUnder, fold)
        }

        let closed = SettingsSearch.visible(projects.entries, open: [], query: "")
        XCTAssertTrue(closed.contains { $0.id == fold })
        XCTAssertFalse(closed.contains { $0.foldedUnder != nil })
        // The pack rows and Un-enrol still draw around the closed heading.
        XCTAssertTrue(closed.contains { $0.id == "custom:unenrol:/Users/me/proj" })

        let opened = SettingsSearch.visible(projects.entries, open: [fold], query: "")
        XCTAssertEqual(opened, projects.entries)

        let searched = SettingsSearch.visible(projects.entries, open: [], query: "opus")
        XCTAssertEqual(searched, projects.entries)
        XCTAssertEqual(SettingsSearch.visible(projects.entries, open: [], query: "  \n"),
                       closed)
    }

    func testTheMachineWidePageIsNeverFolded() {
        let grouped = groups(rows(settings: modelSettings()))
        let page = grouped.first { $0.id == SettingsMenuModel.agentModelsRowId }!
        XCTAssertTrue(page.entries.allSatisfy { $0.foldedUnder == nil })
        XCTAssertEqual(SettingsSearch.visible(page.entries, open: [], query: ""), page.entries)
    }

    // MARK: - Filtering

    func testAnEmptyOrWhitespaceQueryReturnsTheGroupsUnchangedDividersIncluded() {
        let grouped = groups(rows())
        XCTAssertEqual(SettingsSearch.filter(grouped, query: ""), grouped)
        XCTAssertEqual(SettingsSearch.filter(grouped, query: "  \n\t "), grouped)
        XCTAssertTrue(grouped.contains { $0.entries.contains(where: \.isDivider) },
                      "the unfiltered page keeps its dividers")
    }

    func testTwoTokensMatchATitleAndTheGroupIsPipeline() {
        let found = SettingsSearch.filter(groups(rows()), query: "queued automatically")
        XCTAssertEqual(found.count, 1)
        XCTAssertEqual(found[0].title, "Pipeline")
        XCTAssertEqual(found[0].entries.map(\.id), ["toggle:set_board_autostart"])
    }

    func testCompactFindsTheSwitchByTitle() {
        let found = SettingsSearch.filter(groups(rows()), query: "compact")
        let ids = found.flatMap { $0.entries.map(\.id) }
        XCTAssertTrue(ids.contains("toggle:set_auto_compact"))
    }

    func testATooltipOnlyWordFindsItsRow() {
        // "outright" is in the close-terminal tooltip and in no title anywhere in
        // the inventory — the case that proves explanations are searched.
        let tree = rows(enrollment: enrolledOne(), devices: pairedOne())
        let titled = SettingsMenuModel.flattened(tree).filter {
            $0.title.localizedStandardContains("outright")
        }
        XCTAssertTrue(titled.isEmpty, "the word must not be in a title for this to prove anything")
        let found = SettingsSearch.filter(groups(tree), query: "outright")
        XCTAssertEqual(found.flatMap { $0.entries.map(\.id) }, ["toggle:set_board_close_terminal"])
    }

    func testANestedRowIsReachableWithoutNamingItsParent() {
        let found = SettingsSearch.filter(groups(rows(enrollment: enrolledOne())),
                                          query: "un-enrol")
        let entries = found.flatMap(\.entries)
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].id, "custom:unenrol:/Users/me/proj")
        XCTAssertEqual(entries[0].block, "proj")
        XCTAssertEqual(found[0].title, "Projects")
    }

    func testMatchingIsCaseInsensitiveAndNonsenseReturnsNothing() {
        let grouped = groups(rows())
        let upper = SettingsSearch.filter(grouped, query: "NOTIFICATIONS")
        XCTAssertFalse(upper.isEmpty)
        XCTAssertTrue(upper.contains { $0.title == "Notifications" })
        XCTAssertTrue(SettingsSearch.filter(grouped, query: "xqzv-nothing-here").isEmpty)
    }

    func testAGroupHeadingIsSearchable() {
        let found = SettingsSearch.filter(groups(rows()), query: "advanced")
        XCTAssertTrue(found.contains { $0.title == "Advanced" })
    }

    func testNoFilteredGroupContainsADivider() {
        let grouped = groups(rows(enrollment: enrolledOne(), devices: pairedOne()))
        for query in ["a", "e", "un", "quit", "restart"] {
            for group in SettingsSearch.filter(grouped, query: query) {
                XCTAssertFalse(group.entries.contains(where: \.isDivider),
                               "\(group.title) kept a divider for '\(query)'")
            }
        }
    }
}
