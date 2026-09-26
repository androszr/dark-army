import Compression
import CryptoKit
import Foundation

/// The phone's half of the sealed-envelope protocol — the byte-for-byte
/// mirror of the Mac's `relay.py`, pinned by `test_phone_remote.py`.
///
/// Frames are ChaCha20-Poly1305 over raw-DEFLATE JSON; the AAD binds
/// version, channel and direction; a per-direction monotonic counter refuses
/// replays; the wire is `base64(nonce ‖ ciphertext‖tag)` — CryptoKit's own
/// combined-box layout. The relay never holds anything this file did not
/// seal first, and only `https` addresses are ever built here.
enum RelayDirection: String {
    case phoneToMac = "p2m"
    case macToPhone = "m2p"
}

/// The four byte strings one sealed path is derived and bound with —
/// `relay.Namespace` on the Mac. Two exist and they must never be merged:
/// a home frame sealed under the relay's strings would open on the mailbox
/// path, and the other way round. `.relay` is built from the four pinned
/// literals below; `.home` is the home door's own family.
struct SealNamespace {
    let infoChannelId: Data
    let infoPhoneToMac: Data
    let infoMacToPhone: Data
    let aadPrefix: Data

    static let relay = SealNamespace(
        infoChannelId: RelayTransport.infoChannelId,
        infoPhoneToMac: RelayTransport.infoPhoneToMac,
        infoMacToPhone: RelayTransport.infoMacToPhone,
        aadPrefix: RelayTransport.aadPrefix)

    static let home = SealNamespace(
        infoChannelId: Data("bob-home v1 channel-id".utf8),
        infoPhoneToMac: Data("bob-home v1 p2m".utf8),
        infoMacToPhone: Data("bob-home v1 m2p".utf8),
        aadPrefix: Data("bob-home v1|".utf8))
}

enum RelayTransport {
    /// The cross-platform contract. `relay.py` carries the same bytes.
    static let infoChannelId = Data("bob-relay v1 channel-id".utf8)
    static let infoPhoneToMac = Data("bob-relay v1 p2m".utf8)
    static let infoMacToPhone = Data("bob-relay v1 m2p".utf8)
    static let aadPrefix = Data("bob-relay v1|".utf8)

    static let skewSeconds: TimeInterval = 300
    static let frameMaxBytes = 900_000

