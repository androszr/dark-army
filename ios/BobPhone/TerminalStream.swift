import Foundation
import Network

/// The phone's copy of the panel's terminal stream: the same frames
/// (`kind (1 byte) + length (4 bytes, big-endian) + payload`, `D`/`X`/`E`
/// down, `I`/`S`/`P` up), the same hand-parsed HTTP head, and one thing the
/// panel does not need — every frame **sealed**, because this socket
/// crosses the Wi-Fi. The Mac half is `terminal_stream.py`'s `SealedCodec`.
///
/// Everything from the marker line below through the closing brace of
/// `TerminalStreamHead` is byte-identical to the panel's
/// `TerminalStream.swift` (`host/tests/test_phone_terminal_stream_drift.py`);
/// edit both sides. `SealedTerminalStream` beneath is the phone's own.
// MARK: - shared with the panel (byte-pinned)
enum TerminalFrameCodec {
    static let maxFrame = 4 * 1024 * 1024
    static let headSize = 5

    static func encode(kind: UInt8, payload: Data) -> Data {
        var out = Data(capacity: headSize + payload.count)
        out.append(kind)
        let n = UInt32(payload.count).bigEndian
        withUnsafeBytes(of: n) { out.append(contentsOf: $0) }
        out.append(payload)
        return out
    }

    struct Parser {
        private var buffer = Data()

        /// Whole frames out of whatever arrived; a frame above `maxFrame`
        /// throws — the peer is not speaking this protocol.
        mutating func feed(_ data: Data) throws -> [(kind: UInt8, payload: Data)] {
            buffer.append(data)
            var out: [(UInt8, Data)] = []
            while buffer.count >= TerminalFrameCodec.headSize {
                let kind = buffer[buffer.startIndex]
                let lenStart = buffer.startIndex + 1
                var length: UInt32 = 0
                for i in 0..<4 {
                    length = (length << 8) | UInt32(buffer[lenStart + i])
                }
                if Int(length) > TerminalFrameCodec.maxFrame {
                    throw StreamError.frameTooLarge
                }
                let total = TerminalFrameCodec.headSize + Int(length)
                if buffer.count < total { break }
                let payloadStart = buffer.startIndex + TerminalFrameCodec.headSize
                let payload = Data(buffer[payloadStart..<(buffer.startIndex + total)])
                buffer.removeSubrange(buffer.startIndex..<(buffer.startIndex + total))
                out.append((kind, payload))
            }
            return out
        }
    }

    enum StreamError: Error { case frameTooLarge }
}

/// The HTTP head that precedes the stream. Parsed by hand: `URLSession`
/// cannot hand back a socket to write keys into.
enum TerminalStreamHead {
    struct Parsed {
        var status: Int
        var contentType: String
        var contentLength: Int
        /// Where the body begins in the buffer that was parsed.
        var end: Int
    }

    static func parse(_ buffer: Data) -> Parsed? {
        guard let range = buffer.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let headData = buffer[buffer.startIndex..<range.lowerBound]
        guard let head = String(data: headData, encoding: .isoLatin1) else { return nil }
        let lines = head.components(separatedBy: "\r\n")
        let parts = lines.first?.split(separator: " ") ?? []
        let status = parts.count >= 2 ? Int(parts[1]) ?? 0 : 0
        var contentType = ""
        var contentLength = 0
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[line.startIndex..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            if name == "content-type" { contentType = value }
            if name == "content-length" { contentLength = Int(value) ?? 0 }
        }
        return Parsed(status: status, contentType: contentType,
                      contentLength: contentLength,
                      end: range.upperBound - buffer.startIndex)
    }
}

/// One held-open, sealed terminal stream to the Mac's phone door — the
/// at-home, foreground route. iOS suspends the socket in the background, so
/// the coordinator cancels it on `.background` and opens a fresh one on
/// `.active`; away from home the pane is fed by the poll instead.
///
/// The request is a hand-written `POST /api/terminal/stream` carrying the
/// channel id and one sealed `terminal_stream` frame as the body (sealed by
/// `HomeChannel.openTerminalStream`, which owns the counter). Three heads
/// can come back: a non-200 with a JSON body is the door refusing plainly
/// (403 means "not paired here"); a `200 text/plain` is a sealed `err` —
/// opened here, a `ctr_expected` inside is handed back so the channel can
/// fast-forward and the coordinator reopen once; a `200` with
/// `contentType` starts the framed loop. Every frame after the head is a
/// `Z` frame: down, opened with the blob id `<frame id>|m2p|<n>`; up, sealed
/// under `<frame id>|p2m|<n>`, `n` counting from 0 in each direction. A
/// frame that does not open closes the connection — a replay, a reorder or
/// a tampered byte is not this Mac. Every callback fires on the connection's
/// own queue; the caller hops to main.
final class SealedTerminalStream {
    static let contentType = "application/x-bob-terminal-sealed"
    /// How often the last size is restated while the stream is open — the
    /// keepalive on a socket nothing else times out
    /// (`terminal_stream.STREAM_HEARTBEAT_SECONDS`).
    static let heartbeatSeconds: TimeInterval = 5

