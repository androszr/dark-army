import XCTest
@testable import BobPanel

/// The settings window's sidebar is a *placement* of `SettingsMenuModel.rows`:
/// `SettingsSections` names a section for every group and adds no row. These
/// tests drive the whole inventory through it with no window, so a new group
/// that reaches no section, or a row that lands nowhere, fails here.
final class SettingsSectionsTests: XCTestCase {

    // MARK: - Fixtures

    private func rows(
        settings: DaemonClient.Settings = DaemonClient.Settings(),
        context: DaemonClient.PanelContext = DaemonClient.PanelContext(),
        enrollment: Enrollment = Enrollment(),
        devices: Devices = Devices(),
        security: SecuritySection = SecuritySection()
    ) -> [SettingsRow] {
        SettingsMenuModel.rows(
            settings: settings, context: context, enrollment: enrollment,
            devices: devices, recordingShortcut: false, unenrolArmed: nil,
            security: security)
    }

    private func modelSettings() -> DaemonClient.Settings {
        DaemonClient.Settings(SettingsMenuModelTests.modelsRaw, base: DaemonClient.Settings())
    }

    private func enrolledOne() -> Enrollment {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/Users/me/proj", label: "proj")]
        return enrollment
    }

    /// One paired phone with every away-path fact stated, so every Devices
    /// row the tree can emit is emitted.
    private func pairedEverything() throws -> Devices {
        var devices = Devices()
        devices.available = true
        devices.lanEnabled = true
        devices.remoteStated = true
        devices.relayWSStated = true
        var phone = PairedDevice(id: "abc", name: "Kitchen")
        phone.relay = true
        phone.leaseDays = 7
        phone.leaseExpiresAt = Date().timeIntervalSince1970 + 3600
        phone.lockScreenActions = true
        devices.devices = [phone]
        let json = #"{"device_id": "abc", "action": "board_start", "at": 1000, "ok": true}"#
        devices.remoteActivity = [try JSONDecoder().decode(RemoteAction.self,
                                                           from: Data(json.utf8))]
        return devices
    }

    /// The four permutations `SettingsSearchTests` walks, plus the models table.
    private func permutations() throws -> [[SettingsRow]] {
        var built = DaemonClient.PanelContext()
        built.build = "built 5 Sep 14:02"
        var rebuildable = DaemonClient.PanelContext()
        rebuildable.canRebuild = true
        rebuildable.build = "built 5 Sep 14:02"
        var remoteOn = DaemonClient.Settings()
        remoteOn.remoteAccess = true
        remoteOn.relayWS = true
        return [
            rows(),
            rows(context: built),
            rows(context: rebuildable),
            rows(settings: remoteOn, enrollment: enrolledOne(), devices: try pairedEverything()),
            rows(settings: modelSettings(), enrollment: enrolledOne()),
        ]
    }

    private func layout(_ tree: [SettingsRow]) -> (pages: [SettingsPage], footer: [SettingsEntry]) {
        let groups = SettingsSearch.groups(tree)
        return (SettingsSections.pages(groups), SettingsSections.footer(groups))
    }

    private func hits(_ query: String, in tree: [SettingsRow]) -> [SettingsSearch.Hit] {
        let (pages, footer) = layout(tree)
        return SettingsSearch.hits(pages: pages, footer: footer, query: query)
    }

    // MARK: - The eight sections

    func testTheEightSectionsInOrder() {
        XCTAssertEqual(SettingsSectionID.allCases.map(\.title),
                       ["General", "Sessions", "Board", "Models", "Projects", "Devices", "Security", "Advanced"])
        XCTAssertEqual(SettingsSectionID.allCases.map(\.keyDigit), Array(1...8))
        for section in SettingsSectionID.allCases {
            XCTAssertFalse(section.subtitle.isEmpty, "\(section) has no lede")
        }
        // Always eight pages, in that order, whatever the tree carries.
        XCTAssertEqual(SettingsSections.pages([]).map(\.id), SettingsSectionID.allCases)
    }

    // MARK: - No fallback

