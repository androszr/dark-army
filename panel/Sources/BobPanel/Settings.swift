import Foundation

/// The menu bar's context push, decoded — every preference the `⋯` menu
/// draws. Nested in `DaemonClient` (via extension) so the names every
/// caller uses — `DaemonClient.PanelContext`, `DaemonClient.Settings` —
/// are unchanged by the move out of `API.swift`.
extension DaemonClient {
    struct PanelContext {
        var build = ""
        var buildStale = false
        var grokPercent: Double?
        var grokResetsAt: Double?
        var canRebuild = false
        /// What the rebuild actually does, in the menu bar's words: it installs
        /// to /Applications from an installed copy and only fills host/dist from
        /// a checkout, and the button must not promise the wrong one.
        var rebuildLabel = "Rebuild & Reload"
        /// Every preference, pushed by the menu bar.
        ///
        /// The panel is the only surface now, so it is also the only place these
        /// can be read or set — the dropdown that used to hold them is gone. It
        /// never guesses: a switch shows what the menu bar last said, and a flip
        /// sends the wanted state and waits for the answer, so a refusal (macOS
        /// blocking banners, launchd declining) corrects the switch rather than
        /// leaving it lying about the state of the app.
        var settings = Settings()
    }

    struct Settings {
        var notificationSound = true
        var notificationBanners = true
        /// Whether a raised banner also buzzes the paired phone. True by
        /// default like the banners themselves — the switch is a mute, and
        /// the buzz is inert until away access and a registered phone exist.
        var phonePush = true
        /// Dark Army's channel — the only route that reaches *into* a session, and
        /// the one switch here that cannot change anything already on screen:
        /// a channel is a launch flag, so turning it on hands over a command.
        var channelEnabled = false
        var channelCommand = ""
        /// Whether a reply is typed onto the session's own input line in its
        /// VS Code terminal first, with the channel as the fallback — so a
        /// session need not be started with the channel command to be
        /// answered. The daemon publishes the outcome per row as `reply_via`.
        var typedReply = false
        /// Whether Dark Army types `/compact` at a session that has filled up instead
        /// of raising a banner about it. Only ever reaches a session whose
        /// VS Code window can take the keystroke; otherwise the banner fires.
        var autoCompact = true
        /// Whether Dark Army may start a session from a board card at all. The one
        /// switch here that removes a capability rather than changing what an
        /// existing one does — with it off, Start is absent from every card.
        var boardDispatch = true
        var boardAutostart = true
        /// How many agents may work at once in one project. The menu ticks
        /// this row; the daemon clamps what it is told, so a stored figure
        /// outside 1...4 simply fails to match any row rather than drawing a
        /// tick beside a number Dark Army is not obeying.
        var boardParallel = 1
        /// The panel's size as a percent of the design (100/125/150/175).
        /// Stored raw and resolved at every read, for `boardParallel`'s
        /// stated reason (one bound, not two that can drift). An unknown
        /// value draws at 100%.
        var panelScale = PanelScale.defaultPercent
        /// Whether a card arriving in Done disposes the assistant's terminal
        /// tab. False by default — a pre-upgrade daemon omits the key
        /// entirely, and the toggle must render off rather than blank the
        /// menu.
        var boardCloseTerminal = false
        /// Whether a started card runs on a terminal Dark Army itself owns rather
        /// than in the project's VS Code window. False by default: an older
        /// menu bar omits the key, and the toggle must render off rather
        /// than blank the menu.
        var boardOwnTerminal = false
        /// The recorded dictation hotkey's drawing label. Empty means none
        /// has been recorded, so Dictate is absent.
        var dictationShortcutLabel = ""
        /// Live Accessibility grant for the menu-bar process, which is the
        /// bundle that posts the key. Read on every context push, not latched.
        var accessibilityTrusted = false
        /// Whether `/Applications/MacWhisper.app` is on disk.
        var macwhisperInstalled = false
        var hooksInstalled = false
        var vscodeExtension = false
        /// Whether the phone listener is up. Off by default — an older menu
        /// bar omitting the key must render the toggle off, never open a LAN
        /// door the daemon is not actually listening on.
        var lanAccess = false
        /// Whether the phone's away path is on — the relay connector. Its
        /// own key beside `lanAccess`, deliberately: new reach, new consent.
        var remoteAccess = false
        /// Whether the away link's socket lane is on — a trial switch beside
        /// `remoteAccess`, inert without it. Its own key.
        var relayWS = false
        var version = ""
        /// Long jobs in flight, so their rows can say they are working rather
        /// than looking like a click that did nothing.
        var vscodeInstalling = false
        var rebuilding = false
        /// Shared agent-pack installer. Absent (available false) draws no
        /// rows — never an inert button against an older menu bar.
        var agentPack = AgentPack()
        /// Which model each agent and helper runs on, per assistant, for
        /// the settings window's Agent models page. Absent from an older
        /// menu bar: `available` stays false and the page is not drawn.
        var agentModels = AgentModels()
        /// What this launch did to hooks, the editor extension and the
        /// login item — `launch_report`'s vocabulary, one outcome each. An
        /// older menu bar omits the block and every field stays `pending`,
        /// which draws as "checking…", never as a claim.
        var launch = LaunchReport()
        /// macOS's own answer about banners for this app, in
        /// `notifier.STATUSES`' words. `unknown` until the menu bar has read
        /// it; only an explicit `denied` draws the warning.
        var notificationStatus = "unknown"

