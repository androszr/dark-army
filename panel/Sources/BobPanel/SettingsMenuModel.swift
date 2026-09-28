import Foundation

/// One row of the ⋯ menu, as a descriptor. The AppKit host builds an `NSMenu`
/// from these; tests pin the inventory against them. Labels, tooltips, grouping
/// and action names are copied from the SwiftUI menu this replaced — a renamed
/// key would read as absent on every installed machine.
struct SettingsRow: Equatable, Identifiable {
    var id: String
    var title: String
    var kind: Kind
    var tooltip: String = ""
    var disabled: Bool = false
    /// Draws in `Theme.alarm` rather than the phosphor. Colour is never the
    /// only signal — the title already says what the press does — so this is
    /// additive, and today exactly one row sets it.
    var danger: Bool = false
    /// A drawing hint for a `.custom` row that stands in a choice — an
    /// away-days grant, a pack profile: this one is the length or profile in
    /// force. Not part of the id, and never a `.pick`: those rows post to the
    /// daemon, not on the preference channel.
    var checked: Bool = false

    /// True for checkboxes and exclusive pickers (Agents per project).
    /// Action rows, Un-enrol and the rest still close.
    var staysOpen: Bool {
        switch kind {
        case .toggle, .pick: return true
        default: return false
        }
    }

    /// What `Panel.send` already takes, made equatable so tests can pin it.
    /// `Panel.send`'s `value` is `Any?`; there is no parallel type to reuse.
    enum SendValue: Equatable {
        case bool(Bool)
        case int(Int)
        case string(String)
        /// A small string-keyed record — the Agent models chips send
        /// `provider` / `slot` / `model` (and `root` per project). Boxed as
        /// the dictionary itself, which `Panel.send` already JSON-encodes.
        /// `[String: String]`, never `[String: Any]`: this enum is Equatable.
        case map([String: String])

        var boxed: Any {
            switch self {
            case .bool(let value): return value
            case .int(let value): return value
            case .string(let value): return value
            case .map(let value): return value
            }
        }
    }

    enum CustomAction: Equatable {
        case enrolFolder
        case recordShortcut
        case unenrol(root: String)
        case pairDevice
        case unpair(deviceId: String)
        case relayAddress
        /// Grant this phone a length, in days, from `awayDayChoices`.
        case awayDays(deviceId: String, days: Int)
        /// End this phone's away access now — the same verb at zero days.
        case awayOff(deviceId: String)
        /// Let this phone answer a buzz from its lock screen, or stop.
        case lockScreenActions(deviceId: String, enabled: Bool)
        /// Put one side (`read` / `write`) of the bot's access in one
        /// position from `botAccessModes`; the same mode again restarts
        /// its timer.
        case botAccess(deviceId: String, side: String, mode: String)
        /// Install (or re-install) the shared agent pack under `root`.
        case installPack(root: String, profile: String)
        /// Drop `root` from the pack ledger; leave the files where they are.
        case stopPackSync(root: String)
        /// Inspect this enrolled project's knowledge notes.
        case knowledge(root: String)
        /// Open the Checks window on this enrolled project's manual checks.
        case manualChecks(root: String)
        /// Open the phone doors' access log window.
        case accessLog
        /// Open the bundled, offline Signal component workshop.
        case designSystem
        /// Stop everything of Dark Army's that is running, in one press.
        case killSwitch
        /// Copy the loopback door's desk token to the clipboard, armed then
        /// confirmed — for the person's own command-line tools.
        case copyDeskToken
    }

    enum Kind: Equatable {
        case toggle(action: String, isOn: Bool)
        case send(action: String, value: SendValue?)
        case pick(action: String, value: SendValue, selected: Bool)
        case custom(CustomAction)
        case info
        case divider
        case submenu(title: String, rows: [SettingsRow])
    }
}

enum SettingsMenuModel {
    /// The dial's rungs, matching the daemon's own `BOARD_PARALLEL_MIN`…`MAX`.
    /// The floor is 1 because a 0 would park every queue for ever — which is
    /// "Start queued cards automatically" off wearing a number's clothes —
    /// and the ceiling is 4 because the risk it accepts grows pairwise and a
    /// project's pipeline is meant to fit on a screen.
    static let parallelOptions: [(label: String, value: Int)] = [
        ("1 (one at a time)", 1),
        ("2", 2),
        ("3", 3),
        ("4", 4),
    ]

