import Foundation
import SwiftUI

/// The phone's own complete copy of every card and every plan it has seen,
/// and the delta sweep that fills it.
///
/// The live frame carries a *preview* of a card's instructions
/// (`BOARD_SNAPSHOT_PROMPT_CHARS`) and no plan at all, which is deliberate
/// and stays that way — so a phone living off that frame becomes a list of
/// titles the moment it is out of reach. This store is the answer: every
/// card's real text and every plan document, kept on disk, read whenever the
/// live copy is shortened or unavailable.
///
/// **The delta is the whole cheapness argument.** `stale(in:)` names only
/// the ids the cache does not hold or whose `revision` has moved, so an
/// unchanged board yields the empty set and the sweep sends nothing at all.
/// Plans cannot move a revision — the Mac does not watch those files and a
/// stat per card per frame would put disk work on the board snapshot — so
/// they are re-checked by digest on a slow rotation instead.
///
/// `OutboxStore`'s file shape throughout: Application Support, every write
/// `[.atomic, .completeFileProtection]`, tolerant decoding, `@MainActor`
/// with **the encode and the write both off it** (`Task.detached`,
/// `OutboxStore.stagePhoto`'s pattern) behind a cheap change hash, so the
/// every-frame `remember(board:)` costs one `Hasher` pass and, when nothing
/// a person would notice has moved, no disk work at all. The two remaining main-actor reads are
/// `load()`, which runs once behind the Face ID gate, and `planText(_:)`,
/// which the card screen reads **once** into `@State` rather than from a
/// view body.

/// One card as the phone holds it. Tolerant both ways, `OutboxEntry`'s rule:
/// an unknown key is ignored, a wrong-shaped field reads as its default, and
/// only a missing id makes the record absent.
struct CachedCard: Identifiable, Codable, Equatable {
    var id: String = ""
    /// The Mac's change number for this copy. What `stale(in:)` compares and
    /// what a Save sends back as `expected_revision`.
    var revision: Int = 0
    var title: String = ""
    var summary: String = ""
    var prompt: String = ""
    var workflow: String = ""
    var area: String = ""
    var planPath: String = ""
    /// The SHA-256 of the plan bytes on disk beside this record, `""` when
    /// there is no plan file cached. Sent back as the digest stamp, which is
    /// what lets the Mac answer "unchanged" and send nothing.
    var planDigest: String = ""
    /// Whether the plan was readable when it was last fetched, and the Mac's
    /// own words when it was not. Stated, never inferred from empty text.
    var planAvailable: Bool = false
    var planReason: String = ""
    /// When the board last listed this card. The eviction order for plan
    /// files: least-recently-listed goes first.
    var lastSeen: Double = 0
    /// The pairing this copy was filled under — `Receipt.pairingToken`'s
    /// twin, and closed the same way by construction. A cache from another
    /// Mac must never be read as this one's, and an explicit un-pair is not
    /// the only route here: a re-pair against a *different* Mac would
    /// otherwise leave these rows readable. Empty on a record written before
    /// the stamp, which `adopt(_:)` treats as this pairing's.
    var pairingToken: String = ""

    enum CodingKeys: String, CodingKey {
        case id, revision, title, summary, prompt, workflow, area
        case planPath, planDigest, planAvailable, planReason, lastSeen
        case pairingToken
    }

    init(id: String) { self.id = id }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        guard !id.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .id, in: c, debugDescription: "a card with no id")
        }
        revision = c.value(.revision, 0)
        title = c.value(.title, "")
        summary = c.value(.summary, "")
        prompt = c.value(.prompt, "")
        workflow = c.value(.workflow, "")
        area = c.value(.area, "")
        planPath = c.value(.planPath, "")
        planDigest = c.value(.planDigest, "")
        planAvailable = c.value(.planAvailable, false)
        planReason = c.value(.planReason, "")
        lastSeen = c.value(.lastSeen, 0)
        pairingToken = c.value(.pairingToken, "")
    }
}

@MainActor
final class CardCacheStore: ObservableObject {
    @Published private(set) var cards: [String: CachedCard] = [:]

    /// How many stale ids one pass may ask for. The Mac's own
    /// `CARD_SYNC_MAX_IDS`; anything past it stays stale for the next pass,
    /// which is what `more`/`unserved` on the answer already say.
    static let syncBatch = 50
    /// How often the phone re-checks the plans it already holds. A plan file
    /// edited under a card whose fields never moved is invisible any other
    /// way, and this is deliberately slow: the check costs the Mac one stat
    /// per stamp and the wire nothing at all when nothing changed.
    static let planRecheckInterval: TimeInterval = 600
    /// How much disk the plan documents may take. 500 plans at 64 KiB is not
    /// a phone's worth of storage, so the least-recently-listed go first; a
    /// plan evicted and later reopened in reach simply comes back.
    static let planBudgetBytes = 32 * 1024 * 1024

