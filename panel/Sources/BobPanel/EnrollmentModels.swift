import Foundation

// The enrolment ledger, the paired devices, the relay, the security section
// and the inbox acknowledgements — everything on the snapshot that says who
// may talk to Dark Army. Split out of `Models.swift` on 20 Sep 2026.

/// The phone doors' open burst alerts (`BobDaemon.security_snapshot`).
/// Extra keys ignored; missing inner fields default. A present section that
/// will not decode throws, exactly as `InboxAcks` does.
struct SecuritySection: Decodable, Hashable {
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

/// One burst alert: several refused knocks from one source at one door. The
/// sentence is the daemon's (`access_log.sentence`), drawn verbatim.
struct AccessAlert: Decodable, Hashable, Identifiable {
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

/// One line of the access log (`access_log.PUBLISHED_KEYS`), for the
/// Security window. Tolerant: every field defaults.
struct AccessLogEntry: Decodable, Hashable, Identifiable {
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

    enum CodingKeys: String, CodingKey {
        case id, ts, kind, door, peer, reason, text, count
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
    }
}

/// `GET /api/access-log`'s answer. `available` is stated by the daemon.
struct AccessLogReport: Decodable, Hashable {
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

/// Daemon-shared Needs you acknowledgements. Extra keys ignored; missing
/// inner fields default. A present section that will not decode throws.
struct InboxAcks: Decodable, Hashable {
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

struct InboxAckRecord: Decodable, Hashable {
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

/// One project Dark Army is watching, or one it has just turned away.
struct EnrolledProject: Decodable, Identifiable, Hashable {
    var root = ""
    var label = ""
    /// When Dark Army last refused something from this folder. Only pending folders
    /// carry it; enrolled ones leave it at zero.
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

/// The enrolment boundary, as the panel is shown it.
///
/// `available` is a **field, not an inference**. An older daemon sends no
/// `enrollment` section at all, and this struct's default has `available =
/// false` — so "the daemon did not say" and "nothing is enrolled" stay
/// different answers, and only the second one may draw the enrolment prompt
/// over the window. That is `Board.available`'s argument exactly.
///
/// No key and no digest ever appears in this payload; a pytest on the daemon
/// side asserts it.
struct Enrollment: Decodable {
    var available = false
    var enrolled: [EnrolledProject] = []
    /// Folders Dark Army has turned away and could corroborate against an open VS
    /// Code window. Never simply the path a message claimed: the hook socket
    /// authenticates nothing, and an Enrol button beside an arbitrary path
    /// would write a key into it.
    var pending: [EnrolledProject] = []
    /// How many refusals could not be attributed to a folder worth naming.
    var other = 0
    /// The first-run checklist's evidence, per enrolled root. **`nil` means
    /// the daemon did not say** — an older one publishes no `checklist` key
    /// — and the panel keeps its ordinary enrolment UI on that; it is never
    /// proof that nothing is enrolled. A present-but-ragged block decodes to
    /// its defaults rather than throwing the frame away.
    var checklist: EnrollmentChecklist?

    enum CodingKeys: String, CodingKey {
        case available, enrolled, pending, other, checklist
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        enrolled = c.value(.enrolled, [])
        pending = c.value(.pending, [])
        other = c.value(.other, 0)
        checklist = c.maybe(.checklist)
    }

    init() {}

    /// Nothing enrolled, on a daemon that actually said so — the one state that
    /// may replace the fleet with an explanation.
    var isEmpty: Bool { available && enrolled.isEmpty }
}

/// What the daemon has *observed* about each enrolled folder, for the
/// first-run checklist: whether that exact folder is open in a live VS Code
/// window, and whether an admitted main session is running inside it. Bare
/// facts — no key, digest, session id or pid rides here — and each is stated
/// per root so a step for folder A can never be ticked by folder B.
struct EnrollmentChecklist: Decodable, Hashable {
    var available = false
    var roots: [ChecklistRootFacts] = []

    enum CodingKeys: String, CodingKey { case available, roots }

    init() {}

    init(available: Bool, roots: [ChecklistRootFacts]) {
        self.available = available
        self.roots = roots
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        roots = c.value(.roots, [])
    }

    func facts(for root: String) -> ChecklistRootFacts? {
        roots.first { $0.root == root }
    }

    /// The roots the daemon marked as Dark Army's own checkout — the one
    /// `enroll_self()` enrolled, not a folder the person chose. The
    /// checklist never follows one of these or offers it in the picker.
    var ownCheckoutRoots: Set<String> {
        Set(roots.filter(\.ownCheckout).map(\.root))
    }
}

struct ChecklistRootFacts: Decodable, Hashable {
    var root = ""
    var editorObserved = false
    var sessionObserved = false
    /// This root is Dark Army's own checkout (`own_checkout`). An older
    /// daemon omits it; absent decodes as false.
    var ownCheckout = false

    enum CodingKeys: String, CodingKey {
        case root
        case editorObserved = "editor_observed"
        case sessionObserved = "session_observed"
        case ownCheckout = "own_checkout"
    }