    /// Every checkbox action, in the order the tree emits them. Away access
    /// is only in the tree while the daemon states `remote_enabled`, and
    /// Socket link only while it also states `relay_ws_enabled`, so a
    /// default `Settings()` yields all of these bar those two.
    static let toggleActions: [String] = [
        "set_notification_sound",
        "set_notification_banners",
        "set_channel",
        "set_auto_compact",
        "set_board_dispatch",
        "set_board_autostart",
        "set_board_own_terminal",
        "set_board_close_terminal",
        "set_lan_access",
        "set_remote_access",
        "set_relay_ws",
    ]

    /// `Panel.send` carries the state a switch *wants*. Every surviving
    /// toggle is a Bool.
    static func togglePayload(action _: String, wanted: Bool) -> SettingsRow.SendValue {
        return .bool(wanted)
    }

    /// Today's ⋯ inventory, verbatim. Grouped by what a row is *about*:
    /// notifications, then what Dark Army does inside your sessions, then the board,
    /// then repairs, then the app itself.
    static func rows(
        settings: DaemonClient.Settings,
        context: DaemonClient.PanelContext,
        enrollment: Enrollment,
        devices: Devices = Devices(),
        recordingShortcut: Bool,
        unenrolArmed: String?,
        unpairArmed: String? = nil,
        awayOffArmed: String? = nil,
        packArmed: String? = nil,
        stopSyncArmed: String? = nil,
        killArmed: Bool = false,
        deskTokenArmed: Bool = false,
        agentModels: DaemonClient.AgentModels? = nil,
        security: SecuritySection = SecuritySection()
    ) -> [SettingsRow] {
        let s = settings
        // The table rides the settings push; a caller may hand a different
        // one (tests do). Nil means "whatever the settings carry".
        let models = agentModels ?? s.agentModels
        var out: [SettingsRow] = []

        // First row on the page, because the moment you want it is the moment
        // you do not want to read a page to find it. Armed then confirmed, and
        // the armed title says what the second press does rather than only
        // that it is armed.
        out.append(SettingsRow(
            id: killRowId,
            title: killArmed ? killArmedTitle : killTitle,
            kind: .custom(.killSwitch),
            tooltip: killTooltip,
            danger: true))
        out.append(divider("after-kill"))

        out.append(submenu("Notifications", id: "submenu:notifications", rows: [
            toggle("Sound", action: "set_notification_sound", isOn: s.notificationSound),
            toggle("Banners", action: "set_notification_banners", isOn: s.notificationBanners),
            divider("notifications-end"),
            send("Open System Settings…", action: "open_notification_settings"),
        ]))

        out.append(divider("after-notifications"))

        out.append(toggle(
            "Session channel (new sessions)",
            action: "set_channel", isOn: s.channelEnabled,
            tooltip: channelHelp(s)))
        out.append(toggle(
            "Compact full sessions",
            action: "set_auto_compact", isOn: s.autoCompact,
            tooltip: "When a session runs out of context, type /compact into its "
                + "terminal instead of asking you to. Needs the session to "
                + "be in VS Code; if the compact does not land, the "
                + "notification comes back."))

        out.append(divider("after-session"))

        out.append(toggle(
            "Dark Army may start sessions",
            action: "set_board_dispatch", isOn: s.boardDispatch,
            tooltip: "The one switch for Dark Army opening an assistant at all. "
                + "It covers Start on a board card, which opens that assistant "
                + "in the project's own terminal, and + TERMINAL on the Agents "
                + "list, which opens one in a project you pick with no card "
                + "behind it. Off means neither button is there at all."))

        out.append(submenu(
            "Pipeline", id: "submenu:pipeline",
            rows: [
                submenu(
                    "Agents per project",
                    id: "submenu:agents-per-project",
                    tooltip: "How many assistants may work at once in any one project. "
                        + "Above 1, two agents may edit the same files in one "
                        + "project; Dark Army no longer holds work back for that. "
                        + "This is the default: a project given its own dial "
                        + "on the pipeline heading keeps that instead.",
                    rows: parallelOptions.map { option in
                        pick(option.label, action: "set_board_parallel",
                             value: .int(option.value),
                             selected: s.boardParallel == option.value)
                    }),
                toggle(
                    "Start queued cards automatically",
                    action: "set_board_autostart", isOn: s.boardAutostart,
                    tooltip: "When a project has no free place, the card waits its "
                        + "turn. On, Dark Army starts the next one as soon as a place "
                        + "frees. Off, it waits for your press — and says so on "
                        + "the card."),
                toggle(
                    "Dark Army's own terminal",
                    action: "set_board_own_terminal", isOn: s.boardOwnTerminal,
                    tooltip: "Start runs the assistant on a terminal Dark Army "
                        + "itself opens, drawn under the session in this window "
                        + "and watchable from the phone — no editor window "
                        + "needed. Off, Start opens it in the project's VS Code "
                        + "window as before. Refine always uses VS Code. "
                        + "Restarting or quitting Dark Army ends these sessions."),
            ]))

        // Absent, not empty, against a menu bar that sends no table.
        if models.available {
            out.append(submenu(
                "Agent models", id: agentModelsRowId,
                tooltip: agentModelsTooltip,
                rows: agentModelRows(models, root: nil)))
        }

        out.append(toggle(
            "Close the terminal when a card is done",
            action: "set_board_close_terminal", isOn: s.boardCloseTerminal,
            tooltip: "Moving a card to Done closes that assistant's terminal tab "
                + "outright — there is no undo. Where Dark Army cannot close it "
                + "(an editor window on an older extension, or an assistant "
                + "it has no handle on), the tab is left alone."))

        // The one size dial, back on 25 Sep 2026: the four steps exist and
        // were reachable only by hand-editing `panel_scale`. The key is never
        // renamed; the action rides the stdin/stdout channel, no daemon table.
        out.append(submenu(
            "Panel size", id: "submenu:panel-size",
            tooltip: "Grows the window's text, rows, faces and spacing together. "
                + "Does not change the colours.",
            rows: PanelScale.steps.map { step in
                pick(step.label, action: "set_panel_scale",
                     value: .int(step.percent),
                     selected: PanelScale.resolved(s.panelScale) == step.percent)
            }))

        out.append(submenu("Dictation", id: "submenu:dictation",
                           rows: dictationRows(s, recordingShortcut: recordingShortcut)))

        out.append(divider("after-board"))

        out.append(submenu("Projects", id: "submenu:projects",
                           rows: projectRows(enrollment, unenrolArmed: unenrolArmed,
                                             pack: s.agentPack,
                                             packArmed: packArmed,
                                             stopSyncArmed: stopSyncArmed,
                                             agentModels: models)))

        out.append(submenu("Devices", id: "submenu:devices",
                           rows: deviceRows(devices, lanAccess: s.lanAccess,
                                            remoteAccess: s.remoteAccess,
                                            relayWS: s.relayWS,
                                            unpairArmed: unpairArmed,
                                            awayOffArmed: awayOffArmed)))

        out.append(submenu("Security", id: "submenu:security",
                           rows: securityRows(security)))

        out.append(divider("after-projects"))

        out.append(submenu("Design system", id: "submenu:design-system", rows: [
            SettingsRow(id: "custom:design-system", title: "Open Signal workshop",
                        kind: .custom(.designSystem),
                        tooltip: "Explore and export Signal tokens and components"),
        ]))
        out.append(divider("after-design-system"))

        // Everything a person presses only when something is wrong, or only
        // on a development checkout, under one heading: the installers, the
        // launch line, the log, the build line and Rebuild. Restart and Quit
        // stay on the top level — they are how the app is left, not
        // diagnosed.
        var advanced: [SettingsRow] = [
            send(s.hooksInstalled ? "Reinstall hooks" : "Install hooks",
                 action: "install_hooks"),
            send(vscodeLabel(s), action: "install_vscode_extension",
                 disabled: s.vscodeInstalling),
        ]
        if s.agentPack.available {
            advanced.append(send("Re-sync agent packs", action: "resync_agent_packs"))
        }
        // What this launch actually did, in one line. Here as well as on
        // the first-run checklist, so it stays reachable after the checklist
        // has gone; pending results update in place through the context.
        advanced.append(info(FirstRunChecklist.launchLine(s.launch),
                             id: launchRowId))
        advanced.append(divider("advanced-log"))
        advanced.append(send("Open log", action: "open_log"))
        // The desk key, for the person's own command-line tools. Drawn only
        // once the panel holds one, so a press never copies an empty string;
        // armed then confirmed, because it puts a secret on the clipboard.
        if !context.deskToken.isEmpty {
            advanced.append(SettingsRow(
                id: deskTokenRowId,
                title: deskTokenArmed ? deskTokenArmedTitle : deskTokenTitle,
                kind: .custom(.copyDeskToken),
                tooltip: deskTokenTooltip))
        }
        let line = buildLine(settings: s, context: context)
        if !line.isEmpty {
            advanced.append(info(line, id: "info:build", disabled: true))
        }
        if context.canRebuild {
            advanced.append(send(
                s.rebuilding ? "Rebuilding…" : context.rebuildLabel,
                action: "rebuild",
                disabled: s.rebuilding))
        }
        out.append(submenu("Advanced", id: "submenu:advanced", rows: advanced))

        out.append(divider("after-advanced"))

        out.append(send("Restart", action: "restart"))
        out.append(divider("before-quit"))
        out.append(send("Quit", action: "quit_app"))
        return out
    }

