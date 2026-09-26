import Foundation

/// The phone's live line to the socket relay — the away link's fast lane
/// beside the mailbox (`docs/relay-socket-contract.md`;
/// `docs/phone-contract.md`, *The phone goes relay-first*).
///
/// A `URLSessionWebSocketTask` held open while `wanted` — the app on screen
/// and the phone knowing it is away — with a receive loop, a ping every
/// `pingSeconds`, and a reconnect ladder (1, 2, 4, 8, 16, then `maxBackoff`)
/// that resets only after the line has been open `stableSeconds`. Every
/// failure closes the line and climbs the ladder; `wanted = false` closes it
/// and forgets the ladder.
///
/// **Nothing here seals or opens a frame or holds a key.** The socket carries
/// text it never reads; `RelayChannel` seals, opens and counts on both sides
/// of it. This is also the **only** phone file that builds a socket address,
/// and it builds `wss://` alone — a base with any other scheme is refused
/// with nil and no line is opened.
@MainActor
final class RelaySocket {
    enum State: Equatable {
        case off, connecting, open

        /// The word the profile draws; empty for a line nobody wants.
        var word: String {
            switch self {
            case .off: return ""
            case .connecting: return "connecting"
            case .open: return "open"
            }
        }
    }

    enum SocketError: Error {
        case closed
    }

    static let pingSeconds: TimeInterval = 20
    static let firstBackoff: TimeInterval = 1
    static let maxBackoff: TimeInterval = 30
    static let stableSeconds: TimeInterval = 30

    let base: String
    let channel: String

    private(set) var state: State = .off {
        didSet { if state != oldValue { onState?(state) } }
    }
    var isOpen: Bool { state == .open }
    /// Whether the Mac is on the other side of the line right now — the
    /// relay's own `peer:1` / `peer:0` words (plaintext, never sealed),
    /// read here and never handed to `onText`. False on open, reset on
    /// close: a request down a line with no peer would only sit out its
    /// deadline, so the channel takes the socket lane only while this is
    /// true.
    private(set) var peerPresent = false {
        didSet { if peerPresent != oldValue { onPeer?(peerPresent) } }
    }
    /// Every text frame the line carries, in arrival order, on the main
    /// actor — the relay's `peer:` words excepted.
    var onText: ((String) -> Void)?
    /// Every change of `peerPresent`, on the main actor.
    var onPeer: ((Bool) -> Void)?
    /// Every transition, on the main actor.
    var onState: ((State) -> Void)?
    /// Whether the line should be up. Setting it connects or closes.
    var wanted = false {
        didSet {
            guard wanted != oldValue else { return }
            if wanted { connect() } else { close() }
        }
    }

    private var task: URLSessionWebSocketTask?
    /// Stepped on every connect and teardown so a late callback from a
    /// dead line is ignored.
    private var generation = 0
    private var backoff: TimeInterval = RelaySocket.firstBackoff
    private var openedAt: Date?
    /// Whether this line has read the relay's `peer:` word yet.
    private var heardPeer = false
    private var reconnect: Task<Void, Never>?
    private var pinger: Task<Void, Never>?

    init(base: String, channel: String) {
        self.base = base
        self.channel = channel
    }

    /// The one socket address this app builds: `wss://<host>[/path]/ws?ch=…&side=phone`.
    /// Any other scheme, or a base with no host, is nil.
    static func url(base: String, channel: String) -> URL? {
        let trimmed = base.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              components.scheme == "wss",
              let host = components.host, !host.isEmpty else { return nil }
        var path = components.path
        while path.hasSuffix("/") { path.removeLast() }
        components.path = path.hasSuffix("/ws") ? path : path + "/ws"
        components.queryItems = [
            URLQueryItem(name: "ch", value: channel),
            URLQueryItem(name: "side", value: "phone"),
        ]
        return components.url
    }

    /// The relay's control word, or nil for anything else: `peer:1` is the
    /// other side arriving (true), `peer:0` leaving (false). Whitespace
    /// around the word is tolerated; any other `peer:` text is neither.
    static func peerWord(_ text: String) -> Bool? {
        switch text.trimmingCharacters(in: .whitespacesAndNewlines) {
        case "peer:1": return true
        case "peer:0": return false
        default: return nil
        }
    }

    /// How long a request made while the line is still coming up waits for
    /// it (open **and** the Mac on it) before going the mailbox way. A wake
    /// opens the socket and fires its first request in the same breath; the
    /// socket answers in tens of milliseconds once up, the mailbox in
    /// seconds, so a short wait here is the faster road.
    static let readyGrace: TimeInterval = 1.5
    /// A line that opened without the Mac's `peer:1` is given this long for
    /// the word to arrive: the relay sends it to an arriving side straight
    /// after the upgrade when the Mac is already on.
    static let peerGrace: TimeInterval = 0.4

