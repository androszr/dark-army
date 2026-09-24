import AppKit
import Combine
import SwiftUI
import SwiftTerm

/// A real terminal attached to the pty Dark Army hosts for a board-dispatched
/// session. **SwiftTerm is the emulator and the view**: it owns scrollback,
/// selection, colour, and the keyboard. This file opens **one stream** to
/// the daemon per pane (`TerminalStreamConnection`) and feeds SwiftTerm
/// straight from it — never through `@State`, where two chunks landing in
/// one run-loop pass coalesced into the last one and the picture tore.
/// Keys go up the same socket, in order; nothing is polled.
///
/// The first bytes down are the daemon emulator's own drawing of the
/// screen (`Screen.paint()`), scrollback included, so attaching to a
/// running session starts clean rather than mid-escape-sequence in the
/// ring. SwiftTerm is the one owner of the pty's size: `sizeChanged`,
/// debounced, is the only resize sent. Nothing here is a `GeometryReader`.
struct TerminalPane: View {
    let agent: Agent
    @ObservedObject var client: DaemonClient
    var cols: Int = PanelMetrics.terminalCols
    var rows: Int = PanelMetrics.terminalRows
    var onInputFocus: (Bool) -> Void = { _ in }
    var onTerminalFocus: (Bool) -> Void = { _ in }

    @State private var title = ""
    @State private var note = ""
    @State private var exited = false
    @State private var ptySize: PtySize
    /// The panel is covered and the stream's grace is running: the pane
    /// stops naming its session while `client.visible` still says true.
    @ObservedObject private var cover = GraceCover.shared

    init(agent: Agent, client: DaemonClient,
         cols: Int = PanelMetrics.terminalCols,
         rows: Int = PanelMetrics.terminalRows,
         onInputFocus: @escaping (Bool) -> Void = { _ in },
         onTerminalFocus: @escaping (Bool) -> Void = { _ in }) {
        self.agent = agent
        _client = ObservedObject(wrappedValue: client)
        self.cols = cols
        self.rows = rows
        self.onInputFocus = onInputFocus
        self.onTerminalFocus = onTerminalFocus
        _ptySize = State(initialValue: PtySize(cols: cols, rows: rows))
    }

    /// How often the pane restates which session it is showing, so alert
    /// suppression keeps trusting it (`_panel_focused_sessions`).
    static let focusEveryNanoseconds: UInt64 = 2_000_000_000

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            NativeTerminalHost(
                session: agent.sessionId,
                client: client,
                onSize: { c, r in
                    ptySize.cols = c
                    ptySize.rows = r
                },
                onTitle: { title = $0 },
                onNote: { note = $0 },
                onExit: { exited = true },
                onFocus: { focused in
                    onInputFocus(focused)
                    onTerminalFocus(focused)
                })
            .background(TerminalLook.background)
        }
        .background(TerminalLook.background)
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        .overlay(alignment: .bottom) {
            if !note.isEmpty {
                Label(note, systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
                    .labelStyle(.titleAndIcon)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(TerminalLook.background.opacity(0.92))
            }
        }
        .task(id: agent.sessionId) { await keepFocusStated() }
        .onDisappear {
            onTerminalFocus(false)
            onInputFocus(false)
            Panel.send(action: "panel_terminal", value: "")
        }
        .onChange(of: client.visible) { _, now in
            Panel.send(action: "panel_terminal",
                       value: GraceCover.stated(session: agent.sessionId, visible: now,
                                                inGrace: cover.inGrace))
        }
        .onChange(of: cover.inGrace) { _, covered in
            Panel.send(action: "panel_terminal",
                       value: GraceCover.stated(session: agent.sessionId,
                                                visible: client.visible, inGrace: covered))
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("# terminal")
                .foregroundStyle(Theme.faint)
            Text("·")
                .foregroundStyle(Theme.faint)
            Text(title.isEmpty ? (agent.nickname.isEmpty
                                  ? String(agent.sessionId.prefix(8))
                                  : agent.nickname)
                               : title)
                .foregroundStyle(Theme.phosphorBright)
                .lineLimit(1)
            if exited {
                Text("·")
                    .foregroundStyle(Theme.faint)
                Text("ended")
                    .foregroundStyle(Theme.dim)
            }
            Spacer(minLength: 4)
            Text("\(ptySize.cols)×\(ptySize.rows)")
                .foregroundStyle(Theme.faint)
        }
        .font(Theme.mono(10))
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Theme.well)
    }

    /// The heartbeat: the session id every `focusEveryNanoseconds` while the
    /// panel can be seen, and **asleep while it cannot** — a hidden pane
    /// waits for the next visible edge instead of waking every two seconds
    /// to send nothing. The daemon trusts the last beat for
    /// `FRONTMOST_TRUST_SECONDS`, and the `.onChange` below sends `""` on the
    /// hidden edge, so a beat that stops while hidden is what it expects.
    private func keepFocusStated() async {
        title = ""
        note = ""
        exited = false
        while !Task.isCancelled {
            if cover.inGrace {
                // Covered, grace running: the `.onChange` above already
                // said `""`; sleep until the cover lifts or settles.
                for await covered in cover.$inGrace.values where !covered { break }
            } else if client.visible {
                Panel.send(action: "panel_terminal", value: agent.sessionId)
                try? await Task.sleep(nanoseconds: Self.focusEveryNanoseconds)
            } else {
                // Ends on the first `true`, or when the task is cancelled
                // (the sequence finishes), so the loop re-checks either way.
                for await now in client.$visible.values where now { break }
            }
        }
    }
}