    /// The Agent models page's row id, spelled once for the tests.
    static let agentModelsRowId = "submenu:agent-models"
    /// The per-project block's id prefix. Its own prefix, because
    /// `SettingsSearch.foldable` folds exactly these behind their heading:
    /// twenty-one chip runs per enrolled project would otherwise sit open
    /// between the pack rows and Un-enrol.
    static let projectAgentModelsPrefix = "submenu:project-agent-models:"
    /// What a chip says on hover, and what a search matches beside the
    /// name: which assistant and which slot the chip sets, so a search for
    /// `opus` finds each slot's chip and never one run reading `opus opus`.
    static func agentModelTooltip(provider: String, slot: String,
                                  root: String?) -> String {
        let where_ = root.map { " · \(($0 as NSString).lastPathComponent)" } ?? ""
        return "\(provider) · \(slotLabel(slot))\(where_)"
    }
    static let agentModelsTooltip =
        "Which model each assistant runs on: the main session Start, Refine "
        + "and Ask open, and each helper role the agent pack writes a brief "
        + "for. This is the machine-wide setup; a project under Projects may "
        + "say otherwise for any one choice. Default means the assistant's "
        + "own default. The line lands in a project's briefs on the next "
        + "pack install or the next launch."
    /// The chip that removes a project's own choice for a slot.
    static let inheritLabel = "Inherit"
    static let defaultModelLabel = "Default"
    static let agentModelAction = "set_agent_model"
    /// What a slot is called on its row. `main` is the session Dark Army
    /// opens; `worker` is the shunt skill's cheap helper (the model
    /// `bulk_read.py` / `code_write.py` run on, written into each project's
    /// `workers.json`); every other slot is a helper role by its brief's name.
    static func slotLabel(_ slot: String) -> String {
        switch slot {
        case "main": return "Main session"
        case "worker": return "Shunt worker"
        default: return slot
        }
    }