    var onOpen: (() -> Void)?
    var onData: ((Data) -> Void)?
    var onExit: (() -> Void)?
    /// `(status, detail, final)`. **`final` is the whole of the 409
    /// question.** The phone door answers 409 for three different
    /// things: a stale counter, a clock too far out, and the attach
    /// itself refusing. The first two are routine on open — a poll
    /// runs every four seconds and moves the counter under the
    /// stream — and they self-heal on the next try; only the third
    /// means there is nothing here to attach to. The door tells them
    /// apart by naming the stream in `re`: the attach refusal quotes
    /// the stream id, the two transient ones carry `""`.
    var onRefused: ((Int, String, Bool) -> Void)?
    var onClosed: (() -> Void)?
    /// Keys this stream could not send and did not keep — the head never
    /// landed and the hold filled, or the stream ended holding them. Fired
    /// once per losing write; the pane says so, because a key that vanishes
    /// silently is the one bug a person cannot report.
    var onInputDropped: (() -> Void)?
    /// The Mac said the opening frame's counter was stale, and what it
    /// expects next. Fired before `onRefused`.
    var onCounterExpected: ((Int) -> Void)?

    private let host: String
    private let port: Int
    private let channelId: String
    private let key: Data
    private let openingWire: String
    private let frameId: String
    private let queue = DispatchQueue(label: "bob.phone.terminal-stream")
    private var connection: NWConnection?
    private var buffer = Data()
    private var headDone = false
    private var refusedStatus = 0
    private var refusedLength = 0
    private var refusedSealed = false
    private var parser = TerminalFrameCodec.Parser()
    private var finished = false
    private var downCount = 0
    private var upCount = 0
    private var lastSize: (cols: Int, rows: Int)?
    private var heartbeat: DispatchSourceTimer?
    /// Keys typed between `start()` and the head landing. Nothing can be
    /// sealed before the head — the stream id's counters start with it — so
    /// they wait here, in order, and go as one `I` the moment it does.
    private var pendingInput = Data()
    /// How much typing is held that way. A person can outrun a connect by a
    /// few keys, not by a paste.
    static let maxPendingInput = 4096

    init(host: String, port: Int, channelId: String, key: Data,
         openingWire: String, frameId: String) {
        self.host = host
        self.port = port
        self.channelId = channelId
        self.key = key
        self.openingWire = openingWire
        self.frameId = frameId
    }

