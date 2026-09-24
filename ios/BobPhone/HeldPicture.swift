import Foundation

/// The last picture the phone decoded — the bytes of the last full `state`
/// answer — kept on disk so a cold launch with the Mac out of reach draws
/// the board and the fleet as they were, under an "as of" line, instead of
/// nothing.
///
/// `CardCacheStore`'s discipline throughout: Application Support, every
/// write `[.atomic, .completeFileProtection]` with the encode **and** the
/// write inside `Task.detached`, `load()` once behind the Face ID gate, the
/// record stamped with the pairing it came from and dropped by `adopt` on a
/// pairing swap. The store never decodes the body: `PhoneClient
/// .restoreHeldPicture` hands it to the same `Snapshot` decoder
/// `applyState` uses, so nothing is re-derived.
///
/// What the picture is **not**: evidence. A restore fires none of the live
/// machinery (`onLive`, receipts, the widget), sets no `status` and quotes
/// no digest — the first check-in after a launch always asks for the whole
/// thing.
@MainActor
final class HeldPictureStore {
    struct Record: Codable {
        var pairingToken: String = ""
        /// Phone clock, seconds since 1970 — the "as of" line's anchor.
        var savedAt: Double = 0
        var body: Data = Data()
    }

    static let fileName = "held-picture.json"
    /// Two writes never come closer than this; a busy Mac changing the
    /// digest every frame would otherwise rewrite ~80 KB four times a
    /// second.
    static let minWriteInterval: TimeInterval = 15

    private let directory: URL?
    private var record: Record?
    private var lastDigest = ""
    private var lastWrite: Date = .distantPast
    private let fm = FileManager.default
    /// The write in flight, so a test (and `forget`) can wait for it rather
    /// than guess at a sleep; nil when nothing is being written.
    private(set) var pendingWrite: Task<Void, Never>?

    init(directory: URL? = nil) {
        self.directory = directory ?? (try? FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true))
    }

    private var url: URL? { directory?.appendingPathComponent(Self.fileName) }

    /// Read behind the Face ID gate, never before it.
    func load() {
        guard let url, let data = try? Data(contentsOf: url),
              let decoded = try? JSONDecoder().decode(Record.self, from: data) else { return }
        record = decoded
    }

    /// Keep this body as the picture. Skipped when the digest has not moved
    /// and floored at `minWriteInterval`; an older Mac's empty digest reads
    /// as always changed and falls to the floor alone.
    func remember(body: Data, digest: String, token: String) {
        guard let url, !token.isEmpty else { return }
        let now = Date()
        if !digest.isEmpty, digest == lastDigest { return }
        if now.timeIntervalSince(lastWrite) < Self.minWriteInterval { return }
        lastDigest = digest
        lastWrite = now
        let fresh = Record(pairingToken: token, savedAt: now.timeIntervalSince1970,
                           body: body)
        record = fresh
        pendingWrite = Task.detached(priority: .utility) {
            guard let data = try? JSONEncoder().encode(fresh) else { return }
            do {
                try data.write(to: url, options: [.atomic])
                // Best effort: a store that refuses the protection class
                // (the simulator's temporary folder) keeps the picture.
                try? FileManager.default.setAttributes(
                    [.protectionKey: FileProtectionType.complete], ofItemAtPath: url.path)
            } catch {
                // A picture that cannot be written is not a reason to lose
                // the one in memory — `OutboxStore.write`'s rule.
            }
        }
    }

    /// Wait for the write in flight, if any.
    func settle() async {
        await pendingWrite?.value
        pendingWrite = nil
    }

    /// The held body for this pairing, or nil when the file is another
    /// Mac's or there is none.
    func restore(for token: String) -> (body: Data, savedAt: Date)? {
        guard let record, !token.isEmpty, record.pairingToken == token,
              !record.body.isEmpty else { return nil }
        return (record.body, Date(timeIntervalSince1970: record.savedAt))
    }

    /// A pairing became the current one: a picture stamped with another
    /// Mac's token goes, file and all, before its first poll.
    func adopt(_ token: String) {
        guard !token.isEmpty else { return }
        if let record, record.pairingToken != token { forget() }
    }

    func forget() {
        record = nil
        lastDigest = ""
        // A write still landing would otherwise put the file back.
        pendingWrite?.cancel()
        pendingWrite = nil
        if let url { try? fm.removeItem(at: url) }
    }
}
