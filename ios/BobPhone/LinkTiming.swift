import Foundation
import Combine

/// How long the phone's own requests take, leg by leg — the phone's half
/// of the link timing whose Mac half is `link_timing.py` and the `timing`
/// lines on the access log.
///
/// One `LinkTimingSample` per completed request on either route: when it
/// was sent, which way it went, what kind it was, the Mac's inner status,
/// how long the POST took (to the mailbox away; the whole round trip at
/// home), how long the whole trip took, and — where the Mac echoed them
/// on the reply envelope — the Mac's own legs. The store keeps the newest
/// `capacity` on disk under Application Support with `HeldPictureStore`'s
/// discipline (the encode and the write off the main actor, atomic,
/// complete protection) and drops them on un-pair. `LinkTimingSummary`
/// is the Foundation-only arithmetic the access-log screen draws.
///
/// **Nothing here builds a URL or holds a key, an address or a
/// credential.** A sample is a route word, a kind word, a status and
/// seconds; the file is stamped with the pairing-generation token — the
/// same stamp the held picture, the card cache and the receipts carry —
/// so a ring from another Mac is recognised and dropped on `adopt`.
struct LinkTimingSample: Codable, Equatable {
    /// Seconds since 1970, phone clock, when the request was sent.
    var at: Double = 0
    /// `LinkTimingRoute.home` or `LinkTimingRoute.away`.
    var route = ""
    /// The sealed kind: `state`, `usage`, `log`, `action`, …
    var kind = ""
    /// The Mac's inner status (0 for an answer that never named one).
    var status = 0
    /// Send to answer, whole.
    var totalSeconds: Double = 0
    /// Away: until the mailbox took the frame. Home: the whole POST.
    var postSeconds: Double = 0
    /// The Mac's legs, from the reply envelope's `timing`; 0 where the Mac
    /// (an older one, or the home door for `dwell` / `hold`) sent none.
    var macDwell: Double = 0
    var macHold: Double = 0
    var macRun: Double = 0
    var macOpen: Double = 0

    /// What is left of the trip once the post, the mailbox dwell and the
    /// Mac's run are taken out: the answer's way back plus the polling
    /// quantisation on the phone's side. An estimate — `macDwell` spans
    /// two clocks — clamped at zero and labelled `(est.)` wherever drawn.
    var backEstimate: Double {
        max(0, totalSeconds - postSeconds - macDwell - macRun)
    }

    init() {}

    init(at: Double, route: String, kind: String, status: Int,
         totalSeconds: Double, postSeconds: Double,
         macTiming: [String: Double] = [:]) {
        self.at = at
        self.route = route
        self.kind = kind
        self.status = status
        self.totalSeconds = max(0, totalSeconds)
        self.postSeconds = max(0, postSeconds)
        self.macDwell = max(0, macTiming["dwell"] ?? 0)
        self.macHold = max(0, macTiming["hold"] ?? 0)
        self.macRun = max(0, macTiming["run"] ?? 0)
        self.macOpen = max(0, macTiming["open"] ?? 0)
    }

    enum CodingKeys: String, CodingKey {
        case at, route, kind, status, totalSeconds, postSeconds
        case macDwell, macHold, macRun, macOpen
    }

    /// Tolerant: a sample written by a build that knew fewer legs — or a
    /// torn value — reads 0 for the leg, never throws the file away.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        at = (try? c.decodeIfPresent(Double.self, forKey: .at)) ?? 0
        route = (try? c.decodeIfPresent(String.self, forKey: .route)) ?? ""
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? ""
        status = (try? c.decodeIfPresent(Int.self, forKey: .status)) ?? 0
        totalSeconds = (try? c.decodeIfPresent(Double.self, forKey: .totalSeconds)) ?? 0
        postSeconds = (try? c.decodeIfPresent(Double.self, forKey: .postSeconds)) ?? 0
        macDwell = (try? c.decodeIfPresent(Double.self, forKey: .macDwell)) ?? 0
        macHold = (try? c.decodeIfPresent(Double.self, forKey: .macHold)) ?? 0
        macRun = (try? c.decodeIfPresent(Double.self, forKey: .macRun)) ?? 0
        macOpen = (try? c.decodeIfPresent(Double.self, forKey: .macOpen)) ?? 0
    }
}

/// The three route words a sample carries; what the screen draws for each.
/// `socket` is the away link's fast lane (`RelaySocket.swift`): the same
/// kinds of trip as `away`, plus `push` — a picture the Mac sent unasked,
/// whose one figure is how old it was on arrival.
enum LinkTimingRoute {
    static let home = "home"
    static let away = "away"
    static let socket = "ws"
}

/// The phone-side stamps a channel collects while one request runs, so
/// the sample can be composed once the answer is in hand.
struct LinkLegs: Equatable {
    var sentAt: Date?
    var postSeconds: Double = 0
    var totalSeconds: Double = 0
}

