import AppKit
import SwiftUI

/// The settings window's scratch: the search query, the optimistic switch
/// overlay, the armed confirmations and whether a dictation shortcut capture is
/// in flight. One slot per armed verb, for `BoardState`'s stated reason: a
/// shared slot would let arming one row silently re-aim another's confirmation.
@MainActor
final class SettingsWindowState: ObservableObject {
    @Published var query = ""
    /// What a switch *wants*, drawn ahead of the model's `isOn` until the
    /// menu-bar app echoes a new context. Cleared on every `$context` publish
    /// and on nothing else — an agents snapshot arriving between the press and
    /// the echo must not snap the switch back to the stale value.
    @Published var pendingToggles: [String: Bool] = [:]
    @Published var recordingShortcut = false
    @Published var unenrolArmed: String?
    @Published var unpairArmed: String?
    @Published var awayOffArmed: String?
    @Published var packArmed: String?
    @Published var stopSyncArmed: String?
    /// The kill switch's arming slot. Its own, for the reason every other
    /// armed verb has one: a shared slot would let arming one row aim
    /// another's confirmation — and this is the row where that matters most.
    @Published var killArmed = false
    @Published var designSystemOpen = false
    /// Foldable headings (`SettingsSearch.foldable`) a person has opened.
    /// Closed by default and not persisted: the window is reused for the
    /// life of the process and a fold is a reading position, not a setting.
    /// Kept for `SettingsSearch.visible`'s tests; since the sidebar redesign
    /// (26 Sep 2026) no view reads it — the Projects page draws each
    /// project's model table open.
    @Published var openBlocks: Set<String> = []
    /// The sidebar's selected section. Scratch like the query: remembered for
    /// the life of the process (the window is reused), never persisted.
    @Published var section: SettingsSectionID = .general
    /// The project the Projects page shows; nil means the first enrolled one.
    @Published var selectedProjectRoot: String?
    /// The row a search jump landed on. Set by `jump(to:)`, cleared by the
    /// row when its flash ends, by `jump(to:)` itself after `markSeconds` and
    /// by every close — left set, the next jump to the same row would not
    /// flash.
    @Published var highlightedRowId: String?
    /// Stepped on every jump, so the page scrolls to the mark even when the
    /// mark has not changed (a second jump to the same row).
    @Published private(set) var jumpSerial = 0
    /// How long a jump's mark lives before `jump(to:)` clears it itself: the
    /// flash's length and a margin. A drawn row clears it sooner, when its
    /// flash ends; this is the floor for a target that draws no flash.
    var markSeconds: Double = SettingsFlash.seconds + 0.5
    private var markClear: Task<Void, Never>?

    /// Whether the window shows search results rather than a page: a query
    /// with something besides white space. The one predicate the page, the
    /// results and the sidebar's selection all read.
    var isSearching: Bool {
        !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Switch sections — the sidebar, ⌘1…⌘8, the arrows. Clears the query, so
    /// the page shown is the section asked for rather than the results.
    func select(section: SettingsSectionID) {
        query = ""
        self.section = section
    }

    /// Open the section a search hit lives in, on its project where it names
    /// one, and mark the row so the page scrolls to it and flashes it. A hit
    /// in the sidebar's foot keeps the current section.
    func jump(to hit: SettingsSearch.Hit) {
        if let target = hit.section { section = target }
        if let root = hit.projectRoot { selectedProjectRoot = root }
        query = ""
        let mark = hit.entry.id
        highlightedRowId = mark
        jumpSerial += 1
        let serial = jumpSerial
        let delay = UInt64(max(markSeconds, 0) * 1_000_000_000)
        markClear?.cancel()
        markClear = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            guard !Task.isCancelled, let self, self.jumpSerial == serial,
                  self.highlightedRowId == mark else { return }
            self.highlightedRowId = nil
        }
    }

    func toggleBlock(_ id: String) {
        if openBlocks.contains(id) { openBlocks.remove(id) } else { openBlocks.insert(id) }
    }
    /// Stepped on every present so the search field takes the caret again: the
    /// window is reused, so `onAppear` alone would fire once per process.
    @Published var focusRequest = 0
    /// Where `escape()` sends the second press. The controller sets it.
    var onCloseRequest: (() -> Void)?

    enum EscapeOutcome: Equatable {
        case clearedQuery
        case close
    }

