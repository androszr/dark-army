import AppKit
import Combine
import SwiftUI

/// Size constants for the settings window: the size it first opens at, a
/// floor, because a window the user can resize must be honest about it, and
/// the sidebar's fixed width. Logical points: the window is deliberately
/// unscaled (`docs/panel-window-contract.md`, *One scale grows the whole
/// window*).
enum SettingsWindowMetrics {
    static let minWidth: CGFloat = 720
    static let minHeight: CGFloat = 520
    /// The first present's content size, clamped to the screen.
    static let defaultWidth: CGFloat = 860
    static let defaultHeight: CGFloat = 620
    /// The section list on the left; the page takes every point that is left.
    static let sidebarWidth: CGFloat = 210
    /// The rule between the sidebar and the page.
    static let dividerWidth: CGFloat = 1
    /// The page's inset on every side.
    static let pagePadding: CGFloat = 24

    /// The width a page's content gets in a window this wide.
    static func pageContentWidth(windowWidth: CGFloat) -> CGFloat {
        windowWidth - sidebarWidth - dividerWidth - 2 * pagePadding
    }
}

/// The one settings window, and the only thing that opens or closes it.
///
/// `CardWindowController`'s shape with the differences named. **No "what is
/// open" slot to follow**: there is nothing like `BoardState.editing` here, so
/// there is no Combine sink presenting it and the panel's `show()` never
/// re-presents it — the only ways in are the ⋯ button, the app menu's
/// Settings… item and ⌘,. **`windowShouldClose` is true unconditionally**:
/// every control writes through on press and the search query is scratch,
/// and so is the section, so nothing is half-typed; the one thing in flight can be a dictation shortcut
/// capture, and blocking a close on it would strand a window whose only exit
/// is the key the capture is eating — `windowWillClose` cancels the capture
/// instead.
@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private let client: DaemonClient
    let state: SettingsWindowState
    let actions: SettingsActions
    private var window: NSWindow?
    private var contextSink: AnyCancellable?
    /// Whether the window has ever been placed. First present centres it on
    /// the panel's screen; later presents keep whatever frame the user left it
    /// at (clamped), for the life of the process.
    private var placed = false

    /// Called when the window's occlusion changes, so the delegate can
    /// recompute the SSE `visible`/`boardOpen` gate as a three-way union.
    var onOcclusionChange: (() -> Void)?
    /// The screen the window should first appear on — the panel's.
    var screenHint: (() -> NSScreen?)?

    init(client: DaemonClient) {
        self.client = client
        let state = SettingsWindowState()
        self.state = state
        self.actions = SettingsActions(client: client, state: state)
        super.init()
        state.onCloseRequest = { [weak self] in self?.close() }
        // `$context` is the menu-bar app's echo (or refusal) for a switch.
        // Cleared here and on nothing else: an agents snapshot arriving
        // between the press and the echo must not snap a switch back.
        contextSink = client.$context
            .dropFirst()
            .sink { [weak state] _ in
                DispatchQueue.main.async { state?.clearPendingToggles() }
            }
    }

    // MARK: - Presentation

    func present() {
        let win = ensureWindow()
        if !placed { placeCentred(win) } else { reclamp(win) }
        placed = true
        win.makeKeyAndOrderFront(nil)
        state.requestFocus()
    }

    @discardableResult
    private func ensureWindow() -> NSWindow {
        if let window { return window }
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0,
                                width: SettingsWindowMetrics.defaultWidth,
                                height: SettingsWindowMetrics.defaultHeight),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.identifier = NSUserInterfaceItemIdentifier("bob-settings")
        win.title = "Settings"
        win.contentView = NSHostingView(
            rootView: SettingsWindowRoot(client: client, state: state, actions: actions))
        win.delegate = self
        win.level = .normal
        // The red button puts it away; the object is reused for the next open.
        win.isReleasedWhenClosed = false
        win.contentMinSize = NSSize(width: SettingsWindowMetrics.minWidth,
                                    height: SettingsWindowMetrics.minHeight)
        win.backgroundColor = Theme.nsBg
        win.isOpaque = true
        win.collectionBehavior = [.moveToActiveSpace]
        // Dark-only, asserted rather than inherited: there is no light variant
        // of a green CRT.
        win.appearance = NSAppearance(named: .darkAqua)
        window = win
        return win
    }

    private func placeCentred(_ win: NSWindow) {
        guard let screen = screenHint?() ?? win.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        // The default is a *content* size; the frame adds the title bar.
        let size = win.frameRect(forContentRect: NSRect(
            x: 0, y: 0,
            width: SettingsWindowMetrics.defaultWidth,
            height: SettingsWindowMetrics.defaultHeight)).size
        let frame = NSRect(x: (visible.midX - size.width / 2).rounded(),
                           y: (visible.midY - size.height / 2).rounded(),
                           width: size.width, height: size.height)
        win.setFrame(Self.clamped(frame, to: visible), display: false)
    }

    private func reclamp(_ win: NSWindow) {
        guard let screen = win.screen ?? NSScreen.main else { return }
        let clamped = Self.clamped(win.frame, to: screen.visibleFrame)
        if clamped != win.frame { win.setFrame(clamped, display: win.isVisible) }
    }

    /// `PanelPlacement.clamped`'s rule with this window's own floor: that one
    /// floors at the panel's width (520pt), which is wider than this window's.
    static func clamped(_ frame: NSRect, to visibleFrame: NSRect) -> NSRect {
        var out = frame
        out.size.width = min(max(out.width, SettingsWindowMetrics.minWidth),
                             visibleFrame.width)
        out.size.height = min(max(out.height, SettingsWindowMetrics.minHeight),
                              visibleFrame.height)
        if out.maxX > visibleFrame.maxX { out.origin.x = visibleFrame.maxX - out.width }
        if out.minX < visibleFrame.minX { out.origin.x = visibleFrame.minX }
        if out.maxY > visibleFrame.maxY { out.origin.y = visibleFrame.maxY - out.height }
        if out.minY < visibleFrame.minY { out.origin.y = visibleFrame.minY }
        return out
    }

    // MARK: - Routing

    /// Whether this key press belongs to the settings window. Identity against
    /// our own window; a nil window is not ours by this test — `isKey` is the
    /// total one that covers it.
    func owns(_ candidate: NSWindow?) -> Bool {
        guard let candidate, let window else { return false }
        return candidate === window
    }

    /// The settings window is key, so nothing typed anywhere is triage.
    var isKey: Bool { window?.isKeyWindow == true }

    /// Anyone able to see it — the third term of the SSE `visible` gate.
    var isVisiblyOnScreen: Bool {
        guard let window, window.isVisible else { return false }
        return window.occlusionState.contains(.visible)
    }

    /// Test/inspection seam: the window, once it exists.
    var windowForTesting: NSWindow? { window }

    // MARK: - Closing

    /// Take the window off screen whatever it says — the panel-coupled path.
    /// `close()` rather than `orderOut` because `orderOut` leaves a minimised
    /// window's Dock tile behind.
    func forceClose() {
        close()
    }

    private func close() {
        // A capture must not outlive the window that started it. Called by
        // hand as well as from `windowWillClose`, because `close()` posts that
        // only for a window that is actually on screen.
        actions.cancelShortcutRecording()
        actions.state.designSystemOpen = false
        // A flash left set would not flash again on the next jump to it.
        actions.state.highlightedRowId = nil
        guard let window else { return }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.close()
        onOcclusionChange?()
    }

    // MARK: - NSWindowDelegate

    // `System.Notification`, never bare `Notification`: this module has its own
    // `Notification` model (the daemon's alert cards), and a delegate method
    // declared against it merely *nearly* matches the protocol — it compiles,
    // warns, and is never called, which would silently kill the red button's
    // close route and leave the occlusion gate stuck open.

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        true
    }

    func windowWillClose(_ notification: System.Notification) {
        _ = notification
        actions.cancelShortcutRecording()
        // The workshop sheet belongs to this opening: the next one starts
        // on the settings themselves (review, 25 Sep 2026).
        actions.state.designSystemOpen = false
        actions.state.highlightedRowId = nil
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
