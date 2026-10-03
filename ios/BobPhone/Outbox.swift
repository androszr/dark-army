import Foundation
import SwiftUI

/// Cards written on the phone that the Mac has not taken yet, and the
/// last-known lists that let one be written at all with no Mac in reach.
///
/// The shape is the desktop panel's `Drafts.swift` one step further: a
/// half-typed card banked to a local file the daemon never reads, keyed by
/// the composer's own `stagingId`. This store also *sends* — the sweep rides
/// the client's existing 4s poll, because the phone is poll-only by design
/// and there is no background socket to hang a queue off.

/// One card waiting for the Mac. `id` **is** the composer's staging id, which
/// is also the Mac-side staging folder's name, so a photo uploaded on a retry
/// lands where the first attempt would have put it.
struct OutboxEntry: Identifiable, Codable, Equatable {
    var id: String = ""
    /// Stored trimmed, because `BoardStore.create` strips both of these and
    /// they are the duplicate check's join keys.
    var title: String = ""
    var summary: String = ""
    var prompt: String = ""
    var tool: String = ""
    var model: String = ""
    /// The reasoning effort typed with the card, `""` meaning Default. Sent
    /// only when non-empty, so a Default card's create is byte-identical.
    var effort: String = ""
    var project: String = ""
    var root: String = ""
    var workflow: String = ""
    /// The objective typed with the card. Sent only when non-empty, and
    /// never to a Mac whose board did not say it accepts them on create.
    var beneficiary: String = ""
    var intendedBenefit: String = ""
    var successCriterion: String = ""
    /// The importance number typed with the card, `""` for none. Sent only
    /// when non-empty, as a string.
    var priority: String = ""
    var area: String = ""
    /// `""` for a build card, `"scout"` for an investigation — the Mac
    /// composer's Build / Scout chips. Sent only when scout, so an older
    /// Mac never sees the key.
    var kind: String = ""
    /// Paths the Mac already answered with, `<folder>/<name>`. Its answer,
    /// never the name the phone asked for.
    var remotePaths: [String] = []
    /// File names still sitting in this entry's folder on the phone.
    var localPhotos: [String] = []
    var createdAt: Double = 0
    /// How many times the card itself has been POSTed. Non-zero is the scar
    /// that arms the duplicate check — a send may have landed with its reply
    /// lost, and only a scarred entry is allowed to doubt itself.
    var attempts: Int = 0
    var lastError: String = ""
    /// A refusal's backoff: the sweep skips this entry until the moment
    /// passes. Without it a card the Mac refused *in words* was re-POSTed
    /// on every 4s poll — and each 409 answer dragged a whole extra state
    /// refresh behind it — for as long as the refusal stood.
    var heldUntil: Double = 0
    /// The delay last applied, doubled on each consecutive refusal
    /// (`backoffStart` up to `backoffCap`). Reset by the visible RETRY.
    var retryDelay: Double = 0

    enum CodingKeys: String, CodingKey {
        case id, title, summary, prompt, tool, model, project, root, workflow
        case remotePaths, localPhotos, createdAt, attempts, lastError
        case heldUntil, retryDelay
        case beneficiary, intendedBenefit, successCriterion
        case priority, area, kind
        case effort
    }

    init(id: String, title: String, summary: String, prompt: String,
         tool: String, model: String, project: String, root: String,
         workflow: String, remotePaths: [String], localPhotos: [String],
         createdAt: Double, beneficiary: String = "",
         intendedBenefit: String = "", successCriterion: String = "",
         priority: String = "", area: String = "", kind: String = "",
         effort: String = "") {
        self.id = id
        self.title = title
        self.summary = summary
        self.prompt = prompt
        self.tool = tool
        self.model = model
        self.effort = effort
        self.project = project
        self.root = root
        self.workflow = workflow
        self.remotePaths = remotePaths
        self.localPhotos = localPhotos
        self.createdAt = createdAt
        self.beneficiary = beneficiary
        self.intendedBenefit = intendedBenefit
        self.successCriterion = successCriterion
        self.priority = priority
        self.area = area
        self.kind = kind
    }