    /// Serialises the sweep against itself, `OutboxStore.syncing`'s job.
    /// Which pairing this cache belongs to. Written by `adopt(_:)` alone.
    private(set) var pairing = ""
    private var syncing = false
    /// What was last written out, as a **cheap change hash rather than an
    /// encode**. `OutboxStore.lastCatalogueBytes`' guard, and this store
    /// needs it more: `remember(board:)` runs on every board frame, so a
    /// 500-card cache at `MAX_PROMPT_CHARS` was a multi-megabyte
    /// `JSONEncoder` run every four seconds — on the thread drawing the
    /// board — even once the write itself had moved off it.
    ///
    /// The hash is over `id` + `revision` + `planDigest`, which is exactly
    /// what `stale(in:)` already compares: those three are the only fields
    /// that can move without a page landing, and `lastSeen` is deliberately
    /// **not** in it, because it is re-stamped on every pass and would make
    /// the guard fire every time.
    private var lastChangeKey: Int?
    private var lastPlanRecheck: Date = .distantPast
    /// Where the digest rotation has got to, so a board with more plans than
    /// one batch still gets round to all of them.
    private var planRotation = 0
    private let fm = FileManager.default

    // --- where it lives -------------------------------------------------

    private var support: URL? {
        try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask,
                    appropriateFor: nil, create: true)
    }

    private var cardsURL: URL? { support?.appendingPathComponent("cards.json") }

    private var plansRoot: URL? {
        support?.appendingPathComponent("plans", isDirectory: true)
    }

    private func planURL(_ id: String) -> URL? {
        guard !id.isEmpty, !id.contains("/"), !id.contains("..") else { return nil }
        return plansRoot?.appendingPathComponent("\(id).md")
    }

    // --- persistence ----------------------------------------------------

    /// Read behind the Face ID gate, never before it: the file holds the
    /// person's own words and is written with `.completeFileProtection`.
    func load() {
        guard let url = cardsURL, let data = try? Data(contentsOf: url) else { return }
        let tolerated = (try? JSONDecoder().decode([Tolerated].self, from: data)) ?? []
        var out: [String: CachedCard] = [:]
        for row in tolerated.compactMap(\.card) { out[row.id] = row }
        cards = out
        // Seed the write guard from what is already on disk, so the first
        // `remember(board:)` after an unlock does not rewrite an unchanged
        // file just because nothing had been compared yet.
        lastChangeKey = changeKey
    }

    private struct Tolerated: Decodable {
        let card: CachedCard?
        init(from decoder: Decoder) throws {
            card = try? CachedCard(from: decoder)
        }
    }

    /// Write the cache out, **off the main actor and only when something a
    /// person would notice changed**.
    ///
    /// Two guards, both load-bearing: the encode compares against the last
    /// bytes with `lastSeen` zeroed, so the every-frame re-stamp alone never
    /// buys a write; and the write itself is a `Task.detached`, exactly as
    /// `OutboxStore.stagePhoto` does it, so a megabyte of JSON and an atomic
    /// `.completeFileProtection` rename never sit on the thread drawing the
    /// board.
    private func persist(force: Bool = false) {
        guard let url = cardsURL else { return }
        let key = changeKey
        if !force, key == lastChangeKey { return }
        lastChangeKey = key
        let rows = Array(cards.values).sorted { $0.id < $1.id }
        // The **encode** goes with the write, not just the rename: on this
        // path both are off the main actor, so the comment above is true of
        // the JSON as well as of the file.
        Task.detached(priority: .utility) {
            guard let data = try? JSONEncoder().encode(rows) else { return }
            do {
                try data.write(to: url,
                               options: [.atomic, .completeFileProtection])
            } catch {
                // A store that cannot be written is not a reason to lose what
                // is already in memory — `OutboxStore.write`'s rule.
            }
        }
    }

    /// The three fields that can move without a page landing, hashed. Cheap
    /// enough to run on every board frame, which is the point.
    private var changeKey: Int {
        var hasher = Hasher()
        hasher.combine(cards.count)
        for row in cards.values.sorted(by: { $0.id < $1.id }) {
            hasher.combine(row.id)
            hasher.combine(row.revision)
            hasher.combine(row.planDigest)
            hasher.combine(row.pairingToken)
        }
        return hasher.finalize()
    }

    // --- reading --------------------------------------------------------

    func card(_ id: String) -> CachedCard? { cards[id] }

    /// The plan document this phone holds for a card, or nil. Read off disk
    /// each time rather than kept in memory: 64 KiB per card would be a
    /// megabyte of live text for a board nobody is looking at.
    func planText(_ id: String) -> String? {
        guard let row = cards[id], row.planAvailable, let url = planURL(id),
              let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .utf8), !text.isEmpty
        else { return nil }
        return text
    }

    /// Which cards this pass should ask for: an id the cache does not hold,
    /// or one whose cached `revision` differs from the board's.
    ///
    /// **This is the cheapness.** An unchanged board answers with the empty
    /// set and `sync(using:)` sends nothing at all.
    func stale(in board: Board) -> [String] {
        guard board.available else { return [] }
        return board.cards.compactMap { row in
            guard !row.id.isEmpty else { return nil }
            guard let held = cards[row.id] else { return row.id }
            return held.revision == row.revision ? nil : row.id
        }
    }

    /// The board listed these cards just now. Stamps what it holds and drops
    /// what the board no longer carries — a card deleted on the Mac must not
    /// live for ever on the phone.
    ///
    /// Guarded on `available` **and nothing else**: a snapshot that failed
    /// to carry a board is not evidence that every card is gone, and
    /// forgetting the whole cache on a blank frame is exactly the offline
    /// story this store exists to fix. But `available` is *stated* by the
    /// daemon, so `available: true` with no cards is a genuinely empty
    /// board — a second `!cards.isEmpty` guard there left rows on disk that
    /// nothing would ever prune or read.
    func remember(board: Board) {
        guard board.available else { return }
        let now = Date().timeIntervalSince1970
        let listed = Set(board.cards.map(\.id))
        var dropped = false
        for id in cards.keys where !listed.contains(id) {
            cards[id] = nil
            dropped = true
            if let url = planURL(id) { try? fm.removeItem(at: url) }
        }
        for id in listed where cards[id] != nil {
            cards[id]?.lastSeen = now
        }
        // The budget sweep enumerates a directory and stats every plan, so
        // it runs where the set of held plans can actually have changed — a
        // card dropping out here, or a page landing in `apply`. Nothing else
        // in this method adds a byte to disk.
        if dropped { evictPlansPastBudget() }
        persist()
    }

    /// This cache now belongs to `token`. Anything stamped with a
    /// *different* pairing goes, document and all — `ReceiptLedger.rebase`'s
    /// twin, and the construction that makes "a cache from another Mac is
    /// never read as this one's" true rather than merely intended. An
    /// unstamped row (written before the stamp existed) is adopted.
    ///
    /// Called from `PhoneClient.start(record:)`, which is the one place a
    /// pairing becomes the current one — so it runs before any `remember`,
    /// any sweep and any card screen.
    func adopt(_ token: String) {
        guard !token.isEmpty else { return }
        pairing = token
        var dropped = false
        for (id, row) in cards
        where !row.pairingToken.isEmpty && row.pairingToken != token {
            cards[id] = nil
            dropped = true
            if let url = planURL(id) { try? fm.removeItem(at: url) }
        }
        for id in cards.keys { cards[id]?.pairingToken = token }
        if dropped { evictPlansPastBudget() }
        persist()
    }

    /// Forget everything. Called on an explicit un-pair and on a 403 at the
    /// door: a cache filled from another Mac must never be read as this
    /// one's.
    func forget() {
        cards = [:]
        lastChangeKey = nil
        if let url = cardsURL { try? fm.removeItem(at: url) }
        if let root = plansRoot { try? fm.removeItem(at: root) }
    }

    // --- the sweep ------------------------------------------------------

    /// One delta pass. Serialised by `syncing`, driven from the client's
    /// existing `onLive` beside `outbox.sync`, never from `poll` and never
    /// under `backgroundRefresh` (the client's own `backgroundRun` guard
    /// inside `fetchCardSync` is what enforces the last of those).
    func sync(using client: PhoneClient) async {
        guard !syncing else { return }
        let board = client.snapshot.board
        guard board.available else { return }
        // A Mac that 404s the kind never fills this cache, so every later
        // pass would find the whole board stale and ask again — one sealed
        // frame per live poll, for ever, against the one Mac that can do
        // nothing with it. A card carrying a real `revision` is proof the
        // kind is there after all (an older Mac decodes every revision as
        // 0), so an upgrade lifts the latch without waiting for a relaunch.
        if client.cardSyncUnsupported,
           !board.cards.contains(where: { $0.revision > 0 }) { return }
        let ids = Array(stale(in: board).prefix(Self.syncBatch))
        let stamps = planStamps(board: board)
        guard !ids.isEmpty || !stamps.isEmpty else { return }
        syncing = true
        defer { syncing = false }
        guard let page = await client.fetchCardSync(ids: ids, plans: stamps),
              page.available else { return }
        if !stamps.isEmpty { lastPlanRecheck = Date() }
        apply(page)
    }

    /// The `id:digest` stamps this pass carries, at most every
    /// `planRecheckInterval` and at most `syncBatch` of them, rotated over
    /// the cards the cache holds so a long board still gets round to all.
    private func planStamps(board: Board) -> [String: String] {
        guard Date().timeIntervalSince(lastPlanRecheck) >= Self.planRecheckInterval
        else { return [:] }
        let listed = Set(board.cards.map(\.id))
        let holders = cards.values
            .filter { listed.contains($0.id) && !$0.planPath.isEmpty }
            .sorted { $0.id < $1.id }
        guard !holders.isEmpty else { return [:] }
        var out: [String: String] = [:]
        let start = planRotation % holders.count
        for step in 0..<min(Self.syncBatch, holders.count) {
            let row = holders[(start + step) % holders.count]
            out[row.id] = row.planDigest
        }
        planRotation = (start + out.count) % holders.count
        return out
    }

    /// Fold one page in. Cards replace wholesale; a plan marked `unchanged`
    /// leaves the file on disk exactly as it is, which is the point of the
    /// digest.
    func apply(_ page: CardSyncPage) {
        let now = Date().timeIntervalSince1970
        for row in page.cards where !row.id.isEmpty {
            var held = cards[row.id] ?? CachedCard(id: row.id)
            held.revision = row.revision
            held.title = row.title
            held.summary = row.summary
            held.prompt = row.prompt
            held.workflow = row.workflow
            held.area = row.area
            held.planPath = row.planPath
            held.lastSeen = now
            held.pairingToken = pairing
            cards[row.id] = held
        }
        for plan in page.plans where !plan.cardId.isEmpty {
            guard var held = cards[plan.cardId] else { continue }
            held.planPath = plan.path
            if plan.unchanged {
                // The Mac sent no bytes because the phone's digest still
                // matched. Nothing on disk changes, and this is never read
                // as "the plan went away".
                held.planAvailable = true
                held.planReason = ""
            } else if plan.available {
                held.planAvailable = true
                held.planDigest = plan.digest
                held.planReason = ""
                writePlan(plan.cardId, text: plan.text)
            } else {
                held.planAvailable = false
                held.planDigest = ""
                held.planReason = plan.reason
                if let url = planURL(plan.cardId) { try? fm.removeItem(at: url) }
            }
            cards[plan.cardId] = held
        }
        evictPlansPastBudget()
        persist()
    }

    private func writePlan(_ id: String, text: String) {
        guard let root = plansRoot, let url = planURL(id),
              let data = text.data(using: .utf8) else { return }
        Task.detached(priority: .utility) {
            try? FileManager.default.createDirectory(
                at: root, withIntermediateDirectories: true)
            try? data.write(to: url,
                            options: [.atomic, .completeFileProtection])
        }
    }

    /// Least-recently-listed first, until the plan folder is inside budget.
    /// The record stays — only the document goes — so the card still reads
    /// in full offline and the plan comes back on the next pass in reach.
    func evictPlansPastBudget() {
        guard let root = plansRoot else { return }
        let found = (try? fm.contentsOfDirectory(
            at: root, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        var sizes: [String: Int] = [:]
        var total = 0
        for url in found {
            let bytes = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            let id = url.deletingPathExtension().lastPathComponent
            sizes[id] = bytes
            total += bytes
        }
        guard total > Self.planBudgetBytes else { return }
        let order = sizes.keys.sorted {
            (cards[$0]?.lastSeen ?? 0) < (cards[$1]?.lastSeen ?? 0)
        }
        for id in order {
            guard total > Self.planBudgetBytes else { break }
            if let url = planURL(id) { try? fm.removeItem(at: url) }
            total -= sizes[id] ?? 0
            cards[id]?.planDigest = ""
            cards[id]?.planAvailable = false
            cards[id]?.planReason = ""
        }
    }
}
