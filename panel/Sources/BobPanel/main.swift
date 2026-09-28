import AppKit
import SwiftUI

/// Shared panel geometry — the constants the drawn bands are sized from. The
/// old window-height arithmetic (`Layout` / `ideal`) went with the popover:
/// the window is the user's size now and content scrolls inside it.
enum PanelMetrics {
    /// The fleet rail's width; the board takes the leftover beside it. Cut
    /// from 680 to 520 when the board gained its fourth column (Prep): at
    /// 1710pt of visible frame, four columns off a 680pt rail come out at
    /// ~257pt each — narrower than the 260pt the card sheet was drawn for,
    /// and the squeeze lands on the card, which is the thing being read.
    /// 520 puts them back to ~297pt.
    static let width: CGFloat = 520

    /// Docked-only identity row above the tallies: face, wordmark, lock, `⋯`,
    /// and the 2pt rule.
    static let brandBar: CGFloat = 34
    /// One line of 11pt mono chips with 3pt of vertical padding and air
    /// either side. A constant: overflow is answered by scrolling sideways,
    /// never by the row growing. Nothing here may measure the window.
    static let projectSwitches: CGFloat = 30

    static let sectionHeader: CGFloat = 27

    /// One line of the htop table, every field single-line. **Only the
    /// estimate** before a list reports its own height — `PanelView.tableHeight`
    /// and `processViewportHeight(count:measured:)` both replace it with the
    /// measurement as soon as one lands. It is deliberately not the drawn
    /// figure (the 16pt chip slot over 5pt either side draws 26pt): sizing a
    /// viewport from this constant is what once cut the last row mid-line.
    static let processRow: CGFloat = 24
    /// How many items a rail list shows before it scrolls. Shared by the
    /// process table and the pipeline lists so the four rail sections
    /// show the same number of rows. 7.5 peeks the next item; it was 3.5,
    /// which left the rail mostly empty under a fleet of nine.
    static let railVisibleItems: CGFloat = 7.5
    /// One compact list row (the + TERMINAL sheet's folder rows).
    static let compactRow: CGFloat = 20
    /// One caption line under the project tabs.
    static let projectFacts: CGFloat = 22
    /// The Comm tab's terminal column, overlaid on the wide pane's trailing
    /// edge while the board keeps its place under a matching trailing
    /// padding. A constant, never a `GeometryReader`: 640pt is a 100-column
    /// terminal at the pane's type size with the header's air.
    static let commTerminalWidth: CGFloat = 640

    /// A board tile's **ceiling**. The board is four stacked rows whose
    /// cards wrap as tiles (`BoardView.rowTiles`, an adaptive `LazyVGrid`),
    /// and a tile is `boardTileMin`–`boardColumn` wide: the pane decides how
    /// many share a line, and overflow is answered by wrapping onto the next
    /// line, never by scrolling sideways. The four side-by-side columns this
    /// used to size shared the pane (~297pt each at a 1710pt frame), which
    /// put a card's title and summary through a keyhole; 600pt is twice
    /// that. Nothing here measures the window.
    static let boardColumn: CGFloat = 600
    /// The adaptive grid's minimum tile width, `boardColumn` its maximum.
    /// A pane narrower than this — the window's floor is the rail alone
    /// (`PanelPlacement.minWidth`), so it can be — lays one tile this wide
    /// and the vertical scroll clips its right edge; no sideways scroll is
    /// added for it, because a board pane that narrow is not a board anybody
    /// reads. If a real window shows tiles clipped at an ordinary width
    /// (a 600pt pane or wider), this number is wrong, not the design: lower
    /// it before reaching for a `GeometryReader`.
    static let boardTileMin: CGFloat = 420

    /// The terminal pane's type size, and the two figures derived from it
    /// and a measured glyph. The hosted terminal lives in the board's place,
    /// so cols/rows are arithmetic on **that pane's** size (the divider
    /// `GeometryReader` already has it) — never a second reader, never the
    /// rail's 520pt. `terminalCols` / `terminalRows` stay as the rail-sized
    /// fallbacks a view uses before the first layout.
    static let terminalFontSize: CGFloat = 11
    /// Horizontal padding on the grid (12pt each side).
    static let terminalPadX: CGFloat = 12
    /// `# terminal` header: 10pt line plus 6pt padding either side.
    static let terminalHeader: CGFloat = 22
    /// Vertical padding on the grid (6pt either side).
    static let terminalPadY: CGFloat = 6
    static let terminalMinCols = 40
    static let terminalMaxCols = 400
    static let terminalMinRows = 8
    static let terminalMaxRows = 200
    static var terminalGlyphWidth: CGFloat {
        let font = NSFont.monospacedSystemFont(ofSize: terminalFontSize, weight: .regular)
        return max(("M" as NSString).size(withAttributes: [.font: font]).width, 1)
    }
    static var terminalRowHeight: CGFloat {
        let font = NSFont.monospacedSystemFont(ofSize: terminalFontSize, weight: .regular)
        return ceil(font.ascender - font.descender + max(font.leading, 0))
    }
    /// Both grid figures come from a `GeometryReader`, which reports an
    /// unbounded dimension as infinity — and `Int(_:)` of an infinite or NaN
    /// `Double` traps rather than saturating. A dimension that is not a
    /// number is the smallest grid, never a crash.
    static func terminalCols(forWidth width: CGFloat) -> Int {
        guard width.isFinite else { return terminalMinCols }
        let usable = max(0, width - terminalPadX * 2)
        return max(terminalMinCols, min(terminalMaxCols,
                                        Int(usable / terminalGlyphWidth)))
    }
    static func terminalRows(forHeight height: CGFloat) -> Int {
        guard height.isFinite else { return terminalMinRows }
        let usable = max(0, height - terminalHeader - terminalPadY * 2)
        return max(terminalMinRows, min(terminalMaxRows,
                                        Int(usable / max(terminalRowHeight, 1))))
    }
    static var terminalCols: Int { terminalCols(forWidth: width) }
    static var terminalRows: Int { 30 }
    /// The standing chrome band — usage windows plus the lock and `⋯`.
    /// `settingsChrome` is the footer's divider + 8/10 padding (19);
    /// `usageChip` is the four-line chip (label, figure, meter, reset).
    /// One term for both modes on purpose: the footer draws it at the bottom
    /// and `SidebarUsageBand` draws the same content as a top band, so the
    /// arithmetic does not care which end it is spent at. Exact for the
    /// footer; ~4pt conservative for the band (band chrome is 15, not 19).
    static let settingsChrome: CGFloat = 19
    static let usageChip: CGFloat = 51
    static var settings: CGFloat { settingsChrome + usageChip }
}

