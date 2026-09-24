import Foundation
import Network

/// The pane's one connection to the daemon: keys up, screen bytes down, in
/// order, on a single loopback socket. The daemon half is
/// `terminal_stream.py`; the frame is `kind (1 byte) + length (4 bytes,
/// big-endian) + payload` both ways. Down: `D` bytes, `X` exited, `E` a
/// refusal in words. Up: `I` keystrokes, `S` size (`cols rows`), `P` ping.
///
/// Why a socket and not requests: a keystroke per HTTP request travelled
/// on its own TCP connection, so two typed quickly could land in either
/// order, and the 50 ms JSON poll that read the screen back could lose a
/// chunk when two polls landed in one run-loop pass. One ordered stream
/// has neither failure.
///
/// Everything from the marker line below through the closing brace of
/// `TerminalStreamHead` is byte-identical to `ios/BobPhone/TerminalStream.swift`
/// (`host/tests/test_phone_terminal_stream_drift.py`); edit both sides.
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

/// One live stream. Every callback fires on the connection's own queue;
/// the caller hops to the main thread where it needs to.
final class TerminalStreamConnection {
    static let contentType = "application/x-bob-terminal"

    var onOpen: (() -> Void)?
    var onData: ((Data) -> Void)?
    var onExit: (() -> Void)?
    var onRefused: ((Int, String) -> Void)?
    var onClosed: (() -> Void)?

    private let host: String
    private let port: Int
    private let token: String
    private let session: String
    private let queue = DispatchQueue(label: "bob.terminal-stream")
    private var connection: NWConnection?
    private var buffer = Data()
    private var headDone = false
    private var refusedStatus = 0
    private var refusedLength = 0
    private var parser = TerminalFrameCodec.Parser()
    private var finished = false

    init(host: String, port: Int, token: String, session: String) {
        self.host = host
        self.port = port
        self.token = token
        self.session = session
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
            self.connection?.cancel()
            self.connection = nil
        }
    }

    /// Keystrokes, raw. Dropped while the stream is not open — the pane
    /// says so in its note; nothing is ever re-sent out of order.
    func send(input: Data) {
        guard !input.isEmpty else { return }
        write(TerminalFrameCodec.encode(kind: UInt8(ascii: "I"), payload: input))
    }

    func send(cols: Int, rows: Int) {
        let payload = Data("\(max(cols, 1)) \(max(rows, 1))".utf8)
        write(TerminalFrameCodec.encode(kind: UInt8(ascii: "S"), payload: payload))
    }

    private func write(_ data: Data) {
        queue.async { [weak self] in
            guard let self, self.headDone, let conn = self.connection else { return }
            conn.send(content: data, completion: .contentProcessed { _ in })
        }
    }

    private func sendRequest() {
        let encoded = session.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        let request = "GET /api/terminal/stream?session=\(encoded) HTTP/1.1\r\n"
            + "Host: \(host):\(port)\r\n"
            + "X-Bob-Token: \(token)\r\n"
            + "Connection: keep-alive\r\n\r\n"
        connection?.send(content: Data(request.utf8), completion: .contentProcessed { _ in })
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
            if head.status != 200 || head.contentType != Self.contentType {
                refusedStatus = head.status == 200 ? 0 : head.status
                refusedLength = head.contentLength
                headDone = true
                deliverRefusalIfComplete()
                return
            }
            headDone = true
            onOpen?()
        }
        if refusedStatus != 0 || refusedLength != 0 {
            deliverRefusalIfComplete()
            return
        }
        guard let frames = try? parser.feed(buffer) else {
            finish()
            return
        }
        buffer.removeAll(keepingCapacity: true)
        for frame in frames {
            switch frame.kind {
            case UInt8(ascii: "D"):
                onData?(frame.payload)
            case UInt8(ascii: "X"):
                onExit?()
            case UInt8(ascii: "E"):
                onRefused?(0, String(data: frame.payload, encoding: .utf8) ?? "")
            default:
                break
            }
        }
    }

    private func deliverRefusalIfComplete() {
        guard buffer.count >= refusedLength else { return }
        var detail = ""
        if let obj = try? JSONSerialization.jsonObject(with: buffer.prefix(refusedLength)) as? [String: Any] {
            detail = (obj["detail"] as? String) ?? (obj["error"] as? String) ?? ""
        }
        let status = refusedStatus
        refusedStatus = 0
        refusedLength = 0
        onRefused?(status, detail.isEmpty ? "Dark Army refused the terminal (HTTP \(status))." : detail)
        finish()
    }

    private func finish() {
        guard !finished else { return }
        finished = true
        connection?.cancel()
        connection = nil
        onClosed?()
    }
}