/// The colours and the face of the pane, matched to the editor's own
/// terminal (VS Code's Dark Modern) rather than pure white on pure black:
/// the same agent looked a different, harsher thing here than in the
/// window next door.
enum TerminalLook {
    static let backgroundHex: UInt32 = 0x1f1f1f
    static let foregroundHex: UInt32 = 0xcccccc
    static let background = Color(red: 0x1f / 255.0, green: 0x1f / 255.0, blue: 0x1f / 255.0)
    static let backgroundNS = nsColor(backgroundHex)
    static let foregroundNS = nsColor(foregroundHex)
    static let fontName = "Menlo"
    static let fontSize: CGFloat = 12

    /// The 16 ANSI colours, in SwiftTerm's order: the 8 normal then the 8
    /// bright.
    static let ansiHex: [UInt32] = [
        0x000000, 0xcd3131, 0x0dbc79, 0xe5e510, 0x2472c8, 0xbc3fbc, 0x11a8cd, 0xe5e5e5,
        0x666666, 0xf14c4c, 0x23d18b, 0xf5f543, 0x3b8eea, 0xd670d6, 0x29b8db, 0xe5e5e5,
    ]

    static func nsColor(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255.0,
                green: CGFloat((hex >> 8) & 0xff) / 255.0,
                blue: CGFloat(hex & 0xff) / 255.0, alpha: 1)
    }

    static func terminalColor(_ hex: UInt32) -> SwiftTerm.Color {
        SwiftTerm.Color(red: UInt16((hex >> 16) & 0xff) * 257,
                        green: UInt16((hex >> 8) & 0xff) * 257,
                        blue: UInt16(hex & 0xff) * 257)
    }

    static var font: NSFont {
        NSFont(name: fontName, size: fontSize)
            ?? NSFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
    }

    static func apply(to view: TerminalView) {
        view.nativeForegroundColor = foregroundNS
        view.nativeBackgroundColor = backgroundNS
        view.caretColor = foregroundNS
        view.installColors(ansiHex.map(terminalColor))
        view.font = font
        // **The wheel has to reach the application.** SwiftTerm decides
        // what a wheel event means from three facts: whether reporting is
        // allowed, whether the alternate screen is up, and DECSET 1007
        // (alternate scroll, default on). Claude Code runs in the
        // alternate screen with `?1000;1002;1003;1006h` set — the
        // daemon's paint replays every one of them — so with reporting
        // *off* the wheel fell through to the alternate-scroll rung and
        // was typed at the agent as **Up/Down arrow keys**, one per line
        // of the gesture. A flick walked the background-agent list, or
        // the prompt history, or whatever widget held the caret, and only
        // looked like scrolling where that widget happened to scroll.
        // The alternate buffer has no scrollback of ours to move, so
        // there was never a local answer to give.
        //
        // Reporting on is what every other terminal does and it is the
        // only thing that scrolls this pane. The cost is the same one
        // Terminal.app and VS Code charge: while an application is
        // tracking the mouse, a plain drag is the application's, and
        // **Shift** is what selects text (`shiftBypassesMouseReporting`).
        // Pointer *motion* under `?1003h` was never gated by this flag —
        // SwiftTerm's `mouseMoved` consults the mode alone — so nothing
        // here changes how much of that travels.
        view.allowMouseReporting = true
        // **Option composes characters; it is not Meta.** SwiftTerm defaults
        // this to true and sends ESC+char for every Option press, so a Polish
        // or German layout could not type its own letters here — ⌥A produced
        // an escape sequence rather than `ą`. VS Code's
        // `terminal.integrated.macOptionIsMeta` defaults to false and
        // Terminal.app composes by default; this pane matches them. The four
        // things Meta gave — ⌥←, ⌥→, ⌥⌫ and ⌥⏎ — are restored by name in
        // `TerminalKeys.verdict`, so nothing is lost by the change. It also
        // takes away SwiftTerm's hidden ⌘⌥O toggle for this flag, which
        // `TerminalKeys` consumes as `.nobody` so it can never fire by
        // accident.
        view.optionAsMetaKey = false
    }
}