        init() {}

        /// Decoded field by field from the stdin payload, defaulting to whatever
        /// is already held: a context push may carry only some keys, and a
        /// missing one must not silently reset a preference to `false`.
        init(_ raw: [String: Any], base: Settings) {
            self = base
            if let v = raw["notification_sound"] as? Bool { notificationSound = v }
            if let v = raw["notification_banners"] as? Bool { notificationBanners = v }
            if let v = raw["phone_push"] as? Bool { phonePush = v }
            if let v = raw["channel_enabled"] as? Bool { channelEnabled = v }
            if let v = raw["channel_command"] as? String { channelCommand = v }
            if let v = raw["typed_reply"] as? Bool { typedReply = v }
            if let v = raw["auto_compact"] as? Bool { autoCompact = v }
            if let v = raw["board_dispatch"] as? Bool { boardDispatch = v }
            if let v = raw["board_autostart"] as? Bool { boardAutostart = v }
            if let v = raw["board_parallel"] as? Int { boardParallel = v }
            if let v = raw["panel_scale"] as? Int { panelScale = v }
            if let v = raw["board_close_terminal"] as? Bool { boardCloseTerminal = v }
            if let v = raw["board_own_terminal"] as? Bool { boardOwnTerminal = v }
            if let shortcut = raw["dictation_shortcut"] as? [String: Any] {
                dictationShortcutLabel = shortcut["label"] as? String ?? ""
            }
            if let v = raw["accessibility_trusted"] as? Bool { accessibilityTrusted = v }
            if let v = raw["macwhisper_installed"] as? Bool { macwhisperInstalled = v }
            if let v = raw["hooks_installed"] as? Bool { hooksInstalled = v }
            if let v = raw["vscode_extension"] as? Bool { vscodeExtension = v }
            if let v = raw["lan_access"] as? Bool { lanAccess = v }
            if let v = raw["remote_access"] as? Bool { remoteAccess = v }
            if let v = raw["relay_ws"] as? Bool { relayWS = v }
            if let v = raw["version"] as? String { version = v }
            if let v = raw["vscode_installing"] as? Bool { vscodeInstalling = v }
            if let v = raw["rebuilding"] as? Bool { rebuilding = v }
            if let pack = raw["agent_pack"] as? [String: Any] {
                agentPack = AgentPack(pack)
            }
            if AgentModels.carried(raw) {
                agentModels = AgentModels(raw)
            }
            if let block = raw["launch"] as? [String: Any] {
                launch = LaunchReport(block, base: launch)
            }
            if let v = raw["notification_status"] as? String {
                notificationStatus = v
            }
        }
    }

    /// One installer's outcome for this launch. `status` is one of
    /// `pending`, `changed`, `unchanged`, `skipped`, `failed`, `unknown`;
    /// anything else decodes as `unknown` so a typo can never read as
    /// success. `detail` is the menu bar's one safe clause.
    struct LaunchOutcome: Equatable {
        static let statuses: Set<String> = [
            "pending", "changed", "unchanged", "skipped", "failed", "unknown",
        ]
        var status = "pending"
        var detail = ""

        init(status: String = "pending", detail: String = "") {
            self.status = Self.statuses.contains(status) ? status : "unknown"
            self.detail = detail
        }

        init(_ raw: [String: Any], base: LaunchOutcome) {
            self = base
            if let v = raw["status"] as? String {
                status = Self.statuses.contains(v) ? v : "unknown"
            }
            if let v = raw["detail"] as? String { detail = v }
        }
    }

    /// The three outcomes the launch line names. Decoded field by field
    /// from the context push; a missing field keeps what was held.
    struct LaunchReport: Equatable {
        var hooks = LaunchOutcome()
        var editorExtension = LaunchOutcome()
        var loginItem = LaunchOutcome()

        init() {}

        init(hooks: LaunchOutcome, editorExtension: LaunchOutcome, loginItem: LaunchOutcome) {
            self.hooks = hooks
            self.editorExtension = editorExtension
            self.loginItem = loginItem
        }

