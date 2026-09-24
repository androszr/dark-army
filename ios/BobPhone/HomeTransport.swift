import Foundation

/// The sealed home path: one request to the Mac on the same Wi-Fi, sealed
/// under the pairing's home key in the `.home` namespace, and its sealed
/// answer. `RelayChannel`'s shape without the mailbox — the request is a
/// `POST` straight at the Mac's phone door and the answer rides back in the
/// same HTTP response. **The only phone file that builds a home URL**, and it
/// does so through `PhoneActions.homeURL` / `uploadURL`; no device token
/// travels anywhere in it.
///
/// Every answer is HTTP 200 `text/plain` with the real status *inside* the
/// sealed frame. A non-200 from the door is the door itself refusing — 403
/// means this phone is not paired here — and is returned as a bare
/// `Answer(status:)` so the caller can forget the pairing. A transport error
/// returns **nil**, which is the caller's cue to try the next address; a
/// zero-status `Answer` there would stop the host walk on the first silent
/// address.
@MainActor
final class HomeChannel {
    let key: Data
    private(set) var sendCtr: Int
    private(set) var recvCtr: Int
    /// Told whenever a counter moves, so the owner can persist. Throttling
    /// is the owner's job — at most once a minute, and on background.
    var onCounters: ((Int, Int) -> Void)?
    /// Told once per completed request — a `reply` or an `err` the door
    /// answered with — with the trip's legs (`LinkTiming.swift`), the
    /// whole POST being the post leg at home. `RelayChannel.onTiming`'s
    /// twin; a transport error or a plain door refusal reports nothing.
    var onTiming: ((LinkTimingSample) -> Void)?

    init(key: Data, sendCtr: Int, recvCtr: Int) {
        self.key = key
        self.sendCtr = sendCtr
        self.recvCtr = recvCtr
    }

    /// This phone's home channel id — derived from the key, not a credential.
    var channelId: String { RelayTransport.channelId(key: key, ns: .home) }

    /// One sealed request: seal, POST, open. `kind` is `state`, `usage` or
    /// `action`. A stale-counter refusal from the Mac (`ctr_expected`,
    /// itself sealed) fast-forwards once and retries — `RelayChannel.send`'s
    /// exact branch.
    func request(kind: String, body: [String: Any], host: String, port: Int,
                 timeout: TimeInterval) async -> RelayChannel.Answer? {
        await send(kind: kind, body: body, host: host, port: port,
                   timeout: timeout, retried: false)
    }

    private func send(kind: String, body: [String: Any], host: String,
                      port: Int, timeout: TimeInterval,
                      retried: Bool) async -> RelayChannel.Answer? {
        guard case .ok(let url) = PhoneActions.homeURL(host: host, port: port) else {
            return RelayChannel.Answer(failure: "Bad address.")
        }
        sendCtr += 1
        onCounters?(sendCtr, recvCtr)
        let frameId = UUID().uuidString
        guard let wire = RelayTransport.seal(
            key: key, direction: .phoneToMac, ctr: sendCtr, kind: kind,
            body: body, frameId: frameId, ns: .home) else {
            return RelayChannel.Answer(failure: "Could not seal the request.")
        }
        var post = URLRequest(url: url)
        post.httpMethod = "POST"
        post.setValue(channelId, forHTTPHeaderField: PhoneActions.channelHeader)
        post.setValue("text/plain", forHTTPHeaderField: "Content-Type")
        post.httpBody = Data(wire.utf8)
        post.timeoutInterval = timeout
        let t0 = Date()
        let answer: (Data, URLResponse)
        do {
            answer = try await URLSession.shared.data(for: post)
        } catch is CancellationError {
            return RelayChannel.Answer(failure: RelayChannel.Trouble.cutShort)
        } catch {
            return nil
        }
        var legs = LinkLegs(sentAt: t0)
        legs.postSeconds = Date().timeIntervalSince(t0)
        let code = (answer.1 as? HTTPURLResponse)?.statusCode ?? 0
        return await handle(code: code, data: answer.0, frameId: frameId,
                            kind: kind, legs: legs,
                            retry: retried ? nil : {
                                await self.send(kind: kind, body: body,
                                                host: host, port: port,
                                                timeout: timeout,
                                                retried: true)
                            })
    }

