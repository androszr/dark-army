import Foundation

/// Decoding that tolerates a field that is not there.
///
/// Swift's synthesized `Decodable` throws on a missing key even when the
/// property has a default value, and this payload is legitimately ragged:
/// `end_reason` exists only on finished rows, `metrics` is empty until a
/// session's first statusline tick, and the daemon gains fields faster than this
/// panel does. One absent key must not blank the whole panel — which is exactly
/// what it did the first time this ran.
/// Module-scoped rather than file-private: `BoardModels.swift`,
/// `EnrollmentModels.swift`, `TerminalModels.swift` and `ActionModels.swift`
/// decode the same ragged shapes from the same daemon, and a second copy of
/// these helpers is how the files would drift into disagreeing about what a
/// missing key means. This file keeps the snapshot envelope and the fleet —
/// `Snapshot`, `Agents`, `Agent` and what hangs off a row.
extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }

    func maybe<T: Decodable>(_ key: Key) -> T? {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil)
    }
}

/// The daemon's snapshot, decoded exactly as `/api/state` sends it.
///
/// Every field here is already produced by the Python side — nothing was added
/// for this panel. Optionality is meaningful and matches the daemon's own
/// contract: `metrics` is empty until a session's first statusline tick, so an
/// absent cost means *not reported yet*, never zero. Rendering a missing number
/// as 0 is the one wrong answer, and the views below print a dash instead.
struct Snapshot: Decodable {
    var generatedAt: Double = 0
    var counts = Counts()
    var agents = Agents()
    var notifications: [Notification] = []
    var signals: [Signal] = []
    var collaboration: Collaboration?
    var collaborationStamp: Double = 0
    /// Tool calls waiting on a yes or a no, relayed out of sessions started
    /// with Dark Army's channel. Almost always empty — a session has to opt in at
    /// launch — which is exactly why the panel must not reserve room for them.
    var permissions: [PermissionPrompt] = []
    /// Whether `claude agents --json` is answering, and why not when it isn't.
    /// Both have been in every payload and rendered nowhere, which made
    /// "background agents just never show up" undiagnosable from the panel —
    /// indistinguishable from having none. `nil` means the daemon did not say.
    var reconcilerAvailable: Bool?
    var reconcilerError = ""
    /// The Kanban board. Empty on a daemon that could not open `board.db`, which
    /// is why `Board.available` is a field rather than inferred from the card
    /// count — an empty board and a broken one say different things.
    var board = Board()
    /// The sections this payload left out, which `DaemonClient.apply` fills
    /// forward from the last frame that carried them. A slim frame
    /// (`?sections=changed`) omits every section that is unchanged, clocks
    /// aside — on an idle machine, all of them — and *absent must never decode as
    /// empty*: `Board()` has `available = false`, `Agents()` is a blank fleet,
    /// and drawing either over a working panel is exactly the failure the
    /// tolerant helpers exist to prevent. No view learns about this set.
    var carried: Set<Section> = []
    /// When the `agents` section this snapshot carries was composed — the
    /// `generated_at` of the frame that last actually sent one, which is
    /// `generatedAt` itself on any frame that carried it (and so on every
    /// frame from an older daemon, and every frame to a client that never
    /// asked for slim frames). Every "how long" derived from a *relative*
    /// figure on a row — `idleSeconds`, `stats.durationSeconds` — is aged
    /// from this stamp against a live clock, or it would freeze the moment
    /// the fleet stopped being re-sent. Written by `DaemonClient.apply`.
    var agentsStamp: Double = 0
    /// Which projects Dark Army is allowed to watch, and which it has just turned
    /// away. `available` is stated by the daemon, never inferred from an empty
    /// `enrolled` list — an older daemon sends no section at all, and an empty
    /// list decoding as "nothing is enrolled" would draw the enrolment prompt
    /// over a perfectly good fleet.
    var enrollment = Enrollment()
    /// Paired phones, and whether the LAN door is actually listening.
    /// `available` is stated by the daemon, never inferred from an empty
    /// list — an older daemon sends no section at all, and an empty list
    /// decoding as "no devices" would draw a Devices UI over a working panel.
    var devices = Devices()
    /// Inbox dismissals. `available` is stated; an older daemon sends no
    /// section, which leaves this empty and Dismiss absent.
    var inbox = InboxAcks()
    /// Open burst alerts off the phone doors' access log. `available` is
    /// stated; an older daemon sends no section, which leaves this empty
    /// and the Security group's Acknowledge absent.
    var security = SecuritySection()
    /// Mission Control — alive, which session, where. Absent from an older
    /// daemon, which decodes `available == false` and draws the Comm tab's
    /// one sentence.
    var mission = MissionSection()

