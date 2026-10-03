import Foundation

/// Decoding that tolerates a field that is not there.
///
/// Copied from the panel's `Models.swift`: Swift's synthesized `Decodable`
/// throws on a missing key even when the property has a default, and
/// `/api/state` is legitimately ragged. One absent key must not blank the
/// phone.
extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }

    func maybe<T: Decodable>(_ key: Key) -> T? {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil)
    }
}

struct Snapshot: Decodable {
    var generatedAt: Double = 0
    var counts = Counts()
    /// The Mac's fleet cost and tokens, read verbatim. Absent decodes as
    /// no figures — nil, never zero.
    var fleetFigures = FleetFigures()
    var agents = Agents()
    var board = Board()
    var collaboration: Collaboration?
    var notifications: [Notification] = []
    var permissions: [PermissionPrompt] = []
    var devices = PhoneDevices()
    /// Enrolment as the Mac already publishes it. The knowledge picker reads
    /// this existing section, never an all-projects dump.
    var enrollment = Enrollment()
    /// The Mac's fingerprint of this picture, quoted back on the next poll so
    /// a quiet Mac can answer "nothing changed" in one line. An older Mac
    /// sends none, which leaves this `""` and the phone never asks
    /// conditionally.
    var stateDigest = ""
    /// Needs you acknowledgements. Older Macs send none; Acknowledge is absent.
    var inbox = InboxAcks()
    /// Open burst alerts off the phone doors' access log. Older Macs send
    /// none, which decodes `available == false` and lists nothing.
    var security = SecuritySection()
    /// Mission Control — alive, which session, where. Older Macs send none,
    /// which decodes `available == false` and draws the Comm tab's one
    /// sentence.
    var mission = MissionSection()
    /// The Review section: runs with their findings, picks and step ledger.
    /// Older Macs send none, which decodes `available == false` and the
    /// Review screen draws one sentence.
    var review = ReviewSection()
    /// The host Mac's battery for the Fleet tab. Older Macs send none,
    /// which decodes `available == false` and Fleet draws nothing.
    var power = PowerSection()
    /// Rebuild & restart: whether this Mac can rebuild, what the button says,
    /// whether one is running and how the last one ended. Older Macs send
    /// none, which decodes `available == false` and the Menu tile stays dim.
    var rebuild = RebuildSection()

    /// Live decision subjects, one per agent and one per card.
    var decisionItems: [PhoneInboxItem] { PhoneInbox.items(from: self) }

    /// Every decision subject counts: nothing FYI is listed any more, so
    /// the badge is the length of the list and nothing else.
    var needsYouCount: Int { decisionItems.count }

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case counts, agents, board, notifications, permissions, devices, collaboration
        case fleetFigures = "fleet_figures"
        case enrollment, inbox, security, mission, power, rebuild, review
        case stateDigest = "state_digest"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = c.value(.generatedAt, 0)
        counts = c.value(.counts, Counts())
        fleetFigures = c.value(.fleetFigures, FleetFigures())
        agents = c.value(.agents, Agents())
        board = c.value(.board, Board())
        collaboration = c.maybe(.collaboration)
        notifications = c.value(.notifications, [])
        permissions = c.value(.permissions, [])
        devices = c.value(.devices, PhoneDevices())
        enrollment = c.value(.enrollment, Enrollment())
        inbox = c.value(.inbox, InboxAcks())
        security = c.value(.security, SecuritySection())
        mission = c.value(.mission, MissionSection())
        power = c.value(.power, PowerSection())
        rebuild = c.value(.rebuild, RebuildSection())
        review = c.value(.review, ReviewSection())
        stateDigest = c.value(.stateDigest, "")
    }

    init() {}

    /// The sections a delta answer may leave out and name in
    /// `sections_unchanged`. Raw values are the wire names — the daemon's
    /// `_OMITTABLE_SECTIONS` minus `signals` and `mesh`, which the phone does
    /// not decode; `power` is the phone's alone (the panel's Mac shows its
    /// own battery). `generated_at`, `fleet_figures` and `state_digest` are not
    /// sections and are never carried.
    enum Section: String, CaseIterable {
        case counts, notifications, agents, collaboration, permissions, board
        case enrollment, devices, inbox, security, mission, power, rebuild, review
    }

    /// Take one section, named on the wire, from the picture already held.
    ///
    /// `false` for a name that is not a `Section` — nothing is carried and
    /// nothing is blanked; the key is simply absent, as it is today for a
    /// section the phone does not draw. The one place the phone enumerates
    /// its sections, the panel's `DaemonClient.copy` shape.
    mutating func carry(_ name: String, from held: Snapshot) -> Bool {
        guard let section = Section(rawValue: name) else { return false }
        switch section {
        case .counts: counts = held.counts
        case .notifications: notifications = held.notifications
        case .agents: agents = held.agents
        case .collaboration: collaboration = held.collaboration
        case .permissions: permissions = held.permissions
        case .board: board = held.board
        case .enrollment: enrollment = held.enrollment
        case .devices: devices = held.devices
        case .inbox: inbox = held.inbox
        case .security: security = held.security
        case .mission: mission = held.mission
        case .power: power = held.power
        case .rebuild: rebuild = held.rebuild
        case .review: review = held.review
        }
        return true
    }
}

/// The `mission` section of the state (`BobDaemon.mission_snapshot`):
/// whether Mission Control — the Mac's one standing, read-only chief of
/// staff — is alive, which session it is and where it lives. `available` is
/// stated by the Mac; absent decodes false and the Comm tab draws one
/// sentence. Never a handle: the tab aims by `sessionId` alone.
struct MissionSection: Decodable, Equatable {
    var available = false
    var alive = false
    var exited = false
    var sessionId = ""
    var root = ""
    var name = ""
    var openedAt: Double = 0

    enum CodingKeys: String, CodingKey {
        case available, alive, exited, root, name
        case sessionId = "session_id"
        case openedAt = "opened_at"
    }

    init(available: Bool = false, alive: Bool = false, exited: Bool = false,
         sessionId: String = "", root: String = "", name: String = "",
         openedAt: Double = 0) {
        self.available = available
        self.alive = alive
        self.exited = exited
        self.sessionId = sessionId
        self.root = root
        self.name = name
        self.openedAt = openedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        alive = c.value(.alive, false)
        exited = c.value(.exited, false)
        sessionId = c.value(.sessionId, "")
        root = c.value(.root, "")
        name = c.value(.name, "")
        openedAt = c.value(.openedAt, 0)
    }
}

/// The `rebuild` section of the state (`BobDaemon.rebuild_snapshot`): whether
/// this Mac runs from a source checkout and can rebuild at all, the label the
/// Mac's own button wears, whether one is in flight and how the last one
/// ended. Every key has a default and the two clocks are optional, so a
/// section from an older or newer Mac never blanks the frame. No path rides
/// here. `RebuildRules` owns the words.
struct RebuildSection: Decodable, Equatable {
    var available = false
    var label = ""
    var rebuilding = false
    /// The Mac is already restarting, so a press is refused in words.
    var restarting = false
    var startedAt: Double? = nil
    var lastOutcome = ""
    var lastFinishedAt: Double? = nil
    var lastError = ""

    enum CodingKeys: String, CodingKey {
        case available, label, rebuilding, restarting
        case startedAt = "started_at"
        case lastOutcome = "last_outcome"
        case lastFinishedAt = "last_finished_at"
        case lastError = "last_error"
    }

    init(available: Bool = false, label: String = "", rebuilding: Bool = false,
         restarting: Bool = false,
         startedAt: Double? = nil, lastOutcome: String = "",
         lastFinishedAt: Double? = nil, lastError: String = "") {
        self.available = available
        self.label = label
        self.rebuilding = rebuilding
        self.restarting = restarting
        self.startedAt = startedAt
        self.lastOutcome = lastOutcome
        self.lastFinishedAt = lastFinishedAt
        self.lastError = lastError
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        label = c.value(.label, "")
        rebuilding = c.value(.rebuilding, false)
        restarting = c.value(.restarting, false)
        startedAt = c.maybe(.startedAt)
        lastOutcome = c.value(.lastOutcome, "")
        lastFinishedAt = c.maybe(.lastFinishedAt)
        lastError = c.value(.lastError, "")
    }
}

/// The `review` section of the state (`BobDaemon.review_snapshot`): the
/// Review runs, newest first, each with its graded findings, the picks
/// the person made, the after-steps and the step ledger the run reported.
/// `available` is stated by the Mac; absent decodes false and the screen
/// draws one sentence. Every field is defaulted and the arrays decode
/// element by element, so one malformed finding drops that finding and never
/// the frame. Never a handle, a digest or a folder path.
struct ReviewSection: Decodable, Equatable {
    var available = false
    var runs: [ReviewRun] = []

    enum CodingKeys: String, CodingKey { case available, runs }

    init(available: Bool = false, runs: [ReviewRun] = []) {
        self.available = available
        self.runs = runs
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        let rows: [ReviewLossy<ReviewRun>] = c.value(.runs, [])
        runs = rows.compactMap(\.value)
    }

    func run(id: String) -> ReviewRun? { runs.first { $0.id == id } }
}

struct ReviewRun: Decodable, Equatable, Identifiable {
    var id = ""
    var root = ""
    var project = ""
    var tool = ""
    var state = ""
    var scope = ""
    var scopeLine = ""
    var upstream = ""
    var sessionId = ""
    var startedAt: Double = 0
    var findingsAt: Double = 0
    var decidedAt: Double = 0
    var finishedAt: Double = 0
    var verdict = ""
    var error = ""
    var truncated = false
    var findings: [ReviewFinding] = []
    var picks: [Int] = []
    var steps: [ReviewStep] = []
    var ledger: [ReviewLedgerLine] = []

    enum CodingKeys: String, CodingKey {
        case id, root, project, tool, state, scope, upstream, verdict, error
        case truncated, findings, picks, steps, ledger
        case scopeLine = "scope_line"
        case sessionId = "session_id"
        case startedAt = "started_at"
        case findingsAt = "findings_at"
        case decidedAt = "decided_at"
        case finishedAt = "finished_at"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        root = c.value(.root, "")
        project = c.value(.project, "")
        tool = c.value(.tool, "")
        state = c.value(.state, "")
        scope = c.value(.scope, "")
        scopeLine = c.value(.scopeLine, "")
        upstream = c.value(.upstream, "")
        sessionId = c.value(.sessionId, "")
        startedAt = c.value(.startedAt, 0)
        findingsAt = c.value(.findingsAt, 0)
        decidedAt = c.value(.decidedAt, 0)
        finishedAt = c.value(.finishedAt, 0)
        verdict = c.value(.verdict, "")
        error = c.value(.error, "")
        truncated = c.value(.truncated, false)
        let found: [ReviewLossy<ReviewFinding>] = c.value(.findings, [])
        findings = found.compactMap(\.value)
        let picked: [ReviewLossy<Int>] = c.value(.picks, [])
        picks = picked.compactMap(\.value)
        let stepRows: [ReviewLossy<ReviewStep>] = c.value(.steps, [])
        steps = stepRows.compactMap(\.value)
        let lines: [ReviewLossy<ReviewLedgerLine>] = c.value(.ledger, [])
        ledger = lines.compactMap(\.value)
    }
}

struct ReviewFinding: Decodable, Equatable, Identifiable {
    var index = 0
    var grade = ""
    var line = ""
    var `where` = ""
    var fix = ""
    var confidence = ""

    var id: Int { index }

    enum CodingKeys: String, CodingKey {
        case index, grade, line, `where`, fix, confidence
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        index = c.value(.index, 0)
        grade = c.value(.grade, "")
        line = c.value(.line, "")
        `where` = c.value(.where, "")
        fix = c.value(.fix, "")
        confidence = c.value(.confidence, "")
    }
}

struct ReviewStep: Decodable, Equatable, Identifiable {
    var id = ""
    var label = ""
    var status = ""
    var words = ""
    /// Only on an offer: how the step is done. A run's snapshot omits it.
    var how = ""

    enum CodingKeys: String, CodingKey { case id, label, status, words, how }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        label = c.value(.label, "")
        status = c.value(.status, "")
        words = c.value(.words, "")
        how = c.value(.how, "")
    }
}

struct ReviewLedgerLine: Decodable, Equatable, Identifiable {
    var id = ""
    var status = ""
    var words = ""

    enum CodingKeys: String, CodingKey { case id, status, words }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        status = c.value(.status, "")
        words = c.value(.words, "")
    }
}

/// What a review of one project would cover and may do afterwards, read
/// through the sealed `review_offer` kind (`BobDaemon.review_offer`).
struct ReviewOffer: Decodable, Equatable {
    var available = false
    var root = ""
    var project = ""
    var scope = ""
    var scopeLine = ""
    var upstream = ""
    var reason = ""
    var steps: [ReviewStep] = []

    enum CodingKeys: String, CodingKey {
        case available, root, project, scope, upstream, reason, steps
        case scopeLine = "scope_line"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        root = c.value(.root, "")
        project = c.value(.project, "")
        scope = c.value(.scope, "")
        scopeLine = c.value(.scopeLine, "")
        upstream = c.value(.upstream, "")
        reason = c.value(.reason, "")
        let rows: [ReviewLossy<ReviewStep>] = c.value(.steps, [])
        steps = rows.compactMap(\.value)
    }
}

/// One ragged element: a malformed one decodes to nil and is dropped.
struct ReviewLossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

/// The `power` section of the state (`BobDaemon.power_snapshot`): the host
/// Mac's battery as `pmset -g batt` reads it — a whole `percent`, the
/// `source` it draws from (`ac`, `battery`) and the `charge` word
/// (`charging`, `discharging`, `charged`, `not_charging`, `finishing`).
/// `available` is stated by the Mac; absent decodes false and Fleet draws
/// nothing, as it does for `present == false` (a desktop). No clock rides
/// here. `MacPower.line` (`FleetView.swift`) owns the words.
struct PowerSection: Decodable, Equatable {
    var available = false
    var present = false
    var percent: Int? = nil
    var source = ""
    var charge = ""

    enum CodingKeys: String, CodingKey {
        case available, present, percent, source, charge
    }

    init(available: Bool = false, present: Bool = false, percent: Int? = nil,
         source: String = "", charge: String = "") {
        self.available = available
        self.present = present
        self.percent = percent
        self.source = source
        self.charge = charge
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        present = c.value(.present, false)
        percent = c.maybe(.percent)
        source = c.value(.source, "")
        charge = c.value(.charge, "")
    }
}

/// The Mac's open burst alerts (`BobDaemon.security_snapshot`). `available`
/// is stated by the Mac; absent decodes false and draws nothing.
struct SecuritySection: Decodable, Equatable {
    var available = false
    var alerts: [AccessAlert] = []

    enum CodingKeys: String, CodingKey { case available, alerts }

    init(available: Bool = false, alerts: [AccessAlert] = []) {
        self.available = available
        self.alerts = alerts
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        alerts = c.value(.alerts, [])
    }
}

/// One burst alert: several refused knocks from one source at one door.
/// The sentence is the Mac's own (`access_log.sentence`), drawn verbatim.
struct AccessAlert: Decodable, Equatable, Hashable, Identifiable {
    var id = ""
    var ts: Double = 0
    var door = ""
    /// The door in a person's word ("Wi-Fi", "relay"), composed by the
    /// daemon; absent from an older Mac, in which case the token stands in.
    var doorWord = ""
    var peer = ""
    var count = 0
    var windowSeconds = 0
    var text = ""

    enum CodingKeys: String, CodingKey {
        case id, ts, door, peer, count, text
        case doorWord = "door_word"
        case windowSeconds = "window_seconds"
    }

    init(id: String = "", ts: Double = 0, door: String = "", doorWord: String = "",
         peer: String = "", count: Int = 0, windowSeconds: Int = 0,
         text: String = "") {
        self.id = id
        self.ts = ts
        self.door = door
        self.doorWord = doorWord
        self.peer = peer
        self.count = count
        self.windowSeconds = windowSeconds
        self.text = text
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        ts = c.value(.ts, 0)
        door = c.value(.door, "")
        doorWord = c.value(.doorWord, "")
        peer = c.value(.peer, "")
        count = c.value(.count, 0)
        windowSeconds = c.value(.windowSeconds, 0)
        text = c.value(.text, "")
    }

    /// What the Inbox draws as the door: the daemon's word, else the token.
    var doorText: String { doorWord.isEmpty ? door : doorWord }
}

