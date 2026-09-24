import Combine
import Foundation

struct CachedConversation: Codable {
    var sessionId: String = ""
    var provider: String = ""
    var key: String = ""
    var nextSeq: Int = 0
    var total: Int = 0
    var turns: [ConversationTurn] = []
    var updatedAt: Double = 0
    var pairingToken: String = ""
}

@MainActor
final class ConversationCacheStore: ObservableObject {
    @Published private(set) var conversations: [String: CachedConversation] = [:]

    static let fileName = "conversations.json"
    static let maxAge: TimeInterval = 5 * 86400
    static let maxTurnsPerSession = 5000

    private(set) var pairing = ""
    private let fm = FileManager.default
    private let directory: URL?
    private var pendingWrite: Task<Void, Never>?
    /// Sessions whose held turns are newer than their file. A catch-up
    /// applies a page per hop; the file is written once, when the page
    /// says `more == false` or the catch-up flushes — not per page.
    private var dirty: Set<String> = []
    /// Sessions whose cursor was jumped ahead (`skipAhead`): the next page
    /// replaces the held turns, whatever its own `reset` says.
    private var resetOnNext: Set<String> = []

    init(directory: URL? = nil) {
        self.directory = directory ?? (try? fm.url(
            for: .applicationSupportDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true))
    }

    private var indexURL: URL? {
        directory?.appendingPathComponent(Self.fileName)
    }

    private var folderURL: URL? {
        directory?.appendingPathComponent("conversations", isDirectory: true)
    }

    private func sessionURL(_ sessionId: String) -> URL? {
        guard !sessionId.isEmpty, !sessionId.contains("/"),
              !sessionId.contains("..") else { return nil }
        return folderURL?.appendingPathComponent("\(sessionId).json")
    }

    func load() {
        prune(now: Date().timeIntervalSince1970)
        guard let indexURL, let data = try? Data(contentsOf: indexURL),
              let index = try? JSONDecoder().decode([String: Double].self, from: data)
        else { return }
        var out: [String: CachedConversation] = [:]
        let now = Date().timeIntervalSince1970
        for (sid, updated) in index {
            if now - updated > Self.maxAge {
                if let url = sessionURL(sid) { try? fm.removeItem(at: url) }
                continue
            }
            guard let url = sessionURL(sid),
                  let bytes = try? Data(contentsOf: url),
                  let row = try? JSONDecoder().decode(CachedConversation.self, from: bytes)
            else { continue }
            out[sid] = row
        }
        conversations = out
        persistIndex()
    }

    func apply(_ page: ConversationPage, session: String) {
        guard !session.isEmpty, page.available else { return }
        var held = conversations[session] ?? CachedConversation(sessionId: session)
        let reset = page.reset || resetOnNext.contains(session)
        if !reset, !held.key.isEmpty, page.key != held.key { return }
        resetOnNext.remove(session)
        // `landed`: a turn or the key changed — the file and `updatedAt`
        // move. `changed`: anything at all — the held row is published.
        var landed = false
        if reset {
            held.turns = page.turns
            landed = true
        } else {
            var seen = Set(held.turns.map(\.seq))
            for turn in page.turns where turn.seq >= held.nextSeq && !seen.contains(turn.seq) {
                held.turns.append(turn)
                seen.insert(turn.seq)
                landed = true
            }
        }
        if held.key != page.key { held.key = page.key; landed = true }
        var changed = landed
        if held.nextSeq != page.nextSeq { held.nextSeq = page.nextSeq; changed = true }
        if held.total != page.total { held.total = page.total; changed = true }
        if held.provider != page.provider { held.provider = page.provider; changed = true }
        if held.sessionId != session { held.sessionId = session; changed = true }
        if held.pairingToken != pairing { held.pairingToken = pairing; changed = true }
        if held.turns.count > Self.maxTurnsPerSession {
            let drop = held.turns.count - Self.maxTurnsPerSession
            held.turns.removeFirst(drop)
            landed = true
            changed = true
        }
        if landed {
            held.updatedAt = Date().timeIntervalSince1970
            dirty.insert(session)
        }
        // Assigned only on a change: every assignment publishes, and the
        // screen re-diffs a ForEach of up to 5 000 rows on each publish.
        if changed { conversations[session] = held }
        if !page.more { flush(session) }
        prune(now: Date().timeIntervalSince1970)
    }