    /// The kill switch's row, spelled once. The id is `SettingsSearch`'s key
    /// for the heading it lands under, and the panel's arming slot keys off
    /// nothing else, so a rename here is a rename in three tests.
    static let killRowId = "custom:killSwitch"
    /// The Copy desk key row under Advanced, spelled once for the tests.
    static let deskTokenRowId = "custom:copyDeskToken"
    static let deskTokenTitle = "Copy desk key"
    static let deskTokenArmedTitle = "Press again: copy the desk key"
    static let deskTokenTooltip =
        "Puts Dark Army's desk key on the clipboard, for your own command-line "
        + "tools: export it as DARK_ARMY_DESK_TOKEN in the terminal that runs "
        + "them. It opens every action the panel can take, and Dark Army makes "
        + "a new one each time it starts. The key file on disk only closes "
        + "terminals and reads."
    /// The launch line's row id under Advanced.
    static let launchRowId = "info:launch"
    static let killTitle = "Kill switch — stop everything"
    static let killArmedTitle = "Press again: kill everything now"
    static let killTooltip =
        "Stops everything of Dark Army's that is running on this Mac, at once: "
        + "this app and its daemon, the panel, the terminal host and every "
        + "session inside a terminal Dark Army itself opened, the helper Dark "
        + "Army runs inside each of your sessions, and any background helper. "
        + "There is no clean shutdown and no undo — open Dark Army again to "
        + "come back.\n\nAn assistant running in a VS Code terminal is left "
        + "alone: that tab belongs to the editor, not to Dark Army. It carries "
        + "on without it."