/// One line of the Mac's access log (`access_log.PUBLISHED_KEYS`).
struct AccessLogEntry: Decodable, Equatable, Hashable, Identifiable {
    var id = ""
    var ts: Double = 0
    var kind = ""
    var door = ""
    var peer = ""
    var reason = ""
    var deviceId = ""
    var text = ""
    var count = 0
    var windowSeconds = 0
    var alertId = ""
    /// A `timing` line's figures (`access_log.HOP_KEYS`); empty on every
    /// other kind and on a line from an older Mac.
    var hops: [String: Double] = [:]

    enum CodingKeys: String, CodingKey {
        case id, ts, kind, door, peer, reason, text, count, hops
        case deviceId = "device_id"
        case windowSeconds = "window_seconds"
        case alertId = "alert_id"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        ts = c.value(.ts, 0)
        kind = c.value(.kind, "")
        door = c.value(.door, "")
        peer = c.value(.peer, "")
        reason = c.value(.reason, "")
        deviceId = c.value(.deviceId, "")
        text = c.value(.text, "")
        count = c.value(.count, 0)
        windowSeconds = c.value(.windowSeconds, 0)
        alertId = c.value(.alertId, "")
        hops = c.value(.hops, [:])
    }
}

/// The sealed `access_log` read's answer. `available` is stated by the Mac.
struct AccessLogReport: Decodable, Equatable {
    var available = false
    var generatedAt: Double = 0
    var entries: [AccessLogEntry] = []
    var openAlerts: [AccessAlert] = []

    enum CodingKeys: String, CodingKey {
        case available, entries
        case generatedAt = "generated_at"
        case openAlerts = "open_alerts"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        generatedAt = c.value(.generatedAt, 0)
        entries = c.value(.entries, [])
        openAlerts = c.value(.openAlerts, [])
    }
}

struct InboxAcks: Decodable, Equatable {
    var available = false
    var acks: [InboxAckRecord] = []

    enum CodingKeys: String, CodingKey { case available, acks }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        acks = c.value(.acks, [])
    }
}

struct InboxAckRecord: Decodable, Equatable, Hashable {
    var key = ""
    var kind = ""
    var fp = ""

    enum CodingKeys: String, CodingKey { case key, kind, fp }

    init(key: String = "", kind: String = "", fp: String = "") {
        self.key = key
        self.kind = kind
        self.fp = fp
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.value(.key, "")
        kind = c.value(.kind, "")
        fp = c.value(.fp, "")
    }
}

/// Enrolment as the Mac publishes it on `/api/state`. Available is stated,
/// never inferred from an empty list.
struct Enrollment: Decodable, Equatable {
    var available = false
    var enrolled: [EnrolledProject] = []
    var pending: [EnrolledProject] = []
    var other = 0

    enum CodingKeys: String, CodingKey {
        case available, enrolled, pending, other
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        enrolled = c.value(.enrolled, [])
        pending = c.value(.pending, [])
        other = c.value(.other, 0)
    }

    init() {}
}

struct EnrolledProject: Decodable, Equatable, Identifiable, Hashable {
    var root = ""
    var label = ""
    var lastSeen: Double = 0
    var id: String { root }

    enum CodingKeys: String, CodingKey {
        case root, label
        case lastSeen = "last_seen"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        root = c.value(.root, "")
        label = c.value(.label, "")
        lastSeen = c.value(.lastSeen, 0)
    }

    init(root: String = "", label: String = "") {
        self.root = root
        self.label = label
    }
}

/// The Mac's "keep what you have" reply to a conditional `state` read.
///
/// **This must be decoded before `Snapshot`.** `Snapshot` decodes every key
/// tolerantly, so an unchanged body would decode into a perfectly valid
/// *empty* snapshot — a blank fleet, a blank board, a needs-you count of
/// zero. `unchanged` is therefore a present key set to true, and the marker
/// is read here first.
struct StateAnswer: Decodable {
    var unchanged = false
    var stateDigest = ""
    /// The sections this answer left out because the phone's quoted digest
    /// still matched — the marker, a present list, never an absence. An
    /// older Mac sends none, which decodes empty.
    var sectionsUnchanged: [String] = []
    /// One fingerprint per section of the picture this answer describes,
    /// quoted back as `sections` on the next poll. Empty against an older Mac.
    var sectionDigests: [String: String] = [:]

    enum CodingKeys: String, CodingKey {
        case unchanged
        case stateDigest = "state_digest"
        case sectionsUnchanged = "sections_unchanged"
        case sectionDigests = "section_digests"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        unchanged = c.value(.unchanged, false)
        stateDigest = c.value(.stateDigest, "")
        sectionsUnchanged = c.value(.sectionsUnchanged, [])
        sectionDigests = c.value(.sectionDigests, [:])
    }

    init() {}
}

/// The snapshot's devices section, read for two facts: **this** phone's own
/// away-window expiry, so the countdown line is honest, and where home is
/// *now*. Display and routing only — the Mac is the authority and re-checks
/// the lease per write.
struct PhoneDevices: Decodable {
    var devices: [PhoneDeviceRow] = []
    /// The Mac's own LAN addresses as it reads them this second, best first.
    /// The pairing QR's copy is a photograph taken once: a Mac that changed
    /// address afterwards could never be found at home again, and the phone
    /// lived on the relay — AWAY on its own sofa. Empty against an older Mac,
    /// which is why nothing here is ever *removed* from the stored list.
    var hosts: [String] = []

    enum CodingKeys: String, CodingKey { case devices, hosts }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        devices = c.value(.devices, [])
        hosts = c.value(.hosts, [])
    }

    init() {}

    func leaseExpiresAt(deviceId: String) -> Double {
        devices.first { $0.id == deviceId }?.leaseExpiresAt ?? 0
    }

    /// The Mac's bot, where the Mac publishes its grants — the row carrying
    /// `bot_access`. An older Mac publishes none, and the profile screen's
    /// BOT ACCESS section is then absent.
    var bot: PhoneDeviceRow? {
        devices.first { $0.botAccess != nil }
    }
}

struct PhoneDeviceRow: Decodable {
    var id = ""
    var leaseExpiresAt: Double = 0
    /// The name the Mac shows for this device. Empty against an older Mac.
    var name = ""
    /// The bot's two grants. **`nil` means this row is not the bot** (or an
    /// older Mac said nothing); never a zeroed pair.
    var botAccess: PhoneBotAccess?

    enum CodingKeys: String, CodingKey {
        case id, name
        case leaseExpiresAt = "lease_expires_at"
        case botAccess = "bot_access"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        leaseExpiresAt = c.value(.leaseExpiresAt, 0)
        name = c.value(.name, "")
        botAccess = try? c.decode(PhoneBotAccess.self, forKey: .botAccess)
    }
}

/// One side of the bot's access as the Mac states it: a mode word out of
/// `off`, `1h`, `6h`, `24h`, `forever`, and for a timed mode the epoch it
/// ends at. Drawn, never re-derived.
struct PhoneBotGrant: Decodable, Equatable {
    var mode = ""
    var until: Double = 0

    enum CodingKeys: String, CodingKey { case mode, until }

    init(mode: String = "", until: Double = 0) {
        self.mode = mode
        self.until = until
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mode = c.value(.mode, "")
        until = c.value(.until, 0)
    }
}

/// The bot's two grants, read and write. Tolerant: a half-stated object
/// keeps the other side's default.
struct PhoneBotAccess: Decodable, Equatable {
    var read = PhoneBotGrant()
    var write = PhoneBotGrant()

    enum CodingKeys: String, CodingKey { case read, write }

    init(read: PhoneBotGrant = PhoneBotGrant(),
         write: PhoneBotGrant = PhoneBotGrant()) {
        self.read = read
        self.write = write
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        read = c.value(.read, PhoneBotGrant())
        write = c.value(.write, PhoneBotGrant())
    }
}

struct Counts: Decodable {
    var working = 0
    var idle = 0
    var attention = 0
    var subagents = 0

    enum CodingKeys: String, CodingKey {
        case working, idle, attention, subagents
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        working = c.value(.working, 0)
        idle = c.value(.idle, 0)
        attention = c.value(.attention, 0)
        subagents = c.value(.subagents, 0)
    }

    init() {}
}

/// The live fleet's cost and tokens, as the Mac composed them
/// (`fleet_figures`). Every figure is optional: a missing key is unknown,
/// never zero. `costMeasured` / `costRows` ride along so a reader can see
/// that a nil cost is "nobody measured", not a decode failure.
struct FleetFigures: Decodable, Equatable {
    var costUsd: Double?
    var tokensK: Int?
    /// The last hour's burn (`BurnMeter` on the Mac); nil until said.
    var costUsdHour: Double?
    var tokensKHour: Int?
    var costMeasured = 0
    var costRows = 0

    enum CodingKeys: String, CodingKey {
        case costUsd = "cost_usd"
        case tokensK = "tokens_k"
        case costMeasured = "cost_measured"
        case costRows = "cost_rows"
        case costUsdHour = "cost_usd_hour"
        case tokensKHour = "tokens_k_hour"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let number = try? c.decodeIfPresent(Double.self, forKey: .costUsd) {
            costUsd = number
        } else if let whole = try? c.decodeIfPresent(Int.self, forKey: .costUsd) {
            costUsd = Double(whole)
        } else {
            costUsd = nil
        }
        tokensK = c.maybe(.tokensK)
        if let number = try? c.decodeIfPresent(Double.self, forKey: .costUsdHour) {
            costUsdHour = number
        } else if let whole = try? c.decodeIfPresent(Int.self, forKey: .costUsdHour) {
            costUsdHour = Double(whole)
        } else {
            costUsdHour = nil
        }
        tokensKHour = c.maybe(.tokensKHour)
        costMeasured = c.value(.costMeasured, 0)
        costRows = c.value(.costRows, 0)
    }

    init() {}
}

struct Agents: Decodable {
    var waiting: [Agent] = []
    var running: [Agent] = []
    var sleeping: [Agent] = []
    var finished: [Agent] = []
    var abandoned: [Agent] = []

    enum CodingKeys: String, CodingKey {
        case waiting, running, sleeping, finished, abandoned
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        waiting = c.value(.waiting, [])
        running = c.value(.running, [])
        sleeping = c.value(.sleeping, [])
        finished = c.value(.finished, [])
        abandoned = c.value(.abandoned, [])
    }

    init() {}

    var live: [Agent] { running + waiting + sleeping }

    /// Every row in every bucket — the walk `AgentDetailView.agent` makes,
    /// and what a saved sheet trail is matched against.
    var all: [Agent] { waiting + running + sleeping + finished + abandoned }

    /// Find a session's row and the bucket it is in — the panel's own
    /// `Agents.row(session:)`, copied because the card screen needs the same
    /// lookup and a fourth open-coded walk over the buckets is how two
    /// surfaces start disagreeing about which row a card is bound to. Order
    /// is deliberate, and `nil` for an empty id, so a card with no session
    /// never matches anything.
    func row(session: String) -> (Agent, String)? {
        guard !session.isEmpty else { return nil }
        let buckets: [(String, [Agent])] = [
            ("running", running),
            ("waiting", waiting),
            ("sleeping", sleeping),
            ("finished", finished),
        ]
        for (name, rows) in buckets {
            if let hit = rows.first(where: { $0.sessionId == session }) {
                return (hit, name)
            }
        }
        return nil
    }

    /// The session a card is bound to, only when Dark Army itself hosts that
    /// terminal. Jumping to work on the phone opens this agent — the
    /// live screen lives there, not on the card. `nil` for an editor
    /// session, a queued card, or a card whose session the fleet has
    /// forgotten, so those keep opening the card.
    func hostedAgent(for card: BoardCard) -> (Agent, Category)? {
        let sid = !card.sessionId.isEmpty
            ? card.sessionId
            : (card.isRefining ? card.refineSessionId : "")
        guard let (agent, bucket) = row(session: sid), agent.ownTerminal else {
            return nil
        }
        return (agent, Category(rawValue: bucket) ?? .running)
    }
}

/// One live subagent under a session. `depth` is 0 for an agent the session
/// spawned itself and rises for agents spawned by other agents, so a view can
/// indent without walking the tree — the daemon has already flattened it in
/// spawn order (`BobDaemon._subagent_rows`).
struct SubagentRow: Decodable, Identifiable, Hashable {
    var id: String { agentId }

    var agentId = ""
    var subagentType = ""
    var description = ""
    var model = ""
    var activity = ""
    var depth = 0
    /// The board card the *parent* session was started for, published by the
    /// daemon beside the row. Empty from an older daemon and on every helper
    /// under a session nobody dispatched.
    var cardTitle = ""

    enum CodingKeys: String, CodingKey {
        case agentId = "agent_id"
        case subagentType = "subagent_type"
        case cardTitle = "card_title"
        case description, model, activity, depth
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        agentId = c.value(.agentId, "")
        subagentType = c.value(.subagentType, "")
        description = c.value(.description, "")
        model = c.value(.model, "")
        activity = c.value(.activity, "")
        depth = c.value(.depth, 0)
        cardTitle = c.value(.cardTitle, "")
    }

    /// What to call it. The type is the useful name ("Explore", "code-reviewer");
    /// the id is a bare hex string and the last resort, which is exactly what
    /// these rows used to render as before the daemon learned to name them.
    var label: String {
        if !subagentType.isEmpty { return subagentType }
        if !description.isEmpty { return description }
        return String(agentId.prefix(8))
    }

    /// The trade plus the piece of work: what a person reads in the helpers
    /// line and in the CMD column's summary.
    ///
    /// **`label` is deliberately untouched.** It is matched as a `Set` against
    /// a card's declared workflow stages to decide which stage is running
    /// (`liveStageNames`), so qualifying it would silently stop every card
    /// highlighting its running step — the feature looking like it works while
    /// it broke the one beside it.
    var qualified: String {
        cardTitle.isEmpty ? label : "\(label) · \(cardTitle)"
    }
}

struct Agent: Decodable, Identifiable {
    var id: String { sessionId }
    var sessionId = ""

    /// A row with only its session id set — what the terminal cover is
    /// handed for Mission Control before its row is on the snapshot.
    static func stub(sessionId: String) -> Agent {
        var agent = Agent()
        agent.sessionId = sessionId
        return agent
    }

    /// Every field at its default — `stub`'s one route; the wire always
    /// comes through `init(from:)`.
    init() {}
    var decisionEpisodeId = ""
    var nickname = ""
    var name = ""
    /// The title of the board card this session is working, published by the
    /// daemon *only* where the row would otherwise be unnamed. Empty means
    /// "nothing to say" — which is also what an older daemon decodes to.
    var cardTitle = ""
    /// The daemon's own judgment that the row wears its placeholder name —
    /// published `true` only there, absent otherwise, so this decodes
    /// `false` against an older daemon. The phone never learns the
    /// placeholder string itself; the Live Activity's `work` line reads
    /// this to say nothing rather than the placeholder.
    var unnamed = false
    /// What started this session, when Dark Army did. `originBy` is one of
    /// `card-start` / `card-refine` / `card-consult`, `originCard` the board
    /// card's id, and `originLine` the one plain sentence — **composed by the
    /// daemon and drawn verbatim**, `queueReason`'s rule, so this screen and
    /// the phone cannot word it differently. All three are absent on a
    /// session a person started themselves and from an older daemon, and
    /// absent draws nothing at all, which is today's screen exactly.
    var originBy = ""
    var originCard = ""
    var originLine = ""
    var area = ""
    var areaLine = ""
    var project = ""
    var provider = ""
    var idleSeconds: Double = 0
    /// The moment the row went quiet, as the daemon stamped it when it built
    /// the snapshot — the same number the Mac's Live Activity push carries
    /// as `since`, so the card the phone starts and the Mac's first update
    /// agree to the second. `0` from an older Mac: the frame's clock minus
    /// `idleSeconds` stands in (`PhoneInbox.sessionItems`).
    var quietSince: Double = 0
    var startedAt: Double?
    var subagents = 0
    var currentTool = ""
    var lastText = ""
    var lastSummary = ""
    /// The last completion report this agent wrote (`## Work done` onwards),
    /// kept by the daemon across the chatter that follows it. Absent decodes
    /// empty — an older Mac sends no key.
    var lastReport = ""
    /// The same report split into its labelled parts by the Mac
    /// (`work_report.py`), drawn through `WorkReport` and never re-parsed
    /// here. `nil` where there is no report or the Mac is older than the key.
    var workReport: WorkReport.Parsed?
    /// The finished list's own word for this row — `done`, `cut`, `lost`
    /// or `end` (`session_stats.finish_word`), published by the daemon on
    /// finished rows only and drawn verbatim. "done" was the one word every
    /// finished row wore, and it said "done" of a session cut off mid-build
    /// when its editor window went away. An older daemon sends no key and
    /// this decodes empty, and the table then says "done" as it always did.
    var finishWord = ""
    var branch = ""
    var question = AgentQuestion()
    /// Every question of that same dialog, in order — the flat `question` is
    /// always its first element. `[]` from an older Mac; read through
    /// `questionList`, which wraps the flat question so no view branches on
    /// which daemon sent the frame.
    var questions: [AgentQuestion] = []
    var stats = Stats()
    var metrics = Metrics()
    var trend = Trend()
    var subagentRows: [SubagentRow] = []
    var interactionNote = ""
    var askNote = ""
    var canType = false
    var canHide = false
    var canStop = false
    var canClose = false
    /// The Low priority button's reach, decided by the Mac per refresh. An
    /// older Mac sends no key and this decodes false — no button.
    var canLowPriority = false
    /// The agent's report said Dark Army needs rebuilding and restarting to
    /// pick its work up, and the Mac judged it (own checkout, no later
    /// successful rebuild). Absent decodes false — no button.
    var rebuildOffered = false
    /// Whether this session runs on a terminal the Mac's Dark Army itself hosts
    /// rather than in an editor window — the terminal the phone can watch.
    /// An older Mac sends no key and decodes false: no screen, never a
    /// blank one.
    var ownTerminal = false
    /// The session's terminal tab is gone while the turn may still be
    /// running. The words on the row. Absent decodes false.
    var tabGone = false
    /// Whether a line may be typed into that terminal from here — the Mac's
    /// `terminal_input` reach (Dark Army owns the pty, no open permission prompt,
    /// the row live and not background, the process still reading). False
    /// by default, so the box is absent rather than inert.
    var canTerminalInput = false
    /// Whether the Mac could type a person's own message at this session on
    /// behalf of the card it is working — the card screen's SEND A MESSAGE
    /// button. Decided by the Mac per refresh and deliberately not `canType`,
    /// which carries a stopped-category gate and is false for exactly the
    /// running sessions this is for. An older Mac sends no key and this
    /// decodes false — no button.
    var canMessage = false
    var channel = false
    /// Which route the next reply takes — `"typed"`, `"channel"` or `""` —
    /// published by the Mac beside `channel`. Named in the reply box, decided
    /// from nowhere; an older Mac sends no key and this decodes as `""`.
    var replyVia = ""
    var replyOptions: [String] = []