    /// The Mac's answer, opened. `retry` is the once-only replay after a
    /// `ctr_expected` fast-forward, or nil when this *is* the replay.
    /// `legs` is the trip so far; a sealed answer completes it and reports.
    private func handle(code: Int, data: Data, frameId: String,
                        kind: String, legs: LinkLegs,
                        retry: (() async -> RelayChannel.Answer?)?)
        async -> RelayChannel.Answer? {
        // The door refused plainly: 403 is "not paired here", 421 a tunnel
        // address the door does not serve (`Answer.misdirected` — skipped,
        // never un-paired), 426 an older app against a newer Mac (not this
        // build's case), anything else the door's own refusal. The body is
        // the Mac's JSON; carry its words.
        guard code == 200 else {
            var out = RelayChannel.Answer(status: code)
            out.refusedAtTheDoor = true
            if let obj = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any] {
                out.detail = (obj["detail"] as? String)
                    ?? (obj["error"] as? String) ?? ""
            }
            return out
        }
        guard let text = String(data: data, encoding: .utf8) else {
            return RelayChannel.Answer(failure: "An envelope failed verification.")
        }
        let (frame, refusal) = RelayTransport.open(
            key: key, direction: .macToPhone,
            wire: text.trimmingCharacters(in: .whitespacesAndNewlines),
            lastCtr: recvCtr, ns: .home)
        guard let frame else {
            return RelayChannel.Answer(
                failure: refusal == "seal"
                    ? "An envelope failed verification."
                    : "The Mac's answer could not be read (\(refusal)).")
        }
        recvCtr = frame["ctr"] as? Int ?? recvCtr
        onCounters?(sendCtr, recvCtr)
        let payload = frame["body"] as? [String: Any] ?? [:]
        let kindName = frame["kind"] as? String ?? ""
        if kindName == "err" {
            if let expected = payload["ctr_expected"] as? Int {
                // The Mac says this phone's counter is stale — safe to
                // fast-forward, the answer was sealed to this key. Applied
                // whether or not this request retries (an upload does not),
                // so the *next* request already carries a good counter.
                sendCtr = max(sendCtr, expected - 1)
                onCounters?(sendCtr, recvCtr)
                if let retry { return await retry() }
            }
            var refused = RelayChannel.Answer(
                status: payload["status"] as? Int ?? 0,
                detail: payload["error"] as? String ?? "",
                failure: payload["error"] as? String
                    ?? "The Mac refused the request.")
            refused.legs = completed(legs)
            report(kind: kind, answer: refused)
            return refused
        }
        guard kindName == "reply",
              (payload["re"] as? String ?? "") == frameId else {
            return RelayChannel.Answer(failure: "The Mac answered another request.")
        }
        let bodyText = payload["body"] as? String ?? ""
        var out = RelayChannel.Answer(status: payload["status"] as? Int ?? 0,
                                      body: Data(bodyText.utf8))
        if out.status != 200,
           let obj = try? JSONSerialization.jsonObject(with: out.body)
            as? [String: Any] {
            out.detail = (obj["detail"] as? String)
                ?? (obj["error"] as? String) ?? ""
        }
        // The Mac's legs ride beside `body`, never inside it; an older
        // Mac sends none and the dictionary stays empty.
        out.macTiming = payload["timing"] as? [String: Double] ?? [:]
        out.legs = completed(legs)
        report(kind: kind, answer: out)
        return out
    }

    private func completed(_ legs: LinkLegs) -> LinkLegs {
        var done = legs
        if let sentAt = legs.sentAt {
            done.totalSeconds = Date().timeIntervalSince(sentAt)
        }
        return done
    }

    /// One completed trip, handed to the owner as a sample.
    private func report(kind: String, answer: RelayChannel.Answer) {
        guard let onTiming, let sentAt = answer.legs.sentAt else { return }
        onTiming(LinkTimingSample(
            at: sentAt.timeIntervalSince1970, route: LinkTimingRoute.home,
            kind: kind, status: answer.status,
            totalSeconds: answer.legs.totalSeconds,
            postSeconds: answer.legs.postSeconds,
            macTiming: answer.macTiming))
    }

    /// The held-open terminal stream, opened: seals the `terminal_stream`
    /// frame under this channel's own counter — the same counter the polls
    /// spend, so a poll landing between seal and send can make it stale,
    /// which the Mac answers as a sealed `err` with `ctr_expected`; the
    /// stream hands that back through `onCounterExpected` and this channel
    /// fast-forwards exactly as `handle` does, so the coordinator's one
    /// reopen carries a good counter. The address goes through
    /// `HostAddress.check`; this stays the only phone file that turns a
    /// pairing's host into an address.
    func openTerminalStream(session: String, host: String, port: Int) -> SealedTerminalStream? {
        guard case .ok(let cleaned) = HostAddress.check(host: host, typedPort: port) else {
            return nil
        }
        sendCtr += 1
        onCounters?(sendCtr, recvCtr)
        let frameId = UUID().uuidString
        guard let wire = RelayTransport.seal(
            key: key, direction: .phoneToMac, ctr: sendCtr, kind: "terminal_stream",
            body: ["session": session], frameId: frameId, ns: .home) else {
            return nil
        }
        let stream = SealedTerminalStream(host: cleaned.host, port: cleaned.port,
                                          channelId: channelId, key: key,
                                          openingWire: wire, frameId: frameId)
        stream.onCounterExpected = { [weak self] expected in
            Task { @MainActor in self?.fastForward(to: expected) }
        }
        return stream
    }

    /// The Mac says this phone's counter is stale — safe to fast-forward,
    /// the answer was sealed to this key.
    func fastForward(to expected: Int) {
        sendCtr = max(sendCtr, expected - 1)
        onCounters?(sendCtr, recvCtr)
    }

    /// One staged attachment, sealed: a header frame (kind `upload`, body
    /// `{staging, name}`) in `X-Bob-Frame` and the blob — sealed off the
    /// main actor, a 20 MB photo would otherwise stall the composer — as
    /// the HTTP body, bound to that frame's id. Same answer handling as
    /// `request`; the inner body is the Mac's `{ok, path, detail}`.
    func upload(stagingId: String, name: String, data: Data, host: String,
                port: Int, timeout: TimeInterval) async -> RelayChannel.Answer? {
        guard case .ok(let url) = PhoneActions.uploadURL(host: host, port: port) else {
            return RelayChannel.Answer(failure: "Bad address.")
        }
        sendCtr += 1
        onCounters?(sendCtr, recvCtr)
        let frameId = UUID().uuidString
        guard let wire = RelayTransport.seal(
            key: key, direction: .phoneToMac, ctr: sendCtr, kind: "upload",
            body: ["staging": stagingId, "name": name], frameId: frameId,
            ns: .home) else {
            return RelayChannel.Answer(failure: "Could not seal the request.")
        }
        let sealedKey = key
        let blob = await Task.detached(priority: .userInitiated) {
            RelayTransport.sealBlob(key: sealedKey, direction: .phoneToMac,
                                    frameId: frameId, data: data, ns: .home)
        }.value
        guard let blob else {
            return RelayChannel.Answer(failure: "Could not seal the file.")
        }
        var post = URLRequest(url: url)
        post.httpMethod = "POST"
        post.setValue(channelId, forHTTPHeaderField: PhoneActions.channelHeader)
        post.setValue(wire, forHTTPHeaderField: PhoneActions.frameHeader)
        post.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        post.httpBody = blob
        post.timeoutInterval = timeout
        let t0 = Date()
        let answer: (Data, URLResponse)
        do {
            answer = try await URLSession.shared.data(for: post)
        } catch is CancellationError {
            return RelayChannel.Answer(failure: RelayChannel.Trouble.cutShort)
        } catch {
            return nil
        }
        var legs = LinkLegs(sentAt: t0)
        legs.postSeconds = Date().timeIntervalSince(t0)
        let code = (answer.1 as? HTTPURLResponse)?.statusCode ?? 0
        // No fast-forward retry for a blob: the caller re-presses.
        return await handle(code: code, data: answer.0, frameId: frameId,
                            kind: "upload", legs: legs, retry: nil)
    }
}