    /// Tolerant both ways, the desktop draft store's rule: an unknown key is
    /// ignored, a wrong-shaped field reads as its default. Only a missing id
    /// makes the entry absent — without it there is no folder to find and no
    /// staging id to upload against.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        guard !id.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .id, in: c, debugDescription: "an entry with no id")
        }
        title = c.value(.title, "")
        summary = c.value(.summary, "")
        prompt = c.value(.prompt, "")
        tool = c.value(.tool, "")
        model = c.value(.model, "")
        effort = c.value(.effort, "")
        project = c.value(.project, "")
        root = c.value(.root, "")
        workflow = c.value(.workflow, "")
        beneficiary = c.value(.beneficiary, "")
        intendedBenefit = c.value(.intendedBenefit, "")
        successCriterion = c.value(.successCriterion, "")
        priority = c.value(.priority, "")
        area = c.value(.area, "")
        kind = c.value(.kind, "")
        remotePaths = c.value(.remotePaths, [])
        localPhotos = c.value(.localPhotos, [])
        createdAt = c.value(.createdAt, 0)
        attempts = c.value(.attempts, 0)
        lastError = c.value(.lastError, "")
        heldUntil = c.value(.heldUntil, 0)
        retryDelay = c.value(.retryDelay, 0)
    }

    /// Still inside a refusal's backoff window.
    var held: Bool { heldUntil > Date().timeIntervalSince1970 }
}

/// The half-typed card, one slot for the whole phone.
///
/// `id` **is** the composer's staging id, exactly as `OutboxEntry`'s is, so a
/// restored draft keeps uploading into the Mac-side folder the first attempt
/// opened and keeps owning the phone-side folder its offline photos went to.
/// `tab` is the tab the composer was hosted on, so the app can reopen it
/// where it was left.
struct ComposerDraft: Identifiable, Codable, Equatable {
    var id: String = ""
    /// Raw value of `PhoneTab`. A value this build does not know reads back
    /// as its default and simply resumes on the first tab.
    var tab: String = ""
    var title: String = ""
    var summary: String = ""
    var prompt: String = ""
    var tool: String = ""
    var model: String = ""
    /// The reasoning effort banked with the draft; absent reads as Default.
    var effort: String = ""
    var projectRoot: String = ""
    var workflow: String = ""
    /// The one-box idea Prepare writes the other fields from. Composer
    /// scratch: a card has no idea field, so this never leaves the draft.
    var idea: String = ""
    /// The objective typed before the card exists, banked with the rest.
    var beneficiary: String = ""
    var intendedBenefit: String = ""
    var successCriterion: String = ""
    /// The importance number typed before the card exists, banked too.
    var priority: String = ""
    var area: String = ""
    /// Build (`""`) or Scout (`"scout"`), kept with the draft.
    var kind: String = ""
    /// Whether the composer had opened its second half. Default false so an
    /// older row without the key reopens short unless a later box holds text.
    var expanded: Bool = false
    /// Paths the Mac already answered with, `<folder>/<name>`.
    var staged: [String] = []
    /// File names sitting in this draft's folder on the phone.
    var localPhotos: [String] = []
    /// The composer was on screen when the app went away, so it should come
    /// back by itself. Lowered only by the four deliberate exits.
    var open: Bool = false
    var updatedAt: Double = 0

    enum CodingKeys: String, CodingKey {
        case id, tab, title, summary, prompt, tool, model, projectRoot
        case workflow, idea, staged, localPhotos, open, updatedAt
        case beneficiary, intendedBenefit, successCriterion
        case priority, area, kind
        case expanded, effort
    }

    init(id: String, tab: String, title: String, summary: String,
         prompt: String, tool: String, model: String, projectRoot: String,
         workflow: String, idea: String, staged: [String],
         localPhotos: [String],
         open: Bool, beneficiary: String = "",
         intendedBenefit: String = "", successCriterion: String = "",
         priority: String = "", area: String = "", kind: String = "",
         expanded: Bool = false, effort: String = "") {
        self.id = id
        self.effort = effort
        self.tab = tab
        self.title = title
        self.summary = summary
        self.prompt = prompt
        self.tool = tool
        self.model = model
        self.projectRoot = projectRoot
        self.workflow = workflow
        self.idea = idea
        self.staged = staged
        self.localPhotos = localPhotos
        self.open = open
        self.updatedAt = Date().timeIntervalSince1970
        self.beneficiary = beneficiary
        self.intendedBenefit = intendedBenefit
        self.successCriterion = successCriterion
        self.priority = priority
        self.area = area
        self.kind = kind
        self.expanded = expanded
    }

