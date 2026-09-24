import Foundation
import UserNotifications

/// One buzz this phone saw: the two lines iOS drew, when it arrived, whether
/// it was tapped, and the opaque ids the push carried — the receipt the
/// router keys on and the subject (`session_id` / `card_id`) the Mac joined
/// so the tap can open the card or the agent from the picture the phone
/// already holds. `kind` is `""` until a fetched receipt page fills it: the
/// payload carries no kind key (`push.js` turns it into a sound), so a buzz
/// can never say its own kind.
///
/// `from` reads exactly title, subtitle and the three ids off the
/// notification and nothing else — no `act`, no `request_id`, no words the
/// phone could act on. A buzz from a Mac older than receipts has an empty
/// `receiptId`; its `id` is then a UUID minted at bank time, so two such
/// buzzes never merge.
struct NotificationLogEntry: Codable, Identifiable, Equatable {
    var id: String
    var receiptId = ""
    var title = ""
    var subtitle = ""
    var sessionId = ""
    var cardId = ""
    var kind = ""
    /// `UNNotification.date`, seconds since 1970 — when iOS delivered it.
    var seenAt: Double = 0
    var tapped = false

    static func from(_ notification: UNNotification, tapped: Bool) -> NotificationLogEntry {
        from(content: notification.request.content, date: notification.date, tapped: tapped)
    }

    /// The reader itself, over the content alone — a test can build a
    /// `UNMutableNotificationContent`; nobody can build a `UNNotification`.
    static func from(content: UNNotificationContent, date: Date, tapped: Bool) -> NotificationLogEntry {
        let info = content.userInfo
        let receipt = (info["receipt_id"] as? String ?? "").lowercased()
        return NotificationLogEntry(
            id: receipt.isEmpty ? UUID().uuidString.lowercased() : receipt,
            receiptId: receipt,
            title: content.title,
            subtitle: content.subtitle,
            sessionId: info["session_id"] as? String ?? "",
            cardId: info["card_id"] as? String ?? "",
            kind: "",
            seenAt: date.timeIntervalSince1970,
            tapped: tapped)
    }