    /// Display titles of the top-level rows, with dividers as "—". The inventory
    /// test pins this so a rebuild cannot quietly drop or reorder a row.
    static func topLevelLabels(_ rows: [SettingsRow]) -> [String] {
        rows.map { row in
            if case .divider = row.kind { return "—" }
            return row.title
        }
    }

    /// Ids in display order, walking into submenus. Same ids in the same order
    /// means the host can update ticks in place; a mismatch rebuilds that menu.
    static func structureIds(_ rows: [SettingsRow]) -> [String] {
        rows.flatMap { row -> [String] in
            switch row.kind {
            case .submenu(_, let nested):
                return [row.id] + structureIds(nested)
            default:
                return [row.id]
            }
        }
    }

    static func flattened(_ rows: [SettingsRow]) -> [SettingsRow] {
        rows.flatMap { row -> [SettingsRow] in
            switch row.kind {
            case .submenu(_, let nested):
                return [row] + flattened(nested)
            default:
                return [row]
            }
        }
    }

    /// Build state and version as one string. Either half may be missing: a
    /// release build with no source beside it has no staleness to report, and
    /// an unstamped one has no version.
    static func buildLine(settings: DaemonClient.Settings,
                          context: DaemonClient.PanelContext) -> String {
        let build = context.build
        let version = settings.version
        if build.isEmpty { return version.isEmpty ? "" : "Version \(version)" }
        return version.isEmpty ? build : "\(build) — \(version)"
    }

    static func vscodeLabel(_ s: DaemonClient.Settings) -> String {
        if s.vscodeInstalling { return "Installing extension…" }
        return s.vscodeExtension
            ? "Reinstall VS Code extension" : "Install VS Code extension"
    }

    // MARK: - Construction helpers
    //
    // Module-internal rather than private: `SettingsProjectRows.swift` and
    // `SettingsDeviceRows.swift` build their rows with the same seven.

    static func toggle(_ title: String, action: String, isOn: Bool,
                               tooltip: String = "") -> SettingsRow {
        SettingsRow(id: "toggle:\(action)", title: title,
                    kind: .toggle(action: action, isOn: isOn), tooltip: tooltip)
    }

    static func send(_ title: String, action: String,
                             value: SettingsRow.SendValue? = nil,
                             tooltip: String = "",
                             disabled: Bool = false) -> SettingsRow {
        SettingsRow(id: "send:\(action)", title: title,
                    kind: .send(action: action, value: value),
                    tooltip: tooltip, disabled: disabled)
    }

    static func pick(_ label: String, action: String,
                             value: SettingsRow.SendValue,
                             selected: Bool,
                             tooltip: String = "") -> SettingsRow {
        // Bare label: StayOpenToggleView draws the check, the same as a
        // checkbox. A "✓  " prefix here would double it.
        SettingsRow(
            id: "pick:\(action):\(pickId(value))",
            title: label,
            kind: .pick(action: action, value: value, selected: selected),
            tooltip: tooltip)
    }

    static func pickId(_ value: SettingsRow.SendValue) -> String {
        switch value {
        case .bool(let flag): return flag ? "true" : "false"
        case .int(let number): return String(number)
        case .string(let text): return text
        case .map(let record):
            // Sorted, so the id is stable whatever order the record was built in.
            return record.keys.sorted().map { "\($0)=\(record[$0] ?? "")" }
                .joined(separator: ";")
        }
    }

    // MARK: - Agent models

