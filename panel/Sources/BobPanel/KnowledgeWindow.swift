import AppKit
import SwiftUI

enum KnowledgeWindowMetrics {
    static let minWidth: CGFloat = 620
    static let minHeight: CGFloat = 620
}

@MainActor
final class KnowledgeWindowState: ObservableObject {
    @Published var root = ""
    @Published var label = ""
    @Published var report = KnowledgeReport()
    @Published var loading = false
    @Published var detail = ""
    @Published var confirmArmed: String?
    @Published var staleArmed: String?
    @Published var editingKey: String?
    @Published var editQuestion = ""
    @Published var editAnswer = ""

    /// Switch project: drop the previous list before the new fetch lands, so
    /// Confirm cannot fire against B while still showing A's notes.
    func retarget(root: String, label: String) {
        self.root = root
        self.label = label
        report = KnowledgeReport()
        loading = false
        detail = ""
        confirmArmed = nil
        staleArmed = nil
        editingKey = nil
        editQuestion = ""
        editAnswer = ""
    }
}

struct KnowledgeWindowRoot: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: KnowledgeWindowState

    var body: some View {
        KnowledgeView(client: client, state: state)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.bg)
            .preferredColorScheme(.dark)
    }
}

/// One reused knowledge window. `CardWindowController`'s shape: retarget by
/// root, never a second window; `forceClose` from hide and windowWillClose.
@MainActor
final class KnowledgeWindowController: NSObject, NSWindowDelegate {
    private let client: DaemonClient
    let state = KnowledgeWindowState()
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

    func present(root: String) {
        let label = client.snapshot.enrollment.enrolled.first {
            $0.root == root
        }?.label ?? URL(fileURLWithPath: root).lastPathComponent
        state.retarget(root: root, label: label)
        let win = ensureWindow()
        win.title = "Knowledge — \(label.isEmpty ? root : label)"
        if win.isMiniaturized { win.deminiaturize(nil) }
        if !placed { placeCentred(win) } else { reclamp(win) }
        placed = true
        win.makeKeyAndOrderFront(nil)
        Task { await KnowledgeView.load(client: client, state: state) }
    }

    @discardableResult
    private func ensureWindow() -> NSWindow {
        if let window { return window }
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0,
                                width: KnowledgeWindowMetrics.minWidth,
                                height: KnowledgeWindowMetrics.minHeight),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.identifier = NSUserInterfaceItemIdentifier("bob-knowledge")
        let hosting = NSHostingView(
            rootView: KnowledgeWindowRoot(client: client, state: state))
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
        let size = NSSize(width: KnowledgeWindowMetrics.minWidth * factor,
                          height: KnowledgeWindowMetrics.minHeight * factor)
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
        let minW = KnowledgeWindowMetrics.minWidth * factor
        let minH = KnowledgeWindowMetrics.minHeight * factor
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
        let wanted = NSSize(width: KnowledgeWindowMetrics.minWidth * factor,
                            height: KnowledgeWindowMetrics.minHeight * factor)
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