    enum CodingKeys: String, CodingKey {
        case decisionEpisodeId = "decision_episode_id"
        case sessionId = "session_id"
        case nickname, name, project, provider, branch, stats, metrics, question, trend
        case questions
        case subagents
        case subagentRows = "subagent_rows"
        case idleSeconds = "idle_seconds"
        case quietSince = "quiet_since"
        case startedAt = "started_at"
        case currentTool = "current_tool"
        case cardTitle = "card_title"
        case unnamed
        case originBy = "origin_by"
        case originCard = "origin_card"
        case originLine = "origin_line"
        case area
        case areaLine = "area_line"
        case lastText = "last_text"
        case lastSummary = "last_summary"
        case lastReport = "last_report"
        case workReport = "work_report"
        case finishWord = "finish_word"
        case interactionNote = "interaction_note"
        case askNote = "ask_note"
        case canType = "can_type"
        case canHide = "can_hide"
        case canStop = "can_stop"
        case canClose = "can_close"
        case canLowPriority = "can_low_priority"
        case rebuildOffered = "rebuild_offered"
        case ownTerminal = "own_terminal"
        case tabGone = "tab_gone"
        case canTerminalInput = "can_terminal_input"
        case canMessage = "can_message"
        case channel
        case replyVia = "reply_via"
        case replyOptions = "reply_options"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.value(.sessionId, "")
        decisionEpisodeId = c.value(.decisionEpisodeId, "")
        nickname = c.value(.nickname, "")
        name = c.value(.name, "")
        cardTitle = c.value(.cardTitle, "")
        unnamed = c.value(.unnamed, false)
        originBy = c.value(.originBy, "")
        originCard = c.value(.originCard, "")
        originLine = c.value(.originLine, "")
        area = c.value(.area, "")
        areaLine = c.value(.areaLine, "")
        project = c.value(.project, "")
        provider = c.value(.provider, "")
        idleSeconds = c.value(.idleSeconds, 0)
        quietSince = c.value(.quietSince, 0)
        startedAt = c.maybe(.startedAt)
        subagents = c.value(.subagents, 0)
        currentTool = c.value(.currentTool, "")
        lastText = c.value(.lastText, "")
        lastSummary = c.value(.lastSummary, "")
        lastReport = c.value(.lastReport, "")
        workReport = c.maybe(.workReport)
        finishWord = c.value(.finishWord, "")
        branch = c.value(.branch, "")
        question = c.value(.question, AgentQuestion())
        questions = c.value(.questions, [])
        stats = c.value(.stats, Stats())
        metrics = c.value(.metrics, Metrics())
        trend = c.value(.trend, Trend())
        subagentRows = c.value(.subagentRows, [])
        interactionNote = c.value(.interactionNote, "")
        askNote = c.value(.askNote, "")
        canType = c.value(.canType, false)
        canHide = c.value(.canHide, false)
        canStop = c.value(.canStop, false)
        canClose = c.value(.canClose, false)
        canLowPriority = c.value(.canLowPriority, false)
        rebuildOffered = c.value(.rebuildOffered, false)
        ownTerminal = c.value(.ownTerminal, false)
        tabGone = c.value(.tabGone, false)
        canTerminalInput = c.value(.canTerminalInput, false)
        canMessage = c.value(.canMessage, false)
        channel = c.value(.channel, false)
        replyVia = c.value(.replyVia, "")
        replyOptions = c.value(.replyOptions, [])
    }

    /// The dialog as a list, whichever Mac sent it: the `questions` list
    /// where the daemon published one, else the non-empty flat `question`
    /// wrapped as a one-element list, else `[]`.
    var questionList: [AgentQuestion] {
        if !questions.isEmpty { return questions }
        if !question.text.isEmpty { return [question] }
        return []
    }

    /// The subagent footnote for a collapsed row: the count, and the name of the
    /// one that started most recently.
    ///
    /// `+2 sub` said a session was busy with somebody; it never said *what stage
    /// it was at*, which is the fact worth having — an implementer and a verifier
    /// are the same number and mean opposite things about how far along the work
    /// is. The names have always been here (`_subagent_rows` fills
    /// `subagent_type` for every one), they were just unreachable without
    /// unfolding the row.
    ///
    /// The newest is `subagentRows.last`: the daemon flattens the tree in spawn
    /// order, so the last root is the latest thing to start. Falls back to the
    /// bare count when the rows are absent but the count is not — a snapshot can
    /// legitimately carry one without the other, and a count with no name is
    /// still worth more than nothing.
    ///
    /// Empty when there are none, so a caller can append it unconditionally.
    var subagentSummary: String {
        guard subagents > 0 else { return "" }
        if let newest = subagentRows.last?.qualified, !newest.isEmpty {
            return "+\(subagents) · \(newest)"
        }
        return "+\(subagents) sub"
    }
}

/// The question a session is stopped on. `{}` on a session that is not
/// blocked, so an empty `text` is the normal case and never an error.
struct AgentQuestion: Decodable {
    var text = ""
    /// The answer labels the dialog is offering, in the daemon's own order.
    /// `option_index` is an index into *this*, absolute and not into whatever
    /// a stale phone happens to be drawing — which is why `id` travels with
    /// every answer.
    var options: [String] = []
    /// Each option's `description`, same order as `options`. Empty where the
    /// tool sent none. Claude's `preview` is a different layout and is not
    /// this field.
    var details: [String] = []
    var header = ""
    /// The `tool_use` id the daemon matched the question on. Empty on an
    /// older Mac, and the daemon tolerates that on purpose.
    var id = ""
    /// The tool's own `multiSelect`: several answers may be right, and the
    /// terminal draws tick boxes rather than a pick-one select. False from an
    /// older Mac, which is exactly today's drawing.
    var multiSelect = false

    enum CodingKeys: String, CodingKey {
        case text
        case options
        case details
        case header
        case id
        case multiSelect = "multi_select"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = c.value(.text, "")
        options = c.value(.options, [])
        details = c.value(.details, [])
        header = c.value(.header, "")
        id = c.value(.id, "")
        multiSelect = c.value(.multiSelect, false)
    }

    init() {}

    func detail(at index: Int) -> String {
        details.indices.contains(index) ? details[index] : ""
    }
}

/// Which bucket a row came out of. The phone never re-derives this — the
/// daemon has already categorised every row and computing it a second way is
/// how two surfaces come to disagree.
enum Category: String {
    case waiting, running, sleeping, finished, abandoned
}

struct Stats: Decodable {
    var durationSeconds: Double = 0
    var model = ""

    enum CodingKeys: String, CodingKey {
        case durationSeconds = "duration_seconds"
        case model
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        durationSeconds = c.value(.durationSeconds, 0)
        model = c.value(.model, "")
    }

    init() {}
}

struct Metrics: Decodable {
    var costUsd: Double?
    var ctxUsedPct: Double?
    var exceeds200k = false

    enum CodingKeys: String, CodingKey {
        case costUsd = "cost_usd"
        case ctxUsedPct = "ctx_used_pct"
        case exceeds200k = "exceeds_200k"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        costUsd = c.maybe(.costUsd)
        ctxUsedPct = c.maybe(.ctxUsedPct)
        exceeds200k = c.value(.exceeds200k, false)
    }

    init() {}
}

/// What the context number is *doing*, as the Mac measured it. Every rate
/// is optional and stays nil when the Mac published none — a series too
/// short, too sparse or gone quiet — and nil is drawn as nothing. A rate
/// of 0.0 is a real reading (a flat series) and is drawn as steady.
struct Trend: Decodable {
    var ctxPctPerMin: Double?
    var costUsdPerHour: Double?
    var outputTokensPerMin: Double?
    var ctxRunwaySeconds: Double?
    var samples = 0

    enum CodingKeys: String, CodingKey {
        case ctxPctPerMin = "ctx_pct_per_min"
        case costUsdPerHour = "cost_usd_per_hour"
        case outputTokensPerMin = "output_tokens_per_min"
        case ctxRunwaySeconds = "ctx_runway_seconds"
        case samples
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ctxPctPerMin = c.maybe(.ctxPctPerMin)
        costUsdPerHour = c.maybe(.costUsdPerHour)
        outputTokensPerMin = c.maybe(.outputTokensPerMin)
        ctxRunwaySeconds = c.maybe(.ctxRunwaySeconds)
        samples = c.value(.samples, 0)
    }

    init() {}

    /// Below this magnitude, in %/min, the context reads as steady.
    /// One constant, read by the row and the detail screen alike.
    static let steadyBand: Double = 0.05

    enum Pace {
        case rising, steady, falling
        /// Text, never colour: the glyph rides inside the CTX cell.
        var marker: String {
            switch self {
            case .rising: return "↑"
            case .steady: return "→"
            case .falling: return "↓"
            }
        }
        var spoken: String {
            switch self {
            case .rising: return "rising"
            case .steady: return "steady"
            case .falling: return "falling"
            }
        }
    }

    /// nil when the Mac published no rate. Never defaulted to zero.
    var pace: Pace? {
        guard let rate = ctxPctPerMin else { return nil }
        if rate >= Trend.steadyBand { return .rising }
        if rate <= -Trend.steadyBand { return .falling }
        return .steady
    }
}

struct Board: Decodable {
    /// The phone carries no finished clock; id keeps this list stable.
    var needsYouCards: [BoardCard] {
        cards.filter(\.needsYou).sorted { $0.id < $1.id }
    }

    /// Which card a session is on: the card it is **executing** first, the
    /// Prep card it is **refining** second — the Mac's own two rungs
    /// (`Board.card(forSession:)` in the panel's `Models.swift`), so the two
    /// surfaces can never name different cards for one session.
    ///
    /// **The refine rung is gated on `isRefining`, and that gate is the whole
    /// point of the rung.** `refine_session_id` is an audit trail, not a live
    /// link: `BoardStore.attach_plan` clears `refine_state` and deliberately
    /// leaves the id standing, so reading it ungated would make the session
    /// that wrote a plan claim a card another agent is now working, for ever.
    /// An empty id resolves to nothing rather than to the first unbound card.
    func card(forSession id: String) -> BoardCard? {
        guard !id.isEmpty else { return nil }
        return cards.first { $0.sessionId == id }
            ?? cards.first { $0.isRefining && $0.refineSessionId == id }
    }