    /// One block per provider; inside it one `info` row naming the slot,
    /// then the chip run: `Inherit` first on a project's rows, `Default`,
    /// then every allowed name. The `info` row is what keeps six slots from
    /// merging into one chip run (`SettingsSearch.items` joins consecutive
    /// picks sharing an action). `root` nil is the machine-wide page.
    static func agentModelRows(_ models: DaemonClient.AgentModels,
                               root: String?) -> [SettingsRow] {
        let suffix = root.map { ":\($0)" } ?? ""
        return DaemonClient.AgentModels.providers.map { provider in
            var rows: [SettingsRow] = []
            for slot in models.slots {
                rows.append(info(slotLabel(slot),
                                 id: "info:agent-model:\(provider):\(slot)\(suffix)"))
                let chosen = root.map {
                    models.override(root: $0, provider: provider, slot: slot)
                } ?? models.chosen(provider: provider, slot: slot)
                var base = ["provider": provider, "slot": slot]
                if let root { base["root"] = root }
                let help = agentModelTooltip(provider: provider, slot: slot, root: root)
                if root != nil {
                    var value = base
                    value["model"] = "inherit"
                    rows.append(pick(inheritLabel, action: agentModelAction,
                                     value: .map(value), selected: chosen == nil,
                                     tooltip: help))
                }
                var defaultValue = base
                defaultValue["model"] = ""
                rows.append(pick(defaultModelLabel, action: agentModelAction,
                                 value: .map(defaultValue), selected: chosen == "",
                                 tooltip: help))
                for name in models.allowed(provider: provider, slot: slot) {
                    var value = base
                    value["model"] = name
                    rows.append(pick(name, action: agentModelAction,
                                     value: .map(value), selected: chosen == name,
                                     tooltip: help))
                }
            }
            return submenu(provider, id: "submenu:agent-models:\(provider)\(suffix)",
                           rows: rows)
        }
    }

    /// The Security group: how many burst alerts are open off the phone
    /// doors' access log, and the window that lists the log. The count is
    /// stated in words; an older daemon (no section) or one whose log failed
    /// to open says the log is unavailable rather than pretending it is quiet.
    static func securityRows(_ security: SecuritySection) -> [SettingsRow] {
        let line: String
        if !security.available {
            line = "Access log unavailable"
        } else {
            let open = security.alerts.count
            switch open {
            case 0: line = "No open alerts"
            case 1: line = "1 open alert"
            default: line = "\(open) open alerts"
            }
        }
        return [
            info(line, id: "info:security"),
            SettingsRow(id: "custom:accessLog", title: "Access log…",
                        kind: .custom(.accessLog),
                        tooltip: "Every refused attempt to reach Dark Army over "
                            + "the phone doors, with when, where from and why."),
        ]
    }

    static func info(_ title: String, id: String,
                             disabled: Bool = true) -> SettingsRow {
        SettingsRow(id: id, title: title, kind: .info, disabled: disabled)
    }

    static func divider(_ key: String) -> SettingsRow {
        SettingsRow(id: "divider:\(key)", title: "", kind: .divider)
    }

    static func submenu(_ title: String, id: String,
                                tooltip: String = "",
                                rows: [SettingsRow]) -> SettingsRow {
        SettingsRow(id: id, title: title,
                    kind: .submenu(title: title, rows: rows), tooltip: tooltip)
    }

    private static func channelHelp(_ s: DaemonClient.Settings) -> String {
        let base = "Answer a session's permission prompts in Dark Army, reply to it "
            + "from the panel, and let it write its own board cards."
        if s.channelCommand.isEmpty { return base }
        return base + "\n\nStart a session with:\n\(s.channelCommand)"
    }

    private static func dictationRows(_ s: DaemonClient.Settings,
                                      recordingShortcut: Bool) -> [SettingsRow] {
        let recordTitle: String
        if recordingShortcut {
            recordTitle = "Press the dictation shortcut…"
        } else if s.dictationShortcutLabel.isEmpty {
            recordTitle = "Record shortcut…"
        } else {
            recordTitle = "\(s.dictationShortcutLabel) — record again"
        }
        var rows: [SettingsRow] = [
            SettingsRow(
                id: "custom:recordShortcut",
                title: recordTitle,
                kind: .custom(.recordShortcut),
                tooltip: "The hotkey MacWhisper is bound to. Dark Army holds it while you "
                    + "press Dictate."),
        ]
        if !s.macwhisperInstalled {
            rows.append(info("MacWhisper is not in Applications",
                             id: "info:macwhisper"))
        }
        if !s.accessibilityTrusted {
            rows.append(divider("dictation-access"))
            rows.append(send(
                "Open Accessibility Settings",
                action: "open_accessibility_settings",
                tooltip: "Dark Army needs permission to press keys on your behalf."))
        }
        return rows
    }

    static func relativeSpan(_ seconds: Double) -> String {
        let value = max(0, seconds)
        if value < 90 { return "\(Int(value.rounded()))s" }
        if value < 5400 { return "\(Int((value / 60).rounded()))m" }
        if value < 172_800 { return "\(Int((value / 3600).rounded()))h" }
        return "\(Int((value / 86400).rounded()))d"
    }
}
