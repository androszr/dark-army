import AppKit
import SwiftUI

enum AccessLogWindowMetrics {
    static let minWidth: CGFloat = 640
    static let minHeight: CGFloat = 520
    /// How many lines one fetch asks for. The daemon caps the log at 2000;
    /// a month of refusals on a home network is far fewer.
    static let fetchLimit = 500
}

@MainActor
final class AccessLogWindowState: ObservableObject {
    @Published var report = AccessLogReport()
    @Published var loading = false
    @Published var detail = ""
    /// The refusal a press just got, keyed on the alert id.
    @Published var refusals: [String: String] = [:]

    static let fetchFailedLine = "Dark Army could not read the access log."
    static let unavailableLine = "Dark Army could not open its access log."

    func reset() {
        report = AccessLogReport()
        loading = false
        detail = ""
        refusals = [:]
    }
}

struct AccessLogWindowRoot: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: AccessLogWindowState

    var body: some View {
        AccessLogView(client: client, state: state)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.bg)
            .preferredColorScheme(.dark)
    }
}

/// The access log, drawn: open alerts on top, each with Acknowledge, then
/// every line newest first — when, which door, where from, why, and the
/// paired phone where the door knew one. The time is an absolute local
/// clock, not an age: nothing here is still happening.
struct AccessLogView: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: AccessLogWindowState

    static let timeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter
    }()

    static func timeText(_ ts: Double) -> String {
        guard ts > 0 else { return "—" }
        return timeFormatter.string(from: Date(timeIntervalSince1970: ts))
    }

    /// The door, in the words the daemon's own sentence uses.
    static func doorWord(_ door: String) -> String {
        switch door {
        case "lan": return "Wi-Fi"
        case "pairing": return "pairing"
        case "upload": return "upload"
        case "relay": return "relay"
        default: return door.isEmpty ? "—" : door
        }
    }

    /// Open alerts come from the live snapshot where the daemon states the
    /// section, so an acknowledgement lands here on the next frame; the
    /// fetched report's copy is the fallback for a window opened before the
    /// first frame.
    private var openAlerts: [AccessAlert] {
        if client.snapshot.security.available {
            return client.snapshot.security.alerts
        }
        return state.report.openAlerts
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 12) {
                    if !state.detail.isEmpty {
                        Text(state.detail)
                            .font(Theme.mono(11))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    alertsBlock
                    entriesBlock
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: .infinity)
        }
        .task { await Self.load(client: client, state: state) }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("ACCESS LOG")
                .font(Theme.mono(10, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Theme.faint)
            Text("\(state.report.entries.count) lines")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
            Spacer(minLength: 0)
            Button(state.loading ? "Loading…" : "Refresh") {
                Task { await Self.load(client: client, state: state) }
            }
            .buttonStyle(AlarmOutline(color: Theme.phosphor, size: 10))
            .disabled(state.loading)
            .clickable()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
    }

    @ViewBuilder
    private var alertsBlock: some View {
        let alerts = openAlerts
        VStack(alignment: .leading, spacing: 6) {
            Text(alerts.isEmpty ? "NO OPEN ALERTS" : "OPEN ALERTS")
                .font(Theme.mono(10, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(alerts.isEmpty ? Theme.faint : Color.red)
            ForEach(alerts) { alert in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(Self.timeText(alert.ts))
                            .font(Theme.mono(10).monospacedDigit())
                            .foregroundStyle(Theme.faint)
                        Text(Self.doorWord(alert.door))
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.dim)
                        Spacer(minLength: 0)
                        if client.snapshot.security.available {
                            Button("Acknowledge") { acknowledge(alert) }
                                .buttonStyle(AlarmOutline(color: Theme.phosphor, size: 10))
                                .clickable()
                        }
                    }
                    Text(alert.text)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                    if let refusal = state.refusals[alert.id], !refusal.isEmpty {
                        Text(refusal)
                            .font(Theme.mono(10))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(8)
                .overlay(Rectangle().stroke(Color.red.opacity(0.6), lineWidth: 1))
            }
        }
    }

    @ViewBuilder
    private var entriesBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("EVERY REFUSAL, NEWEST FIRST")
                .font(Theme.mono(10, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Theme.faint)
            if state.report.entries.isEmpty {
                Text(state.report.available
                     ? "Nothing has been refused."
                     : (state.loading ? "Loading…" : AccessLogWindowState.unavailableLine))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            ForEach(state.report.entries) { entry in
                HStack(alignment: .top, spacing: 8) {
                    Text(Self.timeText(entry.ts))
                        .font(Theme.mono(10).monospacedDigit())
                        .foregroundStyle(Theme.faint)
                        .frame(width: 150, alignment: .leading)
                    Text(Self.doorWord(entry.door))
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.dim)
                        .frame(width: 56, alignment: .leading)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(entry.text)
                            .font(Theme.mono(11))
                            .foregroundStyle(entry.kind == "burst" ? Color.red : Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                        if !entry.deviceId.isEmpty {
                            Text("phone \(entry.deviceId)")
                                .font(Theme.mono(10))
                                .foregroundStyle(Theme.faint)
                        }
                    }
                }
                .padding(.vertical, 3)
            }
        }
    }

    private func acknowledge(_ alert: AccessAlert) {
        Task { @MainActor in
            let result = await client.accessAlertAck(id: alert.id)
            state.refusals[alert.id] = result.ok ? "" : result.detail
            await client.refresh()
            await Self.load(client: client, state: state)
        }
    }

    @MainActor
    static func load(client: DaemonClient, state: AccessLogWindowState) async {
        state.loading = true
        defer { state.loading = false }
        guard let report = await client.accessLogReport(
            limit: AccessLogWindowMetrics.fetchLimit) else {
            state.detail = AccessLogWindowState.fetchFailedLine
            return
        }
        state.report = report
        state.detail = report.available ? "" : AccessLogWindowState.unavailableLine
    }
}