    var outcomesSupported = false
    /// Whether this Mac serves the human knowledge reader. Absent means
    /// **false**: the Profile row is drawn absent rather than empty.
    var knowledgeSupported = false
    /// Whether this Mac lists its scout reports and serves one on open
    /// (`scout_reports_supported`). Absent means **false**: the Menu's
    /// Scouting tile stays dim and opens the page that says so.
    var scoutReportsSupported = false
    /// Whether this Mac searches the reports' text when the list read
    /// carries `q` (`scout_reports_body_search_supported`). Absent means
    /// **false**: the Scouting screen keeps its instant search alone and
    /// asks for no text search.
    var scoutReportsBodySearchSupported = false
    /// Whether this Mac lists its projects' plans and serves one on open
    /// (`plans_supported`). Absent means **false**: the Menu's Plans tile
    /// stays dim and opens the page that says so.
    var plansSupported = false
    /// Whether this Mac keeps and serves the phone doors' access log.
    /// Absent means **false**: the Profile row is drawn absent.
    var accessLogSupported = false
    /// Whether this Mac times every sealed request and serves ten-minute
    /// rollups behind `timing=1` on the access-log read. Absent means
    /// **false**: the phone asks for none and draws its own side alone.
    var linkTimingSupported = false
    var available = false
    var cards: [BoardCard] = []
    var counts: [String: Int] = [:]
    var tools: [String] = []
    var installed: [String: Bool] = [:]
    func isInstalled(_ tool: String) -> Bool { installed[tool] ?? true }
    /// Which models each assistant may be run on — the daemon's own
    /// `dispatch.MODELS`, published rather than copied here. Empty from a Mac
    /// too old to send it, which is exactly the gate every model chooser keys
    /// off: absent, never blank.
    var models: [String: [String]] = [:]
    var projects: [BoardProject] = []
    var dispatchEnabled = false
    /// Whether the Mac will write a card's instructions from plain words —
    /// the `card_prepare` preference, published rather than guessed. False
    /// against a Mac too old to send it, which also 404s the verb, so the
    /// phone's PREPARE button is absent for two reasons at once.
    var prepareEnabled = false
    /// Whether a queued card may promise that Dark Army will start it by itself.
    /// Default **true**, which is the direction that degrades safely: an
    /// older Mac's queue *did* drain on its own, so a caption promising less
    /// than the Mac will do is the wrong lie to tell.
    var autostartEnabled = true
    /// The machine-wide dial — what a project runs on with no dial of its
    /// own. `0` means the Mac did not say, and is never drawn as a rule.
    var parallelLimit = 0
    /// Which projects have said otherwise. For the picker's tick-state
    /// alone — never a denominator: the number a project actually runs on
    /// arrives per card, already resolved.
    var parallelOverrides: [String: Int] = [:]
    /// Whether this Mac takes the two queue verbs from a phone at all. A
    /// version marker, and absent means **false**: an older Mac 404s them
    /// inside the sealed reply, so the controls are absent rather than
    /// present and silently doing nothing.
    var queueWritable = false
    /// Whether this Mac has anybody who can store a preference — the
    /// menu-bar app. False on a headless daemon, which is exactly when the
    /// write would be refused, so the switch and picker are absent there.
    var preferencesWritable = false
    /// Whether this Mac takes an approval of a plan version at all. A version
    /// marker on `queueWritable`'s argument, and absent means **false**: an
    /// older Mac 404s `board_approve_plan` inside the sealed reply, so the
    /// Approve control is drawn absent rather than present and inert.
    var planApprovalSupported = false
    /// Whether this Mac carries the whole-project press at all. A version
    /// marker on `queueWritable`'s argument, and absent means **false**: an
    /// older Mac 404s `board_start_project` inside the sealed reply, so
    /// START PROJECT is drawn absent rather than present and inert.
    var startProjectWritable = false
    /// Whether this Mac carries `board_message` at all. A version marker on
    /// `startProjectWritable`'s argument, and absent means **false**: an older
    /// Mac 404s the verb inside the sealed reply, so SEND A MESSAGE is drawn
    /// absent rather than present and inert.
    var cardMessageWritable = false
    /// Whether this Mac's Dark Army knows the `start_when_planned` card column at
    /// all. A version marker on `cardMessageWritable`'s argument, and absent
    /// means **false**: an older daemon drops the field and then 400s the
    /// update whose only named field was dropped, so the tick is drawn absent
    /// rather than present and refused.
    var startWhenPlannedSupported = false
    /// Whether this Mac's Dark Army knows the `priority` card column at all. A
    /// version marker on `startWhenPlannedSupported`'s argument, and absent
    /// means **false**: an older Mac drops the field and then 400s the update
    /// whose only named field was dropped, so the number box is drawn
    /// **absent** rather than present and inert.
    var prioritySupported = false
    var areasSupported = false
    /// Whether this Mac serves the sealed `agent_report` read at all. A
    /// version marker on `prioritySupported`'s argument, and absent means
    /// **false**: an older Mac 404s the kind inside the sealed reply, so the
    /// agent report section is drawn **absent** rather than present and
    /// answering nothing.
    var agentReportSupported = false
    /// Whether this Mac serves the sealed `lifecycle` read. Absent means
    /// **false**: an older Mac 404s the kind, so the section is drawn
    /// **absent** rather than present and answering nothing.
    var lifecycleSupported = false
    /// Whether this Mac serves the sealed `history_week` read. Absent means
    /// **false**: an older Mac 404s the kind, so the Menu's History tile is
    /// drawn dim with a sentence rather than lit and answering nothing.
    var historyWeekSupported = false
    /// Whether this Mac stamps `run_figures` — cost so far, working minutes,
    /// live context, attempts — on every card that has had a run. Absent
    /// means **false**: an older Mac sends no key, and the figures line on
    /// the card screen is drawn **absent** rather than blank.
    var runFiguresSupported = false
    /// Whether this Mac gates Start on a card's dependencies and admits
    /// `blocked_by` through `board_update`. Absent means **false**: an older
    /// Mac drops the field at its door, so the WAITS ON editor is drawn
    /// **absent** rather than writing a list nobody keeps.
    var dependenciesSupported = false
    /// Whether this Mac takes `register_activity_token` and pushes Live
    /// Activity updates through the mailbox. Absent means **false**: against
    /// an older Mac the Lock Screen card still starts and ends locally, and
    /// no token is ever posted.
    var liveActivitySupported = false
    /// Whether this Mac publishes the fleet Lock Screen card
    /// (`live_activity_fleet`). Absent means false: a new phone against an
    /// older Mac keeps the face-only card.
    var liveActivityFleet = false
    /// Whether this Mac decides and publishes `run_health` per card. Absent
    /// means **false**: an older Mac sends no key, and the health line on
    /// the card screen is drawn **absent** rather than as a broken line.
    var runHealthSupported = false
    /// Whether this Mac takes `kind` on a phone's `board_create`. Absent
    /// means **false**: an older Mac drops the key and writes a build
    /// card, so the composer's Build / Scout control is drawn absent.
    var scoutSupported = false
    /// Whether this Mac honours `board_promote` from a phone. Absent means
    /// **false**: an older Mac 404s the verb, so the card screen draws
    /// Promote absent rather than present and refused.
    var promoteSupported = false
    /// Whether this Mac honours `board_refine_batch` from a phone. Absent
    /// means **false**: an older Mac 404s the verb, so the Board tab's Prep
    /// row draws its Select control absent rather than present and refused.
    var refineBatchSupported = false
    /// Whether this Mac honours `board_start_batch` from a phone. Absent
    /// means **false**: an older Mac 404s the verb, so the Board tab's
    /// Backlog row draws its Select control absent rather than present and refused.
    var startBatchSupported = false
    /// Whether this Mac carries `rebuild_app` on its away door too. Absent
    /// means **false**: an older Mac 404s the verb from away, so Rebuild &
    /// restart draws dim away from home rather than offering a refused press.
    var rebuildAwaySupported = false
    /// Whether this Mac serves the sealed `conversation` read. Absent means
    /// **false**: an older Mac 404s the kind, so the phone opens Details and
    /// draws one sentence rather than a blank conversation.
    var conversationSupported = false
    /// The Mac takes `agent` on the `conversation` read — one helper's own
    /// journal. Without it Comm draws no helper tabs: an older Mac ignores
    /// the key and would answer with the parent's turns.
    var subagentConversationSupported = false
    /// Whether this Mac's on-open `card` read carries a `timeline`. Absent
    /// means **false**: the TIMELINE row is drawn absent rather than empty.
    var cardTimelineSupported = false
    /// This Mac admits the three objective text fields on a sealed
    /// `board_create`. An older one refuses the *whole* create when any of
    /// them rides along, so the composer draws the boxes absent, not inert.
    var objectiveOnCreateSupported = false
    /// Whether this Mac serves the sealed `terminal` read and carries
    /// `terminal_input` at all. A version marker on `prioritySupported`'s
    /// argument, and absent means **false**: an older Mac 404s both, so the
    /// terminal screen is drawn **absent** rather than present and blank.
    var terminalSupported = false
    /// Whether this Mac holds the sealed terminal **stream** open on its
    /// phone door, paints on `since_bytes=-1` and takes raw `bytes` on
    /// `terminal_input`. A version marker on `terminalSupported`'s argument,
    /// and absent means **false**: an older Mac has no such route, so the
    /// emulator is drawn **absent** — one sentence — rather than present and
    /// waiting for ever.
    var terminalStreamSupported = false
    /// Whether this Mac takes a bulk Done sweep from a phone at all. A version
    /// marker on `terminalSupported`'s argument, and absent means **false**:
    /// an older Mac 404s `board_clear_done` inside the sealed reply, so the
    /// button is drawn **absent** rather than present and 404ing.
    var clearDoneWritable = false
    /// Whether this Mac takes Mark checked from a phone. A version marker on
    /// `clearDoneWritable`'s argument, and absent means **false**: an older
    /// Mac 404s `board_manual_clear` inside the sealed reply, so the button
    /// is drawn **absent** rather than present and 404ing.
    var manualClearWritable = false
    /// Whether this Mac serves the Checks section (the sealed
    /// `manual_checks` read). Absent means **false**: the Menu tile is
    /// drawn dim rather than opening onto an empty 404.
    var manualChecksSupported = false
    /// Whether this Mac takes Passed / Failed on a check file from a phone
    /// (`board_manual_outcome`). Its own marker, so the list can be drawn
    /// against a Mac that refuses the press.
    var manualOutcomeWritable = false
    /// Whether this Mac serves the Review section (`state.review`, the
    /// sealed `review_offer` read). Absent means **false**: the Menu tile is
    /// dim and the screen says so in a sentence.
    var reviewSupported = false
    /// Whether this Mac takes `review_start`, `review_continue` and
    /// `review_end` from a phone — its own marker on `reviewSupported`'s
    /// argument, because the older `review_writable` is "Mark reviewed" and
    /// `review_run_writable` is a Done card's Run review.
    var reviewSectionWritable = false
    /// Its twin for Mark reviewed — `board_review`. Two flags, not one, so a
    /// Mac that honours one verb and not the other hides exactly one button.
    var reviewWritable = false
    /// Review and merge on a Done card (`docs/card-worktrees.md`). Three
    /// markers, `manualOutcomeWritable`'s argument: whether this Mac serves
    /// the sealed `card_changes` read, and whether it honours `board_merge`
    /// / `board_merge_fix` and `board_review_run` from a phone. Absent from
    /// an older Mac is **false**, so the CHANGES row, MERGE, Fix and Run
    /// review are drawn **absent** rather than present and 404ing.
    var cardChangesSupported = false
    var mergeWritable = false
    var reviewRunWritable = false
    /// The Menu's Worktrees screen (`docs/card-worktrees.md`, *Several
    /// finished cards merge one after another*). Two more markers on the
    /// same argument: whether this Mac serves the sealed `worktrees` read,
    /// and whether it honours `board_merge_batch` from a phone. Absent from
    /// an older Mac is **false**: the tile is dim and MERGE is drawn absent.
    var worktreesSupported = false
    var mergeBatchWritable = false
    /// Whether the next Start opens Dark Army's own terminal. START HERE
    /// is drawn only while this is off, so the two buttons cannot do the
    /// same thing. Absent from an older Mac is **false**.
    var ownTerminalEnabled = false
    /// Whether this Mac honours `own_terminal` on `board_dispatch`. A
    /// version marker, and absent means **false**: an older Mac ignores
    /// the flag, so START HERE is drawn absent rather than present and
    /// opening the editor instead.
    var ownTerminalSpawnSupported = false
    /// Exact store-wide Done membership. Empty from an older Mac, which
    /// keeps the destructive control hidden instead of degrading to
    /// count-only protection.
    var doneClearToken = ""
    /// What a *held* copy of the finished column can go stale against:
    /// membership, any revised column, and a review. Beside
    /// `doneClearToken` and never a substitute for it — Clear Done compares
    /// membership and only membership, and passing this to it would refuse
    /// every clear after any finished card was edited. Empty from an older
    /// Mac, which withholds nothing and so never asks for a refetch.
    var doneViewToken = ""

    /// Store-wide, unlike the bounded Done preview in `cards`.
    var doneCount: Int { max(0, counts["done"] ?? 0) }

    var hasSafeDoneClearToken: Bool {
        doneClearToken.count == 64
            && doneClearToken.allSatisfy { "0123456789abcdef".contains($0) }
    }

    enum CodingKeys: String, CodingKey {
        case outcomesSupported = "outcomes_supported"
        case knowledgeSupported = "knowledge_supported"
        case scoutReportsSupported = "scout_reports_supported"
        case scoutReportsBodySearchSupported = "scout_reports_body_search_supported"
        case plansSupported = "plans_supported"
        case accessLogSupported = "access_log_supported"
        case linkTimingSupported = "link_timing_supported"
        case available, cards, counts, tools, installed, models, projects
        case dispatchEnabled = "dispatch_enabled"
        case prepareEnabled = "prepare_enabled"
        case autostartEnabled = "autostart_enabled"
        case parallelLimit = "parallel_limit"
        case parallelOverrides = "parallel_overrides"
        case queueWritable = "queue_writable"
        case preferencesWritable = "preferences_writable"
        case planApprovalSupported = "plan_approval_supported"
        case startProjectWritable = "start_project_writable"
        case cardMessageWritable = "card_message_writable"
        case startWhenPlannedSupported = "start_when_planned_supported"
        case prioritySupported = "priority_supported"
        case areasSupported = "areas_supported"
        case agentReportSupported = "agent_report_supported"
        case lifecycleSupported = "lifecycle_supported"
        case historyWeekSupported = "history_week_supported"
        case runFiguresSupported = "run_figures_supported"
        case dependenciesSupported = "dependencies_supported"
        case liveActivitySupported = "live_activity_supported"
        case liveActivityFleet = "live_activity_fleet"
        case runHealthSupported = "run_health_supported"
        case scoutSupported = "scout_supported"
        case promoteSupported = "promote_supported"
        case refineBatchSupported = "refine_batch_supported"
        case startBatchSupported = "start_batch_supported"
        case rebuildAwaySupported = "rebuild_away_supported"
        case conversationSupported = "conversation_supported"
        case subagentConversationSupported = "subagent_conversation_supported"
        case cardTimelineSupported = "card_timeline_supported"
        case objectiveOnCreateSupported = "objective_on_create_supported"
        case terminalSupported = "terminal_supported"
        case terminalStreamSupported = "terminal_stream_supported"
        case clearDoneWritable = "clear_done_writable"
        case manualClearWritable = "manual_clear_writable"
        case manualChecksSupported = "manual_checks_supported"
        case manualOutcomeWritable = "manual_outcome_writable"
        case reviewWritable = "review_writable"
        case cardChangesSupported = "card_changes_supported"
        case mergeWritable = "merge_writable"
        case reviewSupported = "review_supported"
        case reviewRunWritable = "review_run_writable"
        case worktreesSupported = "worktrees_supported"
        case mergeBatchWritable = "merge_batch_writable"
        case reviewSectionWritable = "review_section_writable"
        case ownTerminalEnabled = "own_terminal_enabled"
        case ownTerminalSpawnSupported = "own_terminal_spawn_supported"
        case doneClearToken = "done_clear_token"
        case doneViewToken = "done_view_token"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        outcomesSupported = c.value(.outcomesSupported, false)
        knowledgeSupported = c.value(.knowledgeSupported, false)
        scoutReportsSupported = c.value(.scoutReportsSupported, false)
        scoutReportsBodySearchSupported = c.value(.scoutReportsBodySearchSupported, false)
        plansSupported = c.value(.plansSupported, false)
        accessLogSupported = c.value(.accessLogSupported, false)
        linkTimingSupported = c.value(.linkTimingSupported, false)
        available = c.value(.available, false)
        cards = c.value(.cards, [])
        counts = c.value(.counts, [:])
        tools = c.value(.tools, [])
        installed = c.value(.installed, [:])
        models = c.value(.models, [:])
        projects = c.value(.projects, [])
        dispatchEnabled = c.value(.dispatchEnabled, false)
        prepareEnabled = c.value(.prepareEnabled, false)
        autostartEnabled = c.value(.autostartEnabled, true)
        parallelLimit = c.value(.parallelLimit, 0)
        parallelOverrides = c.value(.parallelOverrides, [:])
        queueWritable = c.value(.queueWritable, false)
        terminalSupported = c.value(.terminalSupported, false)
        terminalStreamSupported = c.value(.terminalStreamSupported, false)
        preferencesWritable = c.value(.preferencesWritable, false)
        planApprovalSupported = c.value(.planApprovalSupported, false)
        startProjectWritable = c.value(.startProjectWritable, false)
        cardMessageWritable = c.value(.cardMessageWritable, false)
        startWhenPlannedSupported = c.value(.startWhenPlannedSupported, false)
        prioritySupported = c.value(.prioritySupported, false)
        areasSupported = c.value(.areasSupported, false)
        agentReportSupported = c.value(.agentReportSupported, false)
        lifecycleSupported = c.value(.lifecycleSupported, false)
        historyWeekSupported = c.value(.historyWeekSupported, false)
        runFiguresSupported = c.value(.runFiguresSupported, false)
        dependenciesSupported = c.value(.dependenciesSupported, false)
        liveActivitySupported = c.value(.liveActivitySupported, false)
        liveActivityFleet = c.value(.liveActivityFleet, false)
        runHealthSupported = c.value(.runHealthSupported, false)
        scoutSupported = c.value(.scoutSupported, false)
        promoteSupported = c.value(.promoteSupported, false)
        refineBatchSupported = c.value(.refineBatchSupported, false)
        startBatchSupported = c.value(.startBatchSupported, false)
        rebuildAwaySupported = c.value(.rebuildAwaySupported, false)
        conversationSupported = c.value(.conversationSupported, false)
        subagentConversationSupported = c.value(.subagentConversationSupported, false)
        cardTimelineSupported = c.value(.cardTimelineSupported, false)
        objectiveOnCreateSupported = c.value(.objectiveOnCreateSupported, false)
        clearDoneWritable = c.value(.clearDoneWritable, false)
        manualClearWritable = c.value(.manualClearWritable, false)
        manualChecksSupported = c.value(.manualChecksSupported, false)
        manualOutcomeWritable = c.value(.manualOutcomeWritable, false)
        reviewWritable = c.value(.reviewWritable, false)
        cardChangesSupported = c.value(.cardChangesSupported, false)
        mergeWritable = c.value(.mergeWritable, false)
        reviewSupported = c.value(.reviewSupported, false)
        reviewRunWritable = c.value(.reviewRunWritable, false)
        worktreesSupported = c.value(.worktreesSupported, false)
        mergeBatchWritable = c.value(.mergeBatchWritable, false)
        reviewSectionWritable = c.value(.reviewSectionWritable, false)
        ownTerminalEnabled = c.value(.ownTerminalEnabled, false)
        ownTerminalSpawnSupported = c.value(.ownTerminalSpawnSupported, false)
        doneClearToken = c.value(.doneClearToken, "")
        doneViewToken = c.value(.doneViewToken, "")
    }

    init() {}

    func cards(in column: String) -> [BoardCard] {
        cards.filter { $0.column == column }
    }

    /// This project's queue, in the order the daemon's drain obeys — the
    /// coalesced key (`queueRank ?? queuedAt ?? 0`, then id). Copied from
    /// the panel's `Board.queued(in:)` so the two surfaces cannot disagree
    /// about what runs next.
    func queued(in project: String) -> [BoardCard] {
        cards.filter { $0.isQueued && $0.project == project }
            .sorted { a, b in
                let x = a.queueRank ?? a.queuedAt ?? 0
                let y = b.queueRank ?? b.queuedAt ?? 0
                return x == y ? a.id < b.id : x < y
            }
    }