    /// The Escape ladder: a non-empty query is emptied first; an empty one asks
    /// the window to close.
    @discardableResult
    func escape() -> EscapeOutcome {
        if !query.isEmpty {
            query = ""
            return .clearedQuery
        }
        onCloseRequest?()
        return .close
    }

    func requestFocus() {
        focusRequest += 1
    }

    func clearPendingToggles() {
        if !pendingToggles.isEmpty { pendingToggles = [:] }
    }
}

/// The custom-action half lifted out of the old `NSMenu` coordinator: the
/// folder chooser, the shortcut recorder and its one-shot monitor, the pairing
/// and relay window controllers, and the arm-then-confirm verbs. Everything
/// crossing to the menu-bar app is `Panel.send` on stdout; everything crossing
/// to the daemon is `client`'s own loopback verbs. Nothing here writes a
/// preference itself.
@MainActor
final class SettingsActions {
    let client: DaemonClient
    let state: SettingsWindowState
    private var pairingWindow: PairingWindowController?
    private var relayWindow: RelaySheetController?
    private var shortcutHolder: ShortcutMonitor?

    init(client: DaemonClient, state: SettingsWindowState) {
        self.client = client
        self.state = state
    }

    /// Whether a dictation shortcut capture is in flight — the test seam for
    /// `cancelShortcutRecording()`.
    var recordingShortcut: Bool { state.recordingShortcut }

    /// Presents the knowledge window for an enrolled root. Owned by the app
    /// delegate so hide/close force-close it with card/settings/pairing.
    var onKnowledge: ((String) -> Void)?

    /// Presents the Checks window on one enrolled project's manual checks.
    /// Owned by the app delegate for the same reason `onKnowledge` is.
    var onManualChecks: ((String) -> Void)?

    /// Presents the access-log window. Owned by the app delegate for the
    /// same reason `onKnowledge` is.
    var onAccessLog: (() -> Void)?

    /// The descriptor tree, read fresh on every body evaluation exactly as the
    /// coordinator's `currentRows()` did.
    func rows() -> [SettingsRow] {
        SettingsMenuModel.rows(
            settings: client.context.settings,
            context: client.context,
            enrollment: client.snapshot.enrollment,
            devices: client.snapshot.devices,
            recordingShortcut: state.recordingShortcut,
            unenrolArmed: state.unenrolArmed,
            unpairArmed: state.unpairArmed,
            awayOffArmed: state.awayOffArmed,
            packArmed: state.packArmed,
            stopSyncArmed: state.stopSyncArmed,
            killArmed: state.killArmed,
            agentModels: client.context.settings.agentModels,
            security: client.snapshot.security)
    }

    // MARK: - Sending

    func sendToggle(action: String, wanted: Bool) {
        let payload = SettingsMenuModel.togglePayload(action: action, wanted: wanted)
        Panel.send(action: action, value: payload.boxed)
        state.pendingToggles[action] = wanted
    }

    func sendPick(action: String, value: SettingsRow.SendValue) {
        Panel.send(action: action, value: value.boxed)
    }

    func sendAction(action: String, value: SettingsRow.SendValue?) {
        if let value {
            Panel.send(action: action, value: value.boxed)
        } else {
            Panel.send(action: action)
        }
    }

    /// The drawn state of a switch: the optimistic overlay first, the model's
    /// answer otherwise.
    func toggleIsOn(action: String, model: Bool) -> Bool {
        state.pendingToggles[action] ?? model
    }

