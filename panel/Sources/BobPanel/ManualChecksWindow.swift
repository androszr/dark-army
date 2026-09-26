import AppKit
import SwiftUI

enum ManualChecksWindowMetrics {
    static let minWidth: CGFloat = 760
    static let minHeight: CGFloat = 620
}

/// What the Checks window is showing: the search, the filter, the page the
/// daemon returned, the check being read and the note being typed.
@MainActor
final class ManualChecksWindowState: ObservableObject {
    @Published var query = ""
    @Published var filter = "all"
    /// One enrolled project's checks (Settings → Projects → Checks), or
    /// `""` for every project's (a card window's Open in Checks).
    @Published var root = ""
    @Published var report = ManualChecksReport()
    @Published var selected: String?
    @Published var document: ManualCheckDocument?
    @Published var loading = false
    @Published var detail = ""
    @Published var noteDraft = ""
    @Published var recording = false

    /// The entry the reader is showing, off the current page.
    var selectedEntry: ManualCheckEntry? {
        guard let selected else { return nil }
        return report.checks.first { $0.path == selected }
    }

    /// A fresh opening: the previous page, reader and note go, so a press
    /// can never land on a check that is no longer on screen.
    func reset() {
        report = ManualChecksReport()
        selected = nil
        document = nil
        loading = false
        detail = ""
        noteDraft = ""
        recording = false
    }
}

struct ManualChecksWindowRoot: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: ManualChecksWindowState
    var onReveal: ((String) -> Void)?

    var body: some View {
        ManualChecksView(client: client, state: state, onReveal: onReveal)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.bg)
            .preferredColorScheme(.dark)
    }
}

/// One reused Checks window. `KnowledgeWindowController`'s shape: never a
/// second window; `forceClose` from hide and windowWillClose; its occlusion
/// feeds the panel's SSE gate.
@MainActor
final class ManualChecksWindowController: NSObject, NSWindowDelegate {
    private let client: DaemonClient
    let state = ManualChecksWindowState()
    private var window: NSWindow?
    private var placed = false
    private var scaleHost: ScaleHostView?

    var onOcclusionChange: (() -> Void)?
    var screenHint: (() -> NSScreen?)?
    /// Reveal a card on the board — `BoardState.requestReveal` behind it.
    var onReveal: ((String) -> Void)?

    init(client: DaemonClient) {
        self.client = client
        super.init()
    }

    /// Open the window on one enrolled project's checks (`root`), or on
    /// every project's, or aim the reader at one file (a card window's
    /// **Open in Checks**). Aiming at a file clears the search, the filter
    /// and the project, so the chosen check is always on the list.
    func present(root: String = "", path: String = "") {
        state.reset()
        state.root = root
        // Every opening starts un-narrowed: a search or filter left over from
        // another project's list would open this one on "No manual checks match."
        state.query = ""
        state.filter = "all"
        if !path.isEmpty {
            state.selected = path
            state.root = ""
        }
        let win = ensureWindow()
        let label = client.snapshot.enrollment.enrolled.first {
            $0.root == state.root
        }?.label ?? ""
        win.title = state.root.isEmpty
            ? "Manual checks"
            : "Manual checks — \(label.isEmpty ? state.root : label)"
        if win.isMiniaturized { win.deminiaturize(nil) }
        if !placed { placeCentred(win) } else { reclamp(win) }
        placed = true
        win.makeKeyAndOrderFront(nil)
        Task { await ManualChecksView.load(client: client, state: state) }
        // Re-aiming at the check already shown leaves the reader's
        // `.task(id:)` unchanged, so it would never refetch the text that
        // `reset()` just cleared — fetch it here.
        if !path.isEmpty {
            Task { await ManualChecksView.loadDocument(client: client, state: state) }
        }
    }

    @discardableResult
    private func ensureWindow() -> NSWindow {
        if let window { return window }
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0,
                                width: ManualChecksWindowMetrics.minWidth,
                                height: ManualChecksWindowMetrics.minHeight),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.identifier = NSUserInterfaceItemIdentifier("bob-manual-checks")
        let hosting = NSHostingView(
            rootView: ManualChecksWindowRoot(
                client: client, state: state,
                onReveal: { [weak self] id in self?.onReveal?(id) }))
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
        let size = NSSize(width: ManualChecksWindowMetrics.minWidth * factor,
                          height: ManualChecksWindowMetrics.minHeight * factor)
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
        frame.size.width = max(frame.width, ManualChecksWindowMetrics.minWidth * factor)
        frame.size.height = max(frame.height, ManualChecksWindowMetrics.minHeight * factor)
        let clamped = PanelPlacement.clamped(frame, to: screen.visibleFrame,
                                             scale: factor)
        if clamped != win.frame { win.setFrame(clamped, display: win.isVisible) }
    }

    func applyScale(_ percent: Int) {
        let factor = PanelScale.factor(percent)
        guard (scaleHost?.factor ?? -1) != factor else { return }
        scaleHost?.factor = factor
        guard let win = window else { return }
        setContentMinSize(win, factor: factor)
        if win.isVisible || placed { reclamp(win) }
    }

    private func setContentMinSize(_ win: NSWindow, factor: CGFloat) {
        let wanted = NSSize(width: ManualChecksWindowMetrics.minWidth * factor,
                            height: ManualChecksWindowMetrics.minHeight * factor)
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
        guard let window else { return }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.close()
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