    /// The cards this project has in flight right now. The narrowing rules —
    /// a card in Done holds nothing, a `live` card the daemon can no longer
    /// hear holds nothing — arrive already decided as `runActive`; a second
    /// opinion computed here is the two-surfaces-disagree failure.
    /// `runActive`, not `workActive`: a card flagged for a hand-check stops
    /// holding its files, but while its assistant is still typing it is
    /// running.
    func running(in project: String) -> [BoardCard] {
        cards.filter { $0.project == project && $0.isInFlight && $0.runActive }
    }

    /// This project's Backlog, in the snapshot's own order — which is the
    /// store's `column_name, position, created_at`, so it is the order the
    /// board draws and the order one press walks. Deliberately the whole
    /// column: which of these cards actually starts is the Mac's judgment
    /// (the plan gate, the guard, the slot rule), and a filter here would be
    /// a second copy of it that drifts.
    func backlog(in project: String) -> [BoardCard] {
        cards.filter { $0.project == project && $0.column == "backlog" }
    }

    /// The cards this project has a **refinement** running on. Its own group
    /// because a refinement never touches `session_id` / `link_state`, so
    /// `running(in:)` cannot see it — and it claims no parallel slot, so
    /// folding it in would print "RUN 2/1".
    func refining(in project: String) -> [BoardCard] {
        cards.filter { $0.project == project && $0.isRefining }
            .sorted { $0.id < $1.id }
    }

    /// This project's rows past RUN — the panel's `Board.finishedRun(in:)`
    /// rule: a DONE row is a card in the board's Done column and nothing
    /// else, listed for as long as the board carries it; a stopped run
    /// still in In progress (`!runActive`, the exact negation of
    /// `running(in:)`, so no card is ever in both) is ENDED, not DONE,
    /// because that is not where the card is. The ENDED half comes first —
    /// it is the half that still wants a person.
    func finishedRun(in project: String) -> [BoardCard] {
        let stopped = cards.filter {
            $0.project == project && $0.column != "done" && !$0.runActive
                && !$0.sessionId.isEmpty
                && ($0.isInFlight || $0.linkState == "ended")
        }
        let done = cards.filter { $0.project == project && $0.column == "done" }
        return stopped.sorted { $0.id < $1.id } + done.sorted { $0.id < $1.id }
    }

    /// The word a `finishedRun(in:)` row wears — the panel's
    /// `Board.finishedState`, byte for byte.
    static func finishedState(_ card: BoardCard) -> String {
        card.column == "done" ? "DONE" : "ENDED"
    }

    /// Every project with pipeline work — running, waiting, being refined,
    /// just finished, **or banked in Backlog and not started** — in name
    /// order. Absent, never empty: a project with nothing happening has no
    /// section.
    ///
    /// The Backlog term is why a project with work waiting and nothing
    /// running appears here at all. That is the exact morning case START
    /// PROJECT exists for, and without it the press was unreachable on the
    /// one project that needed it. Stated cost: on a busy board every
    /// project with a Backlog card now draws a section.
    var pipelineProjects: [String] {
        var names: Set<String> = []
        for card in cards
        where (card.isInFlight && card.runActive) || card.isQueued
            || card.isRefining
            || card.column == "backlog"
            || card.column == "done"
            || (!card.runActive && !card.sessionId.isEmpty
                && (card.isInFlight || card.linkState == "ended")) {
            let name = card.project
            if !name.isEmpty { names.insert(name) }
        }
        return names.sorted()
    }

    /// How many agents may work at once in the project these rows belong to.
    /// The daemon's own answer, picked up rather than computed: every card
    /// carries the resolved figure, the first one that has it wins, and the
    /// board scalar is used only where every card reads 0 — a Mac from
    /// before the per-project dial, whose one number answered every project.
    static func resolvedLimit(cards: [BoardCard], fallback: Int) -> Int {
        for card in cards where card.parallelLimit > 0 {
            return card.parallelLimit
        }
        return max(1, fallback)
    }

    /// The folder a dial press would aim at: the first of these cards that
    /// names one. Empty means there is nothing to aim at, which is what
    /// makes the picker absent rather than inert.
    static func pipelineRoot(cards: [BoardCard]) -> String {
        for card in cards where !card.root.isEmpty { return card.root }
        return ""
    }

    /// Whether the RUN fraction is drawn at all. Spare capacity and why the
    /// queue waits are the two questions it answers, and a section showing
    /// only finished work has neither.
    static func showsRunHeading(running: Int, queued: Int) -> Bool {
        running > 0 || queued > 0
    }

    /// The models on offer for one assistant, and the whole of the phone's
    /// model logic. Empty for `""` (nothing chosen), for a tool the daemon
    /// ships no catalogue for, and for a Mac that sends none — in all three
    /// cases the chooser is *absent* rather than empty.
    func modelOptions(for tool: String) -> [String] {
        models[tool] ?? []
    }

    /// Presentation only; selection and dispatch keep the daemon's raw id.
    static func modelLabel(_ model: String) -> String {
        model == "gpt-6-astra" ? "Astra 6" : model
    }
}

/// One open VS Code window: what to call it, and the folder a card filed against
/// it would launch into. Copied from the panel — the phone does not import it.
struct BoardProject: Decodable, Identifiable, Hashable {
    var name = ""
    var root = ""

    var id: String { root }

    enum CodingKeys: String, CodingKey { case name, root }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.value(.name, "")
        root = c.value(.root, "")
    }

    init() {}
}

/// A waiting-row card. Copied from the panel's shape; absent on an older Mac
/// must decode as empty, never blank the snapshot.
struct Notification: Decodable, Identifiable {
    var id: String { sessionId }
    var sessionId = ""
    var message = ""
    var project = ""
    var hook = ""
    /// The machine token behind a `StopFailure` (`rate_limit`, …). Absent on
    /// an older Mac decodes as `""`.
    var errorKind = ""

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case message, project, hook
        case errorKind = "error_kind"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.value(.sessionId, "")
        message = c.value(.message, "")
        project = c.value(.project, "")
        hook = c.value(.hook, "")
        errorKind = c.value(.errorKind, "")
    }

    init() {}
}

/// A tool call waiting for a yes or a no. Not a Notification — dismissing a
/// card would leave the session blocked.
struct PermissionPrompt: Decodable, Identifiable, Hashable {
    var id: String { requestId }
    var requestId = ""
    var sessionId = ""
    var toolName = ""
    var detail = ""
    var inputPreview = ""
    var provider = ""
    var answerable = true
    var answerNote = ""

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case sessionId = "session_id"
        case toolName = "tool_name"
        case detail = "description"
        case inputPreview = "input_preview"
        case provider, answerable
        case answerNote = "answer_note"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        requestId = c.value(.requestId, "")
        sessionId = c.value(.sessionId, "")
        toolName = c.value(.toolName, "")
        detail = c.value(.detail, "")
        inputPreview = c.value(.inputPreview, "")
        provider = c.value(.provider, "")
        answerable = c.value(.answerable, true)
        answerNote = c.value(.answerNote, "")
    }

    init() {}
}

/// What Dark Army observed a card's last finished run do — the headline the board
/// frame carries. **Absence means no record**, never an empty one: a card
/// nobody has run and a run that changed nothing are different facts, and the
/// phone is the surface that has to be able to tell them apart.
struct WorkRecordHead: Decodable, Equatable {
    var verdict = ""
    var at: Double = 0
    var files = 0
    var filesTotal = 0
    var added = 0
    var removed = 0
    var report = false
    var filesAvailable = false
    /// What the shunt skill's helper did during the run: how many
    /// delegations, how many lines never entered the main model, and what
    /// the helper cost where the assistant reported it — `nil` otherwise,
    /// never a zero. A missing key is an older Mac and draws nothing.
    var shuntDelegations = 0
    var shuntLinesKeptOut = 0
    var shuntWorkerCostUsd: Double?

    enum CodingKeys: String, CodingKey {
        case verdict, at, files, added, removed, report
        case filesTotal = "files_total"
        case filesAvailable = "files_available"
        case shuntDelegations = "shunt_delegations"
        case shuntLinesKeptOut = "shunt_lines_kept_out"
        case shuntWorkerCostUsd = "shunt_worker_cost_usd"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        verdict = c.value(.verdict, "")
        at = c.value(.at, 0)
        files = c.value(.files, 0)
        filesTotal = c.value(.filesTotal, 0)
        added = c.value(.added, 0)
        removed = c.value(.removed, 0)
        report = c.value(.report, false)
        filesAvailable = c.value(.filesAvailable, false)
        shuntDelegations = c.value(.shuntDelegations, 0)
        shuntLinesKeptOut = c.value(.shuntLinesKeptOut, 0)
        shuntWorkerCostUsd = c.maybe(.shuntWorkerCostUsd)
    }
}

/// One changed file. `added` / `removed` are optionals rather than zeros: git
/// says `-` for a binary file, and a zero would read as no change at all.
struct WorkRecordFile: Decodable, Equatable, Identifiable {
    var path = ""
    var added: Int?
    var removed: Int?
    var binary = false
    var isNew = false

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, added, removed, binary
        case isNew = "new"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.value(.path, "")
        added = c.maybe(.added)
        removed = c.maybe(.removed)
        binary = c.value(.binary, false)
        isNew = c.value(.isNew, false)
    }
}

/// The whole record: the closing words the run left, and what changed.
struct WorkRecord: Decodable, Equatable {
    var cardId = ""
    var runAt: Double = 0
    var sessionId = ""
    var verdict = ""
    var report = ""
    var summary = ""
    var files: [WorkRecordFile] = []
    var filesAvailable = false
    var filesReason = ""
    var filesChanged = 0
    var filesTotal = 0
    var linesAdded = 0
    var linesRemoved = 0
    var recordedAt: Double?
    /// The helper's figures and the daemon's one sentence about them
    /// (`work_record.shunt_words`), drawn verbatim when `shuntDelegations`
    /// is above zero. The view composes nothing from the numbers.
    var shuntDelegations = 0
    var shuntLinesKeptOut = 0
    var shuntWorkerCostUsd: Double?
    var shuntWords = ""

    enum CodingKeys: String, CodingKey {
        case verdict, report, summary, files
        case cardId = "card_id"
        case runAt = "run_at"
        case sessionId = "session_id"
        case filesAvailable = "files_available"
        case filesReason = "files_reason"
        case filesChanged = "files_changed"
        case filesTotal = "files_total"
        case linesAdded = "lines_added"
        case linesRemoved = "lines_removed"
        case recordedAt = "recorded_at"
        case shuntDelegations = "shunt_delegations"
        case shuntLinesKeptOut = "shunt_lines_kept_out"
        case shuntWorkerCostUsd = "shunt_worker_cost_usd"
        case shuntWords = "shunt_words"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cardId = c.value(.cardId, "")
        runAt = c.value(.runAt, 0)
        sessionId = c.value(.sessionId, "")
        verdict = c.value(.verdict, "")
        report = c.value(.report, "")
        summary = c.value(.summary, "")
        files = c.value(.files, [])
        filesAvailable = c.value(.filesAvailable, false)
        filesReason = c.value(.filesReason, "")
        filesChanged = c.value(.filesChanged, 0)
        filesTotal = c.value(.filesTotal, 0)
        linesAdded = c.value(.linesAdded, 0)
        linesRemoved = c.value(.linesRemoved, 0)
        recordedAt = c.maybe(.recordedAt)
        shuntDelegations = c.value(.shuntDelegations, 0)
        shuntLinesKeptOut = c.value(.shuntLinesKeptOut, 0)
        shuntWorkerCostUsd = c.maybe(.shuntWorkerCostUsd)
        shuntWords = c.value(.shuntWords, "")
    }
}

/// The sealed `work_record` read. `available` is stated by the Mac, so "the
/// board is not open" never looks like "this card has no record".
struct WorkRecordReport: Decodable {
    var available = false
    var generatedAt: Double = 0
    var record: WorkRecord?
    var caption = ""
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, record, caption, reason
        case generatedAt = "generated_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        generatedAt = c.value(.generatedAt, 0)
        record = c.maybe(.record)
        caption = c.value(.caption, "")
        reason = c.value(.reason, "")
    }
}

/// One file's changes, fetched on demand and one file at a time.
struct WorkRecordDiff: Decodable {
    var available = false
    var path = ""
    var isNew = false
    var text = ""
    var truncated = false
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, path, text, truncated, reason
        case isNew = "new"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        isNew = c.value(.isNew, false)
        text = c.value(.text, "")
        truncated = c.value(.truncated, false)
        reason = c.value(.reason, "")
    }
}

/// Formatting for a record, in one place so the view composes nothing.
/// Byte-alike with the panel's copy for the same reason `Markdown.swift` is:
/// two surfaces describing one record must not describe it two ways.
enum WorkRecordFormat {
    static func counts(_ file: WorkRecordFile) -> String {
        if file.binary { return "binary" }
        guard let added = file.added, let removed = file.removed else {
            return file.isNew ? "new" : "—"
        }
        let head = file.isNew ? "new · " : ""
        return "\(head)+\(added) −\(removed)"
    }

    static func summary(_ record: WorkRecord) -> String {
        guard record.filesAvailable else { return record.filesReason }
        if record.filesTotal == 0 { return "No files changed." }
        let noun = record.filesTotal == 1 ? "file" : "files"
        var line = "\(record.filesTotal) \(noun) changed"
        line += ", +\(record.linesAdded) −\(record.linesRemoved)"
        if record.filesChanged < record.filesTotal {
            line += " · \(record.filesChanged) listed"
        }
        return line + "."
    }

    /// The daemon's own sentence for a verdict. A verdict this build does not
    /// know draws **nothing** rather than an invented line.
    static func verdictWords(_ verdict: String) -> String {
        switch verdict {
        case "closed": return "The assistant said it had finished this card."
        case "manual": return "The assistant left a check for somebody to do by hand."
        case "quiet": return "The run ended without the assistant saying anything."
        default: return ""
        }
    }
}

/// One commit on a card's branch, as the sealed `card_changes` read lists it.
struct CardChangeCommit: Decodable, Equatable, Identifiable {
    var sha8 = ""
    var author = ""
    var at: Double = 0
    var subject = ""

    var id: String { sha8 + subject }

    enum CodingKeys: String, CodingKey { case sha8, author, at, subject }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sha8 = c.value(.sha8, "")
        author = c.value(.author, "")
        at = c.value(.at, 0)
        subject = c.value(.subject, "")
    }
}

/// One file the branch changed. A binary file carries no counts of its own
/// (`binary` is stated); the view names it rather than drawing zeros.
struct CardChangeFile: Decodable, Equatable, Identifiable {
    var path = ""
    var added = 0
    var removed = 0
    var binary = false

    var id: String { path }

    enum CodingKeys: String, CodingKey { case path, added, removed, binary }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.value(.path, "")
        added = c.value(.added, 0)
        removed = c.value(.removed, 0)
        binary = c.value(.binary, false)
    }
}

/// The review a card carries, as the Changes read states it: `current` is
/// false when the branch moved since the version judged.
struct CardChangeReview: Decodable, Equatable {
    var verdict = ""
    var tip = ""
    var current = false

    enum CodingKeys: String, CodingKey { case verdict, tip, current }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        verdict = c.value(.verdict, "")
        tip = c.value(.tip, "")
        current = c.value(.current, false)
    }

    init() {}
}

/// The sealed `card_changes` page — a Done card's branch against the main
/// line. `available` is **stated**; `files` absent decodes empty.
/// `branchTip` is echoed back on a file's changes and on MERGE.
/// One row of the Worktrees list: a card's side folder with the daemon's own
/// status word and line, drawn verbatim. Every key through the tolerant
/// `value` helper, so one absent key never fails the page.
struct WorktreeRow: Decodable, Equatable, Identifiable {
    var cardId = ""
    var title = ""
    var project = ""
    var column = ""
    var branch = ""
    var folder = ""
    var status = ""
    var word = ""
    var line = ""
    var ahead = 0
    var uncommitted = 0
    var mergeable = false
    var branchTip = ""

    /// A folder with no card has no id: its folder names it.
    var id: String { cardId.isEmpty ? "folder:" + project + ":" + folder : cardId }

    enum CodingKeys: String, CodingKey {
        case title, project, column, branch, folder, status, word, line
        case ahead, uncommitted, mergeable
        case cardId = "card_id"
        case branchTip = "branch_tip"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cardId = c.value(.cardId, "")
        title = c.value(.title, "")
        project = c.value(.project, "")
        column = c.value(.column, "")
        branch = c.value(.branch, "")
        folder = c.value(.folder, "")
        status = c.value(.status, "")
        word = c.value(.word, "")
        line = c.value(.line, "")
        ahead = c.value(.ahead, 0)
        uncommitted = c.value(.uncommitted, 0)
        mergeable = c.value(.mergeable, false)
        branchTip = c.value(.branchTip, "")
    }
}