/// The combo panel process.
///
/// It is launched by the menu-bar app and stays resident, showing and hiding on
/// command over stdin. Relaunching per click would cost a process spawn, a
/// Swift runtime start and a fresh SSE connection every time — perceptible, and
/// the reason the old simulator was kept alive hidden rather than respawned.
///
/// Commands, one JSON object per line on stdin:
///     {"action":"show","x":1200,"y":24}   — show, top-right corner at that point
///     {"action":"hide"}
///     {"action":"toggle","x":…,"y":…}
///     {"action":"quit"}
/// Anchoring is passed in rather than discovered because only the menu-bar app
/// knows where its own status item ended up, and that moves as the strip's width
/// changes.
@MainActor
/// Two extensions in their own files — `KeyMonitor.swift` (the keyboard) and
/// `StdinCommands.swift` (the menu bar's commands) — reach the window, the
/// auxiliary window controllers and the show/hide pair, which is why those
/// members are module-internal rather than private.
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    /// An ordinary titled window since 23 Aug 2026 — the old NSPanel
    /// subclass (a `canBecomeKey` override for a `.borderless` panel) went
    /// with the borderless style; a titled window is key-capable for free.
    var panel: NSWindow!
    let client = DaemonClient()
    /// The one wire from the key monitor into the view's selection.
    let keys = KeyRouter()
    /// The board's own state — the filter, the arms, and the single "what card
    /// is open" slot. Created **here** rather than inside `PanelView` (as a
    /// `@StateObject`) because the card composer is its own window now: the
    /// hide and close paths below have to reach this instance to take that
    /// window with them. `KeyRouter`/`FocusRouter` are the precedent.
    /// Exactly one, for the process's life — a second would be a second draft.
    let boardState = BoardState()
    /// The one reused card window, following `boardState.editing`.
    var cardWindow: CardWindowController!
    /// The one reused settings window. Presented only by the ⋯ button, the
    /// app menu's Settings… item and ⌘, — never by `show`.
    var settingsWindow: SettingsWindowController!
    /// The one reused knowledge window, following Settings → Projects.
    var knowledgeWindow: KnowledgeWindowController!
    /// The one reused Checks window, following Settings → Projects → Checks
    /// and a card window's Open in Checks.
    var manualChecksWindow: ManualChecksWindowController!
    /// The one reused access-log window, following Settings → Security and
    /// the Inbox's Open log.
    var accessLogWindow: AccessLogWindowController!
    /// The scaled AppKit host wrapping the SwiftUI tree. The window's
    /// `contentView`; its `factor` is the panel's size dial.
    private var scaleHost: ScaleHostView!
    /// And the one wire from a `show`/`toggle` that names a session — a tap on
    /// a notification banner — into which row the view puts in front.
    let focus = FocusRouter()
    /// And its twin for the board half — the reverse jump from VS Code asks
    /// for a session's *card* as well as its row. A second router rather than
    /// a flag, because the rail and the board apply it independently; see
    /// `CardFocusRouter`.
    let cardFocus = CardFocusRouter()
    /// The status item's frame in screen coordinates, so a dismissing click on
    /// it can be told from a click anywhere else.
    private var anchorRect: NSRect = .zero
    /// Set when we hide ourselves because of a click *on the status item*. That
    /// same click also fires the menu-bar app's action, and without this the
    /// toggle that follows re-opens what the click just closed — which is what
    /// "the second click does not hide it" actually was.
    private var suppressShowUntil: Date = .distantPast

    /// The app that was in front when a click-open took activation off it, so a
    /// deliberate hide can hand the keyboard straight back. Captured only at
    /// the moment we take over (and only if we were not already active, or we
    /// would record ourselves), cleared the moment it is used or fallen back
    /// from.
    private var reactivateTarget: NSRunningApplication?

    /// When the app last became active — so a Dock click that pulled the
    /// window from behind another app fronts it rather than hiding it
    /// (`DockToggle`).
    private var becameActiveAt: Date?

    /// True when the menu bar launched us, rather than a person.
    private let owned = CommandLine.arguments.contains("--hidden")
    /// Whether anyone is on the other end of stdin — see `stdinIsDriven()`.
    let driven = stdinIsDriven()
    /// The coalescing one-shot behind the per-screen frame save:
    /// `windowDidMove` fires per drag tick, and a JSON write per tick is the
    /// habit this codebase keeps removing.
    private var frameSaveTimer: Timer?
    /// The covered edge's wait before the occlusion writer closes the SSE
    /// gate (`OcclusionGrace`), and the one-shot that ends it. Delegate
    /// state, never a second flag on `DaemonClient`.
    private var grace = OcclusionGrace()
    private var graceTimer: Timer?

    func applicationDidFinishLaunching(_ notification: System.Notification) {
        // A regular app: Dock tile, Cmd-Tab entry, a real menu bar. The window
        // is ordinary now, and an ordinary window should be found where every
        // other window is.
        NSApp.setActivationPolicy(.regular)

        // Before anything is on screen: end any panel that is already up, and
        // arrange to end ourselves if we are ever left without an owner. See
        // Lifecycle.swift for why an orphaned panel is unclosable.
        PanelLock.claim(owned: owned)
        installOrphanWatchdog()

        let content = PanelView(client: client, keys: keys, focus: focus,
                                cardFocus: cardFocus, board: boardState)
        // A plain hosting view: first-mouse was for the non-activating panel.
        // A normal window spends the first click on activation, which is the
        // platform convention.
        let hosting = NSHostingView(rootView: content)
        let host = ScaleHostView(hosting: hosting)
        scaleHost = host

        panel = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: PanelMetrics.width, height: 160),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        panel.title = "Dark Army"
        panel.identifier = NSUserInterfaceItemIdentifier("bob-panel")
        panel.contentView = host
        panel.delegate = self
        panel.level = .normal
        // The red button must put the window away, never deallocate it: the
        // next `show` reuses this object, and the default (true for
        // programmatic windows) crashes it.
        panel.isReleasedWhenClosed = false
        // A resizable window needs a floor, or Dark Army can be dragged down to a
        // sliver; same figures as `PanelPlacement.clamped`'s floor, so a
        // stored sliver cannot round-trip either.
        panel.contentMinSize = NSSize(width: PanelPlacement.minWidth,
                                      height: PanelPlacement.minHeight)
        panel.isMovableByWindowBackground = false
        panel.backgroundColor = Theme.nsBg
        panel.isOpaque = true
        // Produce the window *here* on a strip click rather than yanking the
        // user to the Space it last lived on. Consequence, accepted: it no
        // longer floats over other apps' full-screen Spaces
        // (`.fullScreenAuxiliary` is gone) — that is part of being ordinary.
        panel.collectionBehavior = [.moveToActiveSpace]
        // Dark-only deliberately: every view is designed against `Theme`, and
        // a system-appearance title bar over a dark body flips light on
        // light-mode machines — worse than a uniformly dark window.
        panel.appearance = NSAppearance(named: .darkAqua)

        host.wantsLayer = true
        host.layer?.cornerRadius = 0
        host.layer?.masksToBounds = true

        // Built before the monitor, which asks it whether a press is typing.
        cardWindow = CardWindowController(client: client, state: boardState)
        cardWindow.screenHint = { [weak self] in self?.panel?.screen }
        cardWindow.onOcclusionChange = { [weak self] in self?.publishSeen() }
        settingsWindow = SettingsWindowController(client: client)
        settingsWindow.screenHint = { [weak self] in self?.panel?.screen }
        settingsWindow.onOcclusionChange = { [weak self] in self?.publishSeen() }
        knowledgeWindow = KnowledgeWindowController(client: client)
        knowledgeWindow.screenHint = { [weak self] in self?.panel?.screen }
        knowledgeWindow.onOcclusionChange = { [weak self] in self?.publishSeen() }
        settingsWindow.actions.onKnowledge = { [weak self] root in
            self?.knowledgeWindow.present(root: root)
        }
        manualChecksWindow = ManualChecksWindowController(client: client)
        manualChecksWindow.screenHint = { [weak self] in self?.panel?.screen }
        manualChecksWindow.onOcclusionChange = { [weak self] in self?.publishSeen() }
        manualChecksWindow.onReveal = { [weak self] cardId in
            guard let self else { return }
            self.show(x: nil, y: nil)
            self.boardState.requestReveal(cardId)
        }
        settingsWindow.actions.onManualChecks = { [weak self] root in
            self?.manualChecksWindow.present(root: root)
        }
        boardState.onOpenManualCheck = { [weak self] path in
            self?.manualChecksWindow.present(path: path)
        }
        accessLogWindow = AccessLogWindowController(client: client)
        accessLogWindow.screenHint = { [weak self] in self?.panel?.screen }
        accessLogWindow.onOcclusionChange = { [weak self] in self?.publishSeen() }
        settingsWindow.actions.onAccessLog = { [weak self] in
            self?.accessLogWindow.present()
        }
        applyScale(PanelScale.defaultPercent)

        installMainMenu()
        installKeyMonitor()
        installBoardObserver()
        installSettingsObserver()
        installAccessLogObserver()
        installJumpObserver()
        // A boundary in the log at every launch: the app log holds every panel
        // this machine has run, and "which process wrote that line" is the first
        // thing you ask when two of them overlapped.
        Trace.log("panel launched pid=\(ProcessInfo.processInfo.processIdentifier) "
                  + "owned=\(owned) driven=\(driven)")
        // A `--hidden` instance is launched into a window nobody has asked for
        // yet, and it may sit there for hours. It starts held; `show` releases it.
        client.visible = !owned
        client.start()
        readCommands(quitOnEOF: owned || driven)
        // Run by hand (`swift run`) it should just appear; the menu-bar app
        // passes --hidden and drives it over stdin instead.
        if !owned {
            show(x: nil, y: nil)
        }
    }

    /// Skipped entirely on the hard path (`PanelExit.now`, which the orphan
    /// watchdog takes), and that is safe because the whole body is this one
    /// call and `PanelExit.now` makes it by hand. Anything added here that must
    /// happen on *every* exit has to be added there too.
    func applicationWillTerminate(_ notification: System.Notification) {
        PanelLock.release()
    }

    /// The board is the left pane of this window now. The notification still
    /// exists so an old `⋯` row or a leftover post cannot crash; it just
    /// shows the workspace.
    private func installBoardObserver() {
        NotificationCenter.default.addObserver(
            forName: .panelOpenBoard, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.show(x: nil, y: nil)
                Trace.log("board shown in workspace")
            }
        }
    }

    /// The ⋯ button's request for the settings window, `installBoardObserver`'s
    /// copy. The window is this process's, so nothing crosses to the menu bar.
    private func installSettingsObserver() {
        NotificationCenter.default.addObserver(
            forName: .panelOpenSettings, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.settingsWindow?.present()
                Trace.log("settings shown")
            }
        }
    }

    /// The Inbox's Open log press, `installSettingsObserver`'s copy.
    private func installAccessLogObserver() {
        NotificationCenter.default.addObserver(
            forName: .panelOpenAccessLog, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.accessLogWindow?.present()
                Trace.log("access log shown")
            }
        }
    }

    // MARK: - Placement

    func setAnchor(_ obj: [String: Any]) {
        if let x = obj["ax"] as? Double, let y = obj["ay"] as? Double,
           let w = obj["aw"] as? Double, let h = obj["ah"] as? Double {
            anchorRect = NSRect(x: x, y: y, width: w, height: h)
        }
    }

    /// A `show`/`toggle` may name a session — the menu bar sends one when a
    /// notification banner is tapped. Sent *after* the show, so the view has
    /// already been told it is visible and the request is applied rather than
    /// parked: `syncFocus` holds one until both are true.
    func requestFocus(_ obj: [String: Any]) {
        guard let session = obj["focus"] as? String, !session.isEmpty else { return }
        focus.send(session)
        // `card` is additive and only the reverse jump from VS Code sets it: a
        // banner tap aims the rail and leaves the board exactly as it was.
        if obj["card"] as? Bool == true {
            cardFocus.send(session)
        }
    }

    /// Show the window, on the screen the status item was clicked on.
    ///
    /// The anchor point still arrives on every show — the strip's width moves
    /// the status item, so it has to — and is used only to pick the *screen*.
    /// The window opens at that screen's stored frame (or a centred default
    /// the first time); the user owns the frame from there.
    func show(x: Double?, y: Double?) {
        if Date() < suppressShowUntil { return }
        // An explicit open outranks a cover's pending close: the timer must
        // not fire under a panel stdin just showed.
        cancelGrace()
        if panel.isMiniaturized { panel.deminiaturize(nil) }
        // Already on screen: front it, do not re-place. `show(nil, nil)` is
        // the board observer and would otherwise resolve against main and
        // yank a panel that is sitting on another display.
        let seen = panel.isVisible && panel.occlusionState.contains(.visible)
        if !seen {
            applyStoredFrame(screenHint: screenNear(x: x, y: y))
            // A deliberate open of a panel that was not on screen: the view
            // aims its selection at whoever needs you on this counter. An
            // already-visible panel being fronted (the board observer's
            // `show(nil, nil)`, a banner tap on an open window) is not an
            // open and re-aims nothing — exactly what the old
            // `client.visible` false→true edge meant, minus the occlusion
            // writer that shared that flag.
            focus.noteShown()
        }
        // Activation takes keyboard focus from whatever was in front — the
        // same bargain Spotlight and Raycast make — which is why `hide()`
        // gives it back. `activateForClick` remembers who had it.
        activateForClick()
        panel.makeKeyAndOrderFront(nil)
        // A card that survived the last put-away — the composer that was
        // mid-save, which `closeEditor` refused to discard — comes back with
        // the panel rather than being stranded in `editing` with no window.
        cardWindow?.syncToState()
        client.visible = true
        client.boardOpen = true
        Trace.log("shown (holding \(client.snapshot.digest))")
        // No `/api/state` read here: the stream's attach frame is the open's
        // fetch (`_serve_events` sends a whole `state()` on every connect),
        // decoded off the main actor in `consumeStream`. Reading it here too
        // decoded the whole fleet on main and applied it twice.
        Task {
            await client.refreshUsage()
        }
    }

    /// Screen the status item was on, else the window's, else main.
    /// A nil point must not yank a panel that is already on another display.
    private func screenNear(x: Double?, y: Double?) -> NSScreen? {
        if let x, let y {
            let point = NSPoint(x: x, y: y)
            if let hit = NSScreen.screens.first(where: { NSPointInRect(point, $0.frame) }) {
                return hit
            }
        }
        return panel.screen ?? NSScreen.main
    }

    /// Put the window at its stored frame for the resolved screen — or a
    /// centred 0.8-of-visible default the first time it appears there —
    /// clamped on-screen on **every** show, which is what handles a frame
    /// remembered on a screen whose geometry has since changed.
    private func applyStoredFrame(screenHint: NSScreen? = nil) {
        let screen = screenHint ?? panel.screen ?? NSScreen.main
        guard let screen else { return }
        let visible = screen.visibleFrame
        let stored = screen.displayId.flatMap { PanelPlacement.frame(for: $0) }
        let factor = scaleHost?.factor ?? 1
        let frame = PanelPlacement.clamped(
            stored ?? PanelPlacement.defaultFrame(in: visible), to: visible,
            scale: factor)
        panel.setFrame(frame, display: panel.isVisible, animate: false)
    }

    /// Apply the size dial: scale the host, raise the window floor, and on
    /// an *increase* grow the current physical frame so the logical
    /// rail/board split is preserved. A decrease leaves the frame (the
    /// window is the user's). Never reloads `panel-position.json` — a
    /// drag that has not flushed must not be discarded. A periodic context
    /// push with an unchanged factor is a no-op.
    func applyScale(_ percent: Int) {
        let factor = PanelScale.factor(percent)
        let oldFactor = scaleHost.factor
        let changed = oldFactor != factor
        scaleHost.factor = factor
        guard changed else { return }
        let wanted = PanelScale.minContentSize(factor: factor)
        let screen = panel.screen ?? NSScreen.main
        let visibleSize = screen?.visibleFrame.size ?? wanted
        panel.contentMinSize = PanelScale.cappedContentMinSize(wanted, visible: visibleSize)
        if panel.isVisible {
            let visible = screen?.visibleFrame
                ?? NSRect(origin: .zero, size: visibleSize)
            let grown = PanelScale.grownFrame(
                panel.frame, from: oldFactor, to: factor, visible: visible)
            if grown != panel.frame {
                panel.setFrame(grown, display: true, animate: false)
            }
            saveFrameNow()
        }
        cardWindow?.applyScale(percent)
        knowledgeWindow?.applyScale(percent)
        manualChecksWindow?.applyScale(percent)
        accessLogWindow?.applyScale(percent)
    }

    /// Put the window away without ending anything. `yield` is false only on
    /// the jump that is already aiming activation at the editor.
    func hide(yieldToRemembered: Bool = true) {
        // Immediate, whatever a cover had pending: an explicit hide is never
        // graced, and a stale timer must not write after it.
        cancelGrace()
        // While `panel.screen` is still valid: a move followed within the
        // coalescing window by Escape would otherwise fire the save after
        // `orderOut`, find no screen, and silently lose the frame.
        flushPendingFrameSave()
        if panel.isVisible, NSPointInRect(NSEvent.mouseLocation, anchorRect) {
            suppressShowUntil = Date().addingTimeInterval(0.4)
        }
        Trace.log("hidden")
        releaseSwitcherFocus(reason: "hide")
        focus.noteHidden()
        client.visible = false
        client.boardOpen = false
        panel.orderOut(nil)
        // **The card window never outlives the panel.** Order is the point:
        // `closeEditor` discards the draft and the staged copies — unless a
        // create is on the wire, when it no-ops and the state survives to show
        // the refusal on the next `show` — and `forceClose` takes the window
        // off screen and out of the Dock regardless, so nothing is stranded.
        boardState.closeEditor()
        cardWindow?.forceClose()
        settingsWindow?.forceClose()
        PairingWindowController.current?.forceClose()
        knowledgeWindow?.forceClose()
        manualChecksWindow?.forceClose()
        accessLogWindow?.forceClose()
        // Never the app-wide hide here: on a `.regular` app it takes every
        // window down, including the card window this path already closed.
        yieldActivation(toRemembered: yieldToRemembered)
    }

    /// Let a focused card-face assistant switcher go before the window
    /// leaves the screen. `PanelView` clears `switcherFocused` by hand on
    /// every hide, but a cleared *flag* over a still-focused *row* is the
    /// worse state: the panel comes back with the ring drawn and the monitor
    /// no longer standing aside, so →/Return/Space are eaten and Escape hides
    /// the panel instead of leaving the row. Resigning the first responder
    /// makes the row's `@FocusState` really go false, and its one `onChange`
    /// route releases the slot; the by-hand clear stays as the floor.
    ///
    /// Guarded on the switcher actually holding the slot, so the hosted
    /// terminal's caret (`TerminalFocus.holdsCaret`) and a text view
    /// dictation promoted (`DictationFocus`) survive a hide exactly as they
    /// did before — neither is ever the responder this resigns.
    private func releaseSwitcherFocus(reason: String) {
        guard boardState.switcherFocused != nil else { return }
        Trace.log("switcher released (\(reason))")
        panel?.makeFirstResponder(nil)
    }

    /// Deliberately inert: a normal window does not dismiss on deactivate.
    /// Clicking the editor must not close Dark Army — Escape, ⌘W, the red button
    /// and the menu-bar toggle still do.
    func windowDidResignKey(_ notification: System.Notification) {
        _ = notification
    }

    // MARK: - The red button, the yellow button, and being covered

    /// The red button closes the window; it never ends the process.
    /// `panel_process.py` expects to reopen it with the next `show`, and
    /// `isReleasedWhenClosed` being false is what keeps the object alive for it.
    func windowShouldClose(_ sender: NSWindow) -> Bool { true }

    /// Exactly `hide()`'s bookkeeping, for the close paths (`⌘W`, the red
    /// button) that go through AppKit's close rather than through `hide()`.
    func windowWillClose(_ notification: System.Notification) {
        _ = notification
        cancelGrace()             // a close is immediate, like `hide()`
        flushPendingFrameSave()   // drag-then-⌘W must not lose the frame
        Trace.log("closed")
        releaseSwitcherFocus(reason: "close")
        focus.noteHidden()
        // Same coupling, same order, as `hide()`.
        boardState.closeEditor()
        cardWindow?.forceClose()
        settingsWindow?.forceClose()
        PairingWindowController.current?.forceClose()
        knowledgeWindow?.forceClose()
        manualChecksWindow?.forceClose()
        accessLogWindow?.forceClose()
        client.visible = false
        client.boardOpen = false
        yieldActivation(toRemembered: true)
    }

    /// The platform's own "is anyone able to see this" signal — a second
    /// writer of the same `visible`/`boardOpen` gate the stdin fast path
    /// writes, never a replacement for it. It is what makes a fully covered
    /// or minimised window as cheap as an ordered-out one at the gate level
    /// (SSE closed, snapshots held) once `OcclusionGrace.seconds` have passed
    /// under the cover. Both writers are idempotent against each other
    /// through `didSet`'s `oldValue` guard.
    func windowDidChangeOcclusionState(_ notification: System.Notification) {
        _ = notification
        publishSeen()
    }

    /// The union of the panel's, the card window's, the settings window's
    /// and the knowledge window's visibility. A card window covering the panel
    /// — or outliving a minimised one — is still somebody looking at Dark Army's
    /// data, and without the OR the stream closes and the card's live session
    /// section freezes; the settings window reads the enrolment and device
    /// lists off the same stream; a knowledge window over a hidden panel
    /// must keep the stream open too. Called from every window's occlusion
    /// callback; idempotent through `didSet`, like every other writer of
    /// this gate.
    ///
    /// **The covered edge waits `OcclusionGrace.seconds` before it closes
    /// the gate** — sliding a window over the panel and off again used to
    /// tear the stream down and rebuild it every time. A seen edge writes
    /// the gate open at once and disarms the wait; the fire recomputes the
    /// union (`settleAfterGrace`) rather than trusting the edge that armed
    /// it. Hide, close and show cancel the wait and write the gate
    /// themselves (`cancelGrace`).
    private func publishSeen() {
        let seen = seenNow()
        // The covered-or-minimised edge is a hide too: a switcher left
        // focused under another app's window would come back with its ring
        // drawn and the monitor no longer standing aside. Immediate — it is
        // about the keyboard, not the stream.
        if !seen { releaseSwitcherFocus(reason: "occluded") }
        let verdict = grace.observe(seen: seen, now: Date())
        // The pane's heartbeat must stop naming its session the moment the
        // panel is covered, not when the grace runs out — the stream stays
        // open through the wait, the daemon's alert suppression must not.
        GraceCover.shared.note(verdict)
        switch verdict {
        case .open:
            graceTimer?.invalidate()
            graceTimer = nil
            client.visible = true
            client.boardOpen = true
        case .armed:
            graceTimer?.invalidate()
            Trace.log("occluded; grace \(Int(OcclusionGrace.seconds))s")
            // By interval, not by the wall-clock deadline, and the fire is
            // authoritative (`expired()`): every open and cancel invalidates
            // this timer, so a fire that runs is the current wait.
            let timer = Timer(timeInterval: OcclusionGrace.seconds,
                              repeats: false) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self, self.grace.expired() else { return }
                    self.graceTimer = nil
                    self.settleAfterGrace()
                }
            }
            graceTimer = timer
            // `.common`, like the terminal's focus poll: a menu or modal
            // tracking loop starves a default-mode timer, and the grace
            // would fire late on a machine with a menu open.
            RunLoop.main.add(timer, forMode: .common)
        case .unchanged:
            break
        }
    }

    /// The five-way union of the panel's and its auxiliary windows'
    /// visibility — the one answer both `publishSeen` and the grace's fire
    /// write into the gate.
    private func seenNow() -> Bool {
        (panel?.occlusionState.contains(.visible) ?? false)
            || (cardWindow?.isVisiblyOnScreen ?? false)
            || (settingsWindow?.isVisiblyOnScreen ?? false)
            || (knowledgeWindow?.isVisiblyOnScreen ?? false)
            || (manualChecksWindow?.isVisiblyOnScreen ?? false)
            || (accessLogWindow?.isVisiblyOnScreen ?? false)
    }

    /// The grace ran out: write what is on screen **now**, never the value
    /// that armed it — a stdin hide-then-show inside the wait would
    /// otherwise have the timer close the stream under a shown panel.
    private func settleAfterGrace() {
        let seen = seenNow()
        Trace.log("occlusion settled seen=\(seen)")
        client.visible = seen
        client.boardOpen = seen
        // After the gate: the pane reads both, and must never see the grace
        // end while `visible` still says true under a cover.
        GraceCover.shared.clear()
    }

    /// Disarm a cover's pending close. First on every explicit path that
    /// writes the gate (`show`, `hide`, `windowWillClose`), so a stale
    /// timer can never write after them.
    private func cancelGrace() {
        graceTimer?.invalidate()
        graceTimer = nil
        grace.cancel()
        GraceCover.shared.clear()
    }

    /// The user owns the frame now: remember it per screen, coalesced —
    /// `windowDidMove` fires per drag tick, and a JSON write per tick is the
    /// habit this codebase keeps removing.
    func windowDidMove(_ notification: System.Notification) {
        _ = notification
        scheduleFrameSave()
    }

    func windowDidEndLiveResize(_ notification: System.Notification) {
        _ = notification
        scheduleFrameSave()
    }

    /// The panel came to the front again — after System Settings, say. Ask
    /// the menu bar to re-read the notification permission so a person who
    /// just allowed banners sees the warning go without restarting. A read,
    /// coalesced on the other side; never an authorization request. Only
    /// while the panel can be seen: activation with the window ordered out
    /// is not a return to it.
    func applicationDidBecomeActive(_ notification: System.Notification) {
        _ = notification
        becameActiveAt = Date()
        guard panel.isVisible, panel.occlusionState.contains(.visible) else { return }
        Panel.send(action: "refresh_notification_status")
    }

    /// A click on the Dock icon toggles the window, like the strip: shown if
    /// nothing of ours is on screen or it sits behind another app, put away
    /// if it is already in front (`DockToggle`). Asked of the panel itself,
    /// not `hasVisibleWindows`.
    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows: Bool) -> Bool {
        let seen = panel.isVisible && panel.occlusionState.contains(.visible)
        switch DockToggle.verdict(seen: seen, active: NSApp.isActive,
                                  becameActive: becameActiveAt, now: Date()) {
        case .show: show(x: nil, y: nil)
        case .hide: hide()
        }
        return false
    }

    private func scheduleFrameSave() {
        frameSaveTimer?.invalidate()
        frameSaveTimer = Timer.scheduledTimer(withTimeInterval: 0.5,
                                              repeats: false) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.frameSaveTimer = nil
                self.saveFrameNow()
            }
        }
    }

    /// Write a still-pending save now, before the window leaves the screen —
    /// off-screen, `panel.screen` is nil and the frame is silently lost.
    private func flushPendingFrameSave() {
        guard let timer = frameSaveTimer, timer.isValid else { return }
        timer.invalidate()
        frameSaveTimer = nil
        saveFrameNow()
    }

    private func saveFrameNow() {
        guard let id = panel.screen?.displayId else { return }
        PanelPlacement.saveFrame(panel.frame, for: id)
    }

    /// The keyboard.
    ///
    /// Escape closes the panel — the same key that closes a menu — and the rest
    /// is triage: ↑/↓ walk the rows, Enter jumps to the selected one, Space
    /// unfolds it, D dismisses its card or hides a passive Codex row, S stops it
    /// (twice, through the same gate the button uses). `/` reaches the filter.
    ///
    /// A local monitor rather than SwiftUI's `onKeyPress`, still: one reader,
    /// deciding whether letters are typed or aimed by asking the view
    /// (`KeyRouter.editing`), not by reading `panel.firstResponder`, which
    /// SwiftUI owns inside an `NSHostingView`. A focusable row list beside the
    /// filter field would be a second claimant on focus.
    ///
    /// Consuming (`return nil`) matters as much as mapping: an unconsumed "s"
    /// reaches AppKit, which beeps at a window with nothing to insert text
    /// into.
    /// The real menu bar of a `.regular` app.
    ///
    /// This grew out of `installEditMenu`, a never-drawn menu that existed
    /// only as the ⌘-equivalent lookup table for an `.accessory` app. Drawn
    /// now: an app menu, the same Edit menu, and a Window menu wired to
    /// `NSApp.windowsMenu`. Quit routes through `PanelExit.requested` rather
    /// than bare `NSApp.terminate`, so a quit with a card sheet attached
    /// still succeeds (`terminate` is refused above the delegate while a
    /// sheet is up — the Lifecycle-measured failure). ⌘W goes through
    /// `performClose` into the close path above.
    private func installMainMenu() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(
            title: "About Dark Army",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""))
        appMenu.addItem(.separator())
        // The platform position for Settings; this app has a real menu bar, so
        // ⌘, belongs here. Presents the one reused window.
        let settings = NSMenuItem(title: "Settings…",
                                  action: #selector(openSettingsFromMenu(_:)),
                                  keyEquivalent: ",")
        settings.target = self
        appMenu.addItem(settings)
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(
            title: "Hide Dark Army",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"))
        appMenu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Dark Army",
                              action: #selector(quitFromMenu(_:)),
                              keyEquivalent: "q")
        quit.target = self
        appMenu.addItem(quit)
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        main.addItem(appItem)

        let items: [(String, Selector, String)] = [
            ("Undo",       Selector(("undo:")),      "z"),
            ("Redo",       Selector(("redo:")),      "Z"),
            ("Cut",        #selector(NSText.cut(_:)),        "x"),
            ("Copy",       #selector(NSText.copy(_:)),       "c"),
            ("Paste",      #selector(NSText.paste(_:)),      "v"),
            ("Select All", #selector(NSText.selectAll(_:)),  "a"),
        ]
        let edit = NSMenu(title: "Edit")
        for (title, action, key) in items {
            edit.addItem(NSMenuItem(title: title, action: action, keyEquivalent: key))
        }
        let editItem = NSMenuItem()
        editItem.submenu = edit
        main.addItem(editItem)

        let window = NSMenu(title: "Window")
        window.addItem(NSMenuItem(title: "Close",
                                  action: #selector(NSWindow.performClose(_:)),
                                  keyEquivalent: "w"))
        window.addItem(NSMenuItem(title: "Minimize",
                                  action: #selector(NSWindow.performMiniaturize(_:)),
                                  keyEquivalent: "m"))
        window.addItem(NSMenuItem(title: "Zoom",
                                  action: #selector(NSWindow.performZoom(_:)),
                                  keyEquivalent: ""))
        let windowItem = NSMenuItem()
        windowItem.submenu = window
        main.addItem(windowItem)

        NSApp.mainMenu = main
        NSApp.windowsMenu = window
    }

    /// The app menu's Quit — a real quit, allowed; the menu bar notices the
    /// dead pipe and relaunches on the next `show`/`toggle`.
    @objc private func quitFromMenu(_ sender: Any?) {
        PanelExit.requested("user quit")
    }

    /// The app menu's Settings… and ⌘, — the same window the ⋯ button opens.
    @objc private func openSettingsFromMenu(_ sender: Any?) {
        settingsWindow?.present()
    }

    /// Take activation for a click-open, remembering what we took it from.
    /// Hardware key events are routed to the **active** app, and
    /// `installKeyMonitor` is a *local* `NSEvent` monitor, which only ever
    /// sees events delivered to this process. Every `show` routes through
    /// here so the Spotlight-style "give the keyboard back" on Escape/close
    /// has a remembered target.
    private func activateForClick() {
        if !NSApp.isActive {
            let front = NSWorkspace.shared.frontmostApplication
            if front?.processIdentifier != ProcessInfo.processInfo.processIdentifier {
                reactivateTarget = front
            }
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Give the keyboard back on a deliberate hide. With `toRemembered`, the
    /// app we took activation from — a fact captured at the moment we took
    /// it, which is better than letting AppKit pick a successor. Without it
    /// (a jump to the editor) we only yield: that gesture is already
    /// activating a destination of its own, and re-aiming the
    /// previously-front app would race it.
    private func yieldActivation(toRemembered: Bool) {
        defer { reactivateTarget = nil }
        guard NSApp.isActive else { return }
        if toRemembered, let target = reactivateTarget, !target.isTerminated {
            target.activate(options: [])
        } else {
            NSApp.deactivate()
        }
    }

    /// Jump — from a row, a card, a card's sheet, or "Open in editor" — is
    /// the user deliberately sending their attention to the editor, and a
    /// panel left standing in front of it is covering the answer. Orders the
    /// workspace out and only *yields* activation, because the press is
    /// already aiming at the editor and re-aiming the previously-front app
    /// would race it.
    private func installJumpObserver() {
        NotificationCenter.default.addObserver(
            forName: .panelDidJump, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.yieldToEditor()
            }
        }
    }

    /// `hide()` minus two deliberate differences: no `NSApp.hide(nil)` (that
    /// hands focus to the *previous* app, racing the editor's own activation)
    /// and no `suppressShowUntil` interlock (that exists for the status-item
    /// click echo, and armed here it could swallow a banner-tap `show`).
    private func yieldToEditor() {
        if panel.isVisible {
            Trace.log("workspace yield jump")
            client.visible = false
            client.boardOpen = false
            panel.orderOut(nil)
            yieldActivation(toRemembered: false)
        }
    }

}

// `Notification` is our own model type in this module, so the AppKit callback's
// parameter needs qualifying. Aliased rather than renamed because the API type
// is named for what the daemon calls it.
enum System {
    typealias Notification = Foundation.Notification
}

let app = NSApplication.shared
let delegate = MainActor.assumeIsolated { AppDelegate() }
app.delegate = delegate

app.run()