        init(_ raw: [String: Any], base: LaunchReport) {
            self = base
            if let v = raw["hooks"] as? [String: Any] {
                hooks = LaunchOutcome(v, base: hooks)
            }
            if let v = raw["extension"] as? [String: Any] {
                editorExtension = LaunchOutcome(v, base: editorExtension)
            }
            if let v = raw["login_item"] as? [String: Any] {
                loginItem = LaunchOutcome(v, base: loginItem)
            }
        }

        var anyPending: Bool {
            [hooks, editorExtension, loginItem].contains { $0.status == "pending" }
        }

        var anyFailed: Bool {
            [hooks, editorExtension, loginItem].contains { $0.status == "failed" }
        }
    }

    /// Which model each agent and helper runs on: the resolved machine-wide
    /// table (`agent_models`, every provider and slot present), the override
    /// map as stored (`agent_models_by_root`), the allowlist per provider
    /// and slot (`agent_model_options`) and the drawable slots
    /// (`agent_model_slots`). `available` is true only when the options
    /// decoded — an older menu bar sends none of the four keys and the
    /// page is absent rather than empty. Decoded with `as?` per key, so a
    /// ragged push never blanks the rest of the settings.
    struct AgentModels: Equatable {
        var available = false
        var global: [String: [String: String]] = [:]
        var byRoot: [String: [String: [String: String]]] = [:]
        var options: [String: [String: [String]]] = [:]
        var slots: [String] = []
        /// Provider order as the menu bar states it; a table is unordered.
        static let providers = ["claude", "codex", "grok"]

        init() {}

        static func carried(_ raw: [String: Any]) -> Bool {
            raw["agent_model_options"] != nil
        }

        init(_ raw: [String: Any]) {
            if let table = raw["agent_models"] as? [String: [String: String]] {
                global = table
            }
            if let table = raw["agent_models_by_root"] as? [String: [String: [String: String]]] {
                byRoot = table
            }
            if let table = raw["agent_model_options"] as? [String: [String: [String]]] {
                options = table
                available = true
            }
            if let rows = raw["agent_model_slots"] as? [String] {
                slots = rows
            }
        }

        /// The names a chip run offers for one provider and slot.
        func allowed(provider: String, slot: String) -> [String] {
            options[provider]?[slot] ?? []
        }

        /// The machine-wide choice; `""` is Default.
        func chosen(provider: String, slot: String) -> String {
            global[provider]?[slot] ?? ""
        }

        /// One project's own choice, or nil where it inherits.
        func override(root: String, provider: String, slot: String) -> String? {
            byRoot[root]?[provider]?[slot]
        }
    }

    /// The pack installer the ⋯ → Projects rows draw. Tolerant: an older
    /// menu bar omits the block, and that is `available: false`.
    struct AgentPack {
        var available = false
        var projects: [AgentPackProject] = []
        var installing: [String] = []
        var selfRoot = ""
        /// Every enrolled root the write path would refuse, including a
        /// checkout that only carries `host/build.sh` with no repo-root stamp.
        var selfRoots: [String] = []
        /// The Install submenu's rows as the daemon lists them: the three
        /// built-ins plus every profile folder, the person's own included.
        /// Empty from an older daemon, where the menu keeps its fixed three.
        var profiles: [(id: String, label: String)] = []

        init() {}

        init(_ raw: [String: Any]) {
            available = raw["available"] as? Bool ?? false
            if let rows = raw["profiles"] as? [[String: Any]] {
                profiles = rows.compactMap { row in
                    guard let id = row["id"] as? String, !id.isEmpty else { return nil }
                    let label = row["label"] as? String ?? ""
                    return (id: id, label: label.isEmpty ? id : label)
                }
            }
            selfRoot = raw["self_root"] as? String ?? ""
            if let rows = raw["installing"] as? [String] {
                installing = rows
            }
            if let rows = raw["self_roots"] as? [String] {
                selfRoots = rows
            }
            if let rows = raw["projects"] as? [[String: Any]] {
                projects = rows.map(AgentPackProject.init)
            }
        }

        func project(root: String) -> AgentPackProject? {
            projects.first { $0.root == root }
        }

        func isInstalling(_ root: String) -> Bool {
            installing.contains(root)
        }

        func isSelf(_ root: String) -> Bool {
            if selfRoots.contains(root) { return true }
            return !selfRoot.isEmpty && root == selfRoot
        }
    }

    struct AgentPackProject {
        var root = ""
        var profile = ""
        var lastSyncAt: Double = 0
        var lastResult = ""

        init() {}

        init(_ raw: [String: Any]) {
            root = raw["root"] as? String ?? ""
            profile = raw["profile"] as? String ?? ""
            lastResult = raw["last_result"] as? String ?? ""
            if let value = raw["last_sync_at"] as? Double {
                lastSyncAt = value
            } else if let value = raw["last_sync_at"] as? Int {
                lastSyncAt = Double(value)
            }
        }
    }
}
