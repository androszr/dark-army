import Foundation

/// Where the person was when the app went away: the tab, the Menu section,
/// the profile push, and the trail of sheets with the text typed into them.
///
/// Written to `Application Support/place.json` with
/// `[.atomic, .completeFileProtection]` just before the lock tears the
/// hierarchy down (`PhonePlaceStore.flushNow()`, beside
/// `OutboxStore.flushDraftNow()`), read only behind the Face ID gate, stamped
/// with the SHA-256 of the pairing token (never the token) and dropped on an
/// un-pair or a pairing change. Foundation only, so `host/tests/
/// test_phone_place.py` runs the rules under `swiftc` with nothing else.
/// Contract: `docs/phone-contract.md`, *The phone comes back where you left
/// it*.
struct PhonePlace: Codable, Equatable {
    static let currentVersion = 1

    var version: Int
    /// SHA-256 hex of the pairing token this place was saved under.
    var pairing: String
    /// `PhoneTab`'s raw value; `""` reads back as Needs you.
    var tab: String
    /// `MenuSection`'s raw value, `""` for the grid.
    var menuSection: String
    /// `""` or `"profile"`. The composer push is the outbox's business.
    var push: String
    /// Bottom rung first.
    var trail: [Entry]

    init(version: Int = PhonePlace.currentVersion, pairing: String = "", tab: String = "",
         menuSection: String = "", push: String = "", trail: [Entry] = []) {
        self.version = version
        self.pairing = pairing
        self.tab = tab
        self.menuSection = menuSection
        self.push = push
        self.trail = trail
    }

    /// One sheet rung: what it named and what was typed into it.
    struct Entry: Codable, Equatable {
        /// `PhoneSheetKind`'s raw value.
        var kind: String
        /// The session id, the card id, or `"all"` for Catch up.
        var key: String
        /// `AgentScreen`'s raw value, `""` for anything but an agent.
        var agentScreen: String
        var cardDraft: CardDraft?
        var replyText: String

        init(kind: String = "", key: String = "", agentScreen: String = "",
             cardDraft: CardDraft? = nil, replyText: String = "") {
            self.kind = kind
            self.key = key
            self.agentScreen = agentScreen
            self.cardDraft = cardDraft
            self.replyText = replyText
        }

        /// The same string `PhoneSheet.id` composes for this subject.
        var id: String { kind + "/" + key }

        /// Whether anything typed rides this rung.
        var holdsText: Bool { !replyText.isEmpty || cardDraft != nil }
    }

    /// The card screen's typed text and editor state. Fetched context — the
    /// full card, a conflict, the cached plan — and the Mac's own words (the
    /// status note, a send note) are not kept: they come again. A touched
    /// edit keeps the revision it was typed against, so a Save after the
    /// restore is refused, not applied, when the card moved meanwhile.
    struct CardDraft: Codable, Equatable {
        var title: String
        var summary: String
        var prompt: String
        var priority: String
        var area: String
        var draftFor: String
        var touched: Bool
        /// The card revision the touched editors were typed against; nil
        /// when untouched, or in a file that never said.
        var baseRevision: Int?
        var editing: Bool
        var messageOpen: Bool
        var messageText: String

        init(title: String = "", summary: String = "",
             prompt: String = "", priority: String = "", area: String = "",
             draftFor: String = "", touched: Bool = false, baseRevision: Int? = nil,
             editing: Bool = false, messageOpen: Bool = false, messageText: String = "") {
            self.title = title
            self.summary = summary
            self.prompt = prompt
            self.priority = priority
            self.area = area
            self.draftFor = draftFor
            self.touched = touched
            self.baseRevision = baseRevision
            self.editing = editing
            self.messageOpen = messageOpen
            self.messageText = messageText
        }

        enum CodingKeys: String, CodingKey {
            case title, summary, prompt, priority, area
            case draftFor = "for"
            case touched, baseRevision, editing, messageOpen, messageText
        }
    }
}