    func testEveryGroupHasAPlacementAcrossFivePermutations() throws {
        for tree in try permutations() {
            for group in SettingsSearch.groups(tree) {
                XCTAssertNotNil(SettingsSections.placement(groupId: group.id),
                                "group \(group.id) reaches no section")
            }
        }
        XCTAssertNil(SettingsSections.placement(groupId: "loose:\(SettingsSearch.fallbackSection)"),
                     "the made-up fallback heading must not have a section either")
        XCTAssertNil(SettingsSections.placement(groupId: "submenu:nothing-like-it"))
    }

    /// Every row in the tree lands exactly once: as a page's group (a
    /// top-level submenu), as an entry of one page, or in the sidebar's foot.
    func testEveryRowLandsInExactlyOnePageOrTheFootAcrossFivePermutations() throws {
        for tree in try permutations() {
            let (pages, footer) = layout(tree)
            var landed: [String] = []
            for page in pages {
                for group in page.groups {
                    if group.id.hasPrefix("submenu:") { landed.append(group.id) }
                    landed.append(contentsOf: group.entries.filter { !$0.isDivider }.map(\.id))
                }
            }
            landed.append(contentsOf: footer.map(\.id))
            let expected = SettingsMenuModel.structureIds(tree).filter { !$0.hasPrefix("divider:") }
            XCTAssertEqual(landed.sorted(), expected.sorted())
            XCTAssertEqual(Set(landed).count, landed.count, "a row landed twice")
        }
    }

    func testBoardAndAdvancedHoldTheirGroupsInTheTablesOrder() {
        let (pages, _) = layout(rows())
        let board = pages.first { $0.id == .board }!
        XCTAssertEqual(board.groups.map(\.id), ["loose:Board", "submenu:pipeline"])
        let advanced = pages.first { $0.id == .advanced }!
        XCTAssertEqual(advanced.groups.map(\.id), ["submenu:advanced", "submenu:design-system"])
        let general = pages.first { $0.id == .general }!
        XCTAssertEqual(general.groups.map(\.id),
                       ["submenu:notifications", "submenu:panel-size", "submenu:dictation"])
        let sessions = pages.first { $0.id == .sessions }!
        XCTAssertEqual(sessions.groups.first?.entries.map(\.id),
                       ["toggle:set_channel", "toggle:set_auto_compact"])
    }

    func testTheFootIsTheKillSwitchRestartAndQuit() {
        let (_, footer) = layout(rows())
        XCTAssertEqual(footer.map(\.id),
                       [SettingsMenuModel.killRowId, "send:restart", "send:quit_app"])
        XCTAssertEqual(SettingsSections.placement(groupId: "loose:Kill switch"), .footer)
        XCTAssertEqual(SettingsSections.placement(groupId: "loose:This app"), .footer)
        // The armed title comes from the tree, not the foot.
        let groups = SettingsSearch.groups(SettingsMenuModel.rows(
            settings: DaemonClient.Settings(), context: DaemonClient.PanelContext(),
            enrollment: Enrollment(), recordingShortcut: false, unenrolArmed: nil,
            killArmed: true))
        XCTAssertEqual(SettingsSections.footer(groups).first?.row.title,
                       SettingsMenuModel.killArmedTitle)
    }

    func testModelsIsEmptyWithoutATableAndHoldsItWithOne() {
        let without = layout(rows()).pages.first { $0.id == .models }!
        XCTAssertTrue(without.groups.isEmpty)
        let with = layout(rows(settings: modelSettings())).pages.first { $0.id == .models }!
        XCTAssertEqual(with.groups.map(\.id), [SettingsMenuModel.agentModelsRowId])
        XCTAssertFalse(with.groups[0].tooltip.isEmpty,
                       "the group carries its submenu's hover text")
    }

    // MARK: - The Security badge

    func testTheBadgeIsTheOpenAlertCountAndNothingElse() {
        let one = SecuritySection(available: true, alerts: [AccessAlert(id: "a")])
        let two = SecuritySection(available: true,
                                  alerts: [AccessAlert(id: "a"), AccessAlert(id: "b")])
        XCTAssertEqual(SettingsSections.badge(for: .security, security: one), "1")
        XCTAssertEqual(SettingsSections.badge(for: .security, security: two), "2")
        XCTAssertNil(SettingsSections.badge(for: .security,
                                            security: SecuritySection(available: true)))
        XCTAssertNil(SettingsSections.badge(
            for: .security, security: SecuritySection(available: false,
                                                       alerts: [AccessAlert(id: "a")])))
        XCTAssertNil(SettingsSections.badge(for: .board, security: one))
        // The page states the same count in words.
        let security = rows(security: one).first { $0.id == "submenu:security" }!
        if case .submenu(_, let nested) = security.kind {
            XCTAssertEqual(nested.first?.title, "1 open alert")
        }
    }