/// The sealed `worktrees` page: `available` false with a `reason` for a Mac
/// that cannot list, else the rows in the daemon's order.
struct WorktreesPage: Decodable, Equatable {
    var available = false
    var rows: [WorktreeRow] = []
    var truncated = false
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, rows, truncated, reason
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        let lossy: [ReviewLossy<WorktreeRow>] = c.value(.rows, [])
        rows = lossy.compactMap(\.value)
        truncated = c.value(.truncated, false)
        reason = c.value(.reason, "")
    }
}

struct CardChangesReport: Decodable, Equatable {
    var available = false
    var cardId = ""
    var branch = ""
    var trunk = ""
    var mergeBase = ""
    var branchTip = ""
    var ahead = 0
    var behind = 0
    var commits: [CardChangeCommit] = []
    var files: [CardChangeFile] = []
    var filesTotal = 0
    var filesTruncated = false
    var commitsTruncated = false
    var mergeOffered = false
    var mergeRefusal = ""
    var mergeState = ""
    var mergeNote = ""
    var review = CardChangeReview()
    var generatedAt: Double = 0
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, branch, trunk, ahead, behind, commits, files, review, reason
        case cardId = "card_id"
        case mergeBase = "merge_base"
        case branchTip = "branch_tip"
        case filesTotal = "files_total"
        case filesTruncated = "files_truncated"
        case commitsTruncated = "commits_truncated"
        case mergeOffered = "merge_offered"
        case mergeRefusal = "merge_refusal"
        case mergeState = "merge_state"
        case mergeNote = "merge_note"
        case generatedAt = "generated_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        cardId = c.value(.cardId, "")
        branch = c.value(.branch, "")
        trunk = c.value(.trunk, "")
        mergeBase = c.value(.mergeBase, "")
        branchTip = c.value(.branchTip, "")
        ahead = c.value(.ahead, 0)
        behind = c.value(.behind, 0)
        commits = c.value(.commits, [])
        files = c.value(.files, [])
        filesTotal = c.value(.filesTotal, 0)
        filesTruncated = c.value(.filesTruncated, false)
        commitsTruncated = c.value(.commitsTruncated, false)
        mergeOffered = c.value(.mergeOffered, false)
        mergeRefusal = c.value(.mergeRefusal, "")
        mergeState = c.value(.mergeState, "")
        mergeNote = c.value(.mergeNote, "")
        review = c.value(.review, CardChangeReview())
        generatedAt = c.value(.generatedAt, 0)
        reason = c.value(.reason, "")
    }
}

/// One file's changes on a card's branch, fetched on demand.
struct CardChangeDiff: Decodable, Equatable {
    var available = false
    var path = ""
    var text = ""
    var truncated = false
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, path, text, truncated, reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        text = c.value(.text, "")
        truncated = c.value(.truncated, false)
        reason = c.value(.reason, "")
    }

    init() {}

    /// What the screen draws when the read itself failed (no answer, or the
    /// branch moved since the list was read): words, never a blank.
    static var unreadable: CardChangeDiff {
        var diff = CardChangeDiff()
        diff.reason = "Dark Army could not read that file's changes — "
            + "open the list again."
        return diff
    }
}

struct BoardCard: Decodable, Identifiable, Equatable {
    var outcomeRevision = 0
    var outcomeStatus = "unaccepted"
    var id = ""
    var title = ""
    var summary = ""
    var project = ""
    var column = "backlog"
    var tool = ""
    /// The model the card pins its assistant to, `""` meaning "let the
    /// assistant decide". Cleared by the store whenever the tool changes.
    var model = ""
    /// The composer's staging id, when this card was created with one.
    /// Empty on older Macs and on cards written without a token.
    var createToken = ""
    var linkState = ""
    var dispatchError = ""
    var planPath = ""
    var queueState = ""
    var queueReason = ""
    /// The cards this one waits on, as the store holds them: ids joined by
    /// newlines, written back whole through `board_update` with
    /// `expected_revision`. `""` from an older Mac.
    var blockedBy = ""
    /// Those cards as the Mac resolved them — title, column, and its
    /// `met` word — and the cards waiting on this one; `[]` where none.
    var dependencies: [CardDependency] = []
    var dependents: [CardLink] = []
    /// The Mac's two sentences, drawn verbatim and only where non-empty.
    var dependencyLine = ""
    var dependentsLine = ""
    /// `blockedBy` as a list of ids, the store's own split.
    var dependencyIds: [String] {
        blockedBy.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
    /// The ids of the dependencies that still name a card, in the stored
    /// order — what every write sends back. A deleted card's id stays in
    /// `blockedBy` (the store's rule) but is neither counted against the
    /// limit nor kept by the next edit, so ✕ never faces an id it cannot show.
    var linkedIds: [String] { dependencies.map(\.id) }

    /// What a card's **Add…** may offer as a card to wait on: the cards of
    /// `card`'s own folder, not `card` itself, not already listed, none in
    /// Done — and nothing once the list holds the store's eight
    /// (`board.MAX_BLOCKERS`, which would otherwise cut a ninth without a
    /// word). Pure; the store still refuses a loop, a self-wait and another
    /// project whatever a picker offers.
    static func dependencyChoices(for card: BoardCard,
                                  in cards: [BoardCard]) -> [BoardCard] {
        let listed = Set(card.linkedIds)
        guard listed.count < maxDependencies else { return [] }
        return cards.filter {
            $0.root == card.root && $0.id != card.id
                && !listed.contains($0.id) && $0.column != "done"
        }
    }

    /// The store's own bound on one card's list (`board.MAX_BLOCKERS`).
    static let maxDependencies = 8
    var manualCheckDue = false
    /// The card's branch (`card/…`), `""` where it never had one. With the
    /// next four it is what `CardMerge` reads: the branch the daemon
    /// remembers, what the last MERGE press came to (`merge_state`) with the
    /// daemon's sentence about it (`merge_line`, drawn verbatim), a review's
    /// verdict and whether one is running. Absent keys — an older Mac —
    /// decode empty or false and draw nothing.
    var worktreeBranch = ""
    var mergeState = ""
    var mergeLine = ""
    var reviewVerdict = ""
    var reviewRunning = false
    /// The Mac's one fact for MERGE, Fix and Run review (`merge_offered`);
    /// absent from an older Mac is false.
    var mergeOffered = false
    /// The daemon decides which cards have lost their session.
    var needsYou = false
    /// Mission Control asked for this card to be started (the daemon's
    /// `start_ask_id`, fresh and still startable). The ask starts nothing:
    /// it puts the card on Needs you, and the person's press on START is the
    /// start. `""` for no ask; the id is the Dismiss fingerprint's material.
    var startAskId = ""
    var startAskedAt: Double = 0
    var manualSteps = ""
    /// The check file the flag named, or `""` for steps alone. With a file
    /// the card screen offers Passed / Failed instead of Mark checked.
    var manualCheckPath = ""
    var prompt = ""
    /// Newline-separated stage names, the store's own shape; the cache
    /// keeps it for the card screen.
    var workflow = ""
    /// The specialists Dark Army actually saw run, in the order they ran. Dark Army's own
    /// record — no surface can write it. Newline-separated like `workflow`.
    var agentTrail = ""
    /// Which cast member took each observed stage. Empty for a card recorded
    /// before the crew existed and on every frame an older Mac sends —
    /// `CrewBand` falls back to the roles' anchor faces.
    var crew: [String: String] = [:]
    /// The lead Start will give this card (`lead_face`), so the face shown
    /// before Start is the face that starts. Empty from an older Mac.
    var leadFace: String = ""
    var promptTruncated = false
    var summaryTruncated = false
    var closeNote = ""
    var closedBy = ""
    var closedByName = ""
    /// When a person pressed Reviewed. Nil is absent, null, or malformed —
    /// never a substituted zero. A present 0 is a timestamp, not unknown.
    var reviewedAt: Double?
    /// Whether this finished card reached the frame through the Mac's
    /// *recent preview* read alone — the ones a client asking for
    /// `done=review` stops receiving and fetches itself. A record of which
    /// store read produced the row, never a second judgment about who is
    /// waiting. `false` from an older Mac, which withholds nothing.
    var donePreview = false
    var sessionId = ""
    var refineState = ""
    var refineSessionId = ""
    /// What Dark Army observed this card's last finished run do — the headline
    /// alone. **nil is no record.** The closing words and the file list are
    /// fetched when the card is opened, never on the poll.
    var workRecord: WorkRecordHead?
    /// What this card has cost so far and how long its assistant has
    /// actually worked, composed by the Mac off its two ledgers. **nil is
    /// no run** — `workRecord`'s rule: a card nobody has ever run sends no
    /// key and draws no line, not a zero and not a dash.
    var runFigures: RunFigures.Figures?
    /// How the run is going — the Mac's own reading, drawn verbatim by
    /// `RunHealthLine`. **nil is no run**, `workRecord`'s rule: a card
    /// nobody has started sends no key and draws no line.
    var runHealth: RunHealth?
    /// This card's place in a batch-implement session — the one the
    /// session is on, one waiting its turn, or one dragged out of the line.
    /// **nil is no batch**, `workRecord`'s rule: the daemon sends the key
    /// only for those, and an absent or malformed one decodes to nil rather
    /// than blanking the board. The Mac's `BoardCard.batch`.
    var batch: BatchMark?
    /// The one line the tile draws for `batch`: `BATCH 2/3 · working`, or
    /// `""` where there is no batch. Composed from the daemon's three
    /// fields; nothing is counted here. The panel's body, byte for byte.
    var batchLine: String {
        guard let batch else { return "" }
        // `left` is a card dragged out of the batch's line that still
        // carries its mark: said in words, because its Start is refused.
        let state = batch.state == "left" ? "left the line" : batch.state
        return "BATCH \(batch.rank)/\(batch.size) \u{00B7} \(state)"
    }
    /// Waiting its turn in a batch: its own Start is refused by the daemon
    /// until it leaves the batch.
    var isBatchWaiting: Bool { batch?.state == "waiting" }
    /// Carries a batch mark and no session of its own — waiting in Backlog,
    /// or dragged out of the line (`left`). The daemon refuses its single
    /// Start until Leave batch (`board_reset`) clears the mark, in any column.
    var holdsBatchMark: Bool {
        batch?.state == "waiting" || batch?.state == "left"
    }
    /// The plan version somebody read and said yes to — the SHA-256 of the
    /// plan file's bytes at that moment, `""` for never approved. Written by
    /// the Mac's `approve_plan` alone; empty **never** holds a Start up.
    var planApproved = ""
    /// When they said it, epoch seconds. `0` where nobody has.
    var planApprovedAt: Double = 0
    /// The standing instruction "start this by itself the moment its plan
    /// lands". `'1'` on the wire is on; `''` and an **absent** key are both
    /// off, and off is the required direction — a Mac older than the column
    /// must never make every card look armed. Spent by Dark Army on the attempt, so
    /// it clears itself once Dark Army has acted on it.
    var startWhenPlanned = false
    /// How important this piece of work is, `"0"`..`"100"`, or `""` for
    /// **nobody has scored it**. A string end to end, because the Mac's
    /// column is TEXT and `""` and `"0"` are different states: `""` draws
    /// nothing, `"0"` draws `P 0`. An absent key — a Mac older than the
    /// column — decodes `""`, which is the required direction.
    var priority = ""
    var area = ""
    /// The card's kind. `""` is a build card; `"scout"` is an investigation.
    /// An absent key — a Mac older than the column — decodes `""`.
    var kind = ""
    /// The scout's attached report. An absent key decodes `""`.
    var reportPath = ""
    /// The attached report's one-line verdict and recommendation token, as
    /// the Mac read the answer block at the attach. An absent key — a Mac
    /// older than the column — decodes `""`, and an empty verdict draws
    /// nothing.
    var reportVerdict = ""
    var reportRecommendation = ""
    var isScout: Bool { kind == "scout" }
    /// The card's change number — the Mac's own counter, stepped up whenever
    /// a write changes something a person reads. Sent straight back as
    /// `expected_revision` on a Save, which is what makes two people editing
    /// one card a refusal in words rather than a silent overwrite. An older
    /// Mac sends no key, which reads as `0` and simply guards nothing.
    var revision = 0
    /// The folder this card would dispatch into. What a per-project dial
    /// press is aimed at, sent back to the Mac **verbatim** — the stored map
    /// is keyed by the string it is given, so a phone-normalised variant
    /// would grow a second row meaning the same project.
    var root = ""
    /// How many agents may work at once in this card's project — already
    /// resolved by the daemon. `0` means a Mac from before the per-project
    /// dial, which is the one case the board scalar answers.
    var parallelLimit = 0
    /// Whether an assistant is working on this card right now — the daemon's
    /// own answer, and the RUN/DONE split. Default **true**, the panel's
    /// degrade-safe direction: against a Mac that sends neither flag a
    /// dispatched card reads as running rather than as finished.
    var runActive = true
    /// The queue gate's own per-card answer — `runActive` plus the release
    /// on a manual-check flag. Same default, same reason.
    var workActive = true
    /// When this card joined the queue, and where a person has since moved
    /// it to. `maybe`, so absent and `null` are both nil and the coalesced
    /// sort key falls through to 0 rather than to a made-up number.
    var queuedAt: Double?
    var queueRank: Double?

    var isQueued: Bool { queueState == "queued" }
    var isInFlight: Bool { linkState == "live" || linkState == "dispatching" }
    var isRefining: Bool {
        refineState == "dispatching" || refineState == "live"
    }
    /// An assistant declared this card finished and nobody has looked yet.
    /// Missing `closed_by` is no claim; missing `reviewed_at` with an author
    /// is pending, matching older-server behaviour.
    var awaitsReview: Bool { !closedBy.isEmpty && reviewedAt == nil }

    /// A stored id list back into ids — the panel's `BoardCard.stages`, and
    /// the store's `parse_ids` on the other side.
    static func ids(_ raw: String) -> [String] {
        let parts = raw.contains("\n")
            ? raw.split(separator: "\n")
            : raw.split(separator: ",")
        var out: [String] = []
        for part in parts {
            let id = part.trimmingCharacters(in: .whitespaces)
            if !id.isEmpty && !out.contains(id) { out.append(id) }
        }
        return out
    }

    enum CodingKeys: String, CodingKey {
        case outcomeRevision = "outcome_revision"
        case outcomeStatus = "outcome_status"
        case id, title, summary, project, tool, model, prompt, root
        case workflow, crew
        case leadFace = "lead_face"
        case agentTrail = "agent_trail"
        case parallelLimit = "parallel_limit"
        case runActive = "run_active"
        case workActive = "work_active"
        case queuedAt = "queued_at"
        case queueRank = "queue_rank"
        case createToken = "create_token"
        case column = "column_name"
        case linkState = "link_state"
        case dispatchError = "dispatch_error"
        case planPath = "plan_path"
        case queueState = "queue_state"
        case queueReason = "queue_reason"
        case manualCheckDue = "manual_check_due"
        case worktreeBranch = "worktree_branch"
        case mergeState = "merge_state"
        case mergeLine = "merge_line"
        case reviewVerdict = "review_verdict"
        case reviewRunning = "review_running"
        case mergeOffered = "merge_offered"
        case needsYou = "needs_you"
        case startAskId = "start_ask_id"
        case startAskedAt = "start_asked_at"
        case manualSteps = "manual_steps"
        case manualCheckPath = "manual_check_path"
        case promptTruncated = "prompt_truncated"
        case summaryTruncated = "summary_truncated"
        case closeNote = "close_note"
        case closedBy = "closed_by"
        case closedByName = "closed_by_name"
        case reviewedAt = "reviewed_at"
        case donePreview = "done_preview"
        case sessionId = "session_id"
        case refineState = "refine_state"
        case refineSessionId = "refine_session_id"
        case planApproved = "plan_approved"
        case planApprovedAt = "plan_approved_at"
        case startWhenPlanned = "start_when_planned"
        case priority, area, kind
        case reportPath = "report_path"
        case reportVerdict = "report_verdict"
        case reportRecommendation = "report_recommendation"
        case workRecord = "work_record"
        case runFigures = "run_figures"
        case runHealth = "run_health"
        case batch
        case revision
        case blockedBy = "blocked_by"
        case dependencies, dependents
        case dependencyLine = "dependency_line"
        case dependentsLine = "dependents_line"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        outcomeRevision = c.value(.outcomeRevision, 0)
        outcomeStatus = c.value(.outcomeStatus, "unaccepted")
        id = c.value(.id, "")
        title = c.value(.title, "")
        summary = c.value(.summary, "")
        project = c.value(.project, "")
        column = c.value(.column, "backlog")
        tool = c.value(.tool, "")
        model = c.value(.model, "")
        createToken = c.value(.createToken, "")
        linkState = c.value(.linkState, "")
        dispatchError = c.value(.dispatchError, "")
        planPath = c.value(.planPath, "")
        queueState = c.value(.queueState, "")
        queueReason = c.value(.queueReason, "")
        manualCheckDue = c.value(.manualCheckDue, false)
        worktreeBranch = c.value(.worktreeBranch, "")
        mergeState = c.value(.mergeState, "")
        mergeLine = c.value(.mergeLine, "")
        reviewVerdict = c.value(.reviewVerdict, "")
        reviewRunning = c.value(.reviewRunning, false)
        mergeOffered = c.value(.mergeOffered, false)
        manualSteps = c.value(.manualSteps, "")
        manualCheckPath = c.value(.manualCheckPath, "")
        needsYou = c.value(.needsYou, false)
        startAskId = c.value(.startAskId, "")
        startAskedAt = c.value(.startAskedAt, 0)
        prompt = c.value(.prompt, "")
        workflow = c.value(.workflow, "")
        agentTrail = c.value(.agentTrail, "")
        // Absent on every card with no crew and on an older Mac: the
        // tolerant helper, never `decode`, or one missing key blanks the
        // whole board.
        crew = c.value(.crew, [:])
        leadFace = c.value(.leadFace, "")
        promptTruncated = c.value(.promptTruncated, false)
        summaryTruncated = c.value(.summaryTruncated, false)
        closeNote = c.value(.closeNote, "")
        closedBy = c.value(.closedBy, "")
        closedByName = c.value(.closedByName, "")
        reviewedAt = c.maybe(.reviewedAt)
        donePreview = c.value(.donePreview, false)
        sessionId = c.value(.sessionId, "")
        refineState = c.value(.refineState, "")
        refineSessionId = c.value(.refineSessionId, "")
        planApproved = c.value(.planApproved, "")
        planApprovedAt = c.value(.planApprovedAt, 0)
        startWhenPlanned = c.value(.startWhenPlanned, "") == "1"
        priority = c.value(.priority, "")
        area = c.value(.area, "")
        kind = c.value(.kind, "")
        reportPath = c.value(.reportPath, "")
        // An absent key — a Mac older than the column — decodes "".
        reportVerdict = c.value(.reportVerdict, "")
        reportRecommendation = c.value(.reportRecommendation, "")
        revision = c.value(.revision, 0)
        root = c.value(.root, "")
        parallelLimit = c.value(.parallelLimit, 0)
        runActive = c.value(.runActive, true)
        workActive = c.value(.workActive, true)
        queuedAt = c.maybe(.queuedAt)
        queueRank = c.maybe(.queueRank)
        // `maybe`, deliberately: an absent key is a Mac with no record for
        // this card (or one too old to keep them), and an empty value here
        // would draw as a run that changed nothing.
        workRecord = c.maybe(.workRecord)
        runFigures = c.maybe(.runFigures)
        runHealth = c.maybe(.runHealth)
        // Absent is no batch, malformed is no batch.
        batch = c.maybe(.batch)
        // The dependency five, tolerant with empty defaults: an older Mac
        // sends none, and a card with no links sends none of the four
        // derived ones.
        blockedBy = c.value(.blockedBy, "")
        dependencies = c.value(.dependencies, [])
        dependents = c.value(.dependents, [])
        dependencyLine = c.value(.dependencyLine, "")
        dependentsLine = c.value(.dependentsLine, "")
    }

    init() {}
}

/// A card's place in a batch-implement session, as the daemon published it:
/// `rank` of `size`, `state` `working` or `waiting`. Tolerant on every key,
/// and a mark with no usable rank is no mark at all. The panel's
/// `BoardModels.swift` `BatchMark`, byte for byte.
struct BatchMark: Decodable, Equatable {
    var rank = 0
    var size = 0
    var state = ""

