import AppKit
import Combine
import SwiftUI

/// Size constants for the card window. `620×620` is the fixed frame the sheet
/// used to carry; as a window it becomes the *floor* rather than the whole
/// truth, because a window the user can resize must be honest about it.
enum CardWindowMetrics {
    static let minWidth: CGFloat = 620
    static let minHeight: CGFloat = 620
}

/// The card screen's root view inside its own window.
///
/// It derives *everything* from the two objects it is handed — `state.editing`
/// says which card is open (or `BoardState.newCard` for the composer) and the
/// snapshot says what that card currently holds — so retargeting the window at
/// another card, and every live update inside it, is SwiftUI's job. The
/// controller only ever orders the window around; it never re-hosts a view.
struct CardWindowRoot: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: BoardState

    var body: some View {
        Group {
            if let target = state.editing {
                BoardCardSheet(
                    client: client, state: state,
                    card: target == BoardState.newCard
                        ? nil
                        : client.snapshot.board.cards.first { $0.id == target },
                    isComposer: target == BoardState.newCard)
            } else {
                // Only ever seen for the frame between `editing` going nil and
                // the controller ordering the window out.
                Color.clear
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
    }
}

/// The one card window, and the only thing that opens or closes it.
///
/// **One window, reused.** `BoardState.editing` is the single "what is open"
/// slot and this follows it: non-nil presents (creating the window the first
/// time, retitling and bringing it forward after that), nil orders it out.
/// Opening a second card while one is up therefore swaps the content of the
/// same `NSWindow` — there is never a second one, and no per-window draft
/// state to diverge.
///
/// **Every close lands in `BoardState.closeEditor()`**, which is where the
/// draft and the staged file copies are **banked or discarded** — a composer
/// with anything typed into it (or a file dropped on it) is written to
/// `card-drafts.json` and keeps its staged folder; an empty one discards
/// exactly as before. The fork lives inside `bankComposerDraft()`, so none of
/// the close routes can pick wrong. `windowShouldClose` is the
/// refusal the sheet used to make by disabling its interactive dismiss: while
/// a create is on the wire the red button and ⌘W do nothing, so the copies the
/// request names cannot be pulled out from under it.
@MainActor
final class CardWindowController: NSObject, NSWindowDelegate {
    private let client: DaemonClient
    private let state: BoardState
    private var window: NSWindow?
    private var cancellable: AnyCancellable?
    /// Cuts the close reentrancy: close → `windowWillClose` → `closeEditor()`
    /// → `$editing` nil → the sink asking to close again.
    private var isClosing = false
    /// Whether the window has ever been placed. First present centres it on
    /// the panel's screen; later presents keep whatever frame the user left it
    /// at (clamped), for the life of the process.
    private var placed = false
    /// The scaled host wrapping the card's SwiftUI tree. Nil until the
    /// window exists.
    private var scaleHost: ScaleHostView?

    /// Called when the card window's occlusion changes, so the delegate can
    /// recompute the panel-or-card `visible`/`boardOpen` gate.
    var onOcclusionChange: (() -> Void)?
    /// The screen the card window should first appear on — the panel's.
    var screenHint: (() -> NSScreen?)?
    /// Whether `present` orders the window onto the screen. `true` in the
    /// app; the test target passes `false`, because a `CardWindowController`
    /// built there still runs the full presentation path — the window is
    /// created, titled, sized and placed, everything a test asserts on — but
    /// `makeKeyAndOrderFront` in an `xctest` process puts a real, dead
    /// "Card" window over the person's desktop for every test that presents
    /// one, and steals their keyboard while it is there.
    private let ordersOnScreen: Bool

    init(client: DaemonClient, state: BoardState, ordersOnScreen: Bool = true) {
        self.client = client
        self.state = state
        self.ordersOnScreen = ordersOnScreen
        super.init()
        // `$editing` publishes on `willSet`, so `state.editing` still holds the
        // *old* value inside the sink: act on the emitted value, never re-read
        // the property, or the window presents the previous card for a frame.
        cancellable = state.$editing.sink { [weak self] target in
            self?.apply(target)
        }
    }

    // MARK: - Presentation

    /// Re-present (or order out) to match whatever `editing` says right now.
    /// Called on the panel's `show`, which is what brings back a composer that
    /// was mid-save when the panel was put away.
    func syncToState() {
        apply(state.editing)
    }

    private func apply(_ target: String?) {
        guard !isClosing else { return }
        guard let target else {
            close()
            return
        }
        present(target)
    }

    private func present(_ target: String?) {
        let win = ensureWindow()
        win.title = title(for: target)
        if win.isMiniaturized { win.deminiaturize(nil) }
        if !placed { placeCentred(win) } else { reclamp(win) }
        placed = true
        if ordersOnScreen { win.makeKeyAndOrderFront(nil) }
    }

    private func title(for target: String?) -> String {
        guard let target, target != BoardState.newCard else { return "New Card" }
        let live = client.snapshot.board.cards.first { $0.id == target }
        let name = live?.title.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return name.isEmpty ? "Card" : name
    }

    @discardableResult
    private func ensureWindow() -> NSWindow {
        if let window { return window }
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0,
                                width: CardWindowMetrics.minWidth,
                                height: CardWindowMetrics.minHeight),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.identifier = NSUserInterfaceItemIdentifier("bob-card")
        let hosting = NSHostingView(
            rootView: CardWindowRoot(client: client, state: state))
        let host = ScaleHostView(hosting: hosting)
        scaleHost = host
        win.contentView = host
        win.delegate = self
        win.level = .normal
        // The red button puts it away; the object is reused for the next card.
        win.isReleasedWhenClosed = false
        let factor = PanelScale.factor(client.context.settings.panelScale)
        host.factor = factor
        setContentMinSize(win, factor: factor)
        win.backgroundColor = Theme.nsBg
        win.isOpaque = true
        win.collectionBehavior = [.moveToActiveSpace]
        // Dark-only, asserted rather than inherited: a sheet took the panel's
        // appearance, an ordinary window takes the system's, and a light title
        // bar over `Theme`'s CRT is the result on a light-mode machine.
        win.appearance = NSAppearance(named: .darkAqua)
        window = win
        return win
    }

    private func placeCentred(_ win: NSWindow) {
        guard let screen = screenHint?() ?? win.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let factor = PanelScale.factor(client.context.settings.panelScale)
        let size = NSSize(width: CardWindowMetrics.minWidth * factor,
                          height: CardWindowMetrics.minHeight * factor)
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
        let minW = CardWindowMetrics.minWidth * factor
        let minH = CardWindowMetrics.minHeight * factor
        frame.size.width = max(frame.width, minW)
        frame.size.height = max(frame.height, minH)
        let clamped = PanelPlacement.clamped(frame, to: screen.visibleFrame,
                                             scale: factor)
        if clamped != win.frame { win.setFrame(clamped, display: win.isVisible) }
    }

    /// Match the panel's size dial. Called from the panel's `applyScale` hop
    /// when the factor actually moved, and at first `ensureWindow`.
    func applyScale(_ percent: Int) {
        let factor = PanelScale.factor(percent)
        let changed = (scaleHost?.factor ?? -1) != factor
        guard changed else { return }
        scaleHost?.factor = factor
        guard let win = window else { return }
        setContentMinSize(win, factor: factor)
        if win.isVisible || placed {
            reclamp(win)
        }
    }

    private func setContentMinSize(_ win: NSWindow, factor: CGFloat) {
        let wanted = NSSize(width: CardWindowMetrics.minWidth * factor,
                            height: CardWindowMetrics.minHeight * factor)
        let visible = (screenHint?() ?? win.screen ?? NSScreen.main)?
            .visibleFrame.size ?? wanted
        win.contentMinSize = PanelScale.cappedContentMinSize(wanted, visible: visible)
    }

    // MARK: - Routing

    /// Whether this key press belongs to the card window. Identity against our
    /// own window; a nil window (an event that arrives without one) is not ours
    /// by this test — `isKey` is the total one that covers it.
    func owns(_ candidate: NSWindow?) -> Bool {
        guard let candidate, let window else { return false }
        return candidate === window
    }

    /// The card window is the key window, so nothing typed anywhere is triage.
    var isKey: Bool { window?.isKeyWindow == true }

    /// Anyone able to see it — the second half of the SSE `visible` gate.
    var isVisiblyOnScreen: Bool {
        guard let window, window.isVisible else { return false }
        return window.occlusionState.contains(.visible)
    }

    /// Test/inspection seam: the window, once it exists.
    var windowForTesting: NSWindow? { window }

    // MARK: - Closing

    /// Take the window off screen whatever it says — the panel-coupled path.
    /// `close()` rather than `orderOut` because `orderOut` leaves a minimised
    /// window's Dock tile behind, and rather than `performClose` because that
    /// consults `windowShouldClose` and would let `composerSaving` refuse a
    /// close the panel has already decided.
    func forceClose() {
        close()
    }

    private func close() {
        guard let window else { return }
        isClosing = true
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.close()
        // `close()` posts `windowWillClose` only for a window that is actually
        // on screen, so the single close route is called here rather than left
        // to AppKit — a card window that never made it on screen must still
        // discard its draft. Idempotent: the delegate callback's own call is a
        // no-op the second time (`editing` is nil), and both refuse while a
        // create is on the wire.
        state.closeEditor()
        isClosing = false
    }

    // MARK: - NSWindowDelegate

    // `System.Notification`, never bare `Notification`: this module has its own
    // `Notification` model (the daemon's alert cards), and a delegate method
    // declared against it merely *nearly* matches the protocol — it compiles,
    // warns, and is never called, which would silently kill the red button's
    // close route and the occlusion gate.

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        !state.composerSaving
    }

    func windowWillClose(_ notification: System.Notification) {
        _ = notification
        // The single close route. A no-op when `editing` is already nil or a
        // create is on the wire — both guards live in `closeEditor` itself.
        // The latch is load-bearing: `closeEditor` nils `editing`, the sink
        // fires *inside* this callback, and a second `close()` from there is
        // undefined-behaviour territory.
        let wasClosing = isClosing
        isClosing = true
        state.closeEditor()
        isClosing = wasClosing
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