    func start() {
        guard let nwPort = NWEndpoint.Port(rawValue: UInt16(clamping: port)) else { return }
        let conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: .tcp)
        connection = conn
        conn.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.sendRequest()
                self.receiveLoop()
            case .failed, .cancelled:
                self.finish()
            default:
                break
            }
        }
        conn.start(queue: queue)
    }

    func cancel() {
        queue.async { [weak self] in
            guard let self else { return }
            self.finished = true
            self.stopHeartbeat()
            self.connection?.cancel()
            self.connection = nil
        }
    }

    /// Keystrokes, raw and in order. Before the head lands they are held
    /// (`pendingInput`) and sent as one frame the moment it does; a stream
    /// that has finished, or a hold that is full, reports the loss through
    /// `onInputDropped` rather than returning as though the key had gone.
    func send(input: Data) {
        guard !input.isEmpty else { return }
        queue.async { [weak self] in
            guard let self else { return }
            guard !self.finished else {
                self.onInputDropped?()
                return
            }
            guard self.headDone else {
                guard self.pendingInput.count + input.count <= Self.maxPendingInput else {
                    self.onInputDropped?()
                    return
                }
                self.pendingInput.append(input)
                return
            }
            self.writeFrame(kind: UInt8(ascii: "I"), payload: input)
        }
    }

    func send(cols: Int, rows: Int) {
        let size = (cols: max(cols, 1), rows: max(rows, 1))
        queue.async { [weak self] in self?.lastSize = size }
        sealedWrite(kind: UInt8(ascii: "S"), payload: Data("\(size.cols) \(size.rows)".utf8))
    }

    /// One up frame: `kind + payload` sealed under this stream's id and the
    /// next up position, inside a `Z` frame. Sealed and counted on the
    /// queue, so two keys typed quickly are numbered in the order sent.
    private func sealedWrite(kind: UInt8, payload: Data) {
        queue.async { [weak self] in
            guard let self, self.headDone, !self.finished else { return }
            self.writeFrame(kind: kind, payload: payload)
        }
    }

    /// The write itself, on the queue, with the head already landed.
    private func writeFrame(kind: UInt8, payload: Data) {
        guard !finished, let conn = connection else { return }
        let n = upCount
        upCount += 1
        var plain = Data(capacity: payload.count + 1)
        plain.append(kind)
        plain.append(payload)
        guard let blob = RelayTransport.sealBlob(
            key: key, direction: .phoneToMac,
            frameId: "\(frameId)|p2m|\(n)", data: plain, ns: .home) else { return }
        conn.send(content: TerminalFrameCodec.encode(kind: UInt8(ascii: "Z"), payload: blob),
                  completion: .contentProcessed { _ in })
    }

    private func sendRequest() {
        let body = Data(openingWire.utf8)
        let request = "POST /api/terminal/stream HTTP/1.1\r\n"
            + "Host: \(host):\(port)\r\n"
            + "X-Bob-Channel: \(channelId)\r\n"
            + "Content-Type: text/plain\r\n"
            + "Content-Length: \(body.count)\r\n"
            + "Connection: keep-alive\r\n\r\n"
        connection?.send(content: Data(request.utf8) + body, completion: .contentProcessed { _ in })
    }

    private func startHeartbeat() {
        stopHeartbeat()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.heartbeatSeconds, repeating: Self.heartbeatSeconds)
        timer.setEventHandler { [weak self] in
            guard let self, self.headDone, !self.finished else { return }
            if let size = self.lastSize {
                self.sealedWrite(kind: UInt8(ascii: "S"), payload: Data("\(size.cols) \(size.rows)".utf8))
            } else {
                self.sealedWrite(kind: UInt8(ascii: "P"), payload: Data())
            }
        }
        timer.resume()
        heartbeat = timer
    }

    private func stopHeartbeat() {
        heartbeat?.cancel()
        heartbeat = nil
    }

    private func receiveLoop() {
        connection?.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) {
            [weak self] data, _, isComplete, error in
            guard let self else { return }
            if let data, !data.isEmpty { self.ingest(data) }
            if isComplete || error != nil {
                self.finish()
                return
            }
            if !self.finished { self.receiveLoop() }
        }
    }

    private func ingest(_ data: Data) {
        buffer.append(data)
        if !headDone {
            guard let head = TerminalStreamHead.parse(buffer) else { return }
            buffer.removeSubrange(buffer.startIndex..<(buffer.startIndex + head.end))
            if head.status != 200 {
                // The door refused plainly: a JSON body in its words.
                refusedStatus = head.status
                refusedLength = head.contentLength
                headDone = true
                deliverRefusalIfComplete()
                return
            }
            if head.contentType != Self.contentType {
                // A sealed `err`: the status is inside.
                refusedSealed = true
                refusedLength = head.contentLength
                headDone = true
                deliverRefusalIfComplete()
                return
            }
            headDone = true
            flushPendingInput()
            startHeartbeat()
            onOpen?()
        }
        if refusedStatus != 0 || refusedSealed {
            deliverRefusalIfComplete()
            return
        }
        guard let frames = try? parser.feed(buffer) else {
            finish()
            return
        }
        buffer.removeAll(keepingCapacity: true)
        for frame in frames {
            guard frame.kind == UInt8(ascii: "Z"),
                  let plain = RelayTransport.openBlob(
                    key: key, direction: .macToPhone,
                    frameId: "\(frameId)|m2p|\(downCount)", raw: frame.payload, ns: .home),
                  let kind = plain.first else {
                // Not sealed to this stream at this position: not this Mac.
                finish()
                return
            }
            downCount += 1
            let payload = plain.dropFirst()
            switch kind {
            case UInt8(ascii: "D"):
                onData?(Data(payload))
            case UInt8(ascii: "X"):
                onExit?()
            case UInt8(ascii: "E"):
                onRefused?(0, String(data: payload, encoding: .utf8) ?? "", true)
            default:
                break
            }
        }
    }

    private func deliverRefusalIfComplete() {
        guard buffer.count >= refusedLength else { return }
        let body = buffer.prefix(refusedLength)
        var status = refusedStatus
        var detail = ""
        // A plain refusal is the door turning the request away before it
        // ever named a stream (426, 403, 404): nothing transient about it.
        var isFinal = true
        if refusedSealed {
            let wire = String(data: body, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let (frame, _) = RelayTransport.open(key: key, direction: .macToPhone,
                                                 wire: wire, lastCtr: 0, ns: .home)
            let payload = frame?["body"] as? [String: Any] ?? [:]
            status = payload["status"] as? Int ?? 0
            detail = payload["error"] as? String ?? ""
            isFinal = (payload["re"] as? String ?? "") == frameId
            if let expected = payload["ctr_expected"] as? Int {
                onCounterExpected?(expected)
            }
        } else if let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
            detail = (obj["detail"] as? String) ?? (obj["error"] as? String) ?? ""
        }
        refusedStatus = 0
        refusedLength = 0
        refusedSealed = false
        onRefused?(status,
                   detail.isEmpty
                   ? "Dark Army refused the terminal (HTTP \(status))." : detail,
                   isFinal)
        finish()
    }

    /// Everything typed before the head landed, as one `I`, in order.
    private func flushPendingInput() {
        guard !pendingInput.isEmpty else { return }
        let held = pendingInput
        pendingInput = Data()
        writeFrame(kind: UInt8(ascii: "I"), payload: held)
    }

    private func finish() {
        guard !finished else { return }
        finished = true
        if !pendingInput.isEmpty {
            pendingInput = Data()
            onInputDropped?()
        }
        stopHeartbeat()
        connection?.cancel()
        connection = nil
        onClosed?()
    }
}