    /// Move the cursor to `seq` so the next page is the tail, and mark
    /// that page a reset: the turns before it are not kept on this phone.
    func skipAhead(to seq: Int, session: String) {
        guard var held = conversations[session], seq > held.nextSeq else { return }
        held.nextSeq = seq
        conversations[session] = held
        resetOnNext.insert(session)
    }

    /// Write every session newer than its file — one write each, in order.
    func flush() {
        for sid in dirty { flush(sid) }
    }

    private func flush(_ session: String) {
        guard dirty.remove(session) != nil, let held = conversations[session] else { return }
        persistSession(held)
    }

    func nextSeq(_ session: String) -> Int {
        conversations[session]?.nextSeq ?? 0
    }

    func key(_ session: String) -> String {
        conversations[session]?.key ?? ""
    }

    func turns(_ session: String) -> [ConversationTurn] {
        conversations[session]?.turns ?? []
    }

    func adopt(_ token: String) {
        guard !token.isEmpty else { return }
        pairing = token
        var dropped = false
        for (sid, row) in conversations
        where !row.pairingToken.isEmpty && row.pairingToken != token {
            conversations[sid] = nil
            dropped = true
            if let url = sessionURL(sid) { try? fm.removeItem(at: url) }
        }
        for sid in conversations.keys { conversations[sid]?.pairingToken = token }
        if dropped { persistIndex() }
    }

    func forget() {
        conversations = [:]
        dirty = []
        resetOnNext = []
        pairing = ""
        if let indexURL { try? fm.removeItem(at: indexURL) }
        if let folderURL { try? fm.removeItem(at: folderURL) }
    }

    func settle() async {
        flush()
        await pendingValue()
    }

    /// Wait for the write in flight, if any, without flushing.
    func pendingValue() async {
        await pendingWrite?.value
        pendingWrite = nil
    }

    private func prune(now: Double) {
        var dropped = false
        for (sid, row) in conversations where now - row.updatedAt > Self.maxAge {
            conversations[sid] = nil
            dropped = true
            if let url = sessionURL(sid) { try? fm.removeItem(at: url) }
        }
        if dropped { persistIndex() }
    }

    private func persistSession(_ row: CachedConversation) {
        guard let folderURL, let url = sessionURL(row.sessionId),
              let indexURL else { return }
        var index: [String: Double] = [:]
        for (sid, held) in conversations { index[sid] = held.updatedAt }
        let previous = pendingWrite
        pendingWrite = Task.detached(priority: .utility) {
            // Writes land in order: the one before this finishes first
            // (`NotificationLogStore.write`'s rule), so a later page's
            // file is never overwritten by an earlier page's.
            await previous?.value
            guard let indexData = try? JSONEncoder().encode(index) else { return }
            try? indexData.write(to: indexURL,
                                 options: [.atomic, .completeFileProtection])
            try? FileManager.default.createDirectory(
                at: folderURL, withIntermediateDirectories: true)
            guard let data = try? JSONEncoder().encode(row) else { return }
            try? data.write(to: url, options: [.atomic, .completeFileProtection])
        }
    }

    private func persistIndex() {
        guard let indexURL else { return }
        var index: [String: Double] = [:]
        for (sid, row) in conversations { index[sid] = row.updatedAt }
        let previous = pendingWrite
        pendingWrite = Task.detached(priority: .utility) {
            await previous?.value
            guard let data = try? JSONEncoder().encode(index) else { return }
            try? data.write(to: indexURL,
                            options: [.atomic, .completeFileProtection])
        }
    }
}