    /// The top-level sections a slim frame may omit — `_OMITTABLE_SECTIONS`
    /// in `api_server.py`, in the same order, minus `mesh`: the daemon still
    /// publishes that legacy adapter (`_build_mesh`) but no view here has
    /// read it since `collaboration` replaced it, so the panel neither
    /// decodes nor carries it. `generated_at` and the two `reconciler_*`
    /// scalars are deliberately not here: they ride every frame, so they
    /// keep the tolerant helpers and need no carry story.
    enum Section: String, CaseIterable, Hashable {
        case counts, notifications, agents, signals, collaboration, permissions
        case board, enrollment, devices
        case inbox
        case security
        case mission
    }

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case counts, agents, notifications, signals, collaboration, permissions, board
        case enrollment, devices, inbox, security, mission
        case reconcilerAvailable = "reconciler_available"
        case reconcilerError = "reconciler_error"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // These three ride every frame — they are the frame's clock and two
        // scalars, never sections — so they keep the tolerant helpers.
        generatedAt = c.value(.generatedAt, 0)
        reconcilerAvailable = c.maybe(.reconcilerAvailable)
        reconcilerError = c.value(.reconcilerError, "")
        // Every section is absent-able, and none is defaulted: a slim frame
        // legitimately carries no key for a section that has not changed, and
        // only a *present* key may change what that section shows.
        //
        // Absent and present-but-undecodable are different states, and getting
        // them the same way round is how a schema disagreement between builds
        // becomes a silently stale — or silently blanked — panel. Absent is
        // the slim frame's contract and carries forward. A present section
        // that will not decode **throws**, which `parseFrame` catches: the
        // whole frame is REJECTED out loud and nothing is carried from it.
        if c.contains(.counts) {
            counts = try c.decode(Counts.self, forKey: .counts)
        } else { carried.insert(.counts) }
        if c.contains(.agents) {
            agents = try c.decode(Agents.self, forKey: .agents)
        } else { carried.insert(.agents) }
        if c.contains(.notifications) {
            notifications = try c.decode([Notification].self,
                                         forKey: .notifications)
        } else { carried.insert(.notifications) }
        if c.contains(.signals) {
            signals = try c.decode([Signal].self, forKey: .signals)
        } else { carried.insert(.signals) }
        if c.contains(.collaboration) {
            collaboration = try c.decode(Collaboration.self, forKey: .collaboration)
        } else { carried.insert(.collaboration) }
        if c.contains(.permissions) {
            permissions = try c.decode([PermissionPrompt].self,
                                       forKey: .permissions)
        } else { carried.insert(.permissions) }
        if c.contains(.board) {
            board = try c.decode(Board.self, forKey: .board)
        } else { carried.insert(.board) }
        if c.contains(.enrollment) {
            enrollment = try c.decode(Enrollment.self, forKey: .enrollment)
        } else { carried.insert(.enrollment) }
        if c.contains(.devices) {
            devices = try c.decode(Devices.self, forKey: .devices)
        } else { carried.insert(.devices) }
        if c.contains(.inbox) {
            inbox = try c.decode(InboxAcks.self, forKey: .inbox)
        } else { carried.insert(.inbox) }
        if c.contains(.security) {
            security = try c.decode(SecuritySection.self, forKey: .security)
        } else { carried.insert(.security) }
        if c.contains(.mission) {
            mission = try c.decode(MissionSection.self, forKey: .mission)
        } else { carried.insert(.mission) }
    }

    init() {}
}

struct Counts: Decodable {
    var working = 0
    var idle = 0
    var attention = 0
    var subagents = 0

    enum CodingKeys: String, CodingKey { case working, idle, attention, subagents }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        working = c.value(.working, 0)
        idle = c.value(.idle, 0)
        attention = c.value(.attention, 0)
        subagents = c.value(.subagents, 0)
    }

    init() {}
}