    init(root: String, editorObserved: Bool = false, sessionObserved: Bool = false,
         ownCheckout: Bool = false) {
        self.root = root
        self.editorObserved = editorObserved
        self.sessionObserved = sessionObserved
        self.ownCheckout = ownCheckout
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        root = c.value(.root, "")
        editorObserved = c.value(.editorObserved, false)
        sessionObserved = c.value(.sessionObserved, false)
        ownCheckout = c.value(.ownCheckout, false)
    }
}

/// One phone Dark Army has paired, as the panel is shown it. No token and no digest
/// ever appears here.
struct PairedDevice: Decodable, Identifiable, Hashable {
    var id = ""
    var name = ""
    var pairedAt: Double = 0
    var lastSeen: Double = 0
    /// Whether an away channel exists for this phone — a bare fact, never a
    /// key. An older daemon omits it; absent decodes as false.
    var relay = false
    /// When this phone's away write window lapses, epoch seconds. Zero means
    /// none (no channel, or an older daemon).
    var leaseExpiresAt: Double = 0
    /// When this phone's away channel last carried a message, epoch seconds.
    /// Zero means never (or an older daemon, which publishes no such key) —
    /// so the menu says nothing rather than "0 minutes ago".
    var lastFrameAt: Double = 0
    /// How many days one check-in at home buys this phone — the grant made
    /// on this Mac. **`-1` means the daemon said nothing** (an older one
    /// publishes no such key) and draws nothing; `0` means somebody ended
    /// away access for this phone, which is a different sentence entirely.
    var leaseDays: Int = -1
    /// Whether this phone has a home key — a bare fact, never the key. A
    /// phone paired before sealed home access has none and must re-pair;
    /// absent decodes as false.
    var home = false
    /// Whether this phone may answer a buzz from its lock screen. **`nil`
    /// means the daemon said nothing** (an older one publishes no key) and
    /// the row is absent rather than a wrong "off".
    var lockScreenActions: Bool?
    /// The socket lane's standing for this phone, one word — `off`,
    /// `connecting`, `open`, `armed` — never a key, a digest or a channel
    /// id. Empty on an older daemon, which publishes no such key, and the
    /// line is then absent.
    var socket = ""
    /// The bot's two grants, read and write. **`nil` means this row is not
    /// the bot** (or an older daemon published none), and the day-window
    /// rows are drawn as for any phone; never a zeroed struct, which would
    /// draw two "off" groups on every phone.
    var botAccess: BotAccess?

    enum CodingKeys: String, CodingKey {
        case id, name, relay, home, socket
        case pairedAt = "paired_at"
        case lastSeen = "last_seen"
        case leaseExpiresAt = "lease_expires_at"
        case leaseDays = "lease_days"
        case lastFrameAt = "last_frame_at"
        case lockScreenActions = "lock_screen_actions"
        case botAccess = "bot_access"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        name = c.value(.name, "")
        pairedAt = c.value(.pairedAt, 0)
        lastSeen = c.value(.lastSeen, 0)
        relay = c.value(.relay, false)
        leaseExpiresAt = c.value(.leaseExpiresAt, 0)
        leaseDays = c.value(.leaseDays, -1)
        lastFrameAt = c.value(.lastFrameAt, 0)
        home = c.value(.home, false)
        lockScreenActions = try? c.decode(Bool.self, forKey: .lockScreenActions)
        socket = c.value(.socket, "")
        botAccess = try? c.decode(BotAccess.self, forKey: .botAccess)
    }

    init(id: String = "", name: String = "") {
        self.id = id
        self.name = name
    }
}

/// One side of the bot's access as the daemon states it: a mode word out
/// of `off`, `1h`, `6h`, `24h`, `forever`, and for a timed mode the epoch
/// it ends at. `relay.bot_grant`'s answer, drawn and never re-derived.
struct BotGrant: Decodable, Hashable {
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

/// The bot's two grants. Tolerant: a half-stated object keeps the other
/// side's default rather than failing the row.
struct BotAccess: Decodable, Hashable {
    var read = BotGrant()
    var write = BotGrant()

    enum CodingKeys: String, CodingKey { case read, write }

    init(read: BotGrant = BotGrant(), write: BotGrant = BotGrant()) {
        self.read = read
        self.write = write
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        read = c.value(.read, BotGrant())
        write = c.value(.write, BotGrant())
    }

    func grant(_ side: String) -> BotGrant {
        side == "write" ? write : read
    }
}

/// One write driven from outside the house, for the Devices menu's "Done
/// remotely" list. Bounded on the daemon; never a payload, token or key.
struct RemoteAction: Decodable, Hashable {
    var deviceId = ""
    var action = ""
    var at: Double = 0
    var ok = true