    enum CodingKeys: String, CodingKey { case rank, size, state }

    init(rank: Int, size: Int, state: String) {
        self.rank = rank
        self.size = size
        self.state = state
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rank = c.value(.rank, 0)
        size = c.value(.size, 0)
        state = c.value(.state, "")
        guard rank > 0, !state.isEmpty else {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath,
                debugDescription: "a batch mark needs a rank and a state"))
        }
        size = max(size, rank)
    }
}

/// One card another waits on, as the Mac resolved it. `met` is the Mac's
/// word — Done, or finished and waiting only on a manual check — never
/// re-derived from `column` here.
struct CardDependency: Decodable, Identifiable, Equatable {
    var id = ""
    var title = ""
    var column = ""
    var met = false

    enum CodingKeys: String, CodingKey {
        case id, title, met
        case column = "column_name"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        title = c.value(.title, "")
        column = c.value(.column, "")
        met = c.value(.met, false)
    }

    init() {}
}

/// A card named by another — the "Unblocks" list. Id and title only.
struct CardLink: Decodable, Identifiable, Equatable {
    var id = ""
    var title = ""

    enum CodingKeys: String, CodingKey { case id, title }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        title = c.value(.title, "")
    }

    init() {}
}


/// How a card's run is going, as the Mac publishes it under `run_health`
/// (`run_health.py`): the size class against the project's own finished
/// runs, what the session asked and was refused, how full its context is,
/// and the attempt / return / fix-round counts off the store's ledgers.
/// Every figure is the Mac's; `RunHealthLine` spells them and the phone
/// re-derives none.
///
/// Every field decodes through `value` / `maybe` with a default, and the
/// struct holds no non-optional sub-object, so a ragged object from a newer
/// or older Mac still decodes. `turns`, `tokensK`, `ctxPct` and `fixRounds`
/// are **optionals**, not zeros: the Mac omits each where it is unknown,
/// and a zero there would say something it never said.
struct RunHealth: Decodable, Equatable {
    /// `"typical"`, `"large"`, `"worrying"`, or `""` where the run could not
    /// be sized. The line's first word.
    var sizeClass = ""
    /// `"project"` when judged against this project's own finished runs,
    /// `"default"` before it has enough of them.
    var basis = ""
    var turns: Int?
    var tokensK: Int?
    var ctxPct: Int?
    var asks = 0
    var refusals = 0
    var attempts = 0
    var returns = 0
    var fixRounds: Int?
    /// The Mac's amber bit. Never the only signal: the word comes first.
    var attention = false
    /// Whether the reading came off a live row on this frame rather than
    /// the Mac's frozen record of a finished run.
    var live = false

    enum CodingKeys: String, CodingKey {
        case sizeClass = "class"
        case basis, turns, asks, refusals, attempts, returns, attention, live
        case tokensK = "tokens_k"
        case ctxPct = "ctx_pct"
        case fixRounds = "fix_rounds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sizeClass = c.value(.sizeClass, "")
        basis = c.value(.basis, "")
        turns = c.maybe(.turns)
        tokensK = c.maybe(.tokensK)
        ctxPct = c.maybe(.ctxPct)
        asks = c.value(.asks, 0)
        refusals = c.value(.refusals, 0)
        attempts = c.value(.attempts, 0)
        returns = c.value(.returns, 0)
        fixRounds = c.maybe(.fixRounds)
        attention = c.value(.attention, false)
        live = c.value(.live, false)
    }

    init() {}
}

struct UsageBar: Decodable, Identifiable {
    /// The title is part of the identity because `kind` is not unique. Every
    /// per-model weekly window is `weekly_scoped` — only the title varies — so
    /// two of them collide on a two-part id, and a `ForEach(id: \.id)` over a
    /// colliding pair is undefined. Only one scoped window has ever arrived at
    /// once, which is why this never bit; the live fetch can return more.
    /// One rule for every kind: a split id rule is how the collision returns.
    var id: String { "\(provider):\(kind):\(title)" }
    var kind = ""
    var provider = "claude"
    var shortLabel = ""
    var title = ""
    var percent: Double?
    var resetsAt: Double?
    var stale = false
    /// When this figure was read, epoch seconds. Absent on an older daemon,
    /// which decodes as nil and draws no age note — never as "read just now".
    var asOf: Double?
    /// Where this figure came from: `"statusline"` (Claude Code's own per-turn
    /// report), `"cache"`, or `"oauth"` (the live scoped fetch). Empty where the
    /// daemon names none — an older build, and the Codex and Grok bars it
    /// assembles itself. `ageNote` reads it: a statusline figure is old because
    /// nobody took a turn, which is not a refresh that failed.
    var source = ""
    /// Window family the daemon files the bar under (`session` / `weekly`).
    /// Empty on an older Mac, and on Claude's per-model `weekly_scoped` bars.
    var group = ""

    enum CodingKeys: String, CodingKey {
        case kind, provider, title, percent, stale, source, group
        case shortLabel = "label"
        case resetsAt = "resets_at"
        case asOf = "as_of"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, "")
        provider = c.value(.provider, "claude")
        shortLabel = c.value(.shortLabel, "")
        title = c.value(.title, "")
        percent = c.maybe(.percent)
        resetsAt = c.maybe(.resetsAt)
        stale = c.value(.stale, false)
        asOf = c.maybe(.asOf)
        source = c.value(.source, "")
        group = c.value(.group, "")
    }
}

/// The one key a state answer may carry beside the picture: the usage
/// report the phone asked for with `with_usage`. Decoded on its own, so
/// `Snapshot` never learns the key and the digest rule stands.
struct StateUsageCarrier: Decodable {
    var usage: UsageReport?
    enum CodingKeys: String, CodingKey { case usage }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        usage = c.maybe(.usage)
    }
}

/// One state answer read **off the main actor**, in one parse: the answer's
/// marker, the picture and the usage bars that ride beside it. A whole
/// picture is ~440 KB (the board alone ~400 KB) and was parsed three times
/// on the main actor, inside `applyState` and `takeUsage` — a stall in the
/// middle of whatever the person was scrolling. The poll legs build this
/// with `offMain` and hand it to both; `applyState` still reads `answer`
/// first, and `snapshot` is not decoded at all for an `unchanged` answer —
/// `applyState`'s own order, kept here for the same reason (`Snapshot`
/// decodes anything, so an unchanged body read as one is a blank picture).
/// `snapshot` nil on any other answer is `.unreadable`, exactly as a failed
/// `Snapshot` decode was.
struct StateFrame: @unchecked Sendable {
    var answer = StateAnswer()
    var snapshot: Snapshot?
    var usage: StateUsageCarrier?

    /// The same three reads `applyState` / `takeUsage` make, sharing one
    /// JSON parse. Pure; safe on any thread.
    static func decode(_ body: Data) -> StateFrame {
        (try? JSONDecoder().decode(Whole.self, from: body))?.frame ?? StateFrame()
    }

    /// `decode`, on a background task. The caller awaits it straight after
    /// the transport's own await, so the screen sees nothing new in between.
    static func offMain(_ body: Data) async -> StateFrame {
        await Task.detached(priority: .userInitiated) { decode(body) }.value
    }

    private struct Whole: Decodable {
        var frame = StateFrame()
        init(from decoder: Decoder) throws {
            frame.answer = (try? StateAnswer(from: decoder)) ?? StateAnswer()
            if !frame.answer.unchanged {
                frame.snapshot = try? Snapshot(from: decoder)
            }
            frame.usage = try? StateUsageCarrier(from: decoder)
        }
    }
}

struct UsageReport: Decodable {
    var bars: [UsageBar] = []
    var attribution: UsageAttribution?

    enum CodingKeys: String, CodingKey { case limits, attribution }
    enum LimitsKeys: String, CodingKey { case bars }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        attribution = c.maybe(.attribution)
        if let limits = try? c.nestedContainer(keyedBy: LimitsKeys.self, forKey: .limits) {
            bars = limits.value(.bars, [])
        }
    }
}

struct UsageAttribution: Decodable {
    var available = false
    var models: [ModelShare] = []
    // nil means an older daemon; a present empty array is authoritative.
    var providers: [ProviderShare]?

    struct ModelShare: Decodable, Identifiable {
        var model = ""
        var pct: Double?
        var id: String { model }

        enum CodingKeys: String, CodingKey { case model, pct }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            model = c.value(.model, "")
            pct = c.maybe(.pct)
        }
    }

    struct ProviderShare: Decodable, Identifiable {
        var provider = ""
        var available = false
        var reason = ""
        var measurement = ""
        var windowStart: Double?
        var windowEnd: Double?
        var windowLabel = ""
        var partial = false
        var models: [ModelShare] = []
        var id: String { provider }
        var heading: String { provider.isEmpty ? "SPENDERS" : "\(provider.uppercased()) · SPENDERS" }
        var caption: String {
            let meaning: String
            switch measurement {
            case "local_token_share": meaning = "share of locally recorded tokens"
            case "estimated_cost_share": meaning = "share of this provider’s priced local work · estimated cost"
            case "unpriced": meaning = "price unavailable for recorded work"
            default: meaning = "locally recorded work"
            }
            return windowLabel.isEmpty ? meaning : "\(meaning) · \(windowLabel)"
        }
        var period: String? {
            guard let start = windowStart, let end = windowEnd,
                  start.isFinite, end.isFinite else { return nil }
            let formatter = DateFormatter()
            formatter.dateFormat = "d MMM HH:mm"
            return "\(formatter.string(from: Date(timeIntervalSince1970: start))) – \(formatter.string(from: Date(timeIntervalSince1970: end)))"
        }

        enum CodingKeys: String, CodingKey {
            case provider, available, reason, measurement, partial, models
            case windowStart = "window_start"
            case windowEnd = "window_end"
            case windowLabel = "window_label"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            provider = c.value(.provider, "")
            available = c.value(.available, false)
            reason = c.value(.reason, "")
            measurement = c.value(.measurement, "")
            partial = c.value(.partial, false)
            models = c.value(.models, [])
            windowStart = c.maybe(.windowStart)
            windowEnd = c.maybe(.windowEnd)
            windowLabel = c.value(.windowLabel, "")
        }

        init(legacyModels: [ModelShare]) {
            available = true
            models = legacyModels
        }
    }

    var spenderGroups: [ProviderShare] {
        if let providers { return providers }
        return available && !models.isEmpty ? [ProviderShare(legacyModels: models)] : []
    }

    enum CodingKeys: String, CodingKey { case available, models, providers }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        models = c.value(.models, [])
        providers = c.contains(.providers) ? c.value(.providers, []) : nil
    }

    init() {}
}

/// One line of the Mac's diary (`event_log.py`), drawn verbatim by the Fleet
/// tab's Recently section. Every key is tolerant; `detail` may carry numbers
/// (`cost`, `duration_seconds`) as well as strings, so it is decoded through
/// `LogScalar`, which stringifies — one finish entry must not fail the page.
/// The daemon composes `text`; nothing here rewords it.
struct LogEntry: Decodable, Identifiable, Equatable {
    var id = ""
    var ts: Double = 0
    var kind = ""
    var text = ""
    var project = ""
    var nickname = ""
    var title = ""
    var sessionId = ""
    var cardId = ""
    var detail: [String: String] = [:]

    enum CodingKeys: String, CodingKey {
        case id, ts, kind, text, project, nickname, title, detail
        case sessionId = "session_id"
        case cardId = "card_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        ts = c.value(.ts, 0)
        kind = c.value(.kind, "")
        text = c.value(.text, "")
        project = c.value(.project, "")
        nickname = c.value(.nickname, "")
        title = c.value(.title, "")
        sessionId = c.value(.sessionId, "")
        cardId = c.value(.cardId, "")
        let raw: [String: LogScalar] = c.value(.detail, [:])
        detail = raw.mapValues(\.text)
    }

    init() {}
}

/// A JSON scalar of any type, read as text.
struct LogScalar: Decodable, Equatable {
    var text = ""

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) {
            text = s
        } else if let b = try? c.decode(Bool.self) {
            text = b ? "true" : "false"
        } else if let d = try? c.decode(Double.self) {
            text = d == d.rounded() && abs(d) < 1e15 ? String(Int(d)) : String(d)
        } else {
            text = ""
        }
    }
}

/// `GET /api/log`'s answer: `available` is stated by the daemon (false when
/// its diary failed to open), `events` newest first.
struct LogPage: Decodable {
    var available = true
    var generatedAt: Double = 0
    var events: [LogEntry] = []