    func handleCustom(_ action: SettingsRow.CustomAction) {
        switch action {
        case .killSwitch:
            state.unenrolArmed = nil
            state.unpairArmed = nil
            state.awayOffArmed = nil
            state.packArmed = nil
            state.stopSyncArmed = nil
            if state.killArmed {
                state.killArmed = false
                // The menu-bar app owns every process here, this one included,
                // so the verb crosses on stdout like Quit does. Nothing is
                // sent back: the surface this reply would land on is one of
                // the things being stopped.
                Panel.send(action: "kill_all")
            } else {
                state.killArmed = true
            }
        case .enrolFolder:
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            chooseFolderToEnrol()
        case .recordShortcut:
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            beginRecordingShortcut()
        case .unenrol(let root):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            if state.unenrolArmed == root {
                state.unenrolArmed = nil
                Task { _ = await client.unenrollProject(root) }
            } else {
                state.unenrolArmed = root
            }
        case .pairDevice:
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            presentPairing()
        case .relayAddress:
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            presentRelaySheet()
        case .unpair(let deviceId):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            if state.unpairArmed == deviceId {
                state.unpairArmed = nil
                Task { _ = await client.unpairDevice(deviceId) }
            } else {
                state.unpairArmed = deviceId
            }
        case .awayDays(let deviceId, let days):
            state.killArmed = false
            // Granting is not destructive, so it fires on the first press —
            // and it disarms any half-pressed End, which the person has
            // plainly changed their mind about.
            state.packArmed = nil
            state.stopSyncArmed = nil
            state.awayOffArmed = nil
            Task { _ = await client.setAwayDays(deviceId, days: days) }
        case .lockScreenActions(let deviceId, let enabled):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            state.awayOffArmed = nil
            // A consent switch, not a destruction: it fires on the first
            // press, and the info line above the row says where it stands.
            Task { _ = await client.setLockScreenActions(deviceId, enabled: enabled) }
        case .botAccess(let deviceId, let side, let mode):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            state.awayOffArmed = nil
            // First press, Off included: the person asked for Off to bite
            // at once, and on again is one press away. The info line above
            // the run says which position is in force.
            Task {
                let result = await client.setBotAccess(deviceId, side: side, mode: mode)
                if let words = SettingsMenuModel.botAccessRefusal(result) {
                    let alert = NSAlert()
                    alert.messageText = "Could not change the bot's access"
                    alert.informativeText = words
                    alert.alertStyle = .warning
                    alert.runModal()
                }
            }
        case .awayOff(let deviceId):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            if state.awayOffArmed == deviceId {
                state.awayOffArmed = nil
                Task { _ = await client.setAwayDays(deviceId, days: 0) }
            } else {
                state.awayOffArmed = deviceId
            }
        case .installPack(let root, let profile):
            state.killArmed = false
            state.unenrolArmed = nil
            state.unpairArmed = nil
            state.awayOffArmed = nil
            state.stopSyncArmed = nil
            let key = "\(root)|\(profile)"
            if state.packArmed == key {
                state.packArmed = nil
                Panel.send(action: "install_agent_pack",
                           value: ["root": root, "profile": profile])
            } else {
                state.packArmed = key
            }
        case .stopPackSync(let root):
            state.killArmed = false
            state.unenrolArmed = nil
            state.unpairArmed = nil
            state.awayOffArmed = nil
            state.packArmed = nil
            if state.stopSyncArmed == root {
                state.stopSyncArmed = nil
                Panel.send(action: "stop_agent_pack_sync", value: root)
            } else {
                state.stopSyncArmed = root
            }
        case .knowledge(let root):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            onKnowledge?(root)
        case .manualChecks(let root):
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            onManualChecks?(root)
        case .accessLog:
            state.killArmed = false
            state.packArmed = nil
            state.stopSyncArmed = nil
            onAccessLog?()
        case .designSystem:
            state.designSystemOpen = true
        }
    }

    // MARK: - Other windows

    /// `allowTyped` arms the daemon's plain (typed-address) pair branch for
    /// this one pairing; the window's tick restarts through here, and the
    /// existing window-reuse path swaps in the fresh code either way.
    func presentPairing(allowTyped: Bool = false) {
        Task {
            let result = await client.beginPairing(allowTyped: allowTyped)
            if !result.ok {
                let alert = NSAlert()
                alert.messageText = "Could not start pairing"
                alert.informativeText = result.detail.isEmpty
                    ? "Phone access is not listening." : result.detail
                alert.alertStyle = .warning
                alert.runModal()
                return
            }
            if pairingWindow == nil {
                pairingWindow = PairingWindowController(client: client)
            }
            pairingWindow?.present(result, onRestart: { [weak self] on in
                self?.presentPairing(allowTyped: on)
            })
        }
    }

    func presentRelaySheet() {
        if relayWindow == nil {
            relayWindow = RelaySheetController(client: client)
        }
        relayWindow?.present()
    }

    /// The directory chooser, modal so the key monitor's sheet guard sees it.
    func chooseFolderToEnrol() {
        Self.enrolChosenFolder(client: client) { _ in }
    }

