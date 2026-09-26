import AppKit
import XCTest
@testable import BobPanel

final class SettingsMenuModelTests: XCTestCase {
    /// The typed-reply row's label, bound once so the inventory, the tick
    /// test and the tooltip test cannot drift from each other.


    private func rows(
        settings: DaemonClient.Settings = DaemonClient.Settings(),
        context: DaemonClient.PanelContext = DaemonClient.PanelContext(),
        enrollment: Enrollment = Enrollment(),
        devices: Devices = Devices(),
        recordingShortcut: Bool = false,
        unenrolArmed: String? = nil,
        unpairArmed: String? = nil,
        packArmed: String? = nil,
        stopSyncArmed: String? = nil,
        killArmed: Bool = false,
        agentModels: DaemonClient.AgentModels? = nil
    ) -> [SettingsRow] {
        SettingsMenuModel.rows(
            settings: settings, context: context, enrollment: enrollment,
            devices: devices, recordingShortcut: recordingShortcut,
            unenrolArmed: unenrolArmed, unpairArmed: unpairArmed,
            packArmed: packArmed, stopSyncArmed: stopSyncArmed,
            killArmed: killArmed,
            agentModels: agentModels)
    }

    /// A menu bar that sends the four Agent models keys: the resolved table,
    /// a one-project override map, the allowlist and the drawable slots —
    /// the shape `app.py`'s context push produces.
    static let modelsRaw: [String: Any] = [
        "agent_models": [
            "claude": ["main": "", "planner": "opus", "card-preparer": "haiku"],
            "codex": ["main": "gpt-6-sol", "planner": "gpt-6-astra", "card-preparer": "gpt-6-luna"],
            "grok": ["main": "grok-4.5", "planner": "grok-4.6", "card-preparer": "grok-4.5"],
        ],
        "agent_models_by_root": [
            "/Users/me/proj": ["claude": ["planner": "sonnet"]],
        ],
        "agent_model_options": [
            "claude": ["main": ["opus", "sonnet", "haiku"],
                       "planner": ["opus", "sonnet", "haiku"],
                       "integration-reviewer": ["opus", "sonnet", "haiku"],
                       "card-preparer": ["opus", "sonnet", "haiku"]],
            "codex": ["main": ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"],
                      "planner": ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"],
                      "integration-reviewer": ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"],
                      "card-preparer": ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]],
            "grok": ["main": ["grok-4.6", "grok-4.5"],
                     "planner": ["grok-4.6", "grok-4.5"],
                     "integration-reviewer": ["grok-4.6", "grok-4.5"],
                     "card-preparer": ["grok-4.6", "grok-4.5"]],
        ],
        "agent_model_slots": ["main", "planner", "card-preparer"],
    ]

    private func modelSettings() -> DaemonClient.Settings {
        DaemonClient.Settings(Self.modelsRaw, base: DaemonClient.Settings())
    }

    private func picks(_ rows: [SettingsRow]) -> [SettingsRow] {
        rows.filter {
            if case .pick = $0.kind { return true }
            return false
        }
    }

    private func submenuRows(_ rows: [SettingsRow], id: String) -> [SettingsRow]? {
        for row in SettingsMenuModel.flattened(rows) where row.id == id {
            if case .submenu(_, let nested) = row.kind { return nested }
        }
        return nil
    }

    private func mapValue(_ row: SettingsRow) -> [String: String]? {
        if case .pick(_, let value, _) = row.kind, case .map(let record) = value {
            return record
        }
        return nil
    }

    private func isSelected(_ row: SettingsRow) -> Bool {
        if case .pick(_, _, let selected) = row.kind { return selected }
        return false
    }

    private func findToggle(_ rows: [SettingsRow], title: String) -> SettingsRow? {
        SettingsMenuModel.flattened(rows).first { row in
            row.title == title && {
                if case .toggle = row.kind { return true }
                return false
            }()
        }
    }

    private func toggleAction(_ row: SettingsRow) -> String? {
        if case .toggle(let action, _) = row.kind { return action }
        return nil
    }

    private func toggleIsOn(_ row: SettingsRow) -> Bool? {
        if case .toggle(_, let isOn) = row.kind { return isOn }
        return nil
    }

    // MARK: - Inventory

    func testDefaultTopLevelInventory() {
        // A menu bar that sends the Agent models table: the full inventory.
        let labels = SettingsMenuModel.topLevelLabels(rows(settings: modelSettings()))
        XCTAssertEqual(labels, [
            SettingsMenuModel.killTitle,
            "—",
            "Notifications",
            "—",
            "Session channel (new sessions)",
            "Compact full sessions",
            "—",
            "Dark Army may start sessions",
            "Pipeline",
            "Agent models",
            "Close the terminal when a card is done",
            "Panel size",
            "Dictation",
            "—",
            "Projects",
            "Devices",
            "Security",
            "—",
            "Design system",
            "—",
            "Advanced",
            "—",
            "Restart",
            "—",
            "Quit",
        ])
        let submenus = rows(settings: modelSettings()).compactMap { row -> String? in
            if case .submenu = row.kind { return row.title }
            return nil
        }
        XCTAssertEqual(submenus, [
            "Notifications", "Pipeline", "Agent models", "Panel size", "Dictation",
            "Projects", "Devices", "Security", "Design system", "Advanced",
        ])
        let removed = [
            "Agent name on tab",
            "Name sessions automatically",
            "Plain-language summaries (new sessions)",
            "End-of-work reports (new sessions)",
            "Answer tool asks from Dark Army",
            "Live cost & context",
            "Wrap up when a card is done",
            "Write card text",
            "File Cards into Folders for Me",
            "Edge ribbon",
            "Nudge when an agent needs you",
            "Ribbon",
            "Launch at login",
            "Session timeout",
        ]
        let titles = Set(SettingsMenuModel.flattened(rows()).map(\.title))
        for title in removed {
            XCTAssertFalse(titles.contains(title), "removed row still present: \(title)")
        }
    }

    func testStaysOpenExactlyForEveryToggleAction() {
        // The full inventory needs the two conditional toggles present: Away
        // access (a daemon that states `remote_enabled`) and Socket link (one
        // that states `relay_ws_enabled` too).
        var devices = Devices()
        devices.remoteStated = true
        devices.relayWSStated = true
        let flat = SettingsMenuModel.flattened(rows(devices: devices))
        let toggleRows = flat.filter {
            if case .toggle = $0.kind { return true }
            return false
        }
        let actions = toggleRows.compactMap(toggleAction)
        XCTAssertEqual(actions, SettingsMenuModel.toggleActions)
        XCTAssertEqual(actions.count, SettingsMenuModel.toggleActions.count)
        for row in toggleRows {
            XCTAssertTrue(row.staysOpen, row.title)
        }
        let withoutRemote = SettingsMenuModel.flattened(rows())
        XCTAssertEqual(withoutRemote.filter { toggleAction($0) != nil }.count,
                       SettingsMenuModel.toggleActions.count - 2)
        XCTAssertFalse(withoutRemote.contains { toggleAction($0) == "set_remote_access" })
        XCTAssertFalse(withoutRemote.contains { toggleAction($0) == "set_relay_ws" })
    }

    func testStaysOpenForTogglesAndPicksOnly() {
        let flat = SettingsMenuModel.flattened(rows())
        for row in flat {
            switch row.kind {
            case .toggle, .pick:
                XCTAssertTrue(row.staysOpen, row.title)
            default:
                XCTAssertFalse(row.staysOpen, row.title)
            }
        }
        let pickActions = Set(flat.compactMap { row -> String? in
            if case .pick(let action, _, _) = row.kind { return action }
            return nil
        })
        XCTAssertEqual(pickActions, ["set_board_parallel", "set_panel_scale"])
    }

    func testTickStatesMirrorSettingsFields() {
        var settings = DaemonClient.Settings()
        settings.boardDispatch = false
        settings.boardCloseTerminal = true
        let tree = rows(settings: settings)
        XCTAssertEqual(toggleIsOn(findToggle(tree, title: "Dark Army may start sessions")!), false)
        XCTAssertEqual(toggleIsOn(findToggle(tree, title: "Close the terminal when a card is done")!), true)

        settings.boardDispatch = true
        settings.boardCloseTerminal = false
        let flipped = rows(settings: settings)
        XCTAssertEqual(toggleIsOn(findToggle(flipped, title: "Dark Army may start sessions")!), true)
        XCTAssertEqual(toggleIsOn(findToggle(flipped, title: "Close the terminal when a card is done")!), false)
    }

    /// The switches nobody flipped left the window on 20 Sep 2026; their
    /// preference keys are never renamed and a hand-edited value still
    /// applies, so the *rows* going is all this pins.
    func testRetiredRowsAreAbsentAndTheirKeysStillDecode() {
        let flat = SettingsMenuModel.flattened(rows())
        // Panel size came back on 25 Sep 2026 (the four steps were
        // reachable only by hand-editing the key); it is not retired.
        for title in ["Reply by typing (VS Code sessions)",
                      "Phone push notifications", "Troubleshooting"] {
            XCTAssertFalse(flat.contains { $0.title == title }, title)
        }
        XCTAssertFalse(flat.contains { toggleAction($0) == "set_typed_reply" })
        XCTAssertFalse(flat.contains { toggleAction($0) == "set_phone_push" })
        var settings = DaemonClient.Settings()
        settings.typedReply = true
        settings.panelScale = 150
        settings.phonePush = false
        XCTAssertTrue(settings.typedReply)
        XCTAssertEqual(PanelScale.resolved(settings.panelScale), 150)
    }

    /// Everything a person presses only when something is wrong, or only on
    /// a development checkout, sits under one heading; Restart and Quit stay
    /// on the top level.
    func testAdvancedHoldsTheInstallersTheLaunchLineTheLogAndTheBuild() {
        let tree = rows()
        let advanced = tree.first { $0.title == "Advanced" }
        guard case .submenu(_, let nested)? = advanced?.kind else {
            return XCTFail("Advanced submenu missing")
        }
        XCTAssertEqual(nested.map(\.id), [
            "send:install_hooks",
            "send:install_vscode_extension",
            SettingsMenuModel.launchRowId,
            "divider:advanced-log",
            "send:open_log",
        ])
        let labels = SettingsMenuModel.topLevelLabels(tree)
        XCTAssertTrue(labels.contains("Restart"))
        XCTAssertTrue(labels.contains("Quit"))
    }

    /// The launch line stays reachable after the first-run checklist has
    /// gone (it lives here, not only on the checklist), and a pending
    /// result updates in place as the context echoes.
    func testTheLaunchLineIsAnInfoRowThatUpdatesFromTheContext() {
        var settings = DaemonClient.Settings()
        let pending = SettingsMenuModel.flattened(rows(settings: settings))
            .first { $0.id == SettingsMenuModel.launchRowId }
        XCTAssertNotNil(pending)
        guard case .info = pending?.kind else { return XCTFail("launch row is not info") }
        XCTAssertEqual(pending?.title,
                       "This launch: hooks checking…; editor extension checking…; login item checking….")
        settings = DaemonClient.Settings(
            ["launch": ["hooks": ["status": "unchanged"],
                        "extension": ["status": "changed"],
                        "login_item": ["status": "failed",
                                       "detail": "login item written but launchd did not register it"]]],
            base: settings)
        let settled = SettingsMenuModel.flattened(rows(settings: settings))
            .first { $0.id == SettingsMenuModel.launchRowId }
        XCTAssertEqual(settled?.title,
                       "This launch had a problem: hooks current; editor extension installed; "
                       + "login item failed — login item written but launchd did not register it.")
    }

    func testEnrollmentThreeState() {
        let unavailable = SettingsMenuModel.flattened(rows(enrollment: Enrollment()))
        XCTAssertTrue(unavailable.contains { $0.title == "This daemon does not report enrolment" })
        XCTAssertFalse(unavailable.contains { $0.title == "No project is enrolled" })

        var empty = Enrollment()
        empty.available = true
        let emptyTree = SettingsMenuModel.flattened(rows(enrollment: empty))
        XCTAssertTrue(emptyTree.contains { $0.title == "No project is enrolled" })
        XCTAssertFalse(emptyTree.contains { $0.title == "This daemon does not report enrolment" })

        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        let listedTree = rows(enrollment: listed)
        let projects = listedTree.first { $0.title == "Projects" }
        guard case .submenu(_, let nested)? = projects?.kind else {
            return XCTFail("Projects submenu missing")
        }
        XCTAssertEqual(nested.map(\.title), [
            "proj", "", "Enrol a folder…",
        ])
        XCTAssertTrue(nested.contains { if case .divider = $0.kind { return true }; return false })
    }

    func testKnowledgeRowPerEnrolledProject() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        let with = SettingsMenuModel.flattened(rows(enrollment: listed))
        XCTAssertTrue(with.contains { $0.title == "Knowledge" })
        let knowledge = with.first { $0.title == "Knowledge" }
        if case .custom(.knowledge(let root))? = knowledge?.kind {
            XCTAssertEqual(root, "/a/proj")
        } else {
            XCTFail("Knowledge must be custom knowledge")
        }
    }

    func testArmedUnenrolLabel() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        let idle = SettingsMenuModel.flattened(
            rows(enrollment: listed, unenrolArmed: nil))
        let armed = SettingsMenuModel.flattened(
            rows(enrollment: listed, unenrolArmed: "/a/proj"))
        XCTAssertTrue(idle.contains { $0.title == "Un-enrol" })
        XCTAssertTrue(armed.contains { $0.title == "Un-enrol — really?" })
        let unenrol = armed.first { $0.title == "Un-enrol — really?" }
        if case .custom(.unenrol(let root))? = unenrol?.kind {
            XCTAssertEqual(root, "/a/proj")
        } else {
            XCTFail("armed Un-enrol must be custom unenrol")
        }
    }

    func testPairDeviceIsAbsentWhilePhoneAccessIsOff() {
        var listed = Devices()
        listed.available = true
        let off = SettingsMenuModel.flattened(rows(devices: listed))
        XCTAssertFalse(off.contains { $0.title == "Pair a device…" })
        XCTAssertTrue(off.contains { $0.title == "Phone access" })
    }

    func testPairDeviceIsAbsentWhenLanIsNotListening() {
        var listed = Devices()
        listed.available = true
        listed.lanEnabled = false
        var settings = DaemonClient.Settings()
        settings.lanAccess = true
        let tree = SettingsMenuModel.flattened(
            rows(settings: settings, devices: listed))
        XCTAssertTrue(tree.contains { $0.title == "Phone access" })
        XCTAssertFalse(tree.contains { $0.title == "Pair a device…" })
    }

    func testPairDeviceIsPresentWhenLanEnabled() {
        var listed = Devices()
        listed.available = true
        listed.lanEnabled = true
        var settings = DaemonClient.Settings()
        settings.lanAccess = true
        let tree = SettingsMenuModel.flattened(
            rows(settings: settings, devices: listed))
        XCTAssertTrue(tree.contains { $0.title == "Pair a device…" })
    }

    /// The exposure caption sits under Phone access only while the listener
    /// is actually bound — the preference alone draws nothing.
    func testLanExposureCaptionIsPresentWhileTheDoorIsOpen() {
        var listed = Devices()
        listed.available = true
        listed.lanEnabled = true
        var settings = DaemonClient.Settings()
        settings.lanAccess = true
        let tree = SettingsMenuModel.flattened(
            rows(settings: settings, devices: listed))
        let caption = tree.first { $0.id == "info:lan-exposure" }
        XCTAssertNotNil(caption)
        XCTAssertTrue(caption?.title.contains("every device on this Wi-Fi") ?? false)
        XCTAssertTrue(caption?.title.contains("VPN") ?? false)
        let toggleIndex = tree.firstIndex { $0.title == "Phone access" }
        let captionIndex = tree.firstIndex { $0.id == "info:lan-exposure" }
        XCTAssertEqual(captionIndex, toggleIndex.map { $0 + 1 })
    }

    func testLanExposureCaptionIsAbsentWhileTheDoorIsShut() {
        var listed = Devices()
        listed.available = true
        listed.lanEnabled = false
        var settings = DaemonClient.Settings()
        settings.lanAccess = true
        let tree = SettingsMenuModel.flattened(
            rows(settings: settings, devices: listed))
        XCTAssertFalse(tree.contains { $0.id == "info:lan-exposure" })
        let off = SettingsMenuModel.flattened(rows(devices: listed))
        XCTAssertFalse(off.contains { $0.id == "info:lan-exposure" })
    }

    func testArmedUnpairLabel() {
        var listed = Devices()
        listed.available = true
        listed.devices = [PairedDevice(id: "dev1", name: "Kitchen")]
        var settings = DaemonClient.Settings()
        settings.lanAccess = true
        let idle = SettingsMenuModel.flattened(
            rows(settings: settings, devices: listed, unpairArmed: nil))
        let armed = SettingsMenuModel.flattened(
            rows(settings: settings, devices: listed, unpairArmed: "dev1"))
        XCTAssertTrue(idle.contains { $0.title == "Un-pair" })
        XCTAssertTrue(armed.contains { $0.title == "Un-pair — really?" })
        let unpair = armed.first { $0.title == "Un-pair — really?" }
        if case .custom(.unpair(let deviceId))? = unpair?.kind {
            XCTAssertEqual(deviceId, "dev1")
        } else {
            XCTFail("armed Un-pair must be custom unpair")
        }
    }

    func testPickerSelectedMapping() {
        var settings = DaemonClient.Settings()
        settings.boardParallel = 2
        let flat = SettingsMenuModel.flattened(rows(settings: settings))

        func pick(_ action: String, selected: Bool) -> [String] {
            flat.compactMap { row in
                guard case .pick(let act, _, let isSelected) = row.kind,
                      act == action, isSelected == selected else { return nil }
                return row.title
            }
        }
        XCTAssertEqual(pick("set_board_parallel", selected: true), ["2"])
        XCTAssertTrue(pick("set_board_parallel", selected: false)
            .contains("1 (one at a time)"))
    }

    func testBuildLineIsDisabledAndRebuildIsOmittedByDefault() {
        var context = DaemonClient.PanelContext()
        context.build = "Build stale (built Aug 30 15:06)"
        var settings = DaemonClient.Settings()
        settings.version = "1.2.3"
        let flat = SettingsMenuModel.flattened(rows(settings: settings, context: context))
        let titles = flat.map(\.title)
        XCTAssertTrue(titles.contains("Build stale (built Aug 30 15:06) — 1.2.3"))
        XCTAssertFalse(titles.contains("Rebuild & Reload"))
        let build = flat.first { $0.id == "info:build" }
        XCTAssertEqual(build?.disabled, true)
        if case .info? = build?.kind {} else { XCTFail("build line must be info") }

        XCTAssertFalse(SettingsMenuModel.flattened(rows()).contains { row in
            row.title.hasPrefix("Rebuild")
        })
    }

    func testCanRebuildFalseOmitsRebuildAndTrueInsertsItUnderAdvanced() {
        XCTAssertFalse(SettingsMenuModel.flattened(rows()).contains { $0.title == "Rebuild & Reload" })

        var context = DaemonClient.PanelContext()
        context.canRebuild = true
        context.rebuildLabel = "Rebuild & Deploy"
        let flat = SettingsMenuModel.flattened(rows(context: context))
        XCTAssertTrue(flat.contains { $0.title == "Rebuild & Deploy" })
        // Under Advanced, never on the top level.
        XCTAssertFalse(SettingsMenuModel.topLevelLabels(rows(context: context))
            .contains("Rebuild & Deploy"))
        let rebuild = flat.first { $0.id == "send:rebuild" }
        XCTAssertEqual(rebuild?.disabled, false)

        var settings = DaemonClient.Settings()
        settings.rebuilding = true
        let busy = SettingsMenuModel.flattened(rows(settings: settings, context: context))
            .first { $0.id == "send:rebuild" }
        XCTAssertEqual(busy?.title, "Rebuilding…")
        XCTAssertEqual(busy?.disabled, true)
    }

    func testTooltipsNonEmptyOnEveryRowThatCarriesHelpToday() {
        var settings = DaemonClient.Settings()
        settings.accessibilityTrusted = false
        let flat = SettingsMenuModel.flattened(rows(settings: settings))
        let mustHaveHelp = [
            "Session channel (new sessions)",
            "Compact full sessions",
            "Dark Army may start sessions",
            "Agents per project",
            "Start queued cards automatically",
            "Close the terminal when a card is done",
            "Record shortcut…",
            "Open Accessibility Settings",
        ]
        for title in mustHaveHelp {
            guard let row = flat.first(where: { $0.title == title }) else {
                XCTFail("missing row \(title)")
                continue
            }
            XCTAssertFalse(row.tooltip.isEmpty, "\(title) must keep its help text")
        }
    }

    @MainActor
    // MARK: - Agent pack

    func testPackRowsAbsentWhenUnavailable() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        let flat = SettingsMenuModel.flattened(rows(enrollment: listed))
        XCTAssertFalse(flat.contains { $0.title == "Install agent pack" })
        XCTAssertFalse(flat.contains { $0.title.contains("Dark Army does not install") })
    }

    func testPackSelfRootsShowRefusalWithoutStamp() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/checkout", label: "checkout")]
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        settings.agentPack.selfRoots = ["/a/checkout"]
        let flat = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(flat.contains {
            $0.title == "Dark Army does not install the pack into its own project"
        })
        XCTAssertFalse(flat.contains { $0.title == "Install agent pack" })
        XCTAssertFalse(flat.contains { $0.title == "Web" })
    }

    func testPackSelfRootShowsRefusalNotProfiles() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/bob", label: "bob")]
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        settings.agentPack.selfRoot = "/a/bob"
        let flat = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(flat.contains {
            $0.title == "Dark Army does not install the pack into its own project"
        })
        XCTAssertFalse(flat.contains { $0.title == "Install agent pack" })
        XCTAssertFalse(flat.contains { $0.title == "Web" })
    }

    func testPackInstallingHidesProfiles() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        settings.agentPack.installing = ["/a/proj"]
        let flat = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(flat.contains { $0.title == "Installing the agent pack…" })
        XCTAssertFalse(flat.contains { $0.title == "Web" })
        XCTAssertFalse(flat.contains { $0.title == "Install agent pack" })
    }

    func testPackOfferHasThreeProfilesAndArms() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        let idle = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(idle.contains { $0.title == "Install agent pack" })
        XCTAssertTrue(idle.contains { $0.title == "Web" })
        XCTAssertTrue(idle.contains { $0.title == "iOS" })
        XCTAssertTrue(idle.contains { $0.title == "Both" })
        let armed = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed,
                 packArmed: "/a/proj|web"))
        XCTAssertTrue(armed.contains { $0.title == "Web — overwrite files?" })
        let web = armed.first { $0.title == "Web — overwrite files?" }
        if case .custom(.installPack(let root, let profile))? = web?.kind {
            XCTAssertEqual(root, "/a/proj")
            XCTAssertEqual(profile, "web")
        } else {
            XCTFail("armed Web must be installPack")
        }
    }

    func testPackOfferDrawsTheDaemonsProfileList() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        let raw: [String: Any] = [
            "agent_pack": [
                "available": true,
                "profiles": [
                    ["id": "web", "label": "Web"],
                    ["id": "python-cli", "label": ""],
                    ["label": "no id"],
                ],
            ] as [String: Any],
        ]
        let settings = DaemonClient.Settings(raw, base: DaemonClient.Settings())
        XCTAssertEqual(settings.agentPack.profiles.map(\.id), ["web", "python-cli"])
        let flat = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(flat.contains { $0.title == "Web" })
        XCTAssertTrue(flat.contains { $0.title == "python-cli" })
        XCTAssertFalse(flat.contains { $0.title == "Both" })
        let mine = flat.first { $0.title == "python-cli" }
        if case .custom(.installPack(_, let profile))? = mine?.kind {
            XCTAssertEqual(profile, "python-cli")
        } else {
            XCTFail("a listed profile must be installPack")
        }
    }

    func testPackInstalledShowsStatusReinstallAndStop() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        var row = DaemonClient.AgentPackProject()
        row.root = "/a/proj"
        row.profile = "web"
        row.lastSyncAt = Date().timeIntervalSince1970
        row.lastResult = "ok"
        settings.agentPack.projects = [row]
        let idle = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(idle.contains { $0.title.hasPrefix("Agent pack: web · synced") })
        XCTAssertTrue(idle.contains { $0.title == "Re-install agent pack" })
        XCTAssertTrue(idle.contains { $0.title == "Stop syncing" })
        let armed = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed,
                 stopSyncArmed: "/a/proj"))
        XCTAssertTrue(armed.contains { $0.title == "Stop syncing — really?" })
    }

    func testPackFailedSyncShowsReason() {
        var listed = Enrollment()
        listed.available = true
        listed.enrolled = [EnrolledProject(root: "/a/proj", label: "proj")]
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        var row = DaemonClient.AgentPackProject()
        row.root = "/a/proj"
        row.profile = "web"
        row.lastResult = "not enrolled"
        settings.agentPack.projects = [row]
        let flat = SettingsMenuModel.flattened(
            rows(settings: settings, enrollment: listed))
        XCTAssertTrue(flat.contains {
            $0.title == "Agent pack: web · last sync: not enrolled"
        })
    }

    func testAgentPackDecodesAbsentAsUnavailable() {
        let settings = DaemonClient.Settings([:], base: DaemonClient.Settings())
        XCTAssertFalse(settings.agentPack.available)
        XCTAssertEqual(settings.agentPack.projects.count, 0)
        let raw: [String: Any] = [
            "agent_pack": [
                "available": true,
                "self_root": "/a/bob",
                "installing": ["/a/proj"],
                "projects": [[
                    "root": "/a/proj",
                    "profile": "ios",
                    "last_sync_at": 1,
                    "last_result": "ok",
                ]],
            ] as [String: Any],
        ]
        let decoded = DaemonClient.Settings(raw, base: DaemonClient.Settings())
        XCTAssertTrue(decoded.agentPack.available)
        XCTAssertEqual(decoded.agentPack.selfRoot, "/a/bob")
        XCTAssertEqual(decoded.agentPack.installing, ["/a/proj"])
        XCTAssertEqual(decoded.agentPack.projects.first?.profile, "ios")
        let withSelves = DaemonClient.Settings(
            ["agent_pack": ["available": true, "self_roots": ["/a/checkout"]]
                as [String: Any]],
            base: DaemonClient.Settings())
        XCTAssertEqual(withSelves.agentPack.selfRoots, ["/a/checkout"])
        XCTAssertTrue(withSelves.agentPack.isSelf("/a/checkout"))
    }

    func testResyncRowOnlyWhenPackAvailable() {
        let hidden = SettingsMenuModel.flattened(rows())
        XCTAssertFalse(hidden.contains { $0.title == "Re-sync agent packs" })
        var settings = DaemonClient.Settings()
        settings.agentPack.available = true
        let shown = SettingsMenuModel.flattened(rows(settings: settings))
        XCTAssertTrue(shown.contains { $0.title == "Re-sync agent packs" })
    }

    // MARK: - The kill switch

    /// First in the inventory, and drawn in the settings window's sidebar
    /// footer beside Restart and Quit, on every section.
    func testKillSwitchLeadsTheInventory() {
        let tree = rows()
        XCTAssertEqual(tree.first?.id, SettingsMenuModel.killRowId)
        XCTAssertEqual(tree.first?.title, SettingsMenuModel.killTitle)
        XCTAssertEqual(tree.first?.danger, true)
        if case .custom(let action) = tree.first?.kind {
            XCTAssertEqual(action, .killSwitch)
        } else {
            XCTFail("the kill switch is not a custom row")
        }
        // And it is the only row on the page drawn in the alarm ink.
        XCTAssertEqual(
            SettingsMenuModel.flattened(tree).filter(\.danger).map(\.id),
            [SettingsMenuModel.killRowId])
    }

    /// Armed, the title says what the second press does — not merely that
    /// something is armed.
    func testKillSwitchArmedTitle() {
        XCTAssertEqual(rows(killArmed: true).first?.title,
                       SettingsMenuModel.killArmedTitle)
        XCTAssertNotEqual(SettingsMenuModel.killTitle,
                          SettingsMenuModel.killArmedTitle)
        // The id does not move with the title: the arming slot and the
        // heading both key off it.
        XCTAssertEqual(rows(killArmed: true).first?.id,
                       SettingsMenuModel.killRowId)
    }

    /// The tooltip has to say what survives, or the press is a guess.
    func testKillSwitchTooltipNamesWhatIsLeftAlone() {
        let tip = SettingsMenuModel.killTooltip
        XCTAssertTrue(tip.contains("VS Code"), tip)
        XCTAssertTrue(tip.contains("no undo"), tip)
    }

    // MARK: - Agent models

    func testAgentModelsPageIsAbsentWhenTheMenuBarSendsNoTable() {
        let ids = SettingsMenuModel.structureIds(rows())
        XCTAssertFalse(ids.contains(SettingsMenuModel.agentModelsRowId))
        XCTAssertFalse(ids.contains { $0.hasPrefix("submenu:agent-models") })
        XCTAssertFalse(DaemonClient.Settings().agentModels.available)
        // An older push carrying only the pack block leaves it absent too.
        let older = DaemonClient.Settings(["agent_pack": ["available": true]],
                                          base: DaemonClient.Settings())
        XCTAssertFalse(older.agentModels.available)
    }

    func testAgentModelsDecodeFromTheContextPush() {
        let models = modelSettings().agentModels
        XCTAssertTrue(models.available)
        XCTAssertEqual(models.slots, ["main", "planner", "card-preparer"])
        XCTAssertEqual(models.chosen(provider: "claude", slot: "planner"), "opus")
        XCTAssertEqual(models.chosen(provider: "grok", slot: "main"), "grok-4.5")
        XCTAssertEqual(models.override(root: "/Users/me/proj", provider: "claude",
                                       slot: "planner"), "sonnet")
        XCTAssertNil(models.override(root: "/Users/me/proj", provider: "codex",
                                     slot: "planner"))
        XCTAssertEqual(models.allowed(provider: "codex", slot: "card-preparer"),
                       ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"])
    }

    func testEachProviderBlockHasOneInfoRowThenTheChipsPerDrawableSlot() {
        let tree = rows(settings: modelSettings())
        let models = modelSettings().agentModels
        for provider in DaemonClient.AgentModels.providers {
            guard let block = submenuRows(tree, id: "submenu:agent-models:\(provider)") else {
                return XCTFail("no block for \(provider)")
            }
            var cursor = 0
            for slot in models.slots {
                let head = block[cursor]
                XCTAssertEqual(head.kind, .info)
                XCTAssertEqual(head.title, SettingsMenuModel.slotLabel(slot))
                let allowed = models.allowed(provider: provider, slot: slot)
                let chips = Array(block[(cursor + 1)...(cursor + 1 + allowed.count)])
                XCTAssertEqual(picks(chips).count, 1 + allowed.count, "\(provider)/\(slot)")
                XCTAssertEqual(chips[0].title, SettingsMenuModel.defaultModelLabel)
                XCTAssertEqual(chips.dropFirst().map(\.title), allowed)
                for chip in chips {
                    guard case .pick(let action, _, _) = chip.kind else {
                        return XCTFail("not a pick: \(chip.id)")
                    }
                    XCTAssertEqual(action, SettingsMenuModel.agentModelAction)
                    let value = mapValue(chip)
                    XCTAssertEqual(value?["provider"], provider)
                    XCTAssertEqual(value?["slot"], slot)
                    XCTAssertNotNil(value?["model"])
                    XCTAssertNil(value?["root"])
                }
                cursor += 1 + 1 + allowed.count
            }
            XCTAssertEqual(cursor, block.count, "\(provider) block has extra rows")
        }
        // `main` reads "Main session"; a role is its brief's name.
        XCTAssertEqual(SettingsMenuModel.slotLabel("main"), "Main session")
        XCTAssertEqual(SettingsMenuModel.slotLabel("card-preparer"), "card-preparer")
        // The shunt skill's cheap helper has a plain name; it is not a brief.
        XCTAssertEqual(SettingsMenuModel.slotLabel("worker"), "Shunt worker")
    }

    func testTheSelectedChipMatchesTheResolvedTable() {
        let tree = rows(settings: modelSettings())
        let claude = submenuRows(tree, id: "submenu:agent-models:claude")!
        let selected = picks(claude).filter(isSelected).map { mapValue($0)! }
        XCTAssertEqual(selected, [
            ["provider": "claude", "slot": "main", "model": ""],
            ["provider": "claude", "slot": "planner", "model": "opus"],
            ["provider": "claude", "slot": "card-preparer", "model": "haiku"],
        ])
        let grok = submenuRows(tree, id: "submenu:agent-models:grok")!
        let grokMain = picks(grok).filter(isSelected).first!
        XCTAssertEqual(grokMain.title, "grok-4.5")
        XCTAssertEqual(mapValue(grokMain)?["model"], "grok-4.5")
        let codex = submenuRows(tree, id: "submenu:agent-models:codex")!
        let codexSelected = picks(codex).filter(isSelected).map { mapValue($0)! }
        XCTAssertEqual(codexSelected, [
            ["provider": "codex", "slot": "main", "model": "gpt-6-sol"],
            ["provider": "codex", "slot": "planner", "model": "gpt-6-astra"],
            ["provider": "codex", "slot": "card-preparer", "model": "gpt-6-luna"],
        ])
    }

    func testAnUndrawableSlotProducesNoRows() {
        let tree = rows(settings: modelSettings())
        let flat = SettingsMenuModel.flattened(tree)
        XCTAssertFalse(flat.contains { $0.id.contains("integration-reviewer") })
        XCTAssertFalse(flat.contains { mapValue($0)?["slot"] == "integration-reviewer" })
    }

    func testTheProjectSubmenuLeadsWithInheritAndCarriesTheRoot() {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/Users/me/proj", label: "proj")]
        let tree = rows(settings: modelSettings(), enrollment: enrollment)
        let root = "/Users/me/proj"
        guard let project = submenuRows(
            tree, id: SettingsMenuModel.projectAgentModelsPrefix + root) else {
            return XCTFail("no per-project Agent models submenu")
        }
        XCTAssertEqual(project.count, 3)
        guard let claude = submenuRows(project, id: "submenu:agent-models:claude:\(root)") else {
            return XCTFail("no claude block under the project")
        }
        let models = modelSettings().agentModels
        var cursor = 0
        for slot in models.slots {
            XCTAssertEqual(claude[cursor].kind, .info)
            let allowed = models.allowed(provider: "claude", slot: slot)
            let chips = Array(claude[(cursor + 1)...(cursor + 2 + allowed.count)])
            XCTAssertEqual(chips.count, 2 + allowed.count)
            XCTAssertEqual(chips[0].title, SettingsMenuModel.inheritLabel)
            XCTAssertEqual(mapValue(chips[0])?["model"], "inherit")
            XCTAssertEqual(chips[1].title, SettingsMenuModel.defaultModelLabel)
            for chip in chips {
                XCTAssertEqual(mapValue(chip)?["root"], root)
                XCTAssertEqual(mapValue(chip)?["provider"], "claude")
                XCTAssertEqual(mapValue(chip)?["slot"], slot)
            }
            // Inherit is selected where the override map has no entry; the
            // override's own name where it has one.
            let chosen = chips.filter(isSelected)
            XCTAssertEqual(chosen.count, 1, slot)
            if slot == "planner" {
                XCTAssertEqual(chosen.first?.title, "sonnet")
            } else {
                XCTAssertEqual(chosen.first?.title, SettingsMenuModel.inheritLabel)
            }
            cursor += 2 + allowed.count + 1
        }
        XCTAssertEqual(cursor, claude.count)
        // The machine-wide page carries no root.
        let global = submenuRows(tree, id: "submenu:agent-models:claude")!
        XCTAssertTrue(picks(global).allSatisfy { mapValue($0)?["root"] == nil })
        XCTAssertFalse(picks(global).contains { $0.title == SettingsMenuModel.inheritLabel })
    }

    func testTheProjectSubmenuIsAbsentWithoutATable() {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/Users/me/proj", label: "proj")]
        let ids = SettingsMenuModel.structureIds(rows(enrollment: enrollment))
        XCTAssertFalse(ids.contains { $0.hasPrefix("submenu:agent-models") })
        XCTAssertFalse(ids.contains {
            $0.hasPrefix(SettingsMenuModel.projectAgentModelsPrefix) })
    }

    func testEveryAgentModelChipNamesItsProviderAndSlotOnHover() {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = [EnrolledProject(root: "/Users/me/proj", label: "proj")]
        let tree = rows(settings: modelSettings(), enrollment: enrollment)
        let chips = SettingsMenuModel.flattened(tree).filter {
            if case .pick(let action, _, _) = $0.kind {
                return action == SettingsMenuModel.agentModelAction
            }
            return false
        }
        XCTAssertFalse(chips.isEmpty)
        for chip in chips {
            let value = mapValue(chip)!
            let expected = SettingsMenuModel.agentModelTooltip(
                provider: value["provider"]!, slot: value["slot"]!, root: value["root"])
            XCTAssertEqual(chip.tooltip, expected)
            XCTAssertTrue(chip.tooltip.hasPrefix(value["provider"]! + " · "))
            XCTAssertTrue(chip.tooltip.contains(SettingsMenuModel.slotLabel(value["slot"]!)))
            if value["root"] != nil {
                XCTAssertTrue(chip.tooltip.hasSuffix(" · proj"), chip.tooltip)
            }
        }
    }

    func testEveryAgentModelValueIsAMapAndBoxesToADictionary() {
        let tree = rows(settings: modelSettings())
        let chips = SettingsMenuModel.flattened(tree).filter {
            if case .pick(let action, _, _) = $0.kind {
                return action == SettingsMenuModel.agentModelAction
            }
            return false
        }
        XCTAssertFalse(chips.isEmpty)
        var ids = Set<String>()
        for chip in chips {
            guard case .pick(_, let value, _) = chip.kind,
                  case .map(let record) = value else {
                return XCTFail("not a map: \(chip.id)")
            }
            XCTAssertEqual(Set(record.keys), ["provider", "slot", "model"])
            XCTAssertTrue(value.boxed is [String: String])
            XCTAssertTrue(chip.staysOpen)
            XCTAssertTrue(ids.insert(chip.id).inserted, "duplicate id \(chip.id)")
        }
    }

    // MARK: - Security

    func testSecurityGroupStatesTheOpenAlertCountAndOffersTheLog() {
        let none = SettingsMenuModel.securityRows(SecuritySection(available: true))
        XCTAssertEqual(none.map(\.title), ["No open alerts", "Access log…"])
        XCTAssertEqual(none[1].kind, .custom(.accessLog))
        let one = SettingsMenuModel.securityRows(
            SecuritySection(available: true, alerts: [AccessAlert(id: "a")]))
        XCTAssertEqual(one[0].title, "1 open alert")
        let two = SettingsMenuModel.securityRows(
            SecuritySection(available: true,
                            alerts: [AccessAlert(id: "a"), AccessAlert(id: "b")]))
        XCTAssertEqual(two[0].title, "2 open alerts")
        // An older daemon states nothing: the log is unavailable, and
        // nothing it happens to carry is counted.
        let stale = SettingsMenuModel.securityRows(
            SecuritySection(available: false, alerts: [AccessAlert(id: "a")]))
        XCTAssertEqual(stale[0].title, "Access log unavailable")
        XCTAssertEqual(SettingsMenuModel.securityRows(SecuritySection())[0].title,
                       "Access log unavailable")
    }
}