// Every field decodes with a default, so an older build's file and a newer
// build's extra keys both read. Written here rather than borrowed from
// `Models.swift` so this file compiles alone.

extension PhonePlace {
    enum CodingKeys: String, CodingKey {
        case version, pairing, tab, menuSection, push, trail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(version: c.placeValue(.version, PhonePlace.currentVersion),
                  pairing: c.placeValue(.pairing, ""),
                  tab: c.placeValue(.tab, ""),
                  menuSection: c.placeValue(.menuSection, ""),
                  push: c.placeValue(.push, ""),
                  trail: c.placeValue(.trail, []))
    }
}

extension PhonePlace.Entry {
    enum CodingKeys: String, CodingKey {
        case kind, key, agentScreen, cardDraft, replyText
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(kind: c.placeValue(.kind, ""),
                  key: c.placeValue(.key, ""),
                  agentScreen: c.placeValue(.agentScreen, ""),
                  cardDraft: (try? c.decodeIfPresent(PhonePlace.CardDraft.self,
                                                     forKey: .cardDraft)) ?? nil,
                  replyText: c.placeValue(.replyText, ""))
    }
}

extension PhonePlace.CardDraft {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(title: c.placeValue(.title, ""),
                  summary: c.placeValue(.summary, ""), prompt: c.placeValue(.prompt, ""),
                  priority: c.placeValue(.priority, ""), area: c.placeValue(.area, ""),
                  draftFor: c.placeValue(.draftFor, ""), touched: c.placeValue(.touched, false),
                  baseRevision: c.placeValue(.baseRevision, nil),
                  editing: c.placeValue(.editing, false),
                  messageOpen: c.placeValue(.messageOpen, false),
                  messageText: c.placeValue(.messageText, ""))
    }
}

private extension KeyedDecodingContainer {
    /// The key's value, or `fallback` when it is absent or of another type.
    func placeValue<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        (try? decodeIfPresent(T.self, forKey: key)) ?? nil ?? fallback
    }
}

/// The pure rules: what a saved trail keeps against the current picture,
/// what outranks a saved place, and which agent page a restore lands on.
enum PhonePlaceRules {
    /// `PhoneSheetRouter.MAX_DEPTH`, restated so this file compiles alone.
    static let maxDepth = 3
    /// The rungs whose subject is a snapshot row. A decision, a changed file
    /// and a notification are fetched pages; a Catch up group is a list the
    /// notification brought.
    static let restorableKinds: Set<String> = ["agent", "card", "catchUp"]

    /// Walk from the bottom, keeping each rung whose subject the picture
    /// still lists, and stop at the first one it cannot keep: a rung is
    /// never shown over a gap. Every rung not kept is returned with why.
    static func cut(_ trail: [PhonePlace.Entry], agents: Set<String>,
                    cards: Set<String>) -> (kept: [PhonePlace.Entry],
                                            dropped: [(PhonePlace.Entry, String)]) {
        var kept: [PhonePlace.Entry] = []
        var dropped: [(PhonePlace.Entry, String)] = []
        for entry in trail {
            if !dropped.isEmpty {
                dropped.append((entry, "above a dropped rung"))
                continue
            }
            if let reason = refusal(entry, depth: kept.count, agents: agents, cards: cards) {
                dropped.append((entry, reason))
            } else {
                kept.append(entry)
            }
        }
        return (kept, dropped)
    }

    private static func refusal(_ entry: PhonePlace.Entry, depth: Int,
                                agents: Set<String>, cards: Set<String>) -> String? {
        if depth >= maxDepth { return "trail depth" }
        guard restorableKinds.contains(entry.kind) else { return "kind not restorable" }
        switch entry.kind {
        case "agent": return agents.contains(entry.key) ? nil : "agent gone"
        case "card": return cards.contains(entry.key) ? nil : "card gone"
        default: return entry.key == "all" ? nil : "kind not restorable"
        }
    }

