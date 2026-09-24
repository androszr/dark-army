import SwiftTerm
import SwiftUI
import UIKit

/// The terminal the Mac's Dark Army hosts for this session, on the phone —
/// **the same emulator the Mac's window uses**, fed the same bytes. This is
/// the panel's `TerminalPane` at phone measure: a header line, then a
/// SwiftTerm `TerminalView` filling everything left, fed straight from the
/// socket and never through `@State`. Keys go up the same socket.
///
/// Two feeds, one emulator. At home the coordinator opens one held-open,
/// sealed stream (`SealedTerminalStream`) and every byte the agent prints
/// lands as it is printed; the first bytes down are the Mac emulator's own
/// drawing of the screen (`Screen.paint()`), so attaching starts clean.
/// Away, iOS suspends sockets, so the pane is fed by the poll instead
/// (`PhoneClient.fetchTerminalBytes` → `onTerminalBytes`): a `painted`
/// frame resets the emulator first, then the bytes since the last check-in,
/// capped per poll. Keys away ride `terminal_input` with `bytes`.
///
/// **This file is the one named exception** to two phone rules
/// (`docs/phone-contract.md`): the terminal has its own text size — a real
/// terminal is a grid of cells, not prose that reflows — set here in
/// points with a `+` / `−` in the header and remembered; and it speaks as
/// one element reading the emulator's non-blank rows, because the screen
/// reader would otherwise read a TUI as hundreds of fragments.
struct PhoneTerminalPane: View {
    let agent: Agent
    @ObservedObject var client: PhoneClient
    @Environment(\.scenePhase) private var scenePhase
    /// The one place a point size is named outside `Theme.mono`: the
    /// terminal's own, remembered across launches.
    @AppStorage("terminalFontSize") private var fontSize: Double = PhoneTerminalLook.defaultFontSize
    @State private var title = ""
    @State private var note = ""
    @State private var exited = false
    @State private var spoken = "Terminal, empty."
    /// The pty's size as the emulator last stated it. Two `@State` ints and
    /// not a reference held in one: a class in `@State` is mutated in place,
    /// SwiftUI observes nothing, and the header froze on the first width it
    /// was given — which is exactly the readout the width rule is watched by.
    @State private var cols = 0
    @State private var rows = 0
    /// Whether the feed is up. `exited` is the agent's own ending; this is
    /// the line to it, so a stream that cannot open says so rather than
    /// drawing a black rectangle labelled `live`.
    @State private var linked = false

    static let olderMacSentence = "This Mac's Dark Army is too old to stream a terminal."

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if !client.snapshot.board.terminalStreamSupported {
                Text(Self.olderMacSentence)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Spacer(minLength: 0)
            } else {
                PhoneTerminalHost(
                    session: agent.sessionId,
                    client: client,
                    fontSize: CGFloat(fontSize),
                    active: scenePhase != .background,
                    away: client.knowsItIsAway,
                    onSize: { c, r in
                        cols = c
                        rows = r
                    },
                    onTitle: { title = $0 },
                    onNote: { note = $0 },
                    onExit: { exited = true },
                    onLink: { linked = $0 },
                    onSpoken: { spoken = $0 })
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(PhoneTerminalLook.background)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(spoken)
                // The away hint and the Mac's note sit **under** the
                // grid, in the stack, never over it: as a bottom overlay
                // they covered the last rows — which, with the keyboard
                // up, is the prompt line — so a person typing from away
                // watched their own keys vanish behind the sentence that
                // explains where keys go.
                if note.isEmpty, client.knowsItIsAway, !exited {
                    Text(AwayKeys.hint)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(PhoneTerminalLook.background)
                }
                if !note.isEmpty {
                    Text(note)
                        .font(Theme.mono(11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(PhoneTerminalLook.background)
                }
            }
        }
        .background(PhoneTerminalLook.background)
        .onChange(of: agent.sessionId) { _, _ in
            title = ""
            note = ""
            exited = false
            linked = false
            cols = 0
            rows = 0
        }
    }