/// The pane's own `TerminalView`, for the one thing AppKit will not do by
/// itself: **a click on a plain `NSView` does not make it first responder**.
/// SwiftTerm's `mouseDown` never calls `makeFirstResponder`, so once the
/// caret left the pane — which dictation's modifier monitor used to do on
/// every Shift — clicking the black screen did not bring it back, and only
/// switching sessions or leaving and re-entering the Terminal tab did.
///
/// `keyDown` is `public`, not `open`, and is deliberately **not** overridden:
/// the panel's local `NSEvent` monitor is the seam we own.
final class PaneTerminalView: TerminalView {
    override func mouseDown(with event: NSEvent) {
        // Before `super`: with mouse reporting on, a click inside an
        // application that tracks the mouse is forwarded as a report and
        // returns early, and that click must still take the caret.
        window?.makeFirstResponder(self)
        super.mouseDown(with: event)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard let window, window.isKeyWindow else { return }
        guard TerminalKeys.claimsCaret(
            paneAttached: true,
            windowIsKey: true,
            currentIsTerminal: window.firstResponder === self,
            currentIsEditable: DictationFocus.isEditing(window)) else { return }
        window.makeFirstResponder(self)
    }
}

/// A refusal has to outlive the pane's own reconnect. The stream closes
/// behind an `E` frame and the pane reopens a second later, and an open
/// that cleared the note unconditionally would blink the sentence once a
/// second forever. Held until real screen bytes arrive. Three pure rules:
/// `refused` returns the sentence and records it **only while this
/// connection has shown no screen bytes** — a refusal that follows a `D`
/// frame (a refused keystroke on a printing agent) is drawn as before and
/// never held, or the very next `D` would erase it as a blink; `opened`
/// starts a connection (no bytes seen yet) and returns whatever is held
/// (`""` when nothing is); `received` marks bytes seen and returns `""`
/// exactly once after a held refusal and `nil` thereafter, so the note is
/// cleared on the first `D` frame and the pane is not told to redraw on
/// every later one.
struct TerminalNoteHold: Equatable {
    private(set) var held = ""
    private(set) var sawData = false

    mutating func refused(_ detail: String) -> String {
        if !sawData { held = detail }
        return detail
    }

    mutating func opened() -> String {
        sawData = false
        return held
    }

    mutating func received() -> String? {
        sawData = true
        guard !held.isEmpty else { return nil }
        held = ""
        return ""
    }
}

/// Whether the panel is covered while the stream's `OcclusionGrace` wait
/// runs — the one thing `client.visible` cannot say during those seconds,
/// because the gate deliberately stays open through them. **Not a second
/// visibility flag on `DaemonClient`**: it gates no stream and no snapshot;
/// only the terminal pane's `panel_terminal` heartbeat reads it, so the
/// daemon's alert suppression (`_panel_focused_sessions`) stops trusting a
/// covered pane at the cover, exactly as before the grace. Written only by
/// the app delegate: `note` on every occlusion verdict, `clear` when the
/// wait is cancelled or settles.
@MainActor
final class GraceCover: ObservableObject {
    static let shared = GraceCover()