    /// HKDF-SHA256 with an **empty salt** — the RFC 5869 default, matching
    /// `cryptography`'s `salt=None` on the Mac.
    private static func derive(_ key: Data, info: Data) -> SymmetricKey {
        HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: key),
            salt: Data(),
            info: info,
            outputByteCount: 32)
    }

    /// The mailbox address for this key: 16 derived bytes as 32 hex chars.
    /// Deliberately not a credential — it buys ciphertext and DoS only.
    static func channelId(key: Data, ns: SealNamespace = .relay) -> String {
        let derived = derive(key, info: ns.infoChannelId)
            .withUnsafeBytes { Data($0) }
        return derived.prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    static func directionKey(key: Data, direction: RelayDirection,
                             ns: SealNamespace = .relay) -> SymmetricKey {
        derive(key, info: direction == .phoneToMac
            ? ns.infoPhoneToMac : ns.infoMacToPhone)
    }

    private static func aad(key: Data, direction: RelayDirection,
                            ns: SealNamespace) -> Data {
        ns.aadPrefix + Data(channelId(key: key, ns: ns).utf8) + Data("|".utf8)
            + Data(direction.rawValue.utf8)
    }

    /// The blob AAD: the frame AAD plus `|blob|<frame id>`, so a sealed
    /// upload body is bound to exactly one header frame.
    private static func blobAAD(key: Data, direction: RelayDirection,
                                frameId: String, ns: SealNamespace) -> Data {
        aad(key: key, direction: direction, ns: ns) + Data("|blob|".utf8)
            + Data(frameId.utf8)
    }

    // MARK: - Seal / open

    static func seal(key: Data, direction: RelayDirection, ctr: Int,
                     kind: String, body: [String: Any],
                     frameId: String = UUID().uuidString,
                     ns: SealNamespace = .relay) -> String? {
        let frame: [String: Any] = [
            "v": 1,
            "ctr": ctr,
            "ts": Date().timeIntervalSince1970,
            "id": frameId,
            "kind": kind,
            "body": body,
        ]
        guard let json = try? JSONSerialization.data(withJSONObject: frame),
              let squeezed = deflate(json) else { return nil }
        guard let box = try? ChaChaPoly.seal(
            squeezed,
            using: directionKey(key: key, direction: direction, ns: ns),
            authenticating: aad(key: key, direction: direction, ns: ns)) else {
            return nil
        }
        return box.combined.base64EncodedString()
    }

    /// A raw sealed body — `nonce ‖ ciphertext ‖ tag`, no base64 and no
    /// deflate — for the one route whose payload is a file rather than
    /// JSON. `relay.seal_blob` on the Mac; the AAD carries the header
    /// frame's id, so two uploads in flight cannot swap bodies.
    static func sealBlob(key: Data, direction: RelayDirection, frameId: String,
                         data: Data, ns: SealNamespace = .home) -> Data? {
        guard let box = try? ChaChaPoly.seal(
            data,
            using: directionKey(key: key, direction: direction, ns: ns),
            authenticating: blobAAD(key: key, direction: direction,
                                    frameId: frameId, ns: ns)) else {
            return nil
        }
        return box.combined
    }

    /// The plaintext of a `sealBlob` body, or nil on any refusal — wrong
    /// key, wrong direction, wrong frame id, or bytes that are not a blob at
    /// all. `relay.open_blob`'s mirror; there is no counter here, the id
    /// carries the position (the terminal stream binds `stream|dir|n`).
    static func openBlob(key: Data, direction: RelayDirection, frameId: String,
                         raw: Data, ns: SealNamespace = .home) -> Data? {
        guard raw.count >= 28,
              let box = try? ChaChaPoly.SealedBox(combined: raw),
              let plain = try? ChaChaPoly.open(
                box,
                using: directionKey(key: key, direction: direction, ns: ns),
                authenticating: blobAAD(key: key, direction: direction,
                                        frameId: frameId, ns: ns)) else {
            return nil
        }
        return plain
    }

    /// `(frame, refusal)` — the frame dict and `""`, or nil and a code. The
    /// codes match `relay.py`'s: oversize, seal, shape, ctr, ts.
    static func open(key: Data, direction: RelayDirection, wire: String,
                     lastCtr: Int,
                     ns: SealNamespace = .relay) -> ([String: Any]?, String) {
        guard wire.count <= frameMaxBytes else { return (nil, "oversize") }
        guard let raw = Data(base64Encoded: wire), raw.count >= 28 else {
            return (nil, "seal")
        }
        guard let box = try? ChaChaPoly.SealedBox(combined: raw),
              let squeezed = try? ChaChaPoly.open(
                box,
                using: directionKey(key: key, direction: direction, ns: ns),
                authenticating: aad(key: key, direction: direction, ns: ns)) else {
            return (nil, "seal")
        }
        guard let json = inflate(squeezed, cap: frameMaxBytes) else {
            return (nil, "oversize")
        }
        guard let frame = (try? JSONSerialization.jsonObject(with: json))
                as? [String: Any],
              (frame["v"] as? Int) == 1,
              let ctr = frame["ctr"] as? Int,
              let ts = (frame["ts"] as? Double)
                ?? (frame["ts"] as? NSNumber)?.doubleValue else {
            return (nil, "shape")
        }
        guard ctr > lastCtr else { return (nil, "ctr") }
        guard abs(ts - Date().timeIntervalSince1970) <= skewSeconds else {
            return (nil, "ts")
        }
        return (frame, "")
    }

    // MARK: - Raw DEFLATE

    /// `COMPRESSION_ZLIB` is raw DEFLATE despite its name — no zlib header,
    /// no adler32 — which is exactly the dialect the Mac's `relay.py` speaks
    /// (`wbits=-15`). Do not "fix" either side alone.
    static func deflate(_ data: Data) -> Data? {
        code(data, cap: data.count + 1024, encode: true)
    }

    static func inflate(_ data: Data, cap: Int) -> Data? {
        code(data, cap: cap, encode: false)
    }

    private static func code(_ data: Data, cap: Int, encode: Bool) -> Data? {
        guard !data.isEmpty else { return encode ? Data() : nil }
        let destination = UnsafeMutablePointer<UInt8>.allocate(capacity: cap)
        defer { destination.deallocate() }
        let written = data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Int in
            guard let source = raw.bindMemory(to: UInt8.self).baseAddress else {
                return 0
            }
            if encode {
                return compression_encode_buffer(
                    destination, cap, source, data.count, nil, COMPRESSION_ZLIB)
            }
            return compression_decode_buffer(
                destination, cap, source, data.count, nil, COMPRESSION_ZLIB)
        }
        guard written > 0 else { return nil }
        // A decode that filled the whole cap may have been truncated — the
        // bounded-inflate refusal, matching the Mac's.
        if !encode && written >= cap { return nil }
        return Data(bytes: destination, count: written)
    }

    // MARK: - The mailbox

    /// Builds only `https` box URLs. A base with any other scheme is refused
    /// with nil — there is no plaintext remote path to fall back to.
    static func boxURL(base: String, channel: String, direction: String,
                       wait: Bool) -> URL? {
        let trimmed = base.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              components.scheme == "https" else { return nil }
        components.path = components.path.hasSuffix("/api/box")
            ? components.path : components.path + "/api/box"
        components.queryItems = [
            URLQueryItem(name: "ch", value: channel),
            URLQueryItem(name: "dir", value: direction),
        ] + (wait ? [URLQueryItem(name: "wait", value: "1")] : [])
        return components.url
    }
}