    /// Whether the line is worth waiting for right now: a connect actually
    /// in flight, or just opened with no `peer:` word read yet. The reconnect
    /// ladder's sleep is `.connecting` too but holds no task — nothing is
    /// coming, so nothing waits on it; and a `peer:0` read on arrival ends
    /// the grace at once (bug audit, 25 Sep 2026).
    var comingUp: Bool {
        guard wanted else { return false }
        if state == .connecting { return task != nil }
        if state == .open, !peerPresent, !heardPeer, let openedAt,
           Date().timeIntervalSince(openedAt) < RelaySocket.peerGrace { return true }
        return false
    }

    /// Wait, up to `within`, for the line to be open with the Mac on it.
    /// True the moment it is; false once it cannot be (closed, off, open
    /// with nobody there) or the time is up.
    func ready(within: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(within)
        while true {
            if isOpen && peerPresent { return true }
            guard comingUp, Date() < deadline else { return false }
            try? await Task.sleep(nanoseconds: 25_000_000)
            if Task.isCancelled { return false }
        }
    }

    /// The wait after `current`: doubled, capped at `maxBackoff`.
    static func nextBackoff(after current: TimeInterval) -> TimeInterval {
        min(maxBackoff, max(firstBackoff, current) * 2)
    }

    /// The ladder's first `steps` waits, for the table: 1, 2, 4, 8, 16, 30, 30, …
    static func ladder(steps: Int) -> [TimeInterval] {
        var out: [TimeInterval] = []
        var wait = firstBackoff
        for _ in 0..<max(0, steps) {
            out.append(wait)
            wait = nextBackoff(after: wait)
        }
        return out
    }

    /// One text frame down the line. Throws when the line is not open.
    func send(_ text: String) async throws {
        guard let task, state == .open else { throw SocketError.closed }
        try await task.send(.string(text))
    }

    // MARK: - The line

    private func connect() {
        reconnect?.cancel()
        reconnect = nil
        guard wanted, task == nil else { return }
        guard let url = Self.url(base: base, channel: channel) else {
            // A base that is not `wss://` never opens: off, and it stays off.
            state = .off
            return
        }
        generation += 1
        let gen = generation
        state = .connecting
        openedAt = nil
        let socket = URLSession.shared.webSocketTask(with: url)
        task = socket
        socket.resume()
        receiveLoop(socket, generation: gen)
        socket.sendPing { [weak self] error in
            Task { @MainActor in
                guard let self, gen == self.generation else { return }
                if error == nil { self.markOpen(gen) } else { self.failed(gen) }
            }
        }
    }

    private func receiveLoop(_ socket: URLSessionWebSocketTask, generation gen: Int) {
        socket.receive { [weak self] result in
            Task { @MainActor in
                guard let self, gen == self.generation else { return }
                switch result {
                case .success(let message):
                    self.markOpen(gen)
                    if case .string(let text) = message {
                        if let present = Self.peerWord(text) {
                            self.heardPeer = true
                            self.peerPresent = present
                        } else if !text.hasPrefix("peer:") {
                            self.onText?(text)
                        }
                    }
                    self.receiveLoop(socket, generation: gen)
                case .failure:
                    self.failed(gen)
                }
            }
        }
    }

    private func markOpen(_ gen: Int) {
        guard gen == generation, state != .open else { return }
        openedAt = Date()
        state = .open
        startPinger(gen)
    }

    private func startPinger(_ gen: Int) {
        pinger?.cancel()
        pinger = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.pingSeconds))
                guard !Task.isCancelled, let self, gen == self.generation,
                      let task = self.task else { return }
                task.sendPing { [weak self] error in
                    guard error != nil else { return }
                    Task { @MainActor in self?.failed(gen) }
                }
            }
        }
    }

    /// A failure on the live line: closed, and reopened on the ladder while
    /// still wanted. A line that was up `stableSeconds` starts the ladder
    /// over; one that dropped sooner climbs it.
    private func failed(_ gen: Int) {
        guard gen == generation else { return }
        let stable = openedAt.map {
            Date().timeIntervalSince($0) >= Self.stableSeconds
        } ?? false
        teardown()
        if stable { backoff = Self.firstBackoff }
        guard wanted else {
            state = .off
            return
        }
        state = .connecting
        let wait = backoff
        backoff = Self.nextBackoff(after: backoff)
        reconnect = Task { [weak self] in
            try? await Task.sleep(for: .seconds(wait))
            guard !Task.isCancelled else { return }
            self?.connect()
        }
    }

    private func close() {
        reconnect?.cancel()
        reconnect = nil
        teardown()
        backoff = Self.firstBackoff
        state = .off
    }

    private func teardown() {
        generation += 1
        pinger?.cancel()
        pinger = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        openedAt = nil
        heardPeer = false
        peerPresent = false
    }
}