    /// What the line is doing, in the header's third slot: the agent's
    /// own ending first, then whether there is a feed at all.
    private var state: String {
        if exited { return "ended" }
        return linked ? "live" : "offline"
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
                .fixedSize(horizontal: false, vertical: true)
            Text("·")
                .foregroundStyle(Theme.faint)
            Text(state)
                .foregroundStyle(exited ? Theme.dim
                                        : (linked ? Theme.phosphor : Theme.alarm))
            Spacer(minLength: 4)
            if cols > 0 {
                Text("\(cols)×\(rows)")
                    .foregroundStyle(Theme.faint)
            }
            DecryptButton("−") { fontSize = max(PhoneTerminalLook.minFontSize, fontSize - 1) }
                .buttonStyle(AlarmOutline(color: Theme.phosphor))
                .disabled(fontSize <= PhoneTerminalLook.minFontSize)
                .accessibilityLabel("Smaller terminal text")
            DecryptButton("+") { fontSize = min(PhoneTerminalLook.maxFontSize, fontSize + 1) }
                .buttonStyle(AlarmOutline(color: Theme.phosphor))
                .disabled(fontSize >= PhoneTerminalLook.maxFontSize)
                .accessibilityLabel("Larger terminal text")
        }
        .font(Theme.mono(10))
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Theme.well)
        .accessibilityAddTraits(.isHeader)
    }
}

/// The colours and the face of the pane: the panel's `TerminalLook` — VS
/// Code's Dark Modern — in `UIColor`, so the same agent looks the same
/// thing on both screens.
enum PhoneTerminalLook {
    static let backgroundHex: UInt32 = 0x1f1f1f
    static let foregroundHex: UInt32 = 0xcccccc
    static let background = Color(red: 0x1f / 255.0, green: 0x1f / 255.0, blue: 0x1f / 255.0)
    static let fontName = "Menlo"
    static let defaultFontSize: Double = 12
    static let minFontSize: Double = 8
    static let maxFontSize: Double = 24

    /// The 16 ANSI colours, in SwiftTerm's order: the 8 normal then the 8
    /// bright.
    static let ansiHex: [UInt32] = [
        0x000000, 0xcd3131, 0x0dbc79, 0xe5e510, 0x2472c8, 0xbc3fbc, 0x11a8cd, 0xe5e5e5,
        0x666666, 0xf14c4c, 0x23d18b, 0xf5f543, 0x3b8eea, 0xd670d6, 0x29b8db, 0xe5e5e5,
    ]

    static func uiColor(_ hex: UInt32) -> UIColor {
        UIColor(red: CGFloat((hex >> 16) & 0xff) / 255.0,
                green: CGFloat((hex >> 8) & 0xff) / 255.0,
                blue: CGFloat(hex & 0xff) / 255.0, alpha: 1)
    }

    static func terminalColor(_ hex: UInt32) -> SwiftTerm.Color {
        SwiftTerm.Color(red: UInt16((hex >> 16) & 0xff) * 257,
                        green: UInt16((hex >> 8) & 0xff) * 257,
                        blue: UInt16(hex & 0xff) * 257)
    }

    /// The terminal's own face at its own size — the named exception to
    /// `Theme.mono`: cells, not prose.
    static func font(_ size: CGFloat) -> UIFont {
        UIFont(name: fontName, size: size)
            ?? UIFont.monospacedSystemFont(ofSize: size, weight: .regular)
    }

    static func apply(to view: TerminalView, fontSize: CGFloat) {
        view.nativeForegroundColor = uiColor(foregroundHex)
        view.nativeBackgroundColor = uiColor(backgroundHex)
        view.caretColor = uiColor(foregroundHex)
        view.installColors(ansiHex.map(terminalColor))
        view.font = font(fontSize)
        view.keyboardAppearance = .dark
        // Scrolling and selection belong to the person, not to the
        // application's mouse tracking: with reporting on, every touch was
        // a report the keys queued behind.
        view.allowMouseReporting = false
    }
}