/// How many of the newest trips the access-log screen lists one per row.
enum LinkTimingView {
    static let shownSamples = 20
}

/// The ring of samples, on disk. `record` appends and trims; `forget`
/// drops the file with the pairing; `adopt` stamps the ring with the
/// pairing it belongs to and drops another Mac's. `loadIfNeeded` reads
/// the file once, the first time anything asks — after the Face ID gate
/// by construction, because nothing records or draws before it.
///
/// Writes are **floored and serialised**: a busy phone records a sample
/// every few seconds and the ring is ~100 KB, so at most one write per
/// `minWriteInterval` lands, always carrying the ring as it stands at that
/// moment, through one chained task so an older ring can never overtake
/// a newer one on disk. `forget` cancels whatever is pending and the write
/// re-checks cancellation before and after touching the file, so a
/// removed file is never put back.
@MainActor
final class LinkTimingStore: ObservableObject {
    struct Record: Codable {
        var pairingToken: String = ""
        var samples: [LinkTimingSample] = []
    }

    static let capacity = 500
    static let fileName = "link-timing.json"
    /// Two writes never come closer than this — `HeldPictureStore`'s floor.
    nonisolated static let defaultMinWriteInterval: TimeInterval = 15

    @Published private(set) var samples: [LinkTimingSample] = []

    let minWriteInterval: TimeInterval
    private let directory: URL?
    private var loaded = false
    private var token = ""
    private var lastWrite: Date = .distantPast
    private var flushScheduled = false
    private let fm = FileManager.default
    /// The write in flight, so a test (and `forget`) can wait for it.
    private(set) var pendingWrite: Task<Void, Never>?

    init(directory: URL? = nil,
         minWriteInterval: TimeInterval = LinkTimingStore.defaultMinWriteInterval) {
        self.directory = directory ?? (try? FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true))
        self.minWriteInterval = minWriteInterval
    }

    private var url: URL? { directory?.appendingPathComponent(Self.fileName) }

    /// Read the file once. A missing, torn or foreign file is an empty
    /// ring; a ring stamped with another pairing is dropped on `adopt`.
    func loadIfNeeded() {
        guard !loaded else { return }
        loaded = true
        guard let url, let data = try? Data(contentsOf: url),
              let decoded = try? JSONDecoder().decode(Record.self, from: data)
        else { return }
        if !token.isEmpty, decoded.pairingToken != token {
            forgetRing()
            return
        }
        if token.isEmpty { token = decoded.pairingToken }
        samples = Array(decoded.samples.suffix(Self.capacity))
    }

    /// A pairing became the current one: a ring stamped with another
    /// Mac's token goes, file and all, before its first trip is recorded.
    func adopt(_ token: String) {
        guard !token.isEmpty else { return }
        loadIfNeeded()
        if !self.token.isEmpty, self.token != token { forgetRing() }
        self.token = token
    }

    /// One more trip: appended, the oldest dropped past `capacity`, the
    /// ring written off the actor no sooner than `minWriteInterval` after
    /// the last write.
    func record(_ sample: LinkTimingSample) {
        loadIfNeeded()
        samples.append(sample)
        if samples.count > Self.capacity {
            samples.removeFirst(samples.count - Self.capacity)
        }
        scheduleWrite()
    }

    /// The pairing is gone: the samples go with it, file and all.
    func forget() {
        forgetRing()
        token = ""
    }

    private func forgetRing() {
        samples = []
        loaded = true
        flushScheduled = false
        // A write still landing would otherwise put the file back; the
        // write checks cancellation before and after touching the file.
        pendingWrite?.cancel()
        pendingWrite = nil
        if let url { try? fm.removeItem(at: url) }
    }

    /// Wait for the write in flight, if any.
    func settle() async {
        await pendingWrite?.value
        pendingWrite = nil
    }

    private func scheduleWrite() {
        guard url != nil, !flushScheduled else { return }
        flushScheduled = true
        let wait = max(0, minWriteInterval - Date().timeIntervalSince(lastWrite))
        let previous = pendingWrite
        pendingWrite = Task { [weak self] in
            _ = await previous?.value
            if wait > 0 {
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
            guard let self, !Task.isCancelled, let url = self.url else { return }
            self.flushScheduled = false
            self.lastWrite = Date()
            let ring = Record(pairingToken: self.token, samples: self.samples)
            await Self.write(ring, to: url)
        }
    }

    /// The encode and the write, off the main actor (a nonisolated async
    /// function runs on the generic executor), inside the scheduling
    /// task so `forget`'s cancellation is seen on both sides of the write.
    private nonisolated static func write(_ ring: Record, to url: URL) async {
        guard !Task.isCancelled, let data = try? JSONEncoder().encode(ring) else { return }
        guard !Task.isCancelled else { return }
        do {
            try data.write(to: url, options: [.atomic, .completeFileProtection])
            // Best effort: a store that refuses the protection class
            // (the simulator's temporary folder) keeps the ring.
            try? FileManager.default.setAttributes(
                [.protectionKey: FileProtectionType.complete], ofItemAtPath: url.path)
        } catch {
            // A ring that cannot be written is not a reason to lose
            // the one in memory — `HeldPictureStore`'s rule.
        }
        if Task.isCancelled {
            // `forget` landed while the bytes were going down: the file
            // it removed must not come back.
            try? FileManager.default.removeItem(at: url)
        }
    }
}