    @Published private(set) var inGrace = false

    func note(_ verdict: OcclusionGrace.Verdict) {
        switch verdict {
        case .open: inGrace = false
        case .armed: inGrace = true
        case .unchanged: break
        }
    }

    func clear() { inGrace = false }

    /// What the pane states on an edge: its session only while the panel
    /// can be seen and no cover's grace is running, else `""`.
    nonisolated static func stated(session: String, visible: Bool,
                                   inGrace: Bool) -> String {
        visible && !inGrace ? session : ""
    }
}

/// What a hidden pane does with the pty bytes nobody can see: **hold them,
/// in order, and replay them on the visible edge** — SwiftTerm is fed on
/// main, and feeding a screen under a cover is main-thread work for no
/// reader. Bounded: a hold that would pass `capBytes` flips `overflowed`,
/// empties the store and drops every later hold, because a partial replay
/// leaves the emulator mid-escape-sequence. Two pure rules: `hold` appends
/// (or, past the cap, overflows); `drain` returns `.bytes` with everything
/// held, `.nothing` when empty, and `.repaint` exactly once after an
/// overflow — the pane then reopens its stream and the daemon's
/// `Screen.paint()` redraws the whole picture. Either way the buffer is
/// clean after a drain.
struct HiddenTerminalBuffer: Equatable {
    static let capBytes = 1 << 20

    private(set) var held = Data()
    private(set) var overflowed = false

    enum Drain: Equatable {
        case nothing
        case bytes(Data)
        case repaint
    }

    mutating func hold(_ chunk: Data) {
        guard !overflowed else { return }
        if held.count + chunk.count > Self.capBytes {
            overflowed = true
            held = Data()
            return
        }
        held.append(chunk)
    }

    mutating func drain() -> Drain {
        if overflowed {
            overflowed = false
            held = Data()
            return .repaint
        }
        guard !held.isEmpty else { return .nothing }
        let out = held
        held = Data()
        return .bytes(out)
    }
}