/// UIKit terminal view on SwiftTerm, fed by the coordinator — the panel's
/// `NativeTerminalHost` on the phone.
struct PhoneTerminalHost: UIViewRepresentable {
    var session: String
    var client: PhoneClient
    var fontSize: CGFloat
    /// Whether the scene is active: iOS suspends the socket in the
    /// background, so an inactive scene cancels it and an active one opens
    /// a fresh stream with a fresh paint.
    var active: Bool
    /// Whether the phone knows it is away (`PhoneClient.knowsItIsAway`): a
    /// change re-attaches on the other feed.
    var away: Bool
    var onSize: (Int, Int) -> Void
    var onTitle: (String) -> Void
    var onNote: (String) -> Void
    var onExit: () -> Void
    /// Whether there is a feed at all — the header's `live` / `offline`.
    var onLink: (Bool) -> Void
    var onSpoken: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(client: client, onSize: onSize, onTitle: onTitle,
                    onNote: onNote, onExit: onExit, onLink: onLink,
                    onSpoken: onSpoken)
    }

    func makeUIView(context: Context) -> TerminalView {
        let view = TerminalView(frame: .zero)
        view.terminalDelegate = context.coordinator
        PhoneTerminalLook.apply(to: view, fontSize: fontSize)
        context.coordinator.view = view
        context.coordinator.fontSize = fontSize
        context.coordinator.away = away
        context.coordinator.attach(session: session)
        context.coordinator.setActive(active)
        // After the cover has settled — claiming the keyboard during the
        // full-screen presentation, from inside a detent sheet, is the crash.
        if active { context.coordinator.claimKeyboard() }
        return view
    }

    func updateUIView(_ view: TerminalView, context: Context) {
        let coordinator = context.coordinator
        coordinator.onSize = onSize
        coordinator.onTitle = onTitle
        coordinator.onNote = onNote
        coordinator.onExit = onExit
        coordinator.onLink = onLink
        coordinator.onSpoken = onSpoken
        if coordinator.fontSize != fontSize {
            coordinator.fontSize = fontSize
            view.font = PhoneTerminalLook.font(fontSize)
        }
        var reattach = false
        if coordinator.session != session { reattach = true }
        if coordinator.away != away {
            coordinator.away = away
            reattach = true
        }
        if reattach {
            coordinator.attach(session: session)
            coordinator.claimKeyboard()
        }
        coordinator.setActive(active)
    }

    static func dismantleUIView(_ view: TerminalView, coordinator: Coordinator) {
        coordinator.shutdown()
    }

    final class Coordinator: NSObject, TerminalViewDelegate {
        var onSize: (Int, Int) -> Void
        var onTitle: (String) -> Void
        var onNote: (String) -> Void
        var onExit: () -> Void
        var onLink: (Bool) -> Void
        var onSpoken: (String) -> Void
        weak var view: TerminalView?
        private(set) var session = ""
        var fontSize: CGFloat = 0
        var away = false

        private let client: PhoneClient
        private var stream: SealedTerminalStream?
        private var generation = 0
        private var exited = false
        private var closed = false
        private var active = true
        /// Fed by the poll rather than a socket — the away feed.
        private var polled = false
        private var refused = false
        /// Closes since the last head that opened. Nothing is drawn on the
        /// first — a reconnect inside a second is invisible and ordinary —
        /// but from the second the pane says so and the wait doubles, and
        /// past `maxAttempts` it stops asking until something changes.
        private var attempts = 0
        /// A refusal there is no point retrying (the Mac says it does not
        /// host this session): stop, and say why. Cleared by `attach`.
        private var gaveUp = false
        private var reconnect: DispatchWorkItem?
        private var resize: DispatchWorkItem?
        private var spokenWork: DispatchWorkItem?
        private var keyboardClaim: DispatchWorkItem?
        private var lastSize: (Int, Int) = (0, 0)
        /// Keys typed away while a `terminal_input` is in flight, sent as
        /// one press behind it — in order, one receipt each.
        private var pendingAway = Data()
        private var awaySending = false
        /// The pause timer behind a batch of printable keys typed away.
        private var awayFlush: DispatchWorkItem?

        /// How long a closed stream waits before it is opened again, and
        /// how long a refusal (the session not hosted *yet*, or a stale
        /// counter fast-forwarded) waits.
        static let reconnectDelay: TimeInterval = 1.0
        static let refusedDelay: TimeInterval = 2.0
        /// The wait doubles from `reconnectDelay` and stops here: a Mac that
        /// is off is not helped by a knock a second, and the phone's radio
        /// is the person's battery.
        static let maxReconnectDelay: TimeInterval = 30.0
        /// After this many closes with nothing opening, the pane stops
        /// asking and says so; a re-attach (a new session, coming back to
        /// the app, the home/away flip) starts it over.
        static let maxAttempts = 6
        static let reconnectingNote = "Reconnecting to the terminal…"
        static let unreachableNote =
            "The terminal is not answering. Leave this screen and come back to try again."
        static let notHostedNote =
            "Dark Army is no longer hosting this terminal."
        static let keyLostNote = "The terminal is reconnecting; that key was not sent."

        /// Resizes are stated once the pane has stopped moving.
        static let resizeDebounce: TimeInterval = 0.1
        /// The spoken screen is recomposed once the bytes have stopped.
        static let spokenDebounce: TimeInterval = 0.5
        /// Wait until the full-screen cover's presentation has finished
        /// before SwiftTerm takes the keyboard. Nested sheet + cover +
        /// keyboard during the transition is the crash.
        static let keyboardDelay: TimeInterval = 0.45

        init(client: PhoneClient,
             onSize: @escaping (Int, Int) -> Void,
             onTitle: @escaping (String) -> Void,
             onNote: @escaping (String) -> Void,
             onExit: @escaping () -> Void,
             onLink: @escaping (Bool) -> Void,
             onSpoken: @escaping (String) -> Void) {
            self.client = client
            self.onSize = onSize
            self.onTitle = onTitle
            self.onNote = onNote
            self.onExit = onExit
            self.onLink = onLink
            self.onSpoken = onSpoken
        }

        deinit {
            stream?.cancel()
        }

        // MARK: the feed

        /// Every attach starts from a blank terminal: the first thing down
        /// on either feed is a whole paint.
        /// **The callbacks are deferred a tick, the coordinator's own state
        /// is not.** Both `makeUIView` and `updateUIView` call this from
        /// inside a SwiftUI view update, and `onLink` / `onNote` write the
        /// pane's own observed properties — writing them here is the
        /// "Modifying state during view update" undefined behaviour, which
        /// shows up as a header stuck on the wrong word. The fields above
        /// are set synchronously because `updateUIView` compares them on
        /// the very next pass.
        func attach(session: String) {
            self.session = session
            exited = false
            gaveUp = false
            attempts = 0
            lastSize = (0, 0)
            view?.getTerminal().setup(isReset: true)
            DispatchQueue.main.async { [weak self] in
                guard let self, self.session == session else { return }
                self.onLink(false)
                self.open()
            }
        }

        func setActive(_ on: Bool) {
            guard on != active else { return }
            active = on
            if on {
                attach(session: session)
                claimKeyboard()
            } else {
                pause()
            }
        }

        func shutdown() {
            closed = true
            keyboardClaim?.cancel()
            view?.resignFirstResponder()
            pause()
            spokenWork?.cancel()
            resize?.cancel()
        }

        /// Take the keyboard once the cover is on screen, not from
        /// `makeUIView`. Cancelled by `shutdown` if Done beat the delay.
        func claimKeyboard() {
            keyboardClaim?.cancel()
            let item = DispatchWorkItem { [weak self] in
                guard let self, !self.closed, self.active, let view = self.view else { return }
                _ = view.becomeFirstResponder()
            }
            keyboardClaim = item
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.keyboardDelay, execute: item)
        }

        /// The socket is cancelled and the poll sink released; a later
        /// `attach` opens afresh. Never leave a stream open across
        /// backgrounding hoping it survives — iOS suspends it.
        private func pause() {
            reconnect?.cancel()
            keyboardClaim?.cancel()
            stream?.cancel()
            stream = nil
            generation += 1
            if polled {
                MainActor.assumeIsolated {
                    client.onTerminalBytes = nil
                    client.terminalPhoneSize = nil
                }
                polled = false
            }
        }

        private func open() {
            reconnect?.cancel()
            stream?.cancel()
            stream = nil
            guard !closed, active, !session.isEmpty else { return }
            generation += 1
            let gen = generation
            if away {
                openPolled(gen)
                return
            }
            let client = self.client
            if polled {
                MainActor.assumeIsolated {
                    client.onTerminalBytes = nil
                    client.terminalPhoneSize = nil
                }
                polled = false
            }
            guard let conn = MainActor.assumeIsolated({ client.terminalStream(session: session) }) else {
                onNote("Dark Army is not paired here.")
                return
            }
            refused = false
            conn.onOpen = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.attempts = 0
                    self.view?.getTerminal().setup(isReset: true)
                    self.onNote("")
                    self.onLink(true)
                    self.restateSize()
                }
            }
            conn.onData = { [weak self] data in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation, let view = self.view else { return }
                    view.feed(byteArray: [UInt8](data)[...])
                    self.scheduleSpoken()
                }
            }
            conn.onExit = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.exited = true
                    self.onExit()
                }
            }
            conn.onRefused = { [weak self] status, detail, isFinal in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.refused = true
                    self.onLink(false)
                    if status == 403 {
                        MainActor.assumeIsolated { self.client.noteTerminalStreamRefused() }
                    }
                    // A **final** 409 is the Mac saying it does not host
                    // this session — the pty was reaped, or never was one.
                    // Knocking every two seconds for ever answers nothing.
                    // A 409 that is *not* final is the door's stale-counter
                    // or clock rung, which is routine on open (a poll runs
                    // every four seconds and moves the counter under this
                    // stream) and self-heals on the next try: latching there
                    // strands the pane offline on a race that was already
                    // fixed by the time the note was drawn.
                    if status == 409 && isFinal { self.gaveUp = true }
                    if status == 409 && !isFinal {
                        // The counter has been fast-forwarded; say nothing
                        // and let the ladder open again.
                        self.onNote(Self.reconnectingNote)
                        return
                    }
                    self.onNote(detail.isEmpty
                                ? (status == 409 ? Self.notHostedNote : Self.unreachableNote)
                                : detail)
                }
            }
            conn.onClosed = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.stream = nil
                    self.onLink(false)
                    self.scheduleReconnect()
                }
            }
            conn.onInputDropped = { [weak self] in
                DispatchQueue.main.async {
                    guard let self, gen == self.generation else { return }
                    self.onNote(Self.keyLostNote)
                }
            }
            stream = conn
            conn.start()
        }

        /// Away: no socket. The poll leg feeds this sink; a `painted`
        /// frame resets the emulator first, `exited` ends it. The cursor
        /// was reset to `-1` by `watchTerminal`, so the first frame is a
        /// whole paint; a re-attach asks for one again.
        private func openPolled(_ gen: Int) {
            polled = true
            onLink(true)
            let client = self.client
            // The grid as laid out so far, so the very first check-in
            // already carries it; `sizeChanged` keeps it current.
            let cols = view.map { max($0.getTerminal().cols, 1) } ?? 0
            let rows = view.map { max($0.getTerminal().rows, 1) } ?? 0
            MainActor.assumeIsolated {
                client.terminalPhoneSize = cols > 0 && rows > 0 ? (cols, rows) : nil
                client.restartTerminalFeed()
                client.onTerminalBytes = { [weak self] frame in
                    guard let self, gen == self.generation, let view = self.view else { return }
                    guard frame.available else {
                        self.onNote(frame.reason)
                        return
                    }
                    if frame.painted {
                        view.getTerminal().setup(isReset: true)
                        self.onNote("")
                    }
                    let bytes = frame.bytes
                    if !bytes.isEmpty {
                        view.feed(byteArray: [UInt8](bytes)[...])
                        self.scheduleSpoken()
                    }
                    if frame.cols > 0 { self.onSize(frame.cols, frame.rows) }
                    if frame.exited, !self.exited {
                        self.exited = true
                        self.onExit()
                    }
                }
            }
            onNote("Away from home: the terminal updates on every check-in.")
        }

        /// A closed stream is opened again after a wait that doubles, and
        /// the pane says what is happening rather than sitting black under
        /// the word `live`. Two ways it stops: a refusal there is no point
        /// repeating (`gaveUp`), and `maxAttempts` closes with nothing ever
        /// opening.
        private func scheduleReconnect() {
            guard !closed, !exited, active, !away, !gaveUp else { return }
            attempts += 1
            if attempts > Self.maxAttempts {
                onNote(Self.unreachableNote)
                return
            }
            if attempts > 1 { onNote(Self.reconnectingNote) }
            reconnect?.cancel()
            let item = DispatchWorkItem { [weak self] in self?.open() }
            reconnect = item
            let base = refused ? Self.refusedDelay : Self.reconnectDelay
            let delay = min(base * pow(2, Double(attempts - 1)), Self.maxReconnectDelay)
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
        }

        private func restateSize() {
            guard let view, let stream else { return }
            let cols = max(view.getTerminal().cols, 1)
            let rows = max(view.getTerminal().rows, 1)
            lastSize = (cols, rows)
            stream.send(cols: cols, rows: rows)
            onSize(cols, rows)
        }

        // MARK: the spoken screen

        private func scheduleSpoken() {
            spokenWork?.cancel()
            let item = DispatchWorkItem { [weak self] in self?.speak() }
            spokenWork = item
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.spokenDebounce, execute: item)
        }

        /// The non-blank visible rows, top to bottom, as one sentence.
        private func speak() {
            guard let view else { return }
            let terminal = view.getTerminal()
            var lines: [String] = []
            for row in 0..<terminal.rows {
                guard let line = terminal.getLine(row: row) else { continue }
                let text = line.translateToString(trimRight: true)
                if !text.trimmingCharacters(in: .whitespaces).isEmpty {
                    lines.append(text)
                }
            }
            onSpoken(lines.isEmpty ? "Terminal, empty." : "Terminal. " + lines.joined(separator: ". "))
        }

        // MARK: TerminalViewDelegate

        func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
            let size = (max(newCols, 1), max(newRows, 1))
            resize?.cancel()
            let item = DispatchWorkItem { [weak self] in
                guard let self, size != self.lastSize else { return }
                self.lastSize = size
                if self.polled {
                    // Away there is no stream: the size rides the next
                    // check-in's query instead (`fetchTerminalBytes`),
                    // and the Mac answers a paint once it has taken.
                    MainActor.assumeIsolated {
                        self.client.terminalPhoneSize = (size.0, size.1)
                    }
                    return
                }
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
            if polled {
                // Away, every send is one relay write against a budget of
                // ten a minute, so keys are **batched**: held until Enter
                // or a control key (which act at once), or until the
                // typing pauses. A sentence typed from the sofa is one
                // write, not thirty.
                pendingAway.append(contentsOf: data)
                awayFlush?.cancel()
                if AwayKeys.flushesAtOnce(data) {
                    flushAway()
                } else {
                    let item = DispatchWorkItem { [weak self] in self?.flushAway() }
                    awayFlush = item
                    DispatchQueue.main.asyncAfter(deadline: .now() + AwayKeys.pause,
                                                  execute: item)
                }
                return
            }
            guard let stream else {
                onNote(Self.keyLostNote)
                return
            }
            // The stream may exist and not be open yet — the head has not
            // landed. It holds the keys until it does; a key it cannot hold
            // is said out loud rather than swallowed inside `sealedWrite`.
            stream.send(input: Data(data))
        }

        /// Raw keys from away: `terminal_input` with `bytes`, through
        /// `post` — which asks Face ID and mints the receipt, a keystroke
        /// being nothing idempotent. Keys typed while one press is out are
        /// sent together behind it, in order.
        private func flushAway() {
            guard !awaySending, !pendingAway.isEmpty else { return }
            let payload = pendingAway
            pendingAway = Data()
            awaySending = true
            let session = self.session
            let client = self.client
            Task { @MainActor [weak self] in
                // `refreshAfter: false`: the 8 s poll brings the screen
                // back anyway, and a state frame per keystroke was a
                // second relay frame spent on every press.
                let result = await client.post(
                    action: PhoneActions.terminalInput,
                    fields: ["session_id": session, "bytes": payload.base64EncodedString()],
                    scope: session, refreshAfter: false)
                // The echo, now, not on the next check-in. Keys landed on
                // the pty the moment the 200 came back, but the screen
                // only learned it when the poll's terminal leg ran — up
                // to eight seconds of pause plus three relay legs later —
                // which read as a terminal that takes no input. One
                // sealed **read** (no write budget spent, no lease
                // checked), and only after a send that landed: a refused
                // send has its note to draw instead.
                if result.ok { await client.fetchTerminalBytes() }
                guard let self else { return }
                self.awaySending = false
                if !result.ok, !result.detail.isEmpty { self.onNote(result.detail) }
                self.flushAway()
            }
        }

        func scrolled(source: TerminalView, position: Double) {}

        func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}

        func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {}

        func bell(source: TerminalView) {}

        func clipboardCopy(source: TerminalView, content: Data) {
            UIPasteboard.general.setData(content, forPasteboardType: "public.utf8-plain-text")
        }

        func clipboardRead(source: TerminalView) -> Data? { nil }

        func iTermContent(source: TerminalView, content: ArraySlice<UInt8>) {}

        func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
    }
}

/// How keys typed into a terminal from **away** are grouped into relay
/// writes. Pure, so the XCTest and the Python pin can both read it.
///
/// A key that means "act now" — Enter, a control character, an escape
/// sequence (arrows, Escape itself) — flushes the batch at once; printable
/// text is held until the typing pauses for `pause`. The relay allows
/// `RELAY_MAX_WRITES_PER_MINUTE` (10) writes, and the Mac's own refusal is
/// what the pane draws when that is spent.
enum AwayKeys {
    static let pause: TimeInterval = 1.5
    static let hint = "away: keys are sent on ⏎ or after a pause — ten sends a minute"

    static func flushesAtOnce<C: Collection>(_ bytes: C) -> Bool where C.Element == UInt8 {
        bytes.contains { $0 < 0x20 || $0 == 0x7F }
    }
}