    // MARK: - Dependents

    func testEveryDependentNamesAnIdTheTreeCarries() throws {
        var remoteOn = DaemonClient.Settings()
        remoteOn.remoteAccess = true
        var devices = try pairedEverything()
        devices.lanEnabled = false // so `info:away-needs-lan` is emitted
        let tree = rows(settings: remoteOn, devices: devices)
        let ids = Set(SettingsMenuModel.structureIds(tree))
        for (master, dependents) in SettingsSections.dependents {
            XCTAssertTrue(ids.contains(master), "master \(master) is not in the tree")
            for id in dependents {
                XCTAssertTrue(ids.contains(id), "dependent \(id) is not in the tree")
                XCTAssertEqual(SettingsSections.master(of: id), master)
            }
        }
        XCTAssertNil(SettingsSections.master(of: "toggle:set_channel"))
    }

    // MARK: - Hits and crumbs

    func testOutrightFindsOneRowWithTheTrailBoard() {
        let found = hits("outright", in: rows())
        XCTAssertEqual(found.map(\.entry.id), ["toggle:set_board_close_terminal"])
        XCTAssertEqual(found.first?.crumb, "Board")
        XCTAssertEqual(found.first?.section, .board)
        XCTAssertNil(found.first?.projectRoot)
    }

    func testAPipelineRowCarriesBoardThenPipeline() {
        let found = hits("queued automatically", in: rows())
        XCTAssertEqual(found.map(\.entry.id), ["toggle:set_board_autostart"])
        XCTAssertEqual(found.first?.crumb, "Board › Pipeline")
    }

    func testAProjectRowCarriesTheProjectsLabelInTheTrailAndItsRootOffTheId() {
        let found = hits("un-enrol", in: rows(enrollment: enrolledOne()))
        let unenrol = found.first { $0.entry.id == "custom:unenrol:/Users/me/proj" }
        XCTAssertNotNil(unenrol)
        XCTAssertEqual(unenrol?.crumb, "Projects › proj")
        XCTAssertEqual(unenrol?.section, .projects)
        XCTAssertEqual(unenrol?.projectRoot, "/Users/me/proj")
    }