/// AppKit terminal view on SwiftTerm, fed by the stream's coordinator.
struct NativeTerminalHost: NSViewRepresentable {
    var session: String
    var client: DaemonClient
    var onSize: (Int, Int) -> Void
    var onTitle: (String) -> Void
    var onNote: (String) -> Void
    var onExit: () -> Void
    var onFocus: (Bool) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(client: client, onSize: onSize, onTitle: onTitle,
                    onNote: onNote, onExit: onExit, onFocus: onFocus)
    }

    func makeNSView(context: Context) -> TerminalView {
        let view = PaneTerminalView(frame: .zero)
        view.terminalDelegate = context.coordinator
        TerminalLook.apply(to: view)
        context.coordinator.view = view
        context.coordinator.attach(session: session)
        context.coordinator.startFocusWatch()
        // The caret is claimed by `PaneTerminalView.viewDidMoveToWindow`,
        // which runs when the view actually has one — the old
        // `DispatchQueue.main.async` here raced SwiftUI's own focus work and
        // did nothing durable if the window was not yet key.
        return view
    }

    func updateNSView(_ view: TerminalView, context: Context) {
        context.coordinator.onSize = onSize
        context.coordinator.onTitle = onTitle
        context.coordinator.onNote = onNote
        context.coordinator.onExit = onExit
        context.coordinator.onFocus = onFocus
        if context.coordinator.session != session {
            context.coordinator.attach(session: session)
            DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        }
    }

    static func dismantleNSView(_ view: TerminalView, coordinator: Coordinator) {
        coordinator.shutdown()
    }

    final class Coordinator: NSObject, TerminalViewDelegate {
        var onSize: (Int, Int) -> Void
        var onTitle: (String) -> Void
        var onNote: (String) -> Void
        var onExit: () -> Void
        var onFocus: (Bool) -> Void
        weak var view: TerminalView?
        private(set) var session = ""

        private let client: DaemonClient
        private var stream: TerminalStreamConnection?
        private var generation = 0
        private var exited = false
        private var closed = false
        private var reconnect: DispatchWorkItem?
        private var resize: DispatchWorkItem?
        private var lastSize: (Int, Int) = (0, 0)
        private var reportedFocus = false
        private var focusTimer: Timer?
        private var keyWindowObserver: Any?
        /// The refusal held across a reconnect; main-thread state, read
        /// and written only inside the stream callbacks' main hops.
        private var note = TerminalNoteHold()
        /// Pty bytes that arrived while the panel could not be seen,
        /// replayed on the visible edge (`releaseHidden`). Main-thread
        /// state, like `note`.
        private var hidden = HiddenTerminalBuffer()
        /// The visible-edge watch on `client.$visible`, installed with the
        /// focus watch and cancelled in `shutdown()`.
        private var visibleWatch: AnyCancellable?

        /// How long a closed stream waits before it is opened again, and
        /// how long a refusal (the session not hosted *yet*) waits.
        static let reconnectDelay: TimeInterval = 1.0
        static let refusedDelay: TimeInterval = 2.0
        /// Resizes are stated once the pane has stopped moving.
        static let resizeDebounce: TimeInterval = 0.1

        init(client: DaemonClient,
             onSize: @escaping (Int, Int) -> Void,
             onTitle: @escaping (String) -> Void,
             onNote: @escaping (String) -> Void,
             onExit: @escaping () -> Void,
             onFocus: @escaping (Bool) -> Void) {
            self.client = client
            self.onSize = onSize
            self.onTitle = onTitle
            self.onNote = onNote
            self.onExit = onExit
            self.onFocus = onFocus
        }

        deinit {
            focusTimer?.invalidate()
            stream?.cancel()
            // `dismantleNSView` calls `shutdown()` on every teardown, so this
            // is insurance rather than a found leak — but a coordinator that
            // outlived its pane while still observing key-window changes
            // would claim a caret in a window it no longer draws in.
            if let keyWindowObserver {
                NotificationCenter.default.removeObserver(keyWindowObserver)
            }
        }

        // MARK: the stream

        func attach(session: String) {
            self.session = session
            exited = false
            lastSize = (0, 0)
            // A held refusal is the old session's: a row switch must not
            // draw it over the new one's first open.
            note = TerminalNoteHold()
            if let view {
                // Re-asserted on every attach: the look is the pane's, and
                // `optionAsMetaKey` in particular must not be able to survive
                // a session change in whatever state something left it.
                TerminalLook.apply(to: view)
                view.getTerminal().setup(isReset: true)
            }
            open()
        }

        func shutdown() {
            closed = true
            reconnect?.cancel()
            resize?.cancel()
            stream?.cancel()
            stream = nil
            focusTimer?.invalidate()
            focusTimer = nil
            visibleWatch?.cancel()
            visibleWatch = nil
            hidden = HiddenTerminalBuffer()
            if let keyWindowObserver {
                NotificationCenter.default.removeObserver(keyWindowObserver)
                self.keyWindowObserver = nil
            }
        }

        private func open() {
            reconnect?.cancel()
            stream?.cancel()
            // Before the new connection can call back: it opens with a reset
            // and a full paint, so nothing the old one held may replay over it.
            hidden = HiddenTerminalBuffer()
            guard !closed, !session.isEmpty else { return }
            generation += 1
            let gen = generation
            let client = self.client
            let conn = MainActor.assumeIsolated { client.terminalStream(session: session) }
            conn.onOpen = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    // Every open starts from a blank terminal: the daemon's
                    // first frames are a full paint. A held refusal stays
                    // on screen until those bytes arrive.
                    self.view?.getTerminal().setup(isReset: true)
                    self.onNote(self.note.opened())
                    self.restateSize()
                }
            }
            conn.onData = { [weak self] data in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation, let view = self.view else { return }
                    // Decided here, on main, in frame order: a hidden panel
                    // holds the bytes and the visible edge replays them.
                    if MainActor.assumeIsolated({ self.client.visible }) {
                        if let cleared = self.note.received() { self.onNote(cleared) }
                        view.feed(byteArray: [UInt8](data)[...])
                    } else {
                        self.hidden.hold(data)
                    }
                }
            }
            conn.onExit = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.exited = true
                    self.onExit()
                }
            }
            conn.onRefused = { [weak self] status, detail in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    if status == 403 {
                        MainActor.assumeIsolated { self.client.noteStreamAuthRefused() }
                    }
                    self.onNote(self.note.refused(detail))
                }
            }
            conn.onClosed = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.stream = nil
                    self.scheduleReconnect()
                }
            }
            stream = conn
            conn.start()
        }

        private func scheduleReconnect() {
            guard !closed, !exited else { return }
            reconnect?.cancel()
            let item = DispatchWorkItem { [weak self] in self?.open() }
            reconnect = item
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.reconnectDelay, execute: item)
        }

        private func restateSize() {
            guard let view, let stream else { return }
            let cols = max(view.getTerminal().cols, 1)
            let rows = max(view.getTerminal().rows, 1)
            lastSize = (cols, rows)
            stream.send(cols: cols, rows: rows)
            onSize(cols, rows)
        }

        // MARK: focus

        func startFocusWatch() {
            guard focusTimer == nil else { return }
            let timer = Timer(timeInterval: 0.15, repeats: true) { [weak self] _ in
                self?.sampleFocus()
            }
            focusTimer = timer
            RunLoop.main.add(timer, forMode: .common)
            // The poll stays as the floor; this only makes the flag settle in
            // the same run-loop pass as the window change instead of up to
            // 150 ms later, and claims the caret back when the panel is
            // brought to the front with the pane drawn.
            keyWindowObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didBecomeKeyNotification,
                object: nil, queue: .main
            ) { [weak self] note in
                guard let self, let view = self.view else { return }
                guard let window = note.object as? NSWindow,
                      window === view.window else { return }
                if TerminalKeys.claimsCaret(
                    paneAttached: view.window != nil,
                    windowIsKey: window.isKeyWindow,
                    currentIsTerminal: window.firstResponder === view,
                    currentIsEditable: DictationFocus.isEditing(window)) {
                    window.makeFirstResponder(view)
                }
                self.sampleFocus()
            }
            // The visible edge replays what a hidden pane held. The flag is
            // only ever written on main, so the sink runs there — and runs in
            // the flag's `willSet`, before any later byte's main hop can read
            // the new value, so held bytes always land ahead of fresh ones.
            let client = self.client
            visibleWatch = MainActor.assumeIsolated { client.$visible }
                .removeDuplicates()
                .sink { [weak self] now in
                    guard now else { return }
                    self?.releaseHidden()
                }
        }

        /// Feed what arrived while hidden, in one `feed`, or — past the
        /// buffer's cap — reopen the stream so the daemon's paint redraws
        /// the whole screen rather than a truncated run of bytes.
        private func releaseHidden() {
            #if DEBUG
            dispatchPrecondition(condition: .onQueue(.main))
            #endif
            switch hidden.drain() {
            case .nothing:
                return
            case .bytes(let data):
                guard let view else { return }
                if let cleared = note.received() { onNote(cleared) }
                view.feed(byteArray: [UInt8](data)[...])
            case .repaint:
                Trace.log("terminal held bytes overflowed; repainting")
                open()
            }
        }

        private func sampleFocus() {
            guard let view else { return }
            // Key window as well as first responder: a pane left behind an
            // open card or settings window still *is* its window's first
            // responder, and a flag stuck true there would hand every
            // triage key to a terminal nobody is typing into.
            let focused = view.window?.isKeyWindow == true
                && view.window?.firstResponder === view
            if focused != reportedFocus {
                reportedFocus = focused
                onFocus(focused)
            }
        }

        // MARK: TerminalViewDelegate

        func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
            let size = (max(newCols, 1), max(newRows, 1))
            resize?.cancel()
            let item = DispatchWorkItem { [weak self] in
                guard let self, size != self.lastSize else { return }
                self.lastSize = size
                self.stream?.send(cols: size.0, rows: size.1)
                self.onSize(size.0, size.1)
            }
            resize = item
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.resizeDebounce, execute: item)
        }

        func setTerminalTitle(source: TerminalView, title: String) {
            onTitle(title)
        }

        func send(source: TerminalView, data: ArraySlice<UInt8>) {
            guard !exited else { return }
            guard let stream else {
                onNote("The terminal is reconnecting; that key was not sent.")
                return
            }
            // A multi-line paste is one `insertText` and so one frame; the
            // daemon refuses a raw write over `TERMINAL_MAX_RAW_BYTES` whole,
            // and a refused paste lands as an orange note instead of as text.
            // Order is kept because `TerminalStreamConnection` serialises
            // every write on its own queue. Still one socket, still no drain
            // wait.
            for chunk in TerminalKeys.chunks(Data(data)) {
                stream.send(input: chunk)
            }
        }

        func scrolled(source: TerminalView, position: Double) {}

        func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}

        func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {}

        func bell(source: TerminalView) {}

        func clipboardCopy(source: TerminalView, content: Data) {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setData(content, forType: .string)
        }

        /// **The OSC-52 read stays unanswered, and that is the decision.**
        ///
        /// This is not "an agent asking for the clipboard". SwiftTerm's
        /// `oscClipboard` answers a bare `ESC ] 52 ; c ; ?` by base64-ing
        /// whatever this returns and `sendResponse`-ing it **up the pty, onto
        /// the input line** — and the trigger is *output the pty printed*,
        /// not a gesture a person made: `cat` of an untrusted file, a diff, a
        /// stray `printf`. Answering it would hand the whole system clipboard,
        /// passwords and tokens included, to whatever is running, with no
        /// prompt and no log line. Terminal.app does not implement the read,
        /// VS Code does not, and iTerm2 requires opt-in — so answering would
        /// be *more* permissive than all three of this pane's parity targets,
        /// and the phone's identical delegate returns nil too. The **write**
        /// half (`clipboardCopy`) stays, and it is not free: it is reachable
        /// from pty output the same way, and an agent printing
        /// `ESC ] 52 ; c ; <base64>` replaces the person's clipboard silently.
        /// It is kept because it is the direction a person still has to act
        /// on — the substituted text does nothing until they paste it — and
        /// because it is what the pane has always done; gating it is its own
        /// change, recorded in `docs/panel-window-contract.md`.
        ///
        /// ⌘V does not come this way at all — `TerminalView.paste(_:)` reads
        /// `NSPasteboard.general` itself, on a keystroke.
        func clipboardRead(source: TerminalView) -> Data? { nil }

        func iTermContent(source: TerminalView, content: ArraySlice<UInt8>) {}

        func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
    }
}