    /// The row as one sentence, from the fields the row draws and nothing
    /// else: the title, the second line, and how long ago it arrived.
    func spoken(now: Double) -> String {
        let age = FleetAge.spoken(startedAt: seenAt, now: now)
        return [title, subtitle, age.isEmpty ? "" : "\(age) ago", kind]
            .filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

/// Tolerant decoding, in an extension so the memberwise initialiser stays:
/// Swift's synthesized `Decodable` throws on one missing key even when the
/// property has a default, and a `notification-log.json` written by an
/// older or newer build must never blank the whole log. `id` falls back to
/// the receipt, then to a fresh UUID (`from`'s rule for a legacy buzz).
extension NotificationLogEntry {
    enum CodingKeys: String, CodingKey {
        case id, receiptId, title, subtitle, sessionId, cardId, kind, seenAt, tapped
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        receiptId = c.value(.receiptId, "")
        let stored = c.value(.id, "")
        id = stored.isEmpty ? (receiptId.isEmpty ? UUID().uuidString.lowercased() : receiptId) : stored
        title = c.value(.title, "")
        subtitle = c.value(.subtitle, "")
        sessionId = c.value(.sessionId, "")
        cardId = c.value(.cardId, "")
        kind = c.value(.kind, "")
        seenAt = c.value(.seenAt, 0)
        tapped = c.value(.tapped, false)
    }
}

/// The buzzes this phone has seen, on disk — `HeldPictureStore`'s
/// discipline throughout: Application Support, every write `[.atomic]` then
/// `.protectionKey: .complete` inside `Task.detached`, `load()` once behind
/// the Face ID gate, the record stamped with the pairing and dropped by
/// `adopt` on a pairing swap or `forget` on an un-pair.
///
/// Four sources feed it through `absorb`: the buzz that was tapped, the ones
/// waiting in the tray when the app opened (`PushRegistrar.clearBadge` reads
/// them before it clears), the ones the Mac's own quiet word clears
/// (`clearDeliveredIfQuiet`), and the ones that arrive while the app is open
/// (`willPresent`). Bounded at `maxEntries` and `maxAge`; merged by receipt
/// so one buzz seen four ways is one row. What it is **not**: evidence. It
/// marks nothing read on the Mac and quotes no digest.
@MainActor
final class NotificationLogStore {
    struct Record: Codable {
        var pairingToken: String = ""
        var entries: [NotificationLogEntry] = []

        enum CodingKeys: String, CodingKey { case pairingToken, entries }

        init(pairingToken: String = "", entries: [NotificationLogEntry] = []) {
            self.pairingToken = pairingToken
            self.entries = entries
        }

        /// A record missing a key reads as that key's default — one absent
        /// field never blanks the log (`CachedCard`'s rule).
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            pairingToken = c.value(.pairingToken, "")
            entries = c.value(.entries, [])
        }
    }

    static let fileName = "notification-log.json"
    static let maxEntries = 200
    static let maxAge: TimeInterval = 30 * 86400

    private let directory: URL?
    private var record = Record()
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

    /// Newest first.
    var entries: [NotificationLogEntry] {
        record.entries.sorted { $0.seenAt > $1.seenAt }
    }

    /// True once `load()` has run behind the gate: only then may a bank made
    /// while the app is open be absorbed straight away, because an absorb
    /// before the read would write the fresh entries over the file's own.
    private(set) var loaded = false

    /// Read behind the Face ID gate, never before it — and once per
    /// process. The gate re-runs on every unlock while the foreground
    /// drain, `enrich` and the poller keep writing between them, and a
    /// `write()` is a detached task chained behind the one before: a
    /// re-read while one is pending would take the older file, drop the
    /// entries just absorbed (already drained from the bank) and the
    /// gate's next absorb would write that older record over the pending
    /// one. Memory is the truth once read: `forget()` and `adopt` remove
    /// the file rather than replace it, and no other process writes it
    /// (`BackgroundRefresh` and `LockScreenActions` run fresh clients that
    /// never load or bank).
    func load() {
        guard !loaded else { return }
        loaded = true
        guard let url, let data = try? Data(contentsOf: url),
              let decoded = try? JSONDecoder().decode(Record.self, from: data) else { return }
        record = decoded
    }

    func entry(for receiptId: String) -> NotificationLogEntry? {
        let wanted = receiptId.lowercased()
        guard !wanted.isEmpty else { return nil }
        return record.entries.first { $0.receiptId == wanted }
    }

    /// Merge what was seen into the log for this pairing. An entry with the
    /// same receipt as one already held keeps the earliest `seenAt`, ORs
    /// `tapped` and keeps whichever field was non-empty first; then the log
    /// is pruned by age and count, stamped with the token and written. An
    /// empty token means no pairing to scope to: nothing is kept.
    func absorb(_ fresh: [NotificationLogEntry], token: String, now: Date = Date()) {
        guard !token.isEmpty else { return }
        if !record.pairingToken.isEmpty, record.pairingToken != token { forget() }
        var held = record.entries
        for entry in fresh {
            if let index = held.firstIndex(where: { $0.id == entry.id }) {
                held[index] = Self.merged(held[index], entry)
            } else {
                held.append(entry)
            }
        }
        let floor = now.timeIntervalSince1970 - Self.maxAge
        held = held.filter { $0.seenAt >= floor }
            .sorted { $0.seenAt > $1.seenAt }
        if held.count > Self.maxEntries { held = Array(held.prefix(Self.maxEntries)) }
        record = Record(pairingToken: token, entries: held)
        write()
    }

    private static func merged(_ old: NotificationLogEntry, _ new: NotificationLogEntry) -> NotificationLogEntry {
        var out = old
        out.seenAt = new.seenAt > 0 && (old.seenAt == 0 || new.seenAt < old.seenAt) ? new.seenAt : old.seenAt
        out.tapped = old.tapped || new.tapped
        if out.title.isEmpty { out.title = new.title }
        if out.subtitle.isEmpty { out.subtitle = new.subtitle }
        if out.sessionId.isEmpty { out.sessionId = new.sessionId }
        if out.cardId.isEmpty { out.cardId = new.cardId }
        if out.kind.isEmpty { out.kind = new.kind }
        return out
    }

    /// A fetched receipt page names the buzz's kind, title and ids in the
    /// Mac's own words; fill only the fields the entry still lacks. The
    /// subject — `sessionId` / `cardId` — is taken only from a page of
    /// exactly one item: a collapsed "N agents need you" receipt spans N
    /// decisions and is about none of them in particular, so binding it to
    /// the first item's card would open the wrong card from the held picture.
    func enrich(receiptId: String, page: CatchUpPage) {
        guard page.available, let item = page.items.first,
              let index = record.entries.firstIndex(where: { $0.receiptId == receiptId.lowercased() && !receiptId.isEmpty }) else { return }
        var entry = record.entries[index]
        if entry.kind.isEmpty { entry.kind = item.kind }
        if entry.title.isEmpty { entry.title = item.title }
        if page.items.count == 1 {
            if entry.sessionId.isEmpty { entry.sessionId = item.sessionId }
            if entry.cardId.isEmpty { entry.cardId = item.cardId }
        }
        guard entry != record.entries[index] else { return }
        record.entries[index] = entry
        write()
    }

    /// A pairing became the current one: a log stamped with another Mac's
    /// token goes, file and all, before its first buzz is absorbed.
    func adopt(_ token: String) {
        guard !token.isEmpty else { return }
        if !record.pairingToken.isEmpty, record.pairingToken != token { forget() }
    }

    func forget() {
        record = Record()
        // A write still landing would otherwise put the file back.
        pendingWrite?.cancel()
        pendingWrite = nil
        if let url { try? fm.removeItem(at: url) }
    }

    /// Wait for the write in flight, if any.
    func settle() async {
        await pendingWrite?.value
        pendingWrite = nil
    }

    private func write() {
        guard let url else { return }
        let fresh = record
        let previous = pendingWrite
        pendingWrite = Task.detached(priority: .utility) {
            // Writes land in order: the one before this finishes first.
            await previous?.value
            guard !Task.isCancelled, let data = try? JSONEncoder().encode(fresh) else { return }
            do {
                try data.write(to: url, options: [.atomic])
                // Best effort: a store that refuses the protection class
                // (the simulator's temporary folder) keeps the log.
                try? FileManager.default.setAttributes(
                    [.protectionKey: FileProtectionType.complete], ofItemAtPath: url.path)
            } catch {
                // A log that cannot be written is not a reason to lose the
                // one in memory — `OutboxStore.write`'s rule.
            }
        }
    }
}