    /// The one directory chooser, shared by the settings window and the
    /// first-run checklist. A seam rather than an inline `NSOpenPanel` so a
    /// test can stand in a cancel, and so the two surfaces cannot drift in
    /// what they ask.
    static var folderPicker: () -> URL? = {
        let picker = NSOpenPanel()
        picker.canChooseFiles = false
        picker.canChooseDirectories = true
        picker.allowsMultipleSelection = false
        picker.prompt = "Enrol"
        picker.message = "Choose a project folder for Dark Army to watch."
        guard picker.runModal() == .OK else { return nil }
        return picker.url
    }

    /// Run the chooser and enrol what it names. `completion` gets `nil` on a
    /// cancel — inert, nothing sent — and the daemon's own `ActionResult`
    /// otherwise, so the calling surface can show a refusal where the person
    /// is looking. No optimistic tick: the checklist's step 1 turns green
    /// only when a later snapshot lists the folder.
    static func enrolChosenFolder(client: DaemonClient,
                                  completion: @escaping (ActionResult?) -> Void) {
        guard let url = folderPicker() else {
            completion(nil)
            return
        }
        Task { @MainActor in
            let result = await client.enrollProject(url.path)
            completion(result)
        }
    }

    // MARK: - The shortcut recorder

    func beginRecordingShortcut() {
        guard !state.recordingShortcut else { return }
        state.recordingShortcut = true
        let holder = ShortcutMonitor()
        shortcutHolder = holder
        holder.token = NSEvent.addLocalMonitorForEvents(
            matching: [.keyDown, .flagsChanged]
        ) { [weak self] event in
            guard let self else { return event }
            if event.type == .flagsChanged {
                guard let flag = DictationKey.modifierFlag[event.keyCode] else {
                    return event
                }
                if event.modifierFlags.contains(flag) {
                    holder.pendingModifier = event.keyCode
                    return nil
                }
                guard holder.pendingModifier == event.keyCode else {
                    return nil
                }
                self.finishShortcutMonitor(holder)
                let label = DictationKey.sidedLabel[event.keyCode]
                    ?? String("key\(event.keyCode)".prefix(8))
                Panel.send(action: "set_dictation_shortcut", value: [
                    "key_code": Int(event.keyCode),
                    "modifiers": Int(flag.rawValue),
                    "label": label,
                ] as [String: Any])
                return nil
            }
            self.finishShortcutMonitor(holder)
            if event.keyCode == 53 { return nil }
            let mods = event.modifierFlags.intersection([.shift, .control,
                                                         .option, .command])
            let label = dictationShortcutLabel(
                keyCode: event.keyCode, modifiers: mods,
                chars: event.charactersIgnoringModifiers ?? "")
            Panel.send(action: "set_dictation_shortcut", value: [
                "key_code": Int(event.keyCode),
                "modifiers": Int(mods.rawValue),
                "label": label,
            ] as [String: Any])
            return nil
        }
    }

    /// Abandon a capture in flight. The one-shot local `NSEvent` monitor
    /// removes itself only on the next key or modifier, so a window closed
    /// mid-capture would otherwise leave it swallowing the next press anywhere
    /// in the panel for the life of the process.
    func cancelShortcutRecording() {
        guard let holder = shortcutHolder else {
            state.recordingShortcut = false
            return
        }
        finishShortcutMonitor(holder)
    }

    private func finishShortcutMonitor(_ holder: ShortcutMonitor) {
        if let token = holder.token {
            NSEvent.removeMonitor(token)
            holder.token = nil
        }
        holder.pendingModifier = nil
        state.recordingShortcut = false
        shortcutHolder = nil
    }
}

/// Holds the one-shot `NSEvent` monitor so the callback can remove it.
private final class ShortcutMonitor {
    var token: Any?
    var pendingModifier: UInt16?
}

private func dictationShortcutLabel(keyCode: UInt16,
                                    modifiers: NSEvent.ModifierFlags,
                                    chars: String) -> String {
    var parts = ""
    if modifiers.contains(.control) { parts += "⌃" }
    if modifiers.contains(.option) { parts += "⌥" }
    if modifiers.contains(.shift) { parts += "⇧" }
    if modifiers.contains(.command) { parts += "⌘" }
    let letter = chars.uppercased().filter { $0.isLetter || $0.isNumber }
    if let first = letter.first {
        parts.append(first)
    }
    if parts.isEmpty {
        parts = "key\(keyCode)"
    }
    return String(parts.prefix(8))
}