    /// Two projects sharing a label keep their own roots: the root comes off
    /// the `submenu:project:<root>` id, never the label.
    func testTwoProjectsSharingALabelKeepTheirOwnRoots() {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/a/app", label: "app"),
                               EnrolledProject(root: "/b/app", label: "app")]
        let found = hits("knowledge", in: rows(enrollment: enrollment))
            .filter { $0.entry.id.hasPrefix("custom:knowledge:") }
        XCTAssertEqual(found.map(\.projectRoot), ["/a/app", "/b/app"])
        // The enrol row after the projects belongs to none of them.
        let enrol = hits("enrol a folder", in: rows(enrollment: enrollment))
        XCTAssertEqual(enrol.map(\.entry.id), ["custom:enrolFolder"])
        XCTAssertNil(enrol.first?.projectRoot)
        XCTAssertEqual(enrol.first?.crumb, "Projects")
    }

    func testAModelNameFindsOneHitPerSlotUnderModelsAndTheAssistant() {
        let found = hits("gpt-6-astra", in: rows(settings: modelSettings()))
        XCTAssertEqual(found.count, 3)
        for hit in found {
            XCTAssertEqual(hit.crumb, "Models › codex")
            XCTAssertEqual(hit.section, .models)
        }
    }

    func testQuitFindsTheSidebarFoot() {
        let found = hits("quit", in: rows())
        let foot = found.filter { $0.section == nil }
        XCTAssertEqual(foot.map(\.entry.id), ["send:quit_app"])
        XCTAssertEqual(foot.first?.crumb, SettingsSearch.footerCrumb)
        XCTAssertEqual(SettingsSearch.footerCrumb, "Sidebar")
        // The foot comes last.
        XCTAssertEqual(found.last?.section, nil)
    }

    func testAnEmptyOrWhitespaceQueryFindsNothing() {
        XCTAssertTrue(hits("", in: rows()).isEmpty)
        XCTAssertTrue(hits("   \n\t", in: rows()).isEmpty)
    }

    func testHitsFollowPageOrder() {
        // "on" is everywhere: the hits must come page by page, in order.
        let found = hits("on", in: rows(settings: modelSettings(), enrollment: enrolledOne()))
        let order = found.compactMap(\.section).map { SettingsSectionID.allCases.firstIndex(of: $0)! }
        XCTAssertEqual(order, order.sorted())
    }

    // MARK: - Drawn lines

    /// Agents per project is one line: its submenu's title and explanation
    /// over the four picks, drawn as a segmented control.
    func testAPickRunUnderASubmenuIsOneLineHeadedByIt() {
        let pipeline = SettingsSearch.groups(rows()).first { $0.id == "submenu:pipeline" }!
        let lines = SettingsControls.lines(pipeline.entries)
        guard case .picks(let heading, let action, let run) = lines.first else {
            return XCTFail("the first line is not the dial: \(lines)")
        }
        XCTAssertEqual(heading?.id, "submenu:agents-per-project")
        XCTAssertEqual(action, "set_board_parallel")
        XCTAssertEqual(run.count, SettingsMenuModel.parallelOptions.count)
        XCTAssertLessThanOrEqual(run.count, SettingsControls.segmentedLimit)
        XCTAssertEqual(lines.dropFirst().map(\.id),
                       ["toggle:set_board_autostart", "toggle:set_board_own_terminal"])
    }

    func testAPhonesAwayLengthsAreOneSegmentedLineTickedFromTheGrant() throws {
        var remoteOn = DaemonClient.Settings()
        remoteOn.remoteAccess = true
        let devices = SettingsSearch.groups(rows(settings: remoteOn,
                                                 devices: try pairedEverything()))
            .first { $0.id == "submenu:devices" }!
        let lines = SettingsControls.lines(devices.entries)
        let away = lines.compactMap { line -> [SettingsEntry]? in
            if case .choices(nil, let run, true) = line { return run }
            return nil
        }
        XCTAssertEqual(away.count, 1)
        XCTAssertEqual(away.first?.map(\.row.title),
                       SettingsMenuModel.awayDayChoices.map { "Grant \(SettingsMenuModel.grantWords($0))" })
        XCTAssertEqual(away.first?.filter(\.row.checked).map(\.id), ["custom:away-days:abc:7"])
    }

    func testThePackProfileInForceIsChecked() {
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        settings.agentPack.projects = [DaemonClient.AgentPackProject(
            ["root": "/Users/me/proj", "profile": "ios", "last_result": "ok"])]
        let flat = SettingsMenuModel.flattened(rows(settings: settings, enrollment: enrolledOne()))
        XCTAssertEqual(flat.filter(\.checked).map(\.id), ["custom:installPack:/Users/me/proj:ios"])
        // `checked` is a drawing hint, never part of the id.
        XCTAssertTrue(flat.contains { $0.id == "custom:installPack:/Users/me/proj:web" })
    }

    func testTheIdsAreTheSameWithOrWithoutATickedChoice() throws {
        var remoteOn = DaemonClient.Settings()
        remoteOn.remoteAccess = true
        var devices = try pairedEverything()
        let before = SettingsMenuModel.structureIds(rows(settings: remoteOn, devices: devices))
        devices.devices[0].leaseDays = 3
        let after = SettingsMenuModel.structureIds(rows(settings: remoteOn, devices: devices))
        XCTAssertEqual(before, after)
    }

    func testFactsThatAreSentencesAreProse() {
        XCTAssertTrue(SettingsControls.isProseFact("info:lan-exposure"))
        XCTAssertTrue(SettingsControls.isProseFact("info:pack-self:/x"))
        XCTAssertFalse(SettingsControls.isProseFact("info:build"))
        XCTAssertFalse(SettingsControls.isProseFact("info:project-root:/x"))
    }

    func testTheSidebarArrowsWrap() {
        XCTAssertEqual(SettingsSidebar.step(from: .general, by: -1), .advanced)
        XCTAssertEqual(SettingsSidebar.step(from: .advanced, by: 1), .general)
        XCTAssertEqual(SettingsSidebar.step(from: .board, by: 1), .models)
    }
}
