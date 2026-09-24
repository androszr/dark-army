import AVFoundation
import Foundation
import Security
import SwiftUI

/// Camera QR scan plus a manual host / port / code fallback. POSTs `/api/pair`
/// — sealed under the QR's home key where the square carried one, and from a
/// typed address as an SRP-6a exchange over the code (`SRP.swift`) whose
/// reply is sealed under the derived key — and stores the record in the
/// Keychain with
/// `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` — still bound to this
/// device and still behind the passcode, but readable while the screen is
/// off, which is when `BackgroundRefresh` needs it. `load()` moves an item
/// written under the older when-unlocked rule across in place.
@MainActor
final class PairingStore: ObservableObject {
    @Published var record: PairingRecord?
    @Published var error = ""
    @Published var busy = false

    private let service = "local.bob.BobPhone"
    private let account = "pairing"

    func load() {
        record = Self.readKeychain(service: service, account: account)
        if record != nil {
            Self.relaxAccessibility(service: service, account: account)
        }
    }

    /// An attribute update, never delete-and-add: a re-add that failed
    /// would be the pairing gone. Idempotent, and a refusal is ignored —
    /// the record still reads in the foreground exactly as before.
    private static func relaxAccessibility(service: String, account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let change: [String: Any] = [
            kSecAttrAccessible as String:
                kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        SecItemUpdate(query as CFDictionary, change as CFDictionary)
    }

    func clear() {
        Self.deleteKeychain(service: service, account: account)
        record = nil
    }

    /// The manual screen's route: one typed address, and the code proved
    /// over SRP-6a (`SRPClient`) rather than sent. Two plain-JSON requests
    /// to `/api/pair` — `pake: start` with `A`, `pake: finish` with the
    /// proof — and the Mac's answer is the same sealed `reply` the QR route
    /// gets, opened under the key both sides derived. Nothing secret
    /// crosses the Wi-Fi; a reply that is not the shape asked for is
    /// `PhoneActions.pakeOutOfStep` and writes no record.
    func pair(host: String, port: Int, code: String, name: String) async {
        error = ""
        busy = true
        defer { busy = false }
        // The code as the Mac's redeem normalises it: trimmed, upper-cased.
        let trimmedCode = code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        let typed = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty, port > 0, !trimmedCode.isEmpty else {
            error = "Need a host, a port and a code."
            return
        }
        let target: HostAddress.Cleaned
        switch HostAddress.check(host: typed, typedPort: port) {
        case .refused(let message):
            error = message.isEmpty
                ? "That isn't a usable address — type the host shown on the Mac's pairing sheet."
                : message
            return
        case .ok(let value):
            target = value
        }
        guard let url = URL(string: "http://\(target.host):\(target.port)/api/pair") else {
            error = "That isn't a usable address — type the host shown on the Mac's pairing sheet."
            return
        }
        // `A = g^a mod N` is the first of three modular exponentiations
        // on a 2048-bit modulus; every one runs off the main actor, so the
        // busy state draws before any arithmetic starts.
        let begun: (client: SRPClient, aHex: String) =
            await Task.detached(priority: .userInitiated) {
                let fresh = SRPClient()
                return (fresh, fresh.start())
            }.value
        let client = begun.client
        let aHex = begun.aHex
        guard let started = await Self.postJSON(url, ["pake": "start", "A": aHex]) else {
            error = "Could not reach the Mac."
            return
        }
        guard started.status == 200 else {
            error = Self.refusalWords(started.body, status: started.status)
            return
        }
        guard (started.body["pake"] as? String) == "start",
              let pairing = started.body["pairing"] as? String, !pairing.isEmpty,
              let saltHex = started.body["salt"] as? String, !saltHex.isEmpty,
              let bHex = started.body["B"] as? String, !bHex.isEmpty else {
            error = PhoneActions.pakeOutOfStep
            return
        }
        // The other two modpows, also off the main actor.
        let finished: (client: SRPClient, outcome: Result<(m1: String, k: Data), SRPError>) =
            await Task.detached(priority: .userInitiated) {
                var worker = client
                let outcome = worker.finish(identity: pairing, saltHex: saltHex,
                                            bHex: bHex, password: trimmedCode)
                return (worker, outcome)
            }.value
        let proof: (m1: String, k: Data)
        switch finished.outcome {
        case .failure(let why):
            error = why == .shape ? PhoneActions.pakeOutOfStep : why.words
            return
        case .success(let value):
            proof = value
        }
        let proven = finished.client
        let finishFields: [String: Any] = [
            "pake": "finish", "A": aHex, "M1": proof.m1,
            "name": name.isEmpty ? UIDevice.current.name : name,
        ]
        guard let answer = await Self.postRaw(url, finishFields, channel: nil) else {
            error = "Could not reach the Mac."
            return
        }
        guard answer.status == 200 else {
            let obj = (try? JSONSerialization.jsonObject(with: answer.data)) as? [String: Any]
            error = Self.refusalWords(obj ?? [:], status: answer.status)
            return
        }
        let key = proven.homeKey
        guard key.count == 32 else {
            error = PhoneActions.pakeOutOfStep
            return
        }
        let opened = Self.openReply(answer.data, key: key)
        if !opened.refusal.isEmpty {
            error = opened.refusal
            return
        }
        // The reply must carry the Mac's own proof, or this phone has not
        // been talking to the Mac that issued the code.
        guard let m2 = opened.inner["M2"] as? String, proven.verify(m2Hex: m2) else {
            error = PhoneActions.pakeOutOfStep
            return
        }
        save(reply: opened.inner, key: key, sendCtr: 0, recvCtr: opened.recvCtr,
             target: target, order: [target.host])
    }

    /// Walk the addresses the Mac offered, in order, and keep the first that
    /// answers.
    ///
    /// A request that *throws* — refused connection, timeout — says that
    /// address is not this Mac, so the walk moves on. **Any HTTP response
    /// stops it**: a Mac that answered is the Mac, and its refusal (a wrong
    /// or expired code) is what to show, never a reason to knock on the next
    /// address as well.
    ///
    /// `homeKey` is the QR's: the request is a sealed frame kind `pair`
    /// under it and the reply is sealed too — the key never crosses the
    /// Wi-Fi. A square with no key (an older Mac's) is refused in
    /// `olderMacPair`'s words before any request: this app has no plain
    /// leg to talk to it with.
    func pair(hosts: [String], port: Int, code: String, name: String,
              homeKey: String = "") async {
        error = ""
        busy = true
        defer { busy = false }
        let trimmedCode = code.trimmingCharacters(in: .whitespacesAndNewlines)
        let typed = hosts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !typed.isEmpty, port > 0, !trimmedCode.isEmpty else {
            error = "Need a host, a port and a code."
            return
        }
        guard let key = Data(base64Encoded: homeKey), key.count == 32 else {
            error = PhoneActions.olderMacPair
            return
        }
        var cleaned: [HostAddress.Cleaned] = []
        var firstRefusal = ""
        for candidate in typed {
            switch HostAddress.check(host: candidate, typedPort: port) {
            case .refused(let message):
                if firstRefusal.isEmpty { firstRefusal = message }
            case .ok(let value):
                if !cleaned.contains(value) { cleaned.append(value) }
            }
        }
        guard !cleaned.isEmpty else {
            error = firstRefusal.isEmpty
                ? "That isn't a usable address — type the host shown on the Mac's pairing sheet."
                : firstRefusal
            return
        }
        let fields: [String: Any] = [
            "code": trimmedCode,
            "name": name.isEmpty ? UIDevice.current.name : name,
        ]
        // The sealed pair frame is counter 1; the record starts its home
        // send counter there so the next request is 2.
        let sealedCtr = 1
        let frameId = UUID().uuidString
        guard let wire = RelayTransport.seal(
            key: key, direction: .phoneToMac, ctr: sealedCtr, kind: "pair",
            body: fields, frameId: frameId, ns: .home) else {
            error = "Could not seal the pairing request."
            return
        }
        let body = Data(wire.utf8)
        for (index, target) in cleaned.enumerated() {
            guard let url = URL(string: "http://\(target.host):\(target.port)/api/pair") else {
                continue
            }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue(RelayTransport.channelId(key: key, ns: .home),
                             forHTTPHeaderField: PhoneActions.channelHeader)
            request.setValue("text/plain", forHTTPHeaderField: "Content-Type")
            // Four seconds per attempt once there is somewhere else to go:
            // the walk's worst case is that times the candidates, and it is
            // only ever paid while nothing is answering at all.
            request.timeoutInterval = cleaned.count > 1 ? 4 : 8
            request.httpBody = body
            let answer: (Data, URLResponse)
            do {
                answer = try await URLSession.shared.data(for: request)
            } catch {
                continue
            }
            let status = (answer.1 as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200 else {
                let obj = (try? JSONSerialization.jsonObject(with: answer.0)) as? [String: Any]
                error = Self.refusalWords(obj ?? [:], status: status)
                return
            }
            let opened = Self.openReply(answer.0, key: key)
            if !opened.refusal.isEmpty {
                error = opened.refusal
                return
            }
            // The winner leads: it is the address the phone will poll, and
            // the rest stay on file as what it heals with when the network
            // moves under it.
            var order = cleaned.map(\.host)
            order.remove(at: index)
            order.insert(target.host, at: 0)
            save(reply: opened.inner, key: key, sendCtr: sealedCtr,
                 recvCtr: opened.recvCtr, target: target, order: order)
            return
        }
        error = "Could not reach the Mac."
    }

    /// The Mac's refusal, verbatim, else a status line.
    private static func refusalWords(_ obj: [String: Any], status: Int) -> String {
        (obj["error"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            ?? "Pairing refused (HTTP \(status))."
    }

    /// One plain-JSON POST; `nil` when the Mac could not be reached at all.
    private static func postJSON(_ url: URL, _ fields: [String: Any])
        async -> (status: Int, body: [String: Any])? {
        guard let answer = await postRaw(url, fields, channel: nil) else { return nil }
        let obj = (try? JSONSerialization.jsonObject(with: answer.data)) as? [String: Any]
        return (answer.status, obj ?? [:])
    }

    private static func postRaw(_ url: URL, _ fields: [String: Any], channel: String?)
        async -> (status: Int, data: Data)? {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let channel {
            request.setValue(channel, forHTTPHeaderField: PhoneActions.channelHeader)
        }
        request.timeoutInterval = 8
        request.httpBody = try? JSONSerialization.data(withJSONObject: fields)
        let answer: (Data, URLResponse)
        do {
            answer = try await URLSession.shared.data(for: request)
        } catch {
            return nil
        }
        return ((answer.1 as? HTTPURLResponse)?.statusCode ?? 0, answer.0)
    }

    /// The sealed pair reply, opened under `key` — both routes' decoder. A
    /// `reply` carries the JSON in `body`; an `err` (wrong, used or expired
    /// code, a ninth device) carries the Mac's own sentence and is shown
    /// verbatim. `refusal` is empty on success, `inner` the reply's JSON
    /// with a non-empty `token`.
    private static func openReply(_ data: Data, key: Data)
        -> (inner: [String: Any], recvCtr: Int, refusal: String) {
        let text = String(data: data, encoding: .utf8) ?? ""
        let (frame, refusal) = RelayTransport.open(
            key: key, direction: .macToPhone,
            wire: text.trimmingCharacters(in: .whitespacesAndNewlines),
            lastCtr: 0, ns: .home)
        guard let frame else {
            return ([:], 0, refusal == "seal"
                ? "An envelope failed verification."
                : "The Mac's answer could not be read (\(refusal)).")
        }
        let recvCtr = frame["ctr"] as? Int ?? 0
        let payload = frame["body"] as? [String: Any] ?? [:]
        if (frame["kind"] as? String) == "err" {
            return ([:], recvCtr, (payload["error"] as? String)
                .flatMap { $0.isEmpty ? nil : $0 }
                ?? "The Mac refused the pairing.")
        }
        let innerData = Data((payload["body"] as? String ?? "").utf8)
        let inner = (try? JSONSerialization.jsonObject(with: innerData)) as? [String: Any] ?? [:]
        let status = (payload["status"] as? Int) ?? (payload["status"] as? NSNumber)?.intValue ?? 0
        guard status == 200, let token = inner["token"] as? String, !token.isEmpty else {
            return ([:], recvCtr, refusalWords(inner, status: status))
        }
        return (inner, recvCtr, "")
    }

    /// The decode-and-save tail both routes share: the record written with
    /// the key the route holds — the QR's, or the one the exchange derived
    /// — and `homeKeyCrossedInClear` false, because on no route does it.
    private func save(reply obj: [String: Any], key: Data, sendCtr: Int, recvCtr: Int,
                      target: HostAddress.Cleaned, order: [String]) {
        let saved = PairingRecord(
            token: obj["token"] as? String ?? "",
            host: target.host,
            hosts: order,
            port: target.port,
            deviceId: obj["device_id"] as? String ?? "",
            // The away channel's secret rides the same sealed pair reply the
            // device token does, once, over the home LAN. An older Mac
            // sends neither key nor address: no away path, stated in the
            // profile rather than failed at.
            relayKey: obj["relay_key"] as? String ?? "",
            relayURL: obj["relay_url"] as? String ?? "",
            // The socket relay's address rides the same reply; an older
            // Mac, or one with no socket set, sends none: no socket lane.
            relayWSURL: obj["relay_ws_url"] as? String ?? "",
            homeKey: key.base64EncodedString(),
            homeSendCtr: sendCtr,
            homeRecvCtr: recvCtr,
            homeKeyCrossedInClear: false)
        do {
            try Self.writeKeychain(saved, service: service, account: account)
        } catch {
            self.error = "Paired, but this phone could not save it. Try again."
            return
        }
        record = saved
    }

    /// The sealed-frame counters moved — write them through to the Keychain
    /// record. Called by the client at most once a minute and on background.
    /// Read-merge-write against the Keychain itself, and **never** a publish
    /// of `record`: publishing restarts the client (promote's lesson), and a
    /// counter bump is not news any view draws.
    ///
    /// `token` is the generation guard, and it is the one that holds even if
    /// a call site forgets to sever a dying channel: counters counted under
    /// another pairing must never wind this record's forward, or a fresh
    /// channel starts at a dead pairing's `recvCtr` and refuses every new
    /// answer as a replay.
    func persistCounters(token: String, send: Int, recv: Int) {
        guard var current = Self.readKeychain(service: service,
                                              account: account) else { return }
        guard token == current.token else { return }
        guard send > current.sendCtr || recv > current.recvCtr else { return }
        current.sendCtr = max(send, current.sendCtr)
        current.recvCtr = max(recv, current.recvCtr)
        try? Self.writeKeychain(current, service: service, account: account)
    }

    /// The freshest persisted counters — the Keychain's own, which may be
    /// ahead of the published `record` because `persistCounters` never
    /// publishes. What `PhoneClient` builds its channel from.
    func storedCounters() -> (send: Int, recv: Int)? {
        guard let stored = Self.readKeychain(service: service,
                                             account: account) else { return nil }
        return (stored.sendCtr, stored.recvCtr)
    }

    /// The home channel's pair, `persistCounters`' exact discipline: same
    /// token guard, same monotonic merge, never a publish.
    func persistHomeCounters(token: String, send: Int, recv: Int) {
        guard var current = Self.readKeychain(service: service,
                                              account: account) else { return }
        guard token == current.token else { return }
        guard send > current.homeSendCtr || recv > current.homeRecvCtr else {
            return
        }
        current.homeSendCtr = max(send, current.homeSendCtr)
        current.homeRecvCtr = max(recv, current.homeRecvCtr)
        try? Self.writeKeychain(current, service: service, account: account)
    }

    func storedHomeCounters() -> (send: Int, recv: Int)? {
        guard let stored = Self.readKeychain(service: service,
                                             account: account) else { return nil }
        return (stored.homeSendCtr, stored.homeRecvCtr)
    }

    /// A poll answered on an address other than the one on file — keep it.
    /// The record is rewritten with the winner first, so the fix survives a
    /// relaunch instead of being re-discovered every time.
    ///
    /// The `host != current.host` guard is load-bearing: publishing `record`
    /// restarts the client, which would promote again, for ever.
    func promote(host: String) {
        let winner = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var current = record, !winner.isEmpty, winner != current.host else {
            return
        }
        var order = current.hosts.filter { !$0.isEmpty && $0 != winner }
        order.insert(winner, at: 0)
        current.host = winner
        current.hosts = order
        // The published copy's counters may lag the Keychain's (they are
        // persisted without a publish); a promote must not wind them back.
        if let stored = Self.readKeychain(service: service, account: account) {
            current.sendCtr = max(current.sendCtr, stored.sendCtr)
            current.recvCtr = max(current.recvCtr, stored.recvCtr)
            current.homeSendCtr = max(current.homeSendCtr, stored.homeSendCtr)
            current.homeRecvCtr = max(current.homeRecvCtr, stored.homeRecvCtr)
        }
        try? Self.writeKeychain(current, service: service, account: account)
        record = current
    }

    /// Addresses the Mac published in a snapshot — learn the ones this
    /// record does not already hold. `promote`'s sibling, and the answer to
    /// a Mac that moved: the pairing QR's list is frozen at pairing, so
    /// without this a changed LAN address left the phone reaching home
    /// nowhere and living on the relay for ever.
    ///
    /// **Additive only.** A stored address is never dropped on the strength
    /// of one reading — an interface that is down this second (a Mac on
    /// Ethernet, its Wi-Fi asleep) would otherwise cost the phone the very
    /// address it uses tomorrow — and the address on file keeps its place at
    /// the head of the walk, so a working route is never demoted. Publishing
    /// `record` restarts the client, so this returns without publishing when
    /// there is nothing new: every quiet poll carries these same addresses.
    func learnHosts(_ offered: [String]) {
        guard var current = record else { return }
        let known = Set(current.hosts)
        let fresh = offered
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && !known.contains($0) }
        guard !fresh.isEmpty else { return }
        current.hosts.append(contentsOf: fresh)
        // The published copy's counters may lag the Keychain's, exactly as
        // `promote` guards against: they are persisted without a publish.
        if let stored = Self.readKeychain(service: service, account: account) {
            current.sendCtr = max(current.sendCtr, stored.sendCtr)
            current.recvCtr = max(current.recvCtr, stored.recvCtr)
            current.homeSendCtr = max(current.homeSendCtr, stored.homeSendCtr)
            current.homeRecvCtr = max(current.homeRecvCtr, stored.homeRecvCtr)
        }
        try? Self.writeKeychain(current, service: service, account: account)
        record = current
    }

    func applyScan(_ payload: String) async {
        guard let data = payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let code = obj["code"] as? String else {
            error = "That code is not a Dark Army pairing QR."
            return
        }
        let single = (obj["host"] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var hosts = (obj["hosts"] as? [String] ?? []).filter { !$0.isEmpty }
        // An older Mac's QR carries only `host`; a newer one carries both.
        if hosts.isEmpty, !single.isEmpty { hosts = [single] }
        guard !hosts.isEmpty else {
            error = "That code is not a Dark Army pairing QR."
            return
        }
        let port = (obj["port"] as? Int)
            ?? (obj["port"] as? NSNumber)?.intValue
            ?? 0
        // The home key rides the square; an older Mac's QR has none and the
        // pair is refused in words before any request.
        await pair(hosts: hosts, port: port, code: code,
                   name: UIDevice.current.name,
                   homeKey: obj["home_key"] as? String ?? "")
    }

    private static func writeKeychain(_ record: PairingRecord, service: String,
                                      account: String) throws {
        let data = try JSONEncoder().encode(record)
        deleteKeychain(service: service, account: account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }

    private static func readKeychain(service: String, account: String) -> PairingRecord? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(PairingRecord.self, from: data)
    }

    private static func deleteKeychain(service: String, account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}

struct PairingRecord: Codable, Equatable {
    var token: String
    /// The address this phone is talking to now.
    var host: String
    /// Every address the Mac offered, best first, `host` among them — what
    /// the phone falls back to when the network moves under it.
    var hosts: [String]
    var port: Int
    var deviceId: String
    /// The away channel's shared secret, base64 — minted by the Mac at
    /// pairing time, held only here (Keychain, this device, unlocked) and in
    /// the Mac's own store. Empty means this record predates the away path:
    /// no key, no remote, and re-pairing at home is the upgrade.
    var relayKey: String
    /// The mailbox address the Mac had on file when this phone paired.
    var relayURL: String
    /// The socket relay's address the Mac had on file when this phone
    /// paired — the away link's fast lane. Empty means no socket: the
    /// mailbox alone, and re-pairing at home is how a phone gains it.
    var relayWSURL: String
    /// The sealed-frame counters, persisted so a relaunch cannot replay.
    var sendCtr: Int
    var recvCtr: Int
    /// The home path's shared secret, base64 — the key every request on the
    /// home Wi-Fi is sealed under. From the QR, or derived from the SRP
    /// exchange on a typed address. Empty means this record predates sealed
    /// home access: the Mac refuses it and the phone says to pair again.
    var homeKey: String
    /// The home channel's own counters, a separate sequence from the relay's.
    var homeSendCtr: Int
    var homeRecvCtr: Int
    /// Whether the home key arrived in the clear — true only on a record an
    /// older version of this app made by a typed-address pair; no route of
    /// this version sets it. Kept decodable, and said on the profile screen
    /// so somebody can pair again to replace that key.
    var homeKeyCrossedInClear: Bool

    init(token: String, host: String, hosts: [String] = [], port: Int,
         deviceId: String, relayKey: String = "", relayURL: String = "",
         relayWSURL: String = "",
         sendCtr: Int = 0, recvCtr: Int = 0, homeKey: String = "",
         homeSendCtr: Int = 0, homeRecvCtr: Int = 0,
         homeKeyCrossedInClear: Bool = false) {
        self.token = token
        self.host = host
        self.hosts = hosts.isEmpty ? [host] : hosts
        self.port = port
        self.deviceId = deviceId
        self.relayKey = relayKey
        self.relayURL = relayURL
        self.relayWSURL = relayWSURL
        self.sendCtr = sendCtr
        self.recvCtr = recvCtr
        self.homeKey = homeKey
        self.homeSendCtr = homeSendCtr
        self.homeRecvCtr = homeRecvCtr
        self.homeKeyCrossedInClear = homeKeyCrossedInClear
    }

    /// Hand-written, and that is load-bearing rather than style: the
    /// synthesized decode throws on a missing key **even where the property
    /// has a default**, so a record written before `hosts` existed would
    /// fail to decode, read as "no record", and throw every already-paired
    /// phone back to the QR screen. Absent decodes as `[host]`, and every
    /// relay field decodes tolerantly for the same reason — a pre-relay
    /// record simply has no away path.
    init(from decoder: Decoder) throws {
        let box = try decoder.container(keyedBy: CodingKeys.self)
        token = try box.decode(String.self, forKey: .token)
        host = try box.decode(String.self, forKey: .host)
        port = try box.decode(Int.self, forKey: .port)
        deviceId = try box.decode(String.self, forKey: .deviceId)
        let stored = try box.decodeIfPresent([String].self, forKey: .hosts) ?? []
        let list = stored.filter { !$0.isEmpty }
        hosts = list.isEmpty ? [host] : list
        relayKey = (try? box.decodeIfPresent(String.self, forKey: .relayKey)) ?? ""
        relayURL = (try? box.decodeIfPresent(String.self, forKey: .relayURL)) ?? ""
        relayWSURL = (try? box.decodeIfPresent(String.self, forKey: .relayWSURL)) ?? ""
        sendCtr = (try? box.decodeIfPresent(Int.self, forKey: .sendCtr)) ?? 0
        recvCtr = (try? box.decodeIfPresent(Int.self, forKey: .recvCtr)) ?? 0
        // A record from before sealed home access has none of these: empty
        // key, zero counters — and `PhoneClient.start` says to pair again.
        homeKey = (try? box.decodeIfPresent(String.self, forKey: .homeKey)) ?? ""
        homeSendCtr = (try? box.decodeIfPresent(Int.self, forKey: .homeSendCtr)) ?? 0
        homeRecvCtr = (try? box.decodeIfPresent(Int.self, forKey: .homeRecvCtr)) ?? 0
        homeKeyCrossedInClear = (try? box.decodeIfPresent(
            Bool.self, forKey: .homeKeyCrossedInClear)) ?? false
    }
}

struct PairingView: View {
    @ObservedObject var store: PairingStore
    @State private var host = ""
    @State private var port = "19875"
    @State private var code = ""
    /// Which of the three wells has the keyboard, so its frame can brighten.
    @FocusState private var focus: String?

    var body: some View {
        VStack(spacing: 16) {
            PromptLine(path: "~", command: "pair", size: 13)
            Text("Scan the code on your Mac, or type the host, port and code.")
                .font(Theme.mono(11))
                .multilineTextAlignment(.center)
                .foregroundStyle(Theme.dim)
            ScannerView { payload in
                Task { await store.applyScan(payload) }
            }
            .frame(height: 240)
            .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
            // A viewfinder has nothing to read out, and the three fields
            // below are the whole route for somebody who cannot aim it.
            .accessibilityHidden(true)
            TextField("", text: $host,
                      prompt: Text("Host").foregroundStyle(Theme.faint))
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.asciiCapable)
                .focused($focus, equals: "host")
                .fieldWell(focused: focus == "host", onTap: { focus = "host" })
                .accessibilityLabel("Host")
            TextField("", text: $port,
                      prompt: Text("Port").foregroundStyle(Theme.faint))
                .keyboardType(.numberPad)
                .focused($focus, equals: "port")
                .fieldWell(focused: focus == "port", onTap: { focus = "port" })
                .accessibilityLabel("Port")
            TextField("", text: $code,
                      prompt: Text("Code").foregroundStyle(Theme.faint))
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
                .focused($focus, equals: "code")
                .fieldWell(focused: focus == "code", onTap: { focus = "code" })
                .accessibilityLabel("Code")
            if !store.error.isEmpty {
                Text(store.error)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.alarm)
                    .multilineTextAlignment(.center)
            }
            DecryptButton(store.busy ? "PAIRING…" : "PAIR") {
                Task {
                    await store.pair(host: host,
                                     port: Int(port) ?? 0,
                                     code: code,
                                     name: UIDevice.current.name)
                }
            }
            .disabled(store.busy)
            .buttonStyle(AlarmOutline())
            .accessibilityLabel(store.busy ? "Pairing" : "Pair with this Mac")
        }
        .font(Theme.mono(13))
        .foregroundStyle(Theme.phosphorBright)
        .tint(Theme.phosphor)
        .textFieldStyle(.plain)
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        .decryptSurface("pairing")
    }
}

/// Camera preview that reports the first QR payload, then stops.
struct ScannerView: UIViewControllerRepresentable {
    var onCode: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let controller = ScannerController()
        controller.onCode = onCode
        return controller
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {
        controller.onCode = onCode
    }
}

final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onCode: ((String) -> Void)?
    private let session = AVCaptureSession()
    private var preview: AVCaptureVideoPreviewLayer?
    private var handled = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Theme.uiBg
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else { return }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]
        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        layer.frame = view.bounds
        view.layer.addSublayer(layer)
        preview = layer
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        DispatchQueue.global(qos: .userInitiated).async { [session] in
            if !session.isRunning { session.startRunning() }
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if session.isRunning { session.stopRunning() }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput,
                        didOutput metadataObjects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !handled,
              let obj = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
              let value = obj.stringValue, !value.isEmpty else { return }
        handled = true
        session.stopRunning()
        onCode?(value)
    }
}