    /// Whether something else is waiting at unlock — a notification tap or
    /// widget link's tab, a receipt, or a banked composer draft that was on
    /// screen. Any of them wins, and the saved place is set aside.
    static func wins(pendingTab: Bool, pendingReceipt: Bool, composerResume: Bool) -> Bool {
        pendingTab || pendingReceipt || composerResume
    }

    /// The agent page a restored rung opens on. Terminal is a live socket
    /// that claims the pty's width, so it comes back as Details; anything
    /// unknown comes back as Main.
    static func restoredScreen(_ raw: String) -> String {
        switch raw {
        case "Main", "Conversation", "Details": return raw
        case "Terminal": return "Details"
        default: return "Main"
        }
    }
}

/// The one place store. `ContentView` hands it a `compose` closure that
/// reads the live hierarchy; the scene-phase handler calls `flushNow()`
/// before `lock.lock()`, and the unlock gate loads, adopts and arms it.
/// `PhoneRouter`'s discipline for the file: an injectable directory and
/// reader, a protected-read failure retried at the next `load()`.
@MainActor
final class PhonePlaceStore: ObservableObject {
    static let shared = PhonePlaceStore()
    static let fileName = "place.json"
    /// A trail change is a file write this long after the last one;
    /// durability across the lock is `flushNow()`'s job, not this timer's.
    static let stashDebounce: Duration = .seconds(2)

    /// Bumped when `take()` leaves a profile push for a tab root to apply.
    @Published private(set) var pushSignal = 0
    /// Bumped by `arm()`, so a `ContentView` already on screen applies it.
    @Published private(set) var armSignal = 0

    /// Read the file once it could be read; a protected-read failure leaves
    /// this false so the next `load()` tries again.
    private(set) var loaded = false
    /// The newest place — read from the file, or stashed or flushed since.
    private(set) var record: PhonePlace?
    /// The pairing current places are stamped with; `""` refuses writes.
    private(set) var identity = ""
    /// What the last restore could not keep, and why.
    private(set) var lastDropped: [(PhonePlace.Entry, String)] = []
    /// The live place, composed on demand by `ContentView`; nil while it is
    /// not mounted.
    var compose: (() -> PhonePlace?)?

    private var armed: PhonePlace?
    private var pushSlot: (tab: String, push: String)?
    private var profileTabs: Set<String> = []
    /// Typed text of rungs a tap or a draft displaced, keyed by
    /// `Entry.id`. Memory only.
    private var orphanDrafts: [String: PhonePlace.Entry] = [:]
    private var stashTask: Task<Void, Never>?
    private let directory: URL?
    private let reader: (URL) throws -> Data