/// One device's live channel: the key, the address, and both counters.
/// `PhoneClient` owns exactly one and persists the counters back into the
/// Keychain record (throttled there, not here).
@MainActor
final class RelayChannel {
    let key: Data
    let base: String
    private(set) var sendCtr: Int
    private(set) var recvCtr: Int
    /// Told whenever a counter moves, so the owner can persist. Throttling
    /// is the owner's job — at most once a minute, and on background.
    var onCounters: ((Int, Int) -> Void)?
    /// Told once per request, after the mailbox has accepted the sealed
    /// frame and before the answer poll begins — the moment "asking the
    /// relay" becomes "waiting for the Mac". The owner words it.
    var onSent: (() -> Void)?
    /// Told once per completed request — a `reply` or an `err` frame taken
    /// as this request's answer — with the trip's legs (`LinkTiming.swift`).
    /// A `ctr_expected` retry reports once, for the retried trip, timed
    /// from the first POST; a cancelled or never-answered request reports
    /// nothing. Fired beside `onCounters`; the owner records it.
    var onTiming: ((LinkTimingSample) -> Void)?
    /// The socket lane (`RelaySocket.swift`), attached by `PhoneClient`
    /// when the pairing record carries a socket address and nil otherwise.
    /// Every frame it carries is opened here, under the same key and the
    /// same counters as the mailbox's; the socket itself reads nothing.
    var socket: RelaySocket? {
        didSet {
            oldValue?.onText = nil
            oldValue?.onState = nil
            oldValue?.wanted = false
            oldValue?.onPeer = nil
            socket?.onText = { [weak self] text in self?.tookSocketFrame(text) }
            socket?.onState = { [weak self] state in self?.socketStateChanged(state) }
            socket?.onPeer = { [weak self] present in self?.socketPeerChanged(present) }
        }
    }
    /// A `push` frame the Mac sent down the socket unasked: the reply
    /// envelope's `{status, content_type, body}` plus the frame's own `ts`,
    /// so the owner can say how old the picture was on arrival.
    var onPush: (([String: Any]) -> Void)?
    /// The socket's transitions, forwarded for the profile's word.
    var onSocketState: ((RelaySocket.State) -> Void)?
    /// How long a request waits for its answer down the socket before the
    /// same request goes the mailbox way instead.
    static let socketDeadline: TimeInterval = 10
    /// Requests down the socket waiting for their `re`, by frame id. At most
    /// one at a time — `request` serialises — so a counter refusal with an
    /// empty `re` can only mean the one waiter.
    private var pendingSocket: [String: CheckedContinuation<[String: Any]?, Never>] = [:]