/// One reused access-log window. `KnowledgeWindowController`'s shape, minus
/// the retarget: there is one log.
@MainActor
final class AccessLogWindowController: NSObject, NSWindowDelegate {
    private let client: DaemonClient
    let state = AccessLogWindowState()
    private var window: NSWindow?
    private var isClosing = false
    private var placed = false
    private var scaleHost: ScaleHostView?

    var onOcclusionChange: (() -> Void)?
    var screenHint: (() -> NSScreen?)?

    init(client: DaemonClient) {
        self.client = client
        super.init()
    }

    func present() {
        let win = ensureWindow()
        win.title = "Access log"
        if win.isMiniaturized { win.deminiaturize(nil) }
        if !placed { placeCentred(win) } else { reclamp(win) }
        placed = true
        win.makeKeyAndOrderFront(nil)
        Task { await AccessLogView.load(client: client, state: state) }
    }

    @discardableResult
    private func ensureWindow() -> NSWindow {
        if let window { return window }
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0,
                                width: AccessLogWindowMetrics.minWidth,
                                height: AccessLogWindowMetrics.minHeight),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.identifier = NSUserInterfaceItemIdentifier("bob-access-log")
        let hosting = NSHostingView(
            rootView: AccessLogWindowRoot(client: client, state: state))
        let host = ScaleHostView(hosting: hosting)
        scaleHost = host
        win.contentView = host
        win.delegate = self
        win.level = .normal
        win.isReleasedWhenClosed = false
        let factor = PanelScale.factor(client.context.settings.panelScale)
        host.factor = factor
        setContentMinSize(win, factor: factor)
        win.backgroundColor = Theme.nsBg
        win.isOpaque = true
        win.collectionBehavior = [.moveToActiveSpace]
        win.appearance = NSAppearance(named: .darkAqua)
        window = win
        return win
    }

    private func placeCentred(_ win: NSWindow) {
        guard let screen = screenHint?() ?? win.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let factor = PanelScale.factor(client.context.settings.panelScale)
        let size = NSSize(width: AccessLogWindowMetrics.minWidth * factor,
                          height: AccessLogWindowMetrics.minHeight * factor)
        let frame = NSRect(x: (visible.midX - size.width / 2).rounded(),
                           y: (visible.midY - size.height / 2).rounded(),
                           width: size.width, height: size.height)
        win.setFrame(PanelPlacement.clamped(frame, to: visible, scale: factor),
                     display: false)
    }

    private func reclamp(_ win: NSWindow) {
        guard let screen = win.screen ?? NSScreen.main else { return }
        let factor = PanelScale.factor(client.context.settings.panelScale)
        var frame = win.frame
        let minW = AccessLogWindowMetrics.minWidth * factor
        let minH = AccessLogWindowMetrics.minHeight * factor
        frame.size.width = max(frame.width, minW)
        frame.size.height = max(frame.height, minH)
        let clamped = PanelPlacement.clamped(frame, to: screen.visibleFrame,
                                             scale: factor)
        if clamped != win.frame { win.setFrame(clamped, display: win.isVisible) }
    }

    func applyScale(_ percent: Int) {
        let factor = PanelScale.factor(percent)
        let changed = (scaleHost?.factor ?? -1) != factor
        guard changed else { return }
        scaleHost?.factor = factor
        guard let win = window else { return }
        setContentMinSize(win, factor: factor)
        if win.isVisible || placed { reclamp(win) }
    }

    private func setContentMinSize(_ win: NSWindow, factor: CGFloat) {
        let wanted = NSSize(width: AccessLogWindowMetrics.minWidth * factor,
                            height: AccessLogWindowMetrics.minHeight * factor)
        let visible = (screenHint?() ?? win.screen ?? NSScreen.main)?
            .visibleFrame.size ?? wanted
        win.contentMinSize = PanelScale.cappedContentMinSize(wanted, visible: visible)
    }

    func owns(_ candidate: NSWindow?) -> Bool {
        guard let candidate, let window else { return false }
        return candidate === window
    }

    var isKey: Bool { window?.isKeyWindow == true }

    var isVisiblyOnScreen: Bool {
        guard let window, window.isVisible else { return false }
        return window.occlusionState.contains(.visible)
    }

    func forceClose() {
        close()
    }

    private func close() {
        guard let window else { return }
        isClosing = true
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.close()
        isClosing = false
    }

    func windowWillClose(_ notification: System.Notification) {
        _ = notification
        onOcclusionChange?()
    }

    func windowDidChangeOcclusionState(_ notification: System.Notification) {
        _ = notification
        onOcclusionChange?()
    }

    func windowDidMiniaturize(_ notification: System.Notification) {
        _ = notification
        onOcclusionChange?()
    }

    func windowDidDeminiaturize(_ notification: System.Notification) {
        _ = notification
        onOcclusionChange?()
    }
}