/// The arithmetic the access-log screen draws: percentiles, one summary
/// line per route × kind, one row per trip. Foundation only.
enum LinkTimingSummary {
    /// Nearest-rank percentile — `link_timing.percentile`'s rule: the
    /// value at rank `ceil(q/100 × n)` of the sorted values, so one sample
    /// is itself, two give the lower at 50 and the upper at 90. 0 of none.
    static func percentile(_ values: [Double], _ q: Double) -> Double {
        let ordered = values.sorted()
        guard !ordered.isEmpty else { return 0 }
        let share = min(1, max(0, q / 100))
        let rank = Int((share * Double(ordered.count)).rounded(.up))
        return ordered[max(0, min(ordered.count, rank) - 1)]
    }

    /// Seconds in the words a person reads: two figures, a floor under a
    /// thousandth, never scientific notation.
    static func seconds(_ value: Double) -> String {
        let s = max(0, value)
        if s < 0.0005 { return "<0.001 s" }
        if s >= 100 { return String(format: "%.0f s", s) }
        if s >= 10 { return String(format: "%.0f s", s) }
        if s >= 1 { return String(format: "%.1f s", s) }
        if s >= 0.1 { return String(format: "%.2f s", s) }
        return String(format: "%.3f s", s)
    }

    /// One line per route × kind, routes ordered away, socket, then home,
    /// kinds by name: `away · state · 24 trips · 2.9 s typical · 5.1 s slow`
    /// — so the socket's figures sit beside the mailbox's like for like.
    static func lines(_ samples: [LinkTimingSample]) -> [String] {
        var groups: [String: [String: [Double]]] = [:]
        for sample in samples {
            groups[sample.route, default: [:]][sample.kind, default: []]
                .append(sample.totalSeconds)
        }
        let named = [LinkTimingRoute.away, LinkTimingRoute.socket, LinkTimingRoute.home]
        let routes = named + groups.keys.filter { !named.contains($0) }.sorted()
        var out: [String] = []
        for route in routes {
            guard let kinds = groups[route] else { continue }
            for kind in kinds.keys.sorted() {
                let totals = kinds[kind] ?? []
                guard !totals.isEmpty else { continue }
                let trips = totals.count == 1 ? "1 trip" : "\(totals.count) trips"
                out.append("\(route) · \(kind.isEmpty ? "?" : kind) · \(trips) · "
                           + "\(seconds(percentile(totals, 50))) typical · "
                           + "\(seconds(percentile(totals, 90))) slow")
            }
        }
        return out
    }

    private static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    /// One trip on one row: the clock, the route, the kind, the whole
    /// trip, then its legs. The Mac's legs are drawn only where it sent
    /// them; the back leg is always an estimate and says so.
    static func format(_ sample: LinkTimingSample) -> String {
        let when = sample.at > 0
            ? clock.string(from: Date(timeIntervalSince1970: sample.at)) : "--:--:--"
        if sample.kind == "push" {
            // A pushed picture has no legs of its own: the one figure is
            // how old it was when it arrived.
            return "\(when) · \(sample.route) · push · "
                + "\(seconds(sample.totalSeconds)) — age \(bare(sample.totalSeconds)) on arrival"
        }
        var legs = ["post \(bare(sample.postSeconds))"]
        if sample.route == LinkTimingRoute.away {
            legs.append("pickup \(bare(sample.macDwell))")
        }
        legs.append("Mac \(bare(sample.macRun))")
        legs.append("back \(bare(sample.backEstimate)) (est.)")
        let status = sample.status == 200 || sample.status == 0 ? "" : " · \(sample.status)"
        return "\(when) · \(sample.route) · \(sample.kind.isEmpty ? "?" : sample.kind)"
            + "\(status) · \(seconds(sample.totalSeconds)) — "
            + legs.joined(separator: " · ")
    }

    /// `seconds` without its unit, for the legs of one row.
    static func bare(_ value: Double) -> String {
        let text = seconds(value)
        return text.hasSuffix(" s") ? String(text.dropLast(2)) : text
    }
}