    struct Answer {
        var status = 0
        var body = Data()
        var detail = ""
        /// Empty on success; otherwise the failure in words.
        var failure = ""
        /// The Mac's own legs off the reply envelope's `timing` — `dwell`,
        /// `hold`, `open`, `run` away; `open`, `run` at home. Read
        /// tolerantly: an older Mac sends none and this stays empty.
        var macTiming: [String: Double] = [:]
        /// The phone's own stamps for the same trip.
        var legs = LinkLegs()
        /// Set by `HomeChannel` alone: the Mac's phone door itself answered
        /// with `status` (a plain HTTP refusal — 403 is "not paired here")
        /// rather than a sealed frame carrying an inner status. Only this
        /// kind of 403 means the pairing is gone; an inner 403 is one verb's
        /// own refusal and must not forget the pairing.
        var refusedAtTheDoor = false
        /// The door answered **421 Misdirected Request**: this address is
        /// one of the Mac's VPN / tunnel addresses and the door does not
        /// serve there. The Mac is fine and so is the pairing — the walk
        /// skips this host, forgets nothing and never promotes it. Today's
        /// Mac closes such a knock silently (a transport error, which every
        /// walk already moves past); this rule is belt and braces for a
        /// daemon that one day answers with the status.
        var misdirected: Bool { refusedAtTheDoor && status == 421 }
    }

    /// The four away failures, told apart. One generic sentence for all of
    /// them is what made the last outage undiagnosable from the phone: it
    /// could not distinguish "this phone has no signal" from "the mailbox is
    /// rate-limited" from "the mailbox's storage is down" from "the mailbox
    /// is fine and the Mac has not picked up" — which is the one that
    /// actually happened. The Mac's own sealed refusals (the lease refusal
    /// among them) are never reworded here; they arrive inside an `err`
    /// frame and are shown verbatim.
    enum Trouble {
        static let noConnection =
            "No connection to the relay — check your internet."
        static let rateLimited =
            "The relay refused the request (rate-limited)."
        static let storageDown = "The relay's storage is unavailable."
        static let macSilent =
            "The relay is reachable but the Mac has not answered — "
            + "it may take up to a minute to wake."
        /// The caller walked away — a background check-in that ran out of
        /// budget. Nothing is wrong with the link, and nothing on screen is
        /// waiting on this sentence; it exists so an abandoned request
        /// returns a *failure* rather than a blank success.
        static let cutShort = "The check-in was cut short."

        static func unexpected(_ code: Int) -> String {
            "The relay answered unexpectedly (code \(code))."
        }

        /// What a non-200 from the mailbox means, in the person's words.
        static func forStatus(_ code: Int) -> String {
            switch code {
            case 429: return rateLimited
            case 503: return storageDown
            default: return unexpected(code)
            }
        }
    }

    init(key: Data, base: String, sendCtr: Int, recvCtr: Int) {
        self.key = key
        self.base = base
        self.sendCtr = sendCtr
        self.recvCtr = recvCtr
    }