    init(directory: URL? = nil, reader: ((URL) throws -> Data)? = nil) {
        self.directory = directory ?? FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask).first
        self.reader = reader ?? { try Data(contentsOf: $0) }
    }

    var fileURL: URL? { directory?.appendingPathComponent(Self.fileName) }

    /// Read behind the Face ID gate, never before it.
    func load() {
        guard !loaded, let url = fileURL else { return }
        do {
            let data = try reader(url)
            loaded = true
            guard let decoded = try? JSONDecoder().decode(PhonePlace.self, from: data) else {
                // Unreadable bytes are nobody's place.
                try? FileManager.default.removeItem(at: url)
                return
            }
            if !identity.isEmpty, decoded.pairing != identity {
                try? FileManager.default.removeItem(at: url)
                return
            }
            // A stash made before this read is newer than the file.
            if record == nil { record = decoded }
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            loaded = true
        } catch {
            // Protected data may be unavailable until the next unlock.
        }
    }

    /// A pairing became the current one. A place saved under another drops,
    /// file and all.
    func adopt(identity: String) {
        guard !identity.isEmpty else { return }
        if let record, record.pairing != identity {
            self.record = nil
            armed = nil
            orphanDrafts = [:]
            if let url = fileURL { try? FileManager.default.removeItem(at: url) }
        }
        self.identity = identity
    }

    /// The once-per-unlock slot: the place is applied by the next `take()`.
    func arm() {
        profileTabs = []
        pushSlot = nil
        guard !identity.isEmpty, let record, record.pairing == identity else {
            armed = nil
            return
        }
        armed = record
        armSignal &+= 1
    }

    /// Consume the armed place.
    func take() -> PhonePlace? {
        defer { armed = nil }
        guard let place = armed else { return nil }
        if !place.push.isEmpty {
            pushSlot = (place.tab, place.push)
            pushSignal &+= 1
        }
        return place
    }

    /// Something else won at unlock: drop the armed place and keep only the
    /// text typed into its rungs, for the next time those subjects open.
    func setAside() {
        guard let place = armed else { return }
        armed = nil
        keepDrafts(of: place.trail)
    }

    /// Keep the typed text of rungs that will not be shown.
    func keepDrafts(of trail: [PhonePlace.Entry]) {
        for entry in trail where entry.holdsText {
            var kept = entry
            // A later open is a fresh open: it lands on Main.
            kept.agentScreen = ""
            orphanDrafts[entry.id] = kept
        }
    }

    /// The displaced text for this subject, handed back exactly once.
    func takeOrphanDraft(for id: String) -> PhonePlace.Entry? {
        orphanDrafts.removeValue(forKey: id)
    }

    /// The push the taken place left for this tab, `""` for none. Consumed.
    func takePush(for tab: String) -> String {
        guard let slot = pushSlot, slot.tab == tab else { return "" }
        pushSlot = nil
        return slot.push
    }

    /// A tab root's profile push came or went.
    func noteProfile(_ showing: Bool, on tab: String) {
        if showing { profileTabs.insert(tab) } else { profileTabs.remove(tab) }
    }

    func profileShowing(on tab: String) -> Bool { profileTabs.contains(tab) }

    func noteDropped(_ dropped: [(PhonePlace.Entry, String)]) { lastDropped = dropped }

    /// The place moved. Kept in memory at once, written after a pause.
    func stash(_ place: PhonePlace) {
        guard let stamped = stamped(place) else { return }
        record = stamped
        stashTask?.cancel()
        stashTask = Task { [weak self] in
            try? await Task.sleep(for: Self.stashDebounce)
            guard !Task.isCancelled, let self, let record = self.record else { return }
            self.write(record)
        }
    }

    /// Write the live place **now**, synchronously. Called before
    /// `lock.lock()` in the same handler body: the lock tears the hierarchy
    /// down, so this ordering is what makes the place survive.
    func flushNow() {
        stashTask?.cancel()
        stashTask = nil
        guard let composed = compose?(), let stamped = stamped(composed) else { return }
        record = stamped
        write(stamped)
    }

    /// Un-pair: the place, the slot, the displaced text and the file.
    func forget() {
        stashTask?.cancel()
        stashTask = nil
        record = nil
        armed = nil
        pushSlot = nil
        profileTabs = []
        orphanDrafts = [:]
        lastDropped = []
        identity = ""
        loaded = true
        if let url = fileURL { try? FileManager.default.removeItem(at: url) }
    }

    private func stamped(_ place: PhonePlace) -> PhonePlace? {
        guard !identity.isEmpty else { return nil }
        var place = place
        place.version = PhonePlace.currentVersion
        place.pairing = identity
        return place
    }

    private func write(_ place: PhonePlace) {
        guard let url = fileURL, place.pairing == identity, !identity.isEmpty,
              let data = try? JSONEncoder().encode(place) else { return }
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: url, options: [.atomic, .completeFileProtection])
            loaded = true
        } catch {
            // A place that cannot be written is not a reason to lose the one
            // in memory; the next flush tries again.
        }
    }
}