struct ProjectFacts: Decodable, Equatable {
    var project = ""
    var sessions = 0
    var lastActive: Double = 0

    enum CodingKeys: String, CodingKey {
        case project, sessions
        case lastActive = "last_active"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        project = c.value(.project, "")
        sessions = c.value(.sessions, 0)
        lastActive = c.value(.lastActive, 0)
    }

    init(project: String = "", sessions: Int = 0, lastActive: Double = 0) {
        self.project = project
        self.sessions = sessions
        self.lastActive = lastActive
    }
}

struct Agents: Decodable {
    var waiting: [Agent] = []
    var running: [Agent] = []
    var sleeping: [Agent] = []
    var finished: [Agent] = []
    var abandoned: [Agent] = []
    var projectFacts: [ProjectFacts] = []

    enum CodingKeys: String, CodingKey {
        case waiting, running, sleeping, finished, abandoned
        case projectFacts = "project_facts"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        waiting = c.value(.waiting, [])
        running = c.value(.running, [])
        sleeping = c.value(.sleeping, [])
        finished = c.value(.finished, [])
        abandoned = c.value(.abandoned, [])
        projectFacts = c.value(.projectFacts, [])
    }

    init() {}

    /// The one four-bucket walk: find a session's row and the bucket it is in.
    /// `Board.swift` and `BoardCardSheet.swift` used to hold two verbatim
    /// copies of this loop; the board's jump chips add call sites, which is
    /// why the walk moves here rather than multiplies. Order is deliberate —
    /// running before waiting before sleeping before finished — and `nil` for
    /// an empty id, so a card with no session never matches anything.
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

    /// `row(session:)` as a dictionary: one walk over the same four buckets
    /// in the same order, the **first** hit for an id kept, an empty id and
    /// `abandoned` never entered — so a lookup here resolves exactly the row
    /// and bucket `row(session:)` would, for the board's many cards at the
    /// cost of one pass instead of a scan per card.
    func index() -> [String: FleetRow] {
        var out: [String: FleetRow] = [:]
        let buckets: [(String, [Agent])] = [
            ("running", running),
            ("waiting", waiting),
            ("sleeping", sleeping),
            ("finished", finished),
        ]
        for (name, rows) in buckets {
            for row in rows where !row.sessionId.isEmpty
                && out[row.sessionId] == nil {
                out[row.sessionId] = FleetRow(agent: row, bucket: name)
            }
        }
        return out
    }
}

/// One session's row and the bucket it is in — `row(session:)`'s tuple as a
/// value a view can compare, so a board card handed one redraws only when
/// what the card draws of that row changed.
///
/// **Equal means the same `Face`, not the same `Agent`.** The daemon
/// recomputes a row's clocks and meters — `idle_seconds`, durations,
/// `metrics`, `trend`, the last text — on every refresh, so whole-row
/// equality made every card-named row news on every agents frame and the
/// board redrew as often as it did before `BoardFeed` existed. The card face
/// draws none of those. The consequence: a `FleetRow` held by `BoardFeed`
/// keeps the `agent` it was first stored with while its face is unchanged,
/// so only the face's fields are current — read nothing else off `agent`
/// for drawing, and add a field to `Face` before a card draws it.
struct FleetRow: Equatable {
    let agent: Agent
    let bucket: String

    /// Everything a board card reads off the row: the live line
    /// (`liveLabel`: nickname, id, bucket, `alive`), the face
    /// (`Cast.character(for:)`: nickname, id), the jump chip and its press
    /// (`isJumpable` = `canJump` ∨ `ownTerminal`; the id), and the stage
    /// track's running helpers (`subagentRows` labels).
    struct Face: Equatable {
        let sessionId: String
        let nickname: String
        let alive: Bool?
        let canJump: Bool
        let ownTerminal: Bool
        let stageLabels: [String]
        let bucket: String
    }

    var face: Face {
        Face(sessionId: agent.sessionId, nickname: agent.nickname,
             alive: agent.alive, canJump: agent.canJump,
             ownTerminal: agent.ownTerminal,
             stageLabels: agent.subagentRows.map(\.label), bucket: bucket)
    }

    static func == (lhs: FleetRow, rhs: FleetRow) -> Bool {
        lhs.face == rhs.face
    }
}