    /// The one request in flight, plus whatever is queued behind it. Strictly
    /// one at a time, and that is load-bearing: `poll()`'s timer and a tapped
    /// action both RPOP the same `to-phone` list, and two concurrent waiters
    /// destroy each other's answers — a pop is a removal, so the waiter that
    /// takes the wrong reply has already thrown the other one's away.
    ///
    /// **Keyed by mailbox and `static`, because the invariant is one waiter
    /// per process, not per instance.** `BackgroundRefresh` builds a second
    /// `PhoneClient`, and so a second `RelayChannel` over the very same
    /// mailbox; an instance-scoped queue lets those two pop against each
    /// other, which is the same destruction one paragraph up, arrived at
    /// from a direction the type could not see.
    private static var inFlight: [String: Task<Answer, Never>] = [:]

    /// One request through the mailbox: seal, post, poll for the sealed
    /// answer, verify, return. A stale-counter refusal from the Mac
    /// (`ctr_expected`, itself sealed) fast-forwards once and retries.
    /// Requests are serialised through `inFlight` — each waits for the
    /// previous to finish before touching the mailbox.
    /// The default deadline covers the Mac's slowest idle pickup — the
    /// connector backs off to a 30-second gap once a channel has been quiet
    /// for ten minutes, so a 20-second wait would report a working link as
    /// broken on the first request after a quiet spell. Action routes pass
    /// their own, longer, values.
    func request(kind: String, body: [String: Any],
                 timeout: TimeInterval = 45) async -> Answer {
        let mailbox = RelayTransport.channelId(key: key)
        let previous = Self.inFlight[mailbox]
        let task = Task { () -> Answer in
            _ = await previous?.value
            // A wake opens the socket and asks in the same breath: a line
            // still coming up is worth `readyGrace` of waiting, because it
            // answers in milliseconds and the mailbox in seconds.
            if let socket = self.socket, socket.comingUp {
                _ = await socket.ready(within: RelaySocket.readyGrace)
            }
            // The socket first while it is open **and the Mac is on the
            // other side of it** (the relay's `peer:1`); a line with no
            // peer, a closed socket, a send that failed or no answer
            // inside `socketDeadline` falls the **same** request through
            // to the mailbox, with a fresh counter — a peerless line goes
            // straight there with no ten-second wait.
            if let socket = self.socket, socket.isOpen, socket.peerPresent,
               let answer = await self.sendViaSocket(kind: kind, body: body,
                                                     retried: false,
                                                     carried: nil) {
                return answer
            }
            return await self.send(kind: kind, body: body,
                                   timeout: timeout, retried: false,
                                   carried: nil)
        }
        Self.inFlight[mailbox] = task
        // The queued task is unstructured — it has to be, or a caller that
        // walks away would strand everyone behind it — so cancellation is
        // forwarded by hand. Without this a cancelled caller (the background
        // refresh, cut off at its budget) leaves a poller alive that keeps
        // popping this mailbox, and iOS freezes it with the process rather
        // than ending it: it wakes on the next launch and eats the
        // foreground poll's answers.
        let answer = await withTaskCancellationHandler {
            await task.value
        } onCancel: {
            task.cancel()
        }
        if Self.inFlight[mailbox] == task { Self.inFlight[mailbox] = nil }
        return answer
    }