/// The size the header shows, held outside the `View` value so a live
/// resize reaches it.
private final class PtySize {
    var cols: Int
    var rows: Int
    init(cols: Int, rows: Int) {
        self.cols = cols
        self.rows = rows
    }
}


/// Who holds the caret and the keys AppKit will not carry to it.
///
/// **Focus.** The panel's key monitor decides whether a press is text or
/// triage, and the published `KeyRouter.terminalFocused` reaches it one
/// focus-poll late. `holdsCaret` is the floor underneath it: at the instant
/// of the press, is the window's first responder a hosted terminal? It only
/// ever widens the pass-through, exactly as `DictationFocus.isEditing` does
/// for text fields.
///
/// **The line edits.** A key carrying ⌘ never reaches SwiftTerm's own key
/// handling: `keyDown` hands it to `interpretKeyEvents`, which turns it into
/// a standard editing selector, and `doCommand(by:)` answers one it has no
/// case for by printing "Unhandle selector" and sending nothing. So **⌘⌫ did
/// nothing at all** — the shortcut every macOS terminal spends on "clear the
/// line" — and neither did ⌘⌦ or fn-⌫; ⌘← and ⌘→ were answered with the
/// Emacs *word* moves, which is not where the line ends. The table that says
/// what each combination is for is `TerminalKeys.verdict`; `send` below posts
/// its bytes through the view's own delegate, the one path every other
/// keystroke takes. SwiftTerm's `keyDown` is `public` and not `open`, so that
/// cannot be a subclass; the monitor is the seam we do own.
enum TerminalFocus {

    static func holdsCaret(_ window: NSWindow?) -> Bool {
        terminal(in: window) != nil
    }

    static func terminal(in window: NSWindow?) -> TerminalView? {
        window?.firstResponder as? TerminalView
    }

    /// Hand `bytes` to the focused terminal's delegate — the same route its
    /// own `keyDown` takes. False where nothing is focused.
    @discardableResult
    static func send(_ bytes: [UInt8], to window: NSWindow?) -> Bool {
        guard let view = terminal(in: window),
              let delegate = view.terminalDelegate else { return false }
        delegate.send(source: view, data: bytes[...])
        return true
    }
}