struct Notification: Decodable, Identifiable {
    var id: String { sessionId }
    let sessionId: String
    var message: String = ""
    var project: String = ""
    /// Which hook raised it. Carried so a surface can tell a card that says
    /// something (`StopFailure`, an API error in the harness's own words) from
    /// one that only says a session stopped — see `isGenericWait`.
    var hook: String = ""
    /// The machine token behind a `StopFailure` (`rate_limit`,
    /// `billing_error`, …), never shown raw. Read so the Low priority bar can
    /// say what it is answering; an older daemon sends none and it decodes
    /// as `""`.
    var errorKind: String = ""

    /// A card whose message is the daemon's stock "Waiting for input", set by
    /// `protocol.py` for both hooks that raise one at the end of a turn. It
    /// states nothing the row does not already state, so it is the one card the
    /// agent's own words are allowed to displace.
    var isGenericWait: Bool { hook == "Stop" || hook == "Notification" }

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
}

/// A tool call waiting for a yes or a no, relayed out of a session by Dark Army's
/// channel. Deliberately not a `Notification`: a card is something you dismiss
/// and this is not — dismissing it would leave the session blocked with nothing
/// on screen saying why. It leaves the panel only by being answered, here or at
/// the session's own terminal, whichever happens first.
struct PermissionPrompt: Decodable, Identifiable, Hashable {
    var id: String { requestId }

    var requestId = ""
    var sessionId = ""
    var toolName = ""
    /// The model's summary of its own call, and the arguments it wants to run.
    /// Both are the model's words: rendered as text, never interpreted, and
    /// never trusted to be short.
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

    /// What the prompt is asking, in one line. The description is Claude's own
    /// and is the constant `Run shell command` when the model gave none — worth
    /// nothing on its own, which is why the tool name always leads.
    var summary: String {
        detail.isEmpty ? toolName : "\(toolName): \(detail)"
    }
}

struct Signal: Decodable, Hashable {
    var rule = ""
    var severity = ""
    var text = ""
    var action = ""
    /// Which account the signal is about, so a fleet-level budget line can be
    /// marked with the provider it belongs to. A daemon older than this field
    /// says nothing, so the rule name is the fallback: Grok's global rules are
    /// the ones prefixed `grok-`, which is how they avoid overwriting Claude's,
    /// and everything else is Claude's — a panel running ahead of its daemon
    /// still marks the strip correctly rather than leaving the slot blank.
    var provider = ""

    enum CodingKeys: String, CodingKey {
        case rule, severity, text, action, provider
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rule = c.value(.rule, "")
        severity = c.value(.severity, "")
        text = c.value(.text, "")
        action = c.value(.action, "")
        provider = c.value(.provider, rule.hasPrefix("grok") ? "grok" : "claude")
    }