    enum CodingKeys: String, CodingKey {
        case action, at, ok
        case deviceId = "device_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        deviceId = c.value(.deviceId, "")
        action = c.value(.action, "")
        at = c.value(.at, 0)
        ok = c.value(.ok, true)
    }
}

/// Whether the away link is currently working, as the daemon states it.
/// **Statuses and clock readings only** — the channel id is derived from the
/// channel key and never travels here, and neither does the key or its
/// digest. An older daemon publishes no `relay_health` at all, which is why
/// `stated` is a field rather than an inference from a zeroed struct.
struct RelayHealth: Decodable, Hashable {
    var stated = false
    /// `"ok"` or `"failing"`.
    var state = ""
    /// The last HTTP status that failed, or zero for no answer at all.
    var status = 0
    var failures = 0
    /// Seconds the link has been failing. Zero while healthy.
    var failingFor: Double = 0
    /// When the mailbox last answered, epoch seconds. Zero means never.
    var lastOkAt: Double = 0

    enum CodingKeys: String, CodingKey {
        case state, status, failures
        case failingFor = "failing_for"
        case lastOkAt = "last_ok_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let said: String? = c.maybe(.state)
        stated = said != nil
        state = said ?? ""
        status = c.value(.status, 0)
        failures = c.value(.failures, 0)
        failingFor = c.value(.failingFor, 0)
        lastOkAt = c.value(.lastOkAt, 0)
    }

    init() {}

    var isFailing: Bool { state == "failing" }
}

/// The phone-pairing boundary, as the panel is shown it.
///
/// `available` is a **field, not an inference**. An older daemon sends no
/// `devices` section at all, and this struct's default has `available =
/// false` — so "the daemon did not say" and "no phones are paired" stay
/// different answers. That is `Enrollment.available`'s argument exactly.
struct Devices: Decodable {
    var available = false
    var lanEnabled = false
    var port = 0
    var devices: [PairedDevice] = []
    /// Whether the daemon *said anything* about the away path. An older
    /// daemon omits `remote_enabled` entirely, and the Devices menu must
    /// then draw no Away rows at all — absent is not "off".
    var remoteStated = false
    var remoteEnabled = false
    var relayURL = ""
    var remoteActivity: [RemoteAction] = []
    /// The away link's standing. Absent on an older daemon, and absent too
    /// while no channel has polled yet — `relayHealth.stated` is the test.
    var relayHealth = RelayHealth()
    /// Whether the daemon *said anything* about the socket lane
    /// (`relay_ws_enabled` present) — `remoteStated`'s rule: an older
    /// daemon publishes no key and the menu draws no socket rows at all.
    var relayWSStated = false
    var relayWSEnabled = false
    /// The socket relay's address, as `relayURL` is the mailbox's.
    var relayWSURL = ""
    /// The socket lane's standing — the same decoder and the same `stated`
    /// rule as `relayHealth`; absent while the connector does not exist.
    var relayWSHealth = RelayHealth()
    /// Whether this daemon takes sealed home frames — stated, so the menu
    /// can tell a keyless device on a new daemon (re-pair) from every device
    /// on an older one (nothing to say). Absent decodes as false.
    var homeSealed = false
    /// Whether a pairing code is showing and still spendable. Absent (an
    /// older daemon) decodes false so a live square is not closed on a
    /// missing key.
    var pairingOpen = false

    enum CodingKeys: String, CodingKey {
        case available, devices, port
        case homeSealed = "home_sealed"
        case lanEnabled = "lan_enabled"
        case remoteEnabled = "remote_enabled"
        case relayURL = "relay_url"
        case remoteActivity = "remote_activity"
        case relayHealth = "relay_health"
        case relayWSEnabled = "relay_ws_enabled"
        case relayWSURL = "relay_ws_url"
        case relayWSHealth = "relay_ws_health"
        case pairingOpen = "pairing_open"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        lanEnabled = c.value(.lanEnabled, false)
        port = c.value(.port, 0)
        devices = c.value(.devices, [])
        let stated: Bool? = c.maybe(.remoteEnabled)
        remoteStated = stated != nil
        remoteEnabled = stated ?? false
        relayURL = c.value(.relayURL, "")
        remoteActivity = c.value(.remoteActivity, [])
        relayHealth = c.value(.relayHealth, RelayHealth())
        let socketStated: Bool? = c.maybe(.relayWSEnabled)
        relayWSStated = socketStated != nil
        relayWSEnabled = socketStated ?? false
        relayWSURL = c.value(.relayWSURL, "")
        relayWSHealth = c.value(.relayWSHealth, RelayHealth())
        homeSealed = c.value(.homeSealed, false)
        pairingOpen = c.value(.pairingOpen, false)
    }

    init() {}
}

struct PairingResult {
    let ok: Bool
    let detail: String
    var code = ""
    var host = ""
    /// Every address the phone could try, best first. `host` is `hosts`
    /// first element; both ride the QR so a phone build that knows only
    /// `host` keeps pairing.
    var hosts: [String] = []
    var port = 0
    var expiresAt: Double = 0
    /// The pairing's home key, base64, for the QR alone. Empty on an older
    /// daemon, and then absent from the QR.
    var homeKey = ""
    /// Whether the daemon armed this pairing for a typed-address pair —
    /// its own echo of `allow_typed`, the Pair window's tick state. False
    /// on an older daemon, which sends no such key.
    var allowTyped = false
}