    private func send(kind: String, body: [String: Any], timeout: TimeInterval,
                      retried: Bool, carried: LinkLegs?) async -> Answer {
        if Task.isCancelled { return Answer(failure: Trouble.cutShort) }
        // The trip's clock starts at the first POST and its legs survive
        // the one `ctr_expected` retry — `carried` is the first attempt's
        // stamps, its post leg included — so a retried request is one
        // trip and the retry's own POST never swallows the first attempt.
        let t0 = carried?.sentAt ?? Date()
        var legs = carried ?? LinkLegs(sentAt: t0)
        let channel = RelayTransport.channelId(key: key)
        guard let postURL = RelayTransport.boxURL(
                base: base, channel: channel, direction: "to-mac", wait: false),
              let pollURL = RelayTransport.boxURL(
                base: base, channel: channel, direction: "to-phone", wait: true)
        else {
            return Answer(failure: "Bad relay address.")
        }
        sendCtr += 1
        onCounters?(sendCtr, recvCtr)
        let frameId = UUID().uuidString
        guard let wire = RelayTransport.seal(
            key: key, direction: .phoneToMac, ctr: sendCtr, kind: kind,
            body: body, frameId: frameId) else {
            return Answer(failure: "Could not seal the request.")
        }
        var post = URLRequest(url: postURL)
        post.httpMethod = "POST"
        post.setValue("text/plain", forHTTPHeaderField: "Content-Type")
        post.httpBody = Data(wire.utf8)
        post.timeoutInterval = 10
        do {
            let (_, response) = try await URLSession.shared.data(for: post)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else {
                return Answer(failure: RelayChannel.Trouble.forStatus(code))
            }
            if carried == nil { legs.postSeconds = Date().timeIntervalSince(t0) }
            onSent?()
        } catch {
            return Answer(failure: RelayChannel.Trouble.noConnection)
        }

        let deadline = Date().addingTimeInterval(timeout)
        var sealFailed = false
        /// The last thing the mailbox said other than 200, remembered across
        /// the loop so the deadline can be worded rather than guessed at.
        var lastRefusal = 0
        while Date() < deadline {
            // The one place an abandoned poller stops popping this mailbox.
            if Task.isCancelled { return Answer(failure: Trouble.cutShort) }
            var poll = URLRequest(url: pollURL)
            poll.timeoutInterval = 30
            let answer: (Data, URLResponse)
            do {
                answer = try await URLSession.shared.data(for: poll)
            } catch {
                return Answer(failure: RelayChannel.Trouble.noConnection)
            }
            let code = (answer.1 as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200, !answer.0.isEmpty else {
                // 204 is an empty box, which is the ordinary case while the
                // Mac is still working; anything else is the mailbox itself
                // refusing, and is what the deadline sentence will name.
                if code != 200, code != 204 { lastRefusal = code }
                // A refusing relay (429, 400) answers instantly; without a
                // pause this loop would hammer it for the whole deadline.
                // A 204 is the mailbox's own 20 s hold running out with
                // nothing carried: the next hold is asked for at once,
                // since the reply can land in the second a pause would
                // spend (the audit's proposal 8).
                if code != 204 {
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                }
                continue
            }
            guard let text = String(data: answer.0, encoding: .utf8) else {
                sealFailed = true
                continue
            }
            let (frame, refusal) = RelayTransport.open(
                key: key, direction: .macToPhone,
                wire: text.trimmingCharacters(in: .whitespacesAndNewlines),
                lastCtr: recvCtr)
            guard let frame else {
                if refusal == "seal" { sealFailed = true }
                continue
            }
            recvCtr = frame["ctr"] as? Int ?? recvCtr
            onCounters?(sendCtr, recvCtr)
            let payload = frame["body"] as? [String: Any] ?? [:]
            let kindName = frame["kind"] as? String ?? ""
            if kindName == "err" {
                // An `err` naming another request (a timed-out predecessor's
                // late refusal) is not this one's answer. A ctr refusal
                // carries an empty `re` and is taken here — with requests
                // serialised, the current waiter is the only one it can mean.
                let re = payload["re"] as? String ?? ""
                if !re.isEmpty, re != frameId { continue }
                if let expected = payload["ctr_expected"] as? Int, !retried {
                    // The Mac says this phone's counter is stale — safe to
                    // fast-forward, the answer was sealed to this key.
                    sendCtr = max(sendCtr, expected - 1)
                    onCounters?(sendCtr, recvCtr)
                    return await send(kind: kind, body: body,
                                      timeout: timeout, retried: true,
                                      carried: legs)
                }
                var refused = Answer(
                    status: payload["status"] as? Int ?? 0,
                    detail: payload["error"] as? String ?? "",
                    failure: payload["error"] as? String
                        ?? "The Mac refused the request.")
                legs.totalSeconds = Date().timeIntervalSince(t0)
                refused.legs = legs
                report(kind: kind, answer: refused)
                return refused
            }
            guard kindName == "reply",
                  (payload["re"] as? String ?? "") == frameId else {
                continue  // some other request's answer; keep polling
            }
            let bodyText = payload["body"] as? String ?? ""
            var out = Answer(status: payload["status"] as? Int ?? 0,
                             body: Data(bodyText.utf8))
            if out.status != 200,
               let obj = try? JSONSerialization.jsonObject(with: out.body)
                as? [String: Any] {
                out.detail = (obj["detail"] as? String)
                    ?? (obj["error"] as? String) ?? ""
            }
            // The Mac's legs ride beside `body`, never inside it; an
            // older Mac sends none and the dictionary stays empty.
            out.macTiming = payload["timing"] as? [String: Double] ?? [:]
            legs.totalSeconds = Date().timeIntervalSince(t0)
            out.legs = legs
            report(kind: kind, answer: out)
            return out
        }
        _ = legs
        if sealFailed { return Answer(failure: "An envelope failed verification.") }
        // Nothing arrived before the deadline. If the mailbox was refusing,
        // say so; otherwise the mailbox was fine and the Mac did not pick up,
        // which is both the outage's signature and the honest description of
        // the connector's idle wake-up.
        return Answer(failure: lastRefusal == 0
            ? RelayChannel.Trouble.macSilent
            : RelayChannel.Trouble.forStatus(lastRefusal))
    }

    /// One completed trip, handed to the owner as a sample.
    private func report(kind: String, answer: Answer,
                        route: String = LinkTimingRoute.away) {
        guard let onTiming, let sentAt = answer.legs.sentAt else { return }
        onTiming(LinkTimingSample(
            at: sentAt.timeIntervalSince1970, route: route,
            kind: kind, status: answer.status,
            totalSeconds: answer.legs.totalSeconds,
            postSeconds: answer.legs.postSeconds,
            macTiming: answer.macTiming))
    }

    // MARK: - The socket lane

    /// One request down the socket, or nil meaning "go the mailbox way":
    /// no open socket, a send that failed, the socket closing under the
    /// request, or no answer within `socketDeadline`. The counter spent
    /// here is spent for good (the Mac checks `>`); the mailbox attempt
    /// takes the next one. Sealed with the same key and namespace as
    /// `send`, opened by `tookSocketFrame`, timed under
    /// `LinkTimingRoute.socket`. A stale-counter refusal fast-forwards
    /// once, `send`'s rule.
    private func sendViaSocket(kind: String, body: [String: Any],
                               retried: Bool, carried: LinkLegs?) async -> Answer? {
        guard let socket, socket.isOpen, socket.peerPresent else { return nil }
        if Task.isCancelled { return nil }
        let t0 = carried?.sentAt ?? Date()
        var legs = carried ?? LinkLegs(sentAt: t0)
        sendCtr += 1
        onCounters?(sendCtr, recvCtr)
        let frameId = UUID().uuidString
        guard let wire = RelayTransport.seal(
            key: key, direction: .phoneToMac, ctr: sendCtr, kind: kind,
            body: body, frameId: frameId) else { return nil }
        do {
            try await socket.send(wire)
        } catch {
            return nil
        }
        if carried == nil { legs.postSeconds = Date().timeIntervalSince(t0) }
        // No `onSent` here: that word is the mailbox's ("asking the relay"
        // becoming "waiting for the Mac"); a socket answer is milliseconds
        // away or falls through to the mailbox, which then says it.
        guard let taken = await awaitSocketAnswer(frameId: frameId) else { return nil }
        let kindName = taken["kind"] as? String ?? ""
        let payload = taken["body"] as? [String: Any] ?? [:]
        if kindName == "err" {
            if let expected = payload["ctr_expected"] as? Int, !retried {
                sendCtr = max(sendCtr, expected - 1)
                onCounters?(sendCtr, recvCtr)
                return await sendViaSocket(kind: kind, body: body,
                                           retried: true, carried: legs)
            }
            var refused = Answer(
                status: payload["status"] as? Int ?? 0,
                detail: payload["error"] as? String ?? "",
                failure: payload["error"] as? String
                    ?? "The Mac refused the request.")
            legs.totalSeconds = Date().timeIntervalSince(t0)
            refused.legs = legs
            report(kind: kind, answer: refused, route: LinkTimingRoute.socket)
            return refused
        }
        let bodyText = payload["body"] as? String ?? ""
        var out = Answer(status: payload["status"] as? Int ?? 0,
                         body: Data(bodyText.utf8))
        if out.status != 200,
           let obj = try? JSONSerialization.jsonObject(with: out.body)
            as? [String: Any] {
            out.detail = (obj["detail"] as? String)
                ?? (obj["error"] as? String) ?? ""
        }
        out.macTiming = payload["timing"] as? [String: Double] ?? [:]
        legs.totalSeconds = Date().timeIntervalSince(t0)
        out.legs = legs
        report(kind: kind, answer: out, route: LinkTimingRoute.socket)
        return out
    }

    /// The answer frame naming `frameId` (`["kind", "body"]`), or nil once
    /// `socketDeadline` has passed or the socket closed under it.
    private func awaitSocketAnswer(frameId: String) async -> [String: Any]? {
        let deadline = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.socketDeadline))
            guard !Task.isCancelled else { return }
            self?.resolveSocket(frameId, with: nil)
        }
        let answer = await withCheckedContinuation {
            (continuation: CheckedContinuation<[String: Any]?, Never>) in
            pendingSocket[frameId] = continuation
        }
        deadline.cancel()
        return answer
    }

    private func resolveSocket(_ frameId: String, with value: [String: Any]?) {
        guard let continuation = pendingSocket.removeValue(forKey: frameId) else { return }
        continuation.resume(returning: value)
    }

    private func socketStateChanged(_ state: RelaySocket.State) {
        if state != .open { failEveryWaiter() }
        onSocketState?(state)
    }

    /// The Mac left the line (`peer:0`) — or arrived. A waiter on a line
    /// whose peer has gone falls through to the mailbox at once.
    private func socketPeerChanged(_ present: Bool) {
        if !present { failEveryWaiter() }
    }

    /// Every waiter falls through to the mailbox at once rather than
    /// sitting out the deadline on a line that is gone or peerless.
    private func failEveryWaiter() {
        for frameId in Array(pendingSocket.keys) {
            resolveSocket(frameId, with: nil)
        }
    }

    /// Every frame the socket carried: opened under the key with the shared
    /// `recvCtr` (a frame that does not open — foreign, stale, tampered —
    /// is skipped, as `send`'s loop skips one), then routed by kind: a
    /// `reply` or `err` to the request it names, a `push` to `onPush`.
    func tookSocketFrame(_ text: String) {
        let (frame, _) = RelayTransport.open(
            key: key, direction: .macToPhone,
            wire: text.trimmingCharacters(in: .whitespacesAndNewlines),
            lastCtr: recvCtr)
        guard let frame else { return }
        recvCtr = frame["ctr"] as? Int ?? recvCtr
        onCounters?(sendCtr, recvCtr)
        let kindName = frame["kind"] as? String ?? ""
        let payload = frame["body"] as? [String: Any] ?? [:]
        switch kindName {
        case "push":
            var pushed = payload
            pushed["ts"] = frame["ts"]
            onPush?(pushed)
        case "reply", "err":
            let re = payload["re"] as? String ?? ""
            // A ctr refusal carries an empty `re`; with requests serialised
            // the one waiter is the only request it can mean.
            let target = re.isEmpty
                ? (pendingSocket.count == 1 ? pendingSocket.keys.first : nil)
                : re
            guard let target else { return }
            resolveSocket(target, with: ["kind": kindName, "body": payload])
        default:
            break
        }
    }
}