    /// For signals the panel raises itself about the daemon rather than about a
    /// session — the reconciler being down is a fact only this side can see.
    init(rule: String, severity: String, text: String, action: String = "",
         provider: String = "") {
        self.rule = rule
        self.severity = severity
        self.text = text
        self.action = action
        self.provider = provider
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

struct Agent: Decodable, Identifiable, Equatable {
    var id: String { sessionId }

    let sessionId: String
    var nickname = ""
    var name = ""
    /// The title of the board card this session is working, published by the
    /// daemon *only* where the row would otherwise be unnamed. Empty means
    /// "nothing to say" — which is also what an older daemon decodes to.
    var cardTitle = ""
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
    var branch = ""
    var currentTool = ""
    var agentActivity = ""
    var kind = ""
    var provider = ""
    var idleSeconds: Double = 0
    /// When the session first started — the one number on the row that does not
    /// move. It is what the list is *ordered* by, precisely because everything
    /// else does: sorting by idleness reshuffles the rows several times a minute
    /// under the hand of whoever is reading them. Absent on a row the daemon
    /// could not date, which sorts to the bottom rather than to `now`.
    var startedAt: Double?
    var subagents = 0
    var pid: Int?
    var address = ""
    var addressable = false
    /// Whether Dark Army can put a line in front of this session — Claude via the
    /// channel, Grok via a resident leader client. Published by the daemon
    /// rather than guessed: a push at a session Dark Army cannot reach is dropped
    /// in silence, so the reply box must not be drawn at all rather than
    /// drawn and quietly inert.
    var channel = false
    /// Which route the next reply takes: `"typed"` (onto the session's own
    /// input line in VS Code, with the channel as the fallback), `"channel"`,
    /// or `""` for none. Published by the daemon beside `channel`; the panel
    /// names the route in the reply box and decides nothing from it. Absent
    /// on an older daemon and decodes as `""`.
    var replyVia = ""
    /// Whether Dark Army can *type* at this session — a different reach from
    /// `channel`, and neither implies the other. The channel carries prose to
    /// the model; this is keystrokes into the client's own input line, which is
    /// the only way a slash command runs. It needs a VS Code window with the
    /// 0.1.6+ extension, so it is false for a session in Terminal.app however
    /// reachable it otherwise is. Gates the AskUserQuestion option buttons
    /// and nothing else.
    var interactionNote = ""
    var askNote = ""
    var canType = false
    /// Whether Dark Army can dispose this session's VS Code terminal tab. A different
    /// reach from typing: that needs 0.1.6+ and is false for Codex; closing
    /// needs 0.1.9+ and is true for a Codex row only where stop is. An older
    /// daemon that sends no such key decodes as false — no button, never a
    /// button that types `/clear`.
    var canClose = false
    /// Whether Dark Army may offer Low priority here: a Claude session whose
    /// current card is a rate-limit `StopFailure`, not stopped on a
    /// permission prompt, whose terminal Dark Army can type into, and not switched
    /// within the daemon's cooldown. Decided by the daemon per refresh. An
    /// older daemon decodes false — no button, never one that types into a
    /// terminal.
    var canLowPriority = false
    /// Whether Dark Army may type a person's own message at this session on behalf
    /// of the card it is working — the card screen's Send a message button.
    /// Decided by the daemon per refresh and deliberately not `canType`,
    /// which carries a stopped-category gate and is false for exactly the
    /// running sessions this is for. An older daemon decodes false — no
    /// button, never one that types into a terminal and is then refused.
    var canMessage = false
    /// Whether this session runs on a terminal Dark Army itself hosts (`ptyhost`)
    /// rather than in a VS Code window. The pane draws that terminal under
    /// the row. An older daemon sends no key and decodes false — the
    /// stdout pane alone, exactly as today.
    var ownTerminal = false
    /// The session's terminal tab is gone while the turn may still be
    /// running. The words on the row; Jump and the hosted terminal are
    /// the daemon's `canJump` and `ownTerminal`, forced false with it.
    /// Absent decodes false — the row is not marked gone.
    var tabGone = false
    /// Whether a line may be typed into that terminal — the daemon's
    /// `terminal_input` reach (Dark Army owns the pty, no open permission prompt,
    /// the row is live and not background, the process still reading).
    /// False by default, so the box is absent rather than inert.
    var canTerminalInput = false
    /// Action capabilities are published by the daemon from its current
    /// process-ownership proof. They are UI affordances, not authority: every
    /// write is re-checked by the daemon when it arrives. Missing fields from
    /// an older daemon fail closed through the tolerant decoder below.
    var canStop = false
    var canJump = false
    /// Jump is offered where the editor can raise this terminal, or where
    /// Dark Army hosts it itself. The hosted case opens the agent's full pane
    /// rather than an editor window, so a leftover screen after the
    /// process has exited is still reachable.
    var isJumpable: Bool { canJump || ownTerminal }
    var canHide = false
    var alive: Bool?
    var endReason = ""
    var stats = Stats()
    var metrics = Metrics()
    var trend = Trend()
    var signals: [Signal] = []
    var subagentRows: [SubagentRow] = []
    /// What the agent is asking, when it is stopped on an `AskUserQuestion`.
    /// Read out of the transcript. The channel cannot answer it — a pushed
    /// event queues behind the very dialog it would answer — but the keystroke
    /// route can: `answerQuestion` has the daemon type digit-then-Enter into
    /// the session's own terminal, the same route `/clear` rides. So where
    /// `canType` is true the options are buttons, and everywhere else they are
    /// still worth reading — "waiting for you" and "waiting for you, about
    /// Postgres or SQLite" are different amounts of news, and only one tells
    /// you whether to get up.
    var question = AgentQuestion()
    /// Every question of that same dialog, in order — the tool takes up to
    /// four per call, each with its own header and options, and the flat
    /// `question` above is always its first element. Decodes to `[]` from an
    /// older daemon that publishes only the flat key; read through
    /// `questionList`, which wraps the flat question so the views never
    /// branch on which daemon sent the frame.
    var questions: [AgentQuestion] = []
    /// The agent's closing words on its last turn, for the far commoner wait
    /// that has no tool call behind it at all: a turn that ends "accept?".
    var lastText = ""
    /// The agent's own one-sentence plain-language version of them, parsed by
    /// the daemon from the `bob-tldr` marker — or "" for every session that
    /// never emitted one, which is most. The caption prefers it; the raw tail
    /// stays what the chevron expands to.
    var lastSummary = ""
    /// The last completion report this agent wrote (`## Work done` onwards),
    /// kept by the daemon across the follow-up chatter and cleared by the
    /// person's next prompt. `lastText` is the *latest* message and nothing
    /// else, so a one-line follow-up used to be all the pane had of a finished
    /// piece of work; this is the work itself, drawn beneath those words.
    /// Empty for every provider but Claude and for a session that has finished
    /// nothing yet — absent must decode empty, never blank the pane.
    var lastReport = ""
    /// The finished list's own word for this row — `done`, `cut`, `lost`
    /// or `end` (`session_stats.finish_word`), published by the daemon on
    /// finished rows only and drawn verbatim. "done" was the one word every
    /// finished row wore, and it said "done" of a session cut off mid-build
    /// when its editor window went away. An older daemon sends no key and
    /// this decodes empty, and the table then says "done" as it always did.
    var finishWord = ""
    var reviewReports: [CodexReviewReport] = []
    var reviewReportsOmitted = 0
    /// The answers this agent said it is waiting on, from the `bob-actions`
    /// marker — "Accept", "Iterate", whatever it offered. Drawn as buttons that
    /// send the label back verbatim.
    ///
    /// Empty for nearly every turn, and that emptiness is load-bearing: the
    /// reply bar used to show an **Accept** button on every reachable stopped
    /// row, so a turn that ended in a status report offered the same verdict as
    /// one that ended in a plan. A button may only exist where the agent said
    /// there was a choice; everything else gets the field and Send.
    var replyOptions: [String] = []

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case nickname, name, project, branch, subagents, pid, address
        case addressable, alive, stats, metrics, trend, signals, kind, provider
        case channel
        case replyVia = "reply_via"
        case interactionNote = "interaction_note"
        case askNote = "ask_note"
        case canType = "can_type"
        case canClose = "can_close"
        case canLowPriority = "can_low_priority"
        case canMessage = "can_message"
        case ownTerminal = "own_terminal"
        case tabGone = "tab_gone"
        case canTerminalInput = "can_terminal_input"
        case canStop = "can_stop"
        case canJump = "can_jump"
        case canHide = "can_hide"
        case question
        case questions
        case lastText = "last_text"
        case lastSummary = "last_summary"
        case lastReport = "last_report"
        case finishWord = "finish_word"
        case reviewReports = "review_reports"
        case reviewReportsOmitted = "review_reports_omitted"
        case replyOptions = "reply_options"
        case subagentRows = "subagent_rows"
        case currentTool = "current_tool"
        case cardTitle = "card_title"
        case originBy = "origin_by"
        case originCard = "origin_card"
        case originLine = "origin_line"
        case area
        case areaLine = "area_line"
        case agentActivity = "agent_activity"
        case idleSeconds = "idle_seconds"
        case startedAt = "started_at"
        case endReason = "end_reason"
    }

    /// `Agent.stub(sessionId:)`'s one route: the id and every default.
    init(stubSessionId: String) {
        sessionId = stubSessionId
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.value(.sessionId, "")
        nickname = c.value(.nickname, "")
        name = c.value(.name, "")
        cardTitle = c.value(.cardTitle, "")
        originBy = c.value(.originBy, "")
        originCard = c.value(.originCard, "")
        originLine = c.value(.originLine, "")
        area = c.value(.area, "")
        areaLine = c.value(.areaLine, "")
        project = c.value(.project, "")
        branch = c.value(.branch, "")
        currentTool = c.value(.currentTool, "")
        agentActivity = c.value(.agentActivity, "")
        kind = c.value(.kind, "")
        provider = c.value(.provider, "")
        idleSeconds = c.value(.idleSeconds, 0)
        startedAt = c.maybe(.startedAt)
        subagents = c.value(.subagents, 0)
        pid = c.maybe(.pid)
        address = c.value(.address, "")
        addressable = c.value(.addressable, false)
        channel = c.value(.channel, false)
        replyVia = c.value(.replyVia, "")
        interactionNote = c.value(.interactionNote, "")
        askNote = c.value(.askNote, "")
        canType = c.value(.canType, false)
        canClose = c.value(.canClose, false)
        canLowPriority = c.value(.canLowPriority, false)
        canMessage = c.value(.canMessage, false)
        ownTerminal = c.value(.ownTerminal, false)
        tabGone = c.value(.tabGone, false)
        canTerminalInput = c.value(.canTerminalInput, false)
        canStop = c.value(.canStop, false)
        canJump = c.value(.canJump, false)
        canHide = c.value(.canHide, false)
        alive = c.maybe(.alive)
        endReason = c.value(.endReason, "")
        stats = c.value(.stats, Stats())
        metrics = c.value(.metrics, Metrics())
        trend = c.value(.trend, Trend())
        signals = c.value(.signals, [])
        subagentRows = c.value(.subagentRows, [])
        question = c.value(.question, AgentQuestion())
        questions = c.value(.questions, [])
        lastText = c.value(.lastText, "")
        lastSummary = c.value(.lastSummary, "")
        lastReport = c.value(.lastReport, "")
        finishWord = c.value(.finishWord, "")
        reviewReports = c.value(.reviewReports, [])
        reviewReportsOmitted = c.value(.reviewReportsOmitted, 0)
        replyOptions = c.value(.replyOptions, [])
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

    /// The dialog as a list, whichever daemon sent it: the `questions` list
    /// where the daemon published one, else the non-empty flat `question`
    /// wrapped as a one-element list, else `[]`. Views read this and never
    /// the raw pair, so an older daemon behaves exactly as today.
    var questionList: [AgentQuestion] {
        if !questions.isEmpty { return questions }
        if !question.text.isEmpty { return [question] }
        return []
    }

}

/// A question the agent put to its human, as the transcript recorded it.
///
/// Empty for nearly every row: a session is stopped on one of these rarely, and
/// `text.isEmpty` is the only test any view needs.
struct AgentQuestion: Decodable, Hashable {
    var text = ""
    var options: [String] = []
    /// Each option's `description`, same order as `options`. Empty where the
    /// tool sent none. Claude's `preview` is a different layout and is not
    /// this field.
    var details: [String] = []
    /// The two-word label the tool asks for ("Database", "Approach"). Shown
    /// ahead of the question, where it reads as a subject rather than a repeat.
    var header = ""
    /// The tool_use id, sent back with an answer so a press aimed at a
    /// question the terminal already dealt with misses rather than landing on
    /// its successor. Tolerantly empty, like every field here — a daemon from
    /// before this shipped must not blank the panel.
    var id = ""
    /// The tool's own `multiSelect`: several answers may be right, and the
    /// terminal draws a tick-box widget rather than a pick-one select. False
    /// from an older daemon or hook, which is exactly today's drawing.
    var multiSelect = false

    enum CodingKeys: String, CodingKey {
        case text, options, details, header, id
        case multiSelect = "multi_select"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = c.value(.text, "")
        options = c.value(.options, [])
        details = c.value(.details, [])
        header = c.value(.header, "")
        id = c.value(.id, "")
        multiSelect = c.value(.multiSelect, false)
    }

    func detail(at index: Int) -> String {
        details.indices.contains(index) ? details[index] : ""
    }
}

struct Stats: Decodable, Equatable {
    var model: String?
    var effort = ""
    var fast = false
    var outputTokens = 0
    var totalToolCalls = 0
    var filesTouched = 0
    var durationSeconds: Double = 0

    enum CodingKeys: String, CodingKey {
        case model, effort, fast
        case outputTokens = "output_tokens"
        case totalToolCalls = "total_tool_calls"
        case filesTouched = "files_touched"
        case durationSeconds = "duration_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        model = c.maybe(.model)
        effort = c.value(.effort, "")
        fast = c.value(.fast, false)
        outputTokens = c.value(.outputTokens, 0)
        totalToolCalls = c.value(.totalToolCalls, 0)
        filesTouched = c.value(.filesTouched, 0)
        durationSeconds = c.value(.durationSeconds, 0)
    }

    init() {}
}

/// What a session's numbers are *doing*, from the daemon's few-minute ring
/// buffer (`samples.py`). Every field is optional and usually absent: the ring
/// refuses to fit a slope to a series that is too short, too sparse or has
/// stopped, and an absent rate must never be drawn as zero.
struct Trend: Decodable, Equatable {
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

    var hasRates: Bool {
        ctxPctPerMin != nil || costUsdPerHour != nil || ctxRunwaySeconds != nil
    }
}

struct Metrics: Decodable, Equatable {
    var modelId: String?
    var costUsd: Double?
    var ctxUsedPct: Double?
    var exceeds200k = false
    var fiveHourPct: Double?
    /// What window `five_hour_pct` is a percentage *of*. Claude's is the 5h
    /// window and says nothing; Grok reports its own (`weekly`, …), which is why
    /// the two readings must never be maxed together into one figure.
    var budgetCycle: String?
    var linesAdded: Int?
    var linesRemoved: Int?

    enum CodingKeys: String, CodingKey {
        case modelId = "model_id"
        case costUsd = "cost_usd"
        case ctxUsedPct = "ctx_used_pct"
        case exceeds200k = "exceeds_200k"
        case fiveHourPct = "five_hour_pct"
        case budgetCycle = "budget_cycle"
        case linesAdded = "lines_added"
        case linesRemoved = "lines_removed"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        modelId = c.maybe(.modelId)
        costUsd = c.maybe(.costUsd)
        ctxUsedPct = c.maybe(.ctxUsedPct)
        exceeds200k = c.value(.exceeds200k, false)
        fiveHourPct = c.maybe(.fiveHourPct)
        budgetCycle = c.maybe(.budgetCycle)
        linesAdded = c.maybe(.linesAdded)
        linesRemoved = c.maybe(.linesRemoved)
    }

    init() {}
}


/// New delivery fields are additive only when absent. Validate them at each
/// response root before legacy tolerant containers can swallow a row error.
enum AreaWireFields {
    private enum Key: String, CodingKey {
        case area, area_line, areas_supported, suggested_area
        case board, agents, cards, current
        case running, sleeping, waiting, abandoned, finished
    }
    private typealias Container = KeyedDecodingContainer<Key>

    private static func strings(_ c: Container) throws {
        for key in [Key.area, .area_line, .suggested_area] where c.contains(key) {
            _ = try c.decode(String.self, forKey: key)
        }
    }
    private static func rows(_ c: Container, key: Key) throws {
        // Keep the established fallback for an old malformed container. Once
        // it is an array of objects, new fields in every row must be sound.
        guard var rows = try? c.nestedUnkeyedContainer(forKey: key) else { return }
        while !rows.isAtEnd {
            let row = try rows.superDecoder()
            if let fields = try? row.container(keyedBy: Key.self) {
                try strings(fields)
            }
        }
    }
    private static func board(_ c: Container) throws {
        if c.contains(.areas_supported) {
            _ = try c.decode(Bool.self, forKey: .areas_supported)
        }
        try rows(c, key: .cards)
    }
    private static func agents(_ c: Container) throws {
        for key in [Key.running, .sleeping, .waiting, .abandoned, .finished] {
            try rows(c, key: key)
        }
    }
    static func validate(_ decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: Key.self) else { return }
        try strings(c)
        try board(c)
        try agents(c)
        if let nested = try? c.nestedContainer(keyedBy: Key.self, forKey: .board) {
            try board(nested)
        }
        if let nested = try? c.nestedContainer(keyedBy: Key.self, forKey: .agents) {
            try agents(nested)
        }
        if let current = try? c.nestedContainer(keyedBy: Key.self, forKey: .current) {
            try strings(current)
        }
    }
    private struct Envelope: Decodable {
        init(from decoder: Decoder) throws { try AreaWireFields.validate(decoder) }
    }
    static func accepts(_ data: Data) -> Bool {
        (try? JSONDecoder().decode(Envelope.self, from: data)) != nil
    }
}