    enum CodingKeys: String, CodingKey {
        case available, events
        case generatedAt = "generated_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, true)
        generatedAt = c.value(.generatedAt, 0)
        events = c.value(.events, [])
    }

    init() {}
}

/// One card, in full, with the plan document it points at — the answer to the
/// sealed `card` read.
///
/// The live frame carries a *preview* of a card's instructions
/// (`BOARD_SNAPSHOT_PROMPT_CHARS`), which is deliberate and stays that way;
/// this is where the phone gets the real text before it draws it or lets
/// anybody save over it. Every field is defaulted and decoded through the
/// tolerant helpers: an older Mac's answer must never blank the card screen.
struct CardFull: Decodable {
    var available = false
    var card: BoardCard?
    var plan = CardPlan()
    /// A scout's written report (`report_path`), `plan`'s twin on the
    /// on-open read: the same shape, `digest` empty. Nil from an older Mac.
    var report: CardPlan?
    /// The card's timeline, a sibling of `plan` on the on-open read alone.
    /// Nil from an older Mac, which draws the TIMELINE row absent.
    var timeline: CardTimelineReport?
    /// The check file the card was flagged with (`manual_check_path`),
    /// `report`'s twin on the on-open read. Nil from an older Mac.
    var manualCheck: CardPlan?

    enum CodingKeys: String, CodingKey {
        case available, cards, plan, report, timeline
        case manualCheck = "manual_check"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        card = c.value(.cards, [BoardCard]()).first
        plan = c.value(.plan, CardPlan())
        report = c.maybe(.report)
        timeline = c.maybe(.timeline)
        manualCheck = c.maybe(.manualCheck)
    }

    init() {}
}

/// The plan a card points at, as the Mac read it. `available` is **stated**,
/// never inferred from an empty `text`: a missing file, a path outside the
/// card's project and a card with no plan at all are three different things
/// and each says so in `reason`.
struct CardPlan: Decodable {
    var available = false
    var path = ""
    var text = ""
    /// What an approval echoes back — the SHA-256 of the bytes above. The
    /// phone never computes a hash; the Mac refuses one it did not just send.
    var digest = ""
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, path, text, digest, reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        text = c.value(.text, "")
        digest = c.value(.digest, "")
        reason = c.value(.reason, "")
    }

    init() {}
}

/// The card timeline's wire shapes. **Byte-identical to the three structs in
/// `panel/Sources/BobPanel/CardTimeline.swift`** from the line below, pinned
/// by `host/tests/test_card_timeline.py`; the rule (`enum CardTimeline`) is
/// the phone copy in `CardDetailView.swift`. They live here rather than
/// beside it because `CardFull` names them and `test_phone_inbox.py`
/// compiles this file with only `Inbox.swift` and `Collab.swift` beside it.
/// The card's timeline, as the daemon composes it (`card_timeline.py`) and
/// as both card screens draw it — the moments in the card's life in order,
/// the time between neighbours, and one line saying where the card is now.
///
/// **Words on the wire, drawn verbatim.** Every label, every gap word and
/// the open line's label come from the daemon; this file decides only how a
/// step becomes a row (`rows`), how seconds become `"2h 10m"` (`elapsed`,
/// pinned equal to the Python `elapsed_text` by one fixture table), and how
/// the open line is composed. No colour is a meaning: a `Row` carries words
/// only. Foundation only. Copied byte-equal into
/// `ios/BobPhone/CardDetailView.swift` after `CardSections` and pinned by
/// `host/tests/test_card_timeline.py`; tabled in
/// `panel/Tests/BobPanelTests/CardTimelineTests.swift`.
///
/// Every field decodes through the tolerant helpers: `at`,
/// `since_previous_seconds`, `open` and `note` are legitimately absent, and
/// an unknown `kind` is kept and drawn by its `label`.
struct CardTimelineStep: Decodable, Identifiable {
    var kind = ""
    var label = ""
    var at: Double?
    var observed = false
    var note = ""
    var durable = true
    var sincePreviousSeconds: Double?
    var elapsed = ""
    var gap = ""

    var id: String {
        kind + "@" + (at.map { String($0) } ?? "none") + "/" + note
    }

    enum CodingKeys: String, CodingKey {
        case kind, label, at, observed, note, durable, elapsed, gap
        case sincePreviousSeconds = "since_previous_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, "")
        label = c.value(.label, "")
        at = c.maybe(.at)
        observed = c.value(.observed, at != nil)
        note = c.value(.note, "")
        durable = c.value(.durable, true)
        sincePreviousSeconds = c.maybe(.sincePreviousSeconds)
        elapsed = c.value(.elapsed, "")
        gap = c.value(.gap, "")
    }

    init(kind: String = "", label: String = "", at: Double? = nil,
         note: String = "", durable: Bool = true,
         sincePreviousSeconds: Double? = nil, gap: String = "") {
        self.kind = kind
        self.label = label
        self.at = at
        self.observed = at != nil
        self.note = note
        self.durable = durable
        self.sincePreviousSeconds = sincePreviousSeconds
        // The daemon's own figure; `CardTimeline.rows` derives the drawn
        // gap from `sincePreviousSeconds`, never from this string.
        self.elapsed = ""
        self.gap = gap
    }
}

/// Where the card is now. `since` nil means the moment it entered that
/// state was never witnessed; the line then reads "not observed".
struct CardTimelineOpen: Decodable, Equatable {
    var kind = ""
    var label = ""
    var since: Double?

    enum CodingKeys: String, CodingKey { case kind, label, since }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, "")
        label = c.value(.label, "")
        since = c.maybe(.since)
    }

    init(kind: String = "", label: String = "", since: Double? = nil) {
        self.kind = kind
        self.label = label
        self.since = since
    }
}

struct CardTimelineReport: Decodable {
    var available = false
    var reason = ""
    var generatedAt: Double = 0
    var trackingSince: Double?
    var leftCensored = false
    var recentOnlyNote = ""
    var truncated = false
    var steps: [CardTimelineStep] = []
    var open: CardTimelineOpen?

    enum CodingKeys: String, CodingKey {
        case available, reason, steps, open, truncated
        case generatedAt = "generated_at"
        case trackingSince = "tracking_since"
        case leftCensored = "left_censored"
        case recentOnlyNote = "recent_only_note"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        reason = c.value(.reason, "")
        generatedAt = c.value(.generatedAt, 0)
        trackingSince = c.maybe(.trackingSince)
        leftCensored = c.value(.leftCensored, false)
        recentOnlyNote = c.value(.recentOnlyNote, "")
        truncated = c.value(.truncated, false)
        steps = c.value(.steps, [])
        open = c.maybe(.open)
    }

    init(available: Bool = true, generatedAt: Double = 0,
         steps: [CardTimelineStep] = [], open: CardTimelineOpen? = nil,
         recentOnlyNote: String = "", reason: String = "") {
        self.available = available
        self.generatedAt = generatedAt
        self.steps = steps
        self.open = open
        self.recentOnlyNote = recentOnlyNote
        self.reason = reason
    }
}

/// The answer to the sealed `card_sync` read: many cards in full, plus the
/// plan documents that moved since the phone last looked.
///
/// The delta is what makes it cheap. The phone asks only for the ids whose
/// `revision` it does not already hold, and hands back a digest per plan it
/// has; a plan whose digest still matches comes back `unchanged` with no
/// text at all. An unchanged board asks for nothing and this read never runs.
///
/// Tolerant like every other model here — Swift's synthesized `Decodable`
/// throws on a missing key even with a default, and one absent field must
/// never cost the phone a whole page.
/// What the sealed `done` read answers with: the whole finished column in the
/// board's own card shape, plus the two tokens the held copy is stamped with.
///
/// The tokens are read on the Mac **before** the cards, deliberately: a card
/// written mid-read leaves this copy stamped older than it is and costs one
/// extra refetch, where the other order would leave it stamped fresh and
/// stale for ever.
struct DoneArchivePage: Decodable {
    var available = false
    var generatedAt: Double = 0
    var cards: [BoardCard] = []
    var count = 0
    /// Cards the Mac's 300 KB budget did not reach. The page says so rather
    /// than pretending the column is shorter than it is.
    var more = false
    var doneClearToken = ""
    var doneViewToken = ""

    enum CodingKeys: String, CodingKey {
        case available, cards, count, more
        case generatedAt = "generated_at"
        case doneClearToken = "done_clear_token"
        case doneViewToken = "done_view_token"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        generatedAt = c.value(.generatedAt, 0)
        cards = c.value(.cards, [])
        count = c.value(.count, 0)
        more = c.value(.more, false)
        doneClearToken = c.value(.doneClearToken, "")
        doneViewToken = c.value(.doneViewToken, "")
    }

    init() {}
}

struct CardSyncPage: Decodable {
    var available = false
    var generatedAt: Double = 0
    var cards: [BoardCard] = []
    var plans: [SyncedPlan] = []
    /// Ids the Mac's 300 KB budget did not reach. They stay stale and the
    /// next pass asks again; `more` is the Mac's own statement of that.
    var unserved: [String] = []
    var more = false

    enum CodingKeys: String, CodingKey {
        case available, cards, plans, unserved, more
        case generatedAt = "generated_at"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        generatedAt = c.value(.generatedAt, 0)
        cards = c.value(.cards, [])
        plans = c.value(.plans, [])
        unserved = c.value(.unserved, [])
        more = c.value(.more, false)
    }

    init() {}
}

/// One plan document inside a `CardSyncPage`. `CardPlan`'s fields plus the
/// card it belongs to and the Mac's `unchanged` mark.
///
/// `available` is **stated**, never inferred from an empty `text`: a card
/// with no plan, a path outside the card's project, a missing file and a
/// plan whose digest the phone already holds are four different things and
/// each says which in `reason` or `unchanged`.
struct SyncedPlan: Decodable {
    var cardId = ""
    var path = ""
    var available = false
    var text = ""
    var digest = ""
    var reason = ""
    /// The digest the phone sent still matches, so the Mac sent no bytes.
    /// The cached copy stands; this is never "the plan is gone".
    var unchanged = false

    enum CodingKeys: String, CodingKey {
        case path, available, text, digest, reason, unchanged
        case cardId = "card_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cardId = c.value(.cardId, "")
        path = c.value(.path, "")
        available = c.value(.available, false)
        text = c.value(.text, "")
        digest = c.value(.digest, "")
        reason = c.value(.reason, "")
        unchanged = c.value(.unchanged, false)
    }

    init() {}
}

// MARK: - A Dark Army-owned terminal's raw feed

/// One answer to the sealed `terminal` read asked with `grid=0` and
/// `since_bytes` — the away phone's feed for its own emulator. `data` is
/// base64 of raw pty bytes: a whole `Screen.paint()` when `painted` (the
/// phone held nothing, or its cursor fell off the Mac's ring —
/// `ringOverflowed`), else the bytes since the cursor, capped per poll with
/// `dataMore` saying the rest is waiting. `bytesRead` is the cursor to
/// quote back. Every key decodes tolerantly; `painted` and `dataMore`
/// default **false**, and an absent `data` is no bytes, never a reset.
struct TerminalBytesFrame: Decodable, Equatable {
    var available = false
    var session = ""
    var data = ""
    var bytesRead = -1
    var painted = false
    var dataMore = false
    var ringOverflowed = false
    var cols = 0
    var rows = 0
    var exited = false
    var reason = ""

    init() {}

    enum CodingKeys: String, CodingKey {
        case available, session, data, painted, cols, rows, exited, reason
        case bytesRead = "bytes_read"
        case dataMore = "data_more"
        case ringOverflowed = "ring_overflowed"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        session = c.value(.session, "")
        data = c.value(.data, "")
        bytesRead = c.value(.bytesRead, -1)
        painted = c.value(.painted, false)
        dataMore = c.value(.dataMore, false)
        ringOverflowed = c.value(.ringOverflowed, false)
        cols = c.value(.cols, 0)
        rows = c.value(.rows, 0)
        exited = c.value(.exited, false)
        reason = c.value(.reason, "")
    }

    /// The raw bytes, decoded; empty for anything that is not base64.
    var bytes: Data { Data(base64Encoded: data) ?? Data() }
}

/// One picture from the Mac (the sealed `image` read): shrunk there to fit
/// one answer, or refused in words (`reason`). Every key tolerant: an older
/// or newer Mac never blanks the sheet.
struct ImagePreview: Decodable, Equatable {
    var available = false
    var path = ""
    var name = ""
    var reason = ""
    var format = ""
    var sourceFormat = ""
    var data = ""
    var bytes = 0
    var modified: Double = 0
    var width = 0
    var height = 0
    var shownWidth = 0
    var shownHeight = 0
    var animated = false
    var frames = 1
    var reduced = false

    enum CodingKeys: String, CodingKey {
        case available, path, name, reason, format, data, bytes, modified
        case width, height, animated, frames, reduced
        case sourceFormat = "source_format"
        case shownWidth = "shown_width"
        case shownHeight = "shown_height"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        name = c.value(.name, "")
        reason = c.value(.reason, "")
        format = c.value(.format, "")
        sourceFormat = c.value(.sourceFormat, "")
        data = c.value(.data, "")
        bytes = c.value(.bytes, 0)
        modified = c.value(.modified, 0.0)
        width = c.value(.width, 0)
        height = c.value(.height, 0)
        shownWidth = c.value(.shownWidth, 0)
        shownHeight = c.value(.shownHeight, 0)
        animated = c.value(.animated, false)
        frames = c.value(.frames, 1)
        reduced = c.value(.reduced, false)
    }

    var pictureData: Data? { available ? Data(base64Encoded: data) : nil }
}

/// The picture sheet's words, Foundation only.
enum ImagePreviewWords {
    /// `1024 × 1536 · JPEG · 4.1 MB · changed 3m ago`.
    static func meta(_ p: ImagePreview, now: Date = Date()) -> String {
        var parts: [String] = []
        if p.width > 0, p.height > 0 { parts.append("\(p.width) × \(p.height)") }
        let kind = (p.sourceFormat.isEmpty ? p.format : p.sourceFormat).uppercased()
        if !kind.isEmpty { parts.append(kind) }
        if p.bytes > 0 { parts.append(size(p.bytes)) }
        if p.modified > 0 { parts.append("changed " + age(now.timeIntervalSince1970 - p.modified)) }
        return parts.joined(separator: " · ")
    }

    /// What the phone is showing instead of the Mac's own file, or "".
    static func reducedLine(_ p: ImagePreview) -> String {
        if p.animated && p.format != "gif" {
            return "animated, \(p.frames) frames — shown as its first frame; open it on the Mac to see it move"
        }
        if p.shownWidth > 0, p.width > 0, p.shownWidth < p.width {
            return "shown at \(p.shownWidth) × \(p.shownHeight) — the Mac's copy is larger"
        }
        return ""
    }

    static func size(_ bytes: Int) -> String {
        if bytes < 1024 { return "\(bytes) B" }
        if bytes < 1024 * 1024 { return "\(Int((Double(bytes) / 1024).rounded())) KB" }
        return String(format: "%.1f MB", Double(bytes) / 1024 / 1024)
    }

    static func age(_ seconds: Double) -> String {
        let s = max(0, Int(seconds))
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60)m ago" }
        if s < 86400 { return "\(s / 3600)h ago" }
        return "\(s / 86400)d ago"
    }

    static func spoken(_ p: ImagePreview) -> String {
        var words = "Picture \(p.name.isEmpty ? (p.path as NSString).lastPathComponent : p.name)"
        if p.width > 0, p.height > 0 { words += ", \(p.width) by \(p.height)" }
        return words
    }
}


/// The phone's only copy of a picture it was shown: in memory, at most
/// `limit` of them, each dropped `lifetime` after it arrived (a day) —
/// never written to disk, gone with the pairing and with the app.
struct ImageMemo {
    static let lifetime: TimeInterval = 24 * 60 * 60
    static let limit = 16
    private var entries: [(key: String, at: Date, preview: ImagePreview)] = []

    var count: Int { entries.count }

    mutating func put(_ preview: ImagePreview, session: String, path: String,
                      now: Date = Date()) {
        let key = session + "\u{0}" + path
        prune(now: now)
        entries.removeAll(where: { $0.key == key })
        entries.append((key, now, preview))
        if entries.count > Self.limit { entries.removeFirst(entries.count - Self.limit) }
    }

    mutating func get(session: String, path: String, now: Date = Date()) -> ImagePreview? {
        prune(now: now)
        return entries.last(where: { $0.key == session + "\u{0}" + path })?.preview
    }

    mutating func prune(now: Date = Date()) {
        entries.removeAll(where: { now.timeIntervalSince($0.at) >= Self.lifetime })
    }
}