    /// `OutboxEntry`'s exact rule: an unknown key is ignored, a wrong-shaped
    /// field reads as its default, and only a missing id makes the draft
    /// absent — without it there is no staging folder to keep.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        guard !id.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .id, in: c, debugDescription: "a draft with no id")
        }
        tab = c.value(.tab, "")
        title = c.value(.title, "")
        summary = c.value(.summary, "")
        prompt = c.value(.prompt, "")
        tool = c.value(.tool, "")
        model = c.value(.model, "")
        effort = c.value(.effort, "")
        projectRoot = c.value(.projectRoot, "")
        workflow = c.value(.workflow, "")
        beneficiary = c.value(.beneficiary, "")
        intendedBenefit = c.value(.intendedBenefit, "")
        successCriterion = c.value(.successCriterion, "")
        priority = c.value(.priority, "")
        area = c.value(.area, "")
        kind = c.value(.kind, "")
        expanded = c.value(.expanded, false)
        idea = c.value(.idea, "")
        staged = c.value(.staged, [])
        localPhotos = c.value(.localPhotos, [])
        open = c.value(.open, false)
        updatedAt = c.value(.updatedAt, 0)
    }

    /// Something a person actually typed or attached. `tool`, `model` and
    /// `projectRoot` deliberately do not count: `ComposerView.onAppear`
    /// auto-fills the first two of those, so counting them would make every
    /// pristine composer worth banking.
    var worthKeeping: Bool {
        let typed = [title, summary, prompt, workflow, idea].contains {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        let objective = [beneficiary, intendedBenefit, successCriterion,
                         priority, area].contains {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return typed || objective || !staged.isEmpty || !localPhotos.isEmpty
    }
}

/// The last-known assistant, project and model lists. Remembered so the
/// composer's pickers work with no Mac in reach; a phone that has never seen
/// a board has no file and the pickers stay empty, which is stated behaviour
/// rather than a bug.
struct BoardCatalogue: Codable, Equatable {
    struct Project: Codable, Equatable {
        var name: String = ""
        var root: String = ""

        enum CodingKeys: String, CodingKey { case name, root }

        init(name: String, root: String) {
            self.name = name
            self.root = root
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.value(.name, "")
            root = c.value(.root, "")
        }
    }

    var tools: [String] = []
    var models: [String: [String]] = [:]
    /// The remembered effort levels per assistant and model; absent from a
    /// catalogue file written before the setting reads as empty.
    var efforts: [String: [String: [String]]] = [:]
    var projects: [Project] = []
    var prepareEnabled: Bool = false

    enum CodingKeys: String, CodingKey {
        case tools, models, projects, prepareEnabled, efforts
    }

    init() {}

    init(board: Board) {
        tools = board.tools
        models = board.models
        efforts = board.efforts
        projects = board.projects.map { Project(name: $0.name, root: $0.root) }
        prepareEnabled = board.prepareEnabled
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        tools = c.value(.tools, [])
        models = c.value(.models, [:])
        efforts = c.value(.efforts, [:])
        projects = c.value(.projects, [])
        prepareEnabled = c.value(.prepareEnabled, false)
    }

    var isEmpty: Bool { tools.isEmpty && projects.isEmpty }

    /// A `Board` value the composer can read exactly as it reads the live
    /// one: available, with the remembered lists and no cards. Offline
    /// browsing of the Mac's own cards is deliberately not offered.
    func board() -> Board {
        var value = Board()
        value.available = true
        value.tools = tools
        value.models = models
        value.efforts = efforts
        value.projects = projects.map { project in
            var made = BoardProject()
            made.name = project.name
            made.root = project.root
            return made
        }
        value.prepareEnabled = prepareEnabled
        return value
    }
}

@MainActor
final class OutboxStore: ObservableObject {
    /// Oldest first: the order they were written is the order they go.
    @Published private(set) var entries: [OutboxEntry] = []
    @Published private(set) var catalogue = BoardCatalogue()
    /// The half-typed card, one slot for the whole phone. Written by the
    /// composer as it is typed, forced to disk by the app's own lock handler.
    @Published private(set) var draft: ComposerDraft?
    /// Bumped when a banked draft should reopen itself. `FocusRouter`'s
    /// shape: a request that routinely arrives before the view that answers
    /// it exists, so it is a sequence number rather than a one-shot call.
    @Published private(set) var resumeSignal = 0
    /// The tab a resume is aimed at. Read, never taken.
    @Published private(set) var resumeTab: PhoneTab?

    /// A transport failure rather than a refusal. `PhoneClient` spells the
    /// middle three; `post`'s in-flight collision — the sweep hitting a verb
    /// a live composer still holds — rides in as the shared constant, so a
    /// rewording cannot split the guard from this classification. The empty
    /// string is a cancelled request's teardown. None of these may ever be
    /// painted on an entry as an error.
    static let transportSentences: Set<String> = [
        "", "Could not reach the Mac.", "Bad address.", "no longer paired",
        PhoneClient.stillSendingRefusal,
        // The queue's own dedupe, for the same reason: a live composer's
        // press already in the queue is not the Mac refusing the card.
        PhoneClient.queuedTwiceRefusal,
        // Not a refusal of the card: the photo leg is home-only, and a
        // banked card with a photo simply waits for the next home poll.
        PhoneActions.photosNeedHome,
    ]
    static let waitingLine = "will send when the Mac is back in reach"
    /// A refused entry's first retry delay, doubled per consecutive refusal.
    static let backoffStart: TimeInterval = 10
    static let backoffCap: TimeInterval = 300

    /// Published, and that is the point rather than decoration: a person's
    /// RETRY re-enters `sync`, and a sweep already running returns at the
    /// guard below, so a view-local flag around that call would clear in the
    /// same frame and show nothing. The row reads this instead.
    @Published private(set) var syncing = false
    private var lastCatalogueBytes: Data?
    private let fm = FileManager.default
    /// The armed re-open, consumed by the tab root that owns it.
    private var pendingResumeTab: PhoneTab?
    /// The same tab, **not** consumed: `ContentView` reads it to bring the
    /// tab to the front. A single consumable slot would race — the two
    /// observers of `resumeSignal` fire in no defined order, and a root that
    /// took the resume first would leave the selection where it was.
    /// The debounced draft write. Cancelled and re-armed per stash, and
    /// cancelled outright by `flushDraftNow()`.
    private var draftWrite: Task<Void, Never>?
    /// How many composers are mounted on each staging id. `TabView` keeps an
    /// inactive tab's hierarchy alive and the banked draft is one slot for the
    /// whole phone, so two tabs can end up holding the same id — and Discard
    /// on the second one used to delete the folder the first was still filling,
    /// leaving it showing photos whose files were gone. Counted rather than
    /// flagged: the pair mounts and unmounts in no fixed order.
    private var composerHolders: [String: Int] = [:]
    private static let draftDebounce: Duration = .seconds(2)

    // --- where it lives -------------------------------------------------

    private var support: URL? {
        try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask,
                    appropriateFor: nil, create: true)
    }

    private var entriesURL: URL? { support?.appendingPathComponent("outbox.json") }
    private var catalogueURL: URL? { support?.appendingPathComponent("catalogue.json") }
    private var draftURL: URL? { support?.appendingPathComponent("draft.json") }
    private var photosRoot: URL? { support?.appendingPathComponent("outbox", isDirectory: true) }

    func folder(for id: String) -> URL? {
        guard !id.isEmpty else { return nil }
        return photosRoot?.appendingPathComponent(id, isDirectory: true)
    }

    // --- persistence ----------------------------------------------------

    /// Read behind the Face ID gate, never before it: the file holds the
    /// person's own words and is written with `.completeFileProtection`.
    func load() {
        if let url = entriesURL, let data = try? Data(contentsOf: url) {
            let tolerated = (try? JSONDecoder().decode([Tolerated].self, from: data)) ?? []
            entries = tolerated.compactMap(\.entry).sorted { $0.createdAt < $1.createdAt }
        }
        if let url = catalogueURL, let data = try? Data(contentsOf: url) {
            if let decoded = try? JSONDecoder().decode(BoardCatalogue.self, from: data) {
                catalogue = decoded
                lastCatalogueBytes = data
            }
        }
        loadDraft()
        sweepOrphanFolders()
    }

    /// Tolerant: garbage, a missing file or a draft with no id all read as no
    /// draft. **The entry wins on a collision** — if an `OutboxEntry` already
    /// carries this id the card is queued, and resurrecting the draft would
    /// send it twice. That covers a crash between `enqueue` and `clearDraft`.
    private func loadDraft() {
        guard let url = draftURL, let data = try? Data(contentsOf: url) else { return }
        guard let decoded = try? JSONDecoder().decode(ComposerDraft.self, from: data) else {
            try? fm.removeItem(at: url)
            return
        }
        if entries.contains(where: { $0.id == decoded.id }) {
            try? fm.removeItem(at: url)
            draft = nil
            return
        }
        draft = decoded
    }

    private struct Tolerated: Decodable {
        let entry: OutboxEntry?
        init(from decoder: Decoder) throws {
            entry = try? OutboxEntry(from: decoder)
        }
    }

    private func persist() {
        guard let url = entriesURL else { return }
        guard let data = try? JSONEncoder().encode(entries) else { return }
        write(data, to: url)
    }

    private func write(_ data: Data, to url: URL) {
        do {
            try data.write(to: url, options: [.atomic, .completeFileProtection])
        } catch {
            // A store that cannot be written is not a reason to lose the
            // card that is already in memory.
        }
    }

    /// No entry owns this folder any more. Called from `load()` alone, before
    /// any composer exists, so the sweep cannot race one being filled.
    private func sweepOrphanFolders() {
        guard let root = photosRoot else { return }
        // The banked draft's folder is not an orphan: forgetting it here
        // silently eats a half-typed card's photos, one unlock later.
        var known = Set(entries.map(\.id))
        if let id = draft?.id, !id.isEmpty { known.insert(id) }
        let found = (try? fm.contentsOfDirectory(at: root,
                                                 includingPropertiesForKeys: nil)) ?? []
        for url in found where !known.contains(url.lastPathComponent) {
            try? fm.removeItem(at: url)
        }
    }

    // --- who is holding a staging id ------------------------------------

    /// A composer mounted on this id. Balanced by `releaseComposer`; the two
    /// ride `onAppear`/`onDisappear`, which pair up across a push and a pop.
    func holdComposer(_ id: String) {
        guard !id.isEmpty else { return }
        composerHolders[id, default: 0] += 1
    }

    func releaseComposer(_ id: String) {
        guard !id.isEmpty, let held = composerHolders[id] else { return }
        if held <= 1 {
            composerHolders[id] = nil
        } else {
            composerHolders[id] = held - 1
        }
    }

    /// Composers currently mounted on this id — **including the one asking**,
    /// which is why the delete gate below reads "no more than me".
    func composerHolderCount(_ id: String) -> Int {
        composerHolders[id] ?? 0
    }

    // --- the draft slot -------------------------------------------------

    /// The composer, saying what it currently holds. A draft with nothing
    /// worth keeping empties the slot instead of banking an empty form.
    ///
    /// The disk write is debounced ~2s so a keystroke is not a file write;
    /// durability across the app's own lock is `flushDraftNow()`'s job, not
    /// this timer's.
    func stashDraft(_ d: ComposerDraft) {
        draft = d.worthKeeping ? d : nil
        draftWrite?.cancel()
        draftWrite = Task { [weak self] in
            try? await Task.sleep(for: Self.draftDebounce)
            guard !Task.isCancelled else { return }
            self?.writeDraft()
        }
    }

    /// Write the slot to disk **now**, synchronously. `BobPhoneApp` calls
    /// this before `lock.lock()` in the same handler body: the lock tears the
    /// whole view hierarchy down, so this ordering — not the debounce — is
    /// what makes the draft survive.
    func flushDraftNow() {
        draftWrite?.cancel()
        draftWrite = nil
        writeDraft()
    }

    private func writeDraft() {
        guard let url = draftURL else { return }
        guard let d = draft else {
            try? fm.removeItem(at: url)
            return
        }
        guard let data = try? JSONEncoder().encode(d) else { return }
        write(data, to: url)
    }

    /// Throw the draft away. `deletingPhotos` is Discard's own half: the
    /// phone-side folder goes with it. A successful save or bank passes
    /// false — the entry, or the Mac, owns the folder from then on.
    func clearDraft(deletingPhotos: Bool) {
        let id = draft?.id ?? ""
        draftWrite?.cancel()
        draftWrite = nil
        draft = nil
        pendingResumeTab = nil
        resumeTab = nil
        if let url = draftURL { try? fm.removeItem(at: url) }
        // Only where nothing but the discarding composer still holds the id.
        // A second mounted composer on the same draft keeps its own view of
        // these files, and deleting them under it is silent data loss; the
        // folder then ages out through `sweepOrphanFolders` on the next
        // `load()`, by which time no composer survives to be surprised.
        if deletingPhotos, composerHolderCount(id) <= 1,
           let dir = folder(for: id) {
            try? fm.removeItem(at: dir)
        }
    }

    // --- reopening it ---------------------------------------------------

    /// Called right after `load()` on the unlock path. Arms a re-open only
    /// for a draft that was on screen when the app went away and still holds
    /// something worth coming back to.
    func armResumeIfNeeded() {
        pendingResumeTab = nil
        resumeTab = nil
        guard let d = draft, d.open, d.worthKeeping else { return }
        let tab = PhoneTab(stored: d.tab) ?? .needs
        pendingResumeTab = tab
        resumeTab = tab
        resumeSignal &+= 1
    }

    /// Consumed on apply: true once, for the matching tab only. Asked from
    /// both `.onAppear` and `.onChange(of: resumeSignal)` because `TabView`
    /// mounts a never-visited tab lazily — the root the resume selects may
    /// not exist until the selection lands.
    func takeResume(for tab: PhoneTab) -> Bool {
        guard pendingResumeTab == tab else { return false }
        pendingResumeTab = nil
        return true
    }

    // --- the catalogue --------------------------------------------------

    /// Called on every live poll. Writes only when the bytes actually differ,
    /// so a 4s poll is not a 4s disk write.
    func remember(board: Board) {
        guard board.available else { return }
        let made = BoardCatalogue(board: board)
        guard let data = try? JSONEncoder().encode(made) else { return }
        catalogue = made
        guard data != lastCatalogueBytes else { return }
        lastCatalogueBytes = data
        if let url = catalogueURL { write(data, to: url) }
    }

    func catalogueBoard() -> Board { catalogue.board() }

    // --- the queue ------------------------------------------------------

    @discardableResult
    func enqueue(id: String, title: String, summary: String, prompt: String,
                 tool: String, model: String, project: String, root: String,
                 workflow: String, remotePaths: [String],
                 localPhotos: [String], beneficiary: String = "",
                 intendedBenefit: String = "",
                 successCriterion: String = "", effort: String = "",
                 priority: String = "", area: String = "",
                 kind: String = "") -> OutboxEntry {
        let trim = { (text: String) in
            text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let entry = OutboxEntry(
            id: id, title: trim(title), summary: trim(summary), prompt: prompt,
            tool: tool, model: model, project: project, root: root,
            workflow: workflow, remotePaths: remotePaths,
            localPhotos: localPhotos, createdAt: Date().timeIntervalSince1970,
            beneficiary: beneficiary, intendedBenefit: intendedBenefit,
            successCriterion: successCriterion, priority: priority, area: area,
            kind: kind, effort: effort)
        entries.removeAll { $0.id == entry.id }
        entries.append(entry)
        entries.sort { $0.createdAt < $1.createdAt }
        persist()
        return entry
    }

    /// Drop the card and everything of it still on this phone.
    func remove(_ id: String) {
        entries.removeAll { $0.id == id }
        if let dir = folder(for: id) {
            try? fm.removeItem(at: dir)
        }
        persist()
    }

    // --- photos on the phone --------------------------------------------

    /// Copy one picked photo into this card's own folder. The bytes are
    /// written off the main actor — a 20 MB write on it stalls the poll loop
    /// and every button on screen.
    func stagePhoto(stagingId: String, name: String, data: Data) async -> Bool {
        guard let dir = folder(for: stagingId) else { return false }
        let target = dir.appendingPathComponent(name)
        return await Task.detached(priority: .utility) { () -> Bool in
            do {
                try FileManager.default.createDirectory(
                    at: dir, withIntermediateDirectories: true)
                try data.write(to: target, options: [.atomic, .completeFileProtection])
                return true
            } catch {
                return false
            }
        }.value
    }

    func dropPhoto(stagingId: String, name: String) {
        guard let dir = folder(for: stagingId) else { return }
        try? fm.removeItem(at: dir.appendingPathComponent(name))
    }

    private func photoData(stagingId: String, name: String) async -> Data? {
        guard let dir = folder(for: stagingId) else { return nil }
        let source = dir.appendingPathComponent(name)
        return await Task.detached(priority: .utility) { () -> Data? in
            try? Data(contentsOf: source)
        }.value
    }

    // --- the sweep ------------------------------------------------------

    /// Send whatever is waiting, silently, in the order it was written.
    /// One at a time and serialized by `syncing`: `onLive` fires every 4s and
    /// a sweep with a 20 MB photo in it outlives several polls.
    func sync(using client: PhoneClient) async {
        guard !syncing, !entries.isEmpty else { return }
        syncing = true
        defer { syncing = false }

        for entry in entries.sorted(by: { $0.createdAt < $1.createdAt }) {
            guard entries.contains(where: { $0.id == entry.id }) else { continue }
            if alreadyLanded(entry, on: client.snapshot.board) {
                remove(entry.id)
                continue
            }
            if uncertain(entry), !client.snapshot.board.available {
                // Never resend what cannot be checked.
                continue
            }
            if entries.first(where: { $0.id == entry.id })?.held == true {
                // A refusal's backoff window. The landed check above still
                // ran, so a card that did arrive is removed even while held.
                continue
            }
            switch await sendPhotos(of: entry.id, using: client) {
            case .sent:
                break
            case .refused:
                continue
            case .unreachable:
                return
            }
            guard let current = entries.first(where: { $0.id == entry.id }) else { continue }
            note(current.id) { $0.attempts += 1; $0.lastError = "" }
            let result = await client.post(action: PhoneActions.boardCreate,
                                           fields: fields(for: current))
            if result.ok {
                remove(current.id)
                continue
            }
            if Self.transportSentences.contains(result.detail) {
                // The Mac is gone again, or a live composer holds the verb.
                // Retried on a later poll; nothing is painted on the entry.
                return
            }
            // The Mac's own words. Kept, worn, and the ones behind it still
            // go — with a doubling backoff, because a standing refusal
            // re-POSTed every 4s is a hammer, not a retry.
            hold(current.id, error: result.detail)
        }
    }

    /// Record a refusal and arm its backoff: start at `backoffStart`,
    /// double per consecutive refusal, cap at `backoffCap`.
    private func hold(_ id: String, error: String) {
        note(id) { entry in
            entry.lastError = error
            let delay = min(Self.backoffCap,
                            max(Self.backoffStart, entry.retryDelay * 2))
            entry.retryDelay = delay
            entry.heldUntil = Date().timeIntervalSince1970 + delay
        }
    }

    /// The visible RETRY — a person overriding the backoff. The doubling
    /// resets too: their press is a fresh decision, not attempt n+1.
    func retryNow(_ id: String) {
        note(id) { entry in
            entry.heldUntil = 0
            entry.retryDelay = 0
        }
    }

    /// Only a scarred entry doubts itself: a fresh card sends unconditionally,
    /// so two deliberately identical cards both land.
    private func uncertain(_ entry: OutboxEntry) -> Bool { entry.attempts > 0 }

    /// The card the earlier send may already have made. Prefer the stored
    /// `create_token` (any column, no scar) over the scarred Prep
    /// title/project/summary match, which remains the older-Mac fallback.
    /// The snapshot clamps a long summary (`BOARD_SNAPSHOT_SUMMARY_CHARS`)
    /// and says so, so a truncated summary is matched on its prefix rather
    /// than exactly.
    private func alreadyLanded(_ entry: OutboxEntry, on board: Board) -> Bool {
        if !entry.id.isEmpty,
           board.cards.contains(where: { $0.createToken == entry.id }) {
            return true
        }
        guard uncertain(entry), board.available else { return false }
        return board.cards.contains { card in
            card.column == "prep"
                && card.title == entry.title
                && card.project == entry.project
                && summaryMatches(entry.summary, card)
        }
    }

    private func summaryMatches(_ summary: String, _ card: BoardCard) -> Bool {
        if card.summaryTruncated {
            return summary.hasPrefix(card.summary) && !card.summary.isEmpty
        }
        return card.summary == summary
    }

    private enum PhotoLeg {
        case sent
        case refused
        case unreachable
    }

    /// Photos before the card: it must arrive whole or not yet.
    private func sendPhotos(of id: String, using client: PhoneClient) async -> PhotoLeg {
        while let entry = entries.first(where: { $0.id == id }),
              let name = entry.localPhotos.first {
            guard let data = await photoData(stagingId: id, name: name),
                  !data.isEmpty else {
                // Gone from under us. Drop the name rather than loop on it.
                note(id) { $0.localPhotos.removeAll { $0 == name } }
                continue
            }
            let result = await client.upload(stagingId: id, name: name, data: data)
            if result.ok, !result.detail.isEmpty {
                note(id) {
                    $0.remotePaths.append(result.detail)
                    $0.localPhotos.removeAll { $0 == name }
                }
                dropPhoto(stagingId: id, name: name)
                continue
            }
            if Self.transportSentences.contains(result.detail) {
                return .unreachable
            }
            // A refused photo blocks its card, so it wears the same backoff.
            hold(id, error: result.detail)
            return .refused
        }
        return .sent
    }

    private func fields(for entry: OutboxEntry) -> [String: String] {
        var fields = [
            "title": entry.title,
            "summary": entry.summary,
            "prompt": entry.prompt,
            "tool": entry.tool,
            "model": entry.model,
            "column_name": "prep",
        ]
        // Only when chosen: a Default card's create stays byte-identical.
        if !entry.effort.isEmpty {
            fields["effort"] = entry.effort
        }
        if !entry.remotePaths.isEmpty {
            fields["attachments"] = entry.remotePaths.joined(separator: "\n")
        }
        if !entry.workflow.isEmpty {
            fields["workflow"] = entry.workflow
        }
        // Only when typed: the boxes were drawn only against a Mac that
        // said it takes them, and an empty key must never ride.
        if !entry.beneficiary.isEmpty {
            fields["beneficiary"] = entry.beneficiary
        }
        if !entry.intendedBenefit.isEmpty {
            fields["intended_benefit"] = entry.intendedBenefit
        }
        if !entry.successCriterion.isEmpty {
            fields["success_criterion"] = entry.successCriterion
        }
        if !entry.area.isEmpty { fields["area"] = entry.area }
        // A scout only: a build card's kind is the store's default, and an
        // older Mac's door never meets the key.
        if entry.kind == "scout" { fields["kind"] = entry.kind }
        if !entry.priority.isEmpty {
            fields["priority"] = entry.priority
        }
        if !entry.root.isEmpty {
            fields["project"] = entry.project
            fields["root"] = entry.root
        }
        if !entry.id.isEmpty {
            fields["create_token"] = entry.id
        }
        return fields
    }

    /// One edit to one entry, published and written through. Every mutation
    /// on the sweep goes this way, so a send cut off mid-air always leaves
    /// the state it had already reached on disk.
    private func note(_ id: String, _ change: (inout OutboxEntry) -> Void) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        change(&entries[index])
        persist()
    }
}
