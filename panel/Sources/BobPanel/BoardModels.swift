import Foundation

// The board as the panel decodes it: cards, the board payload, the card
// thread, the work record. Split out of `Models.swift` on 20 Sep 2026.

/// One unit of work somebody wrote down. Decoded **only** through the tolerant
/// helpers above, and that is not habit: this payload is ragged by construction
/// — `session_id`, `dispatch_error`, `done_at` and `session_ended_at` exist on
/// some cards and not others — and Swift's synthesized `Decodable` throws on a
/// missing key even where the property has a default. One absent field must
/// never blank the board.
/// What `GET /api/board` answers with — the Done archive, or a single card
/// fetched in full. Tolerant like everything else here: a daemon that predates
/// the single-card query answers with an archive, and an empty `cards` is a
/// miss rather than an error.
struct BoardReport: Decodable {
    var available = false
    var cards: [BoardCard] = []
    /// The two Done tokens, on the `?column=done` shape alone — empty on the
    /// archive and single-card shapes, and empty from an older daemon. They
    /// are the stamps a held copy carries, and they are read *before* the
    /// cards on the daemon's side for a stated reason (`done_archive_cards`).
    var doneClearToken = ""
    var doneViewToken = ""
    /// The card's timeline, on the single-card on-open read alone — a
    /// top-level sibling of `plan`, never on the card. Nil from an older
    /// daemon, and nil on every other shape.
    var timeline: CardTimelineReport?

    enum CodingKeys: String, CodingKey {
        case available, cards, timeline
        case doneClearToken = "done_clear_token"
        case doneViewToken = "done_view_token"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        cards = c.value(.cards, [])
        doneClearToken = c.value(.doneClearToken, "")
        doneViewToken = c.value(.doneViewToken, "")
        timeline = c.maybe(.timeline)
    }

    init() {}
}

/// One message on a card's question-and-answer thread. Rides the per-card
/// `GET /api/board?card=` fetch, never the SSE frame — only `threadCount`
/// does that.
struct CardMessage: Decodable, Equatable, Identifiable {
    var id = ""
    var cardId = ""
    var author = ""
    var authorName = ""
    var kind = "question"
    var via = ""
    var text = ""
    var createdAt: Double = 0

    enum CodingKeys: String, CodingKey {
        case id, author, kind, via, text
        case cardId = "card_id"
        case authorName = "author_name"
        case createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        cardId = c.value(.cardId, "")
        author = c.value(.author, "")
        authorName = c.value(.authorName, "")
        kind = c.value(.kind, "question")
        via = c.value(.via, "")
        text = c.value(.text, "")
        createdAt = c.value(.createdAt, 0)
    }

    init() {}
}

/// What Dark Army observed a card's last finished run do — the headline the board
/// frame carries, eight scalars and nothing else.
///
/// **Absence is the load-bearing part.** A card nobody has run and a run that
/// changed nothing are different facts; if this decoded to an empty record the
/// two would look identical on every surface, which is the one thing a person
/// judging an outcome must be able to tell apart. So `BoardCard.workRecord` is
/// an optional decoded with `maybe`, never a defaulted value.
struct WorkRecordHead: Decodable, Equatable {
    var verdict = ""
    /// When Dark Army wrote it down.
    var at: Double = 0
    /// How many files the record *lists*, and how many actually changed. The
    /// two differ where the list hit its bound, and the count stays honest.
    var files = 0
    var filesTotal = 0
    var added = 0
    var removed = 0
    /// Whether the run left any closing words at all.
    var report = false
    /// Whether Dark Army could read the folder. **False is not zero** — the card
    /// draws the daemon's own reason instead of a count.
    var filesAvailable = false
    /// What the shunt skill's helper did during the run: how many
    /// delegations, how many lines never entered the main model, and what
    /// the helper cost where the assistant reported it — `nil` otherwise,
    /// never a zero. A missing key is an older daemon and draws nothing.
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

/// One changed file. `added` / `removed` are **optionals**, not zeros: git
/// says `-` for a binary file and a zero there would read as a file that
/// changed by nothing.
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

/// `GET /api/work-record?card=` — `available` is **stated**, so "the board is
/// not open" and "this card has no record" never look like each other.
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

/// One file's changes, fetched on demand. `truncated` is stated by the daemon,
/// never inferred from a length.
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

/// Formatting for a record, in one place so the sheet composes nothing.
enum WorkRecordFormat {
    /// `+3 −1`, or the word for a file with no line counts. A binary file and
    /// a new file are **named**, never drawn as a pair of zeros.
    static func counts(_ file: WorkRecordFile) -> String {
        if file.binary { return "binary" }
        guard let added = file.added, let removed = file.removed else {
            return file.isNew ? "new" : "—"
        }
        let head = file.isNew ? "new · " : ""
        return "\(head)+\(added) −\(removed)"
    }

    /// The one line under the verdict: how many files, and how many lines.
    /// Says "more than listed" rather than quietly drawing the bound as if it
    /// were the truth.
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
}

struct BoardCard: Decodable, Identifiable, Equatable {
    var outcomeRevision = 0
    var outcomeStatus = "unaccepted"
    var id = ""
    var project = ""
    /// The absolute folder a Start would launch into. Shown so a card cannot
    /// silently point somewhere other than where its project heading says.
    var root = ""
    var title = ""
    /// One or two plain sentences saying what this is for, written for somebody
    /// who will never open the prompt. **The card leads with this**, not with
    /// the instructions: a board is read by whoever walks past it, and the first
    /// three lines of an agent prompt ("Implement the accepted plan at plans/…")
    /// say nothing to that reader. Empty on nearly every card, which is why the
    /// prompt preview is still drawn underneath rather than replaced by it.
    var summary = ""
    var prompt = ""
    /// `""` (nobody yet), `claude`, `codex`, `grok`.
    var tool = ""
    /// Which model that assistant runs on. `""` is **Default** — the card
    /// names none and Start launches with no model flag at all — and it is
    /// also what an older daemon's cards decode to, which is why every model
    /// chooser is gated on a non-empty catalogue rather than on this.
    var model = ""
    var column = "backlog"
    var sessionId = ""
    /// `""`, `dispatching`, `live`, `ended`. Never a session *category* — that
    /// is read live off `snapshot.agents` by looking `sessionId` up, so the board
    /// can never disagree with the rest of the panel about what an agent is doing.
    var linkState = ""
    var dispatchError = ""
    var author = "user"
    var createdAt: Double = 0
    var sessionEndedAt: Double?
    var doneAt: Double?

    /// The specialists this card *says* it expects, in order — declared by
    /// whoever wrote the card, never inferred from what has run. This is the
    /// only thing that may draw a still-to-come marker, and that restriction is
    /// the whole design: a confident marker for a stage that never happens is
    /// worse than no marker at all.
    var workflow: [String] = []
    /// The specialists Dark Army actually saw run, in the order they ran. Dark Army's own
    /// record — no surface can write it.
    var agentTrail: [String] = []
    /// Which cast member took each observed stage, allocated by the daemon
    /// when the stage was first seen and remembered on the card forever.
    /// Empty for a card recorded before the crew existed and for every card an
    /// older daemon serves — `CrewBand` falls back to the roles' anchor faces,
    /// which is the same thing the board drew before this arrived.
    var crew: [String: String] = [:]
    /// The lead Start will give this card (`lead_face`), so the face shown
    /// before Start is the face that starts. Empty from an older daemon.
    var leadFace: String = ""
    /// Whether `prompt` here is a preview rather than the whole thing. Stated by
    /// the daemon rather than guessed from a length, because the editor blocks
    /// Save on it: saving a truncated body would overwrite somebody's
    /// instructions with the first 400 characters of them.
    var promptTruncated = false
    /// Same contract as `promptTruncated` for the plain-words description.
    /// Absent (an older daemon) must decode as not-truncated, or Save would
    /// hold on every card.
    var summaryTruncated = false
    /// Relative paths of files attached when the card was written. Empty on
    /// a daemon that predates the field — absent must never blank the panel.
    var attachments: [String] = []

    /// Which session declared this card finished, and the one sentence it said
    /// it with. Empty on a card a human dragged into Done, which is the whole
    /// point of the pair: a reader can tell a checked close from a drag.
    ///
    /// Dark Army's record of somebody's *statement*, not a fact Dark Army established —
    /// nothing inspects the work — so the panel draws it as "closed by …" and
    /// never as a tick. Read-only here: no surface can write either field, and
    /// both are cleared by the store when a card leaves Done, so Reopen undoes
    /// the whole close rather than just the column.
    var closedBy = ""
    var closeNote = ""
    /// Nickname Dark Army already assigned the filing session. Empty on a human
    /// card (`author == "user"`) and on a daemon that has not been restarted
    /// after this field shipped — the board must still decode.
    var authorName = ""
    var closedByName = ""
    /// The plan attached by a refinement (or by a hand-run /ship). The Prep
    /// column's exit condition: written only by the store's `attach_plan`, so
    /// a non-empty value really means a plan was attached. Read-only here —
    /// the API's field allow-list excludes it.
    var planPath = ""
    /// The plan *version* a person read and said yes to — the SHA-256 of the
    /// plan file's bytes at that moment, `""` for never approved. Written by
    /// the store's `approve_plan` alone, so a card can never read as approved
    /// against a plan nobody read; empty **never** holds a Start up.
    var planApproved = ""
    /// When they said it. `0` where nobody has.
    var planApprovedAt: Double = 0
    /// The standing instruction "start this by itself the moment its plan
    /// lands". `'1'` on the wire is on; `''` and an **absent** key are both
    /// off, and off is the required direction — a daemon older than the
    /// column must never make every card look armed. Spent by the daemon on
    /// the attempt, so it clears itself once Dark Army has acted on it.
    var startWhenPlanned = false
    /// How important this piece of work is, `"0"`..`"100"`, or `""` for
    /// **nobody has scored it**. A string end to end, because the store's
    /// column is TEXT and `""` and `"0"` are different states: `""` draws
    /// nothing on the face, `"0"` draws `P 0`. An absent key — a daemon
    /// older than the column — decodes `""` and is indistinguishable from
    /// unscored, which is the required direction.
    var priority = ""
    var area = ""
    /// The card's kind. `""` is a build card (never published as the word
    /// `"ship"`); `"scout"` is an investigation ending in a report. An
    /// absent key — a daemon older than the column — decodes `""` and is
    /// a build card, which is the required direction.
    var kind = ""
    /// The scout's attached report. Written by `attach_report` alone; an
    /// absent key — a daemon older than the column — decodes `""`.
    var reportPath = ""
    /// The attached report's one-line verdict and recommendation token, as
    /// the daemon read the answer block at the attach (`attach_report`
    /// alone writes both). An absent key — a daemon older than the column
    /// — decodes `""`, and an empty verdict draws nothing.
    var reportVerdict = ""
    var reportRecommendation = ""
    var isScout: Bool { kind == "scout" }
    /// The number `cardOrder` sorts on. `""` and `"0"` fold together here
    /// exactly as `CAST(priority AS INTEGER)` folds them in
    /// `board.CARD_ORDER_SQL` — one rule, stated once in SQL and once here.
    var priorityValue: Int { Int(priority) ?? 0 }
    /// The card's change number — the store's own counter, stepped up
    /// whenever a write changes something a person reads. Sent straight back
    /// as `expected_revision` on a Save, so a card somebody changed
    /// underneath is refused in the daemon's words rather than overwritten.
    /// A daemon older than the column sends no key, which reads as `0` and
    /// simply guards nothing.
    var revision = 0
    /// The *refinement's* link: `""`, `dispatching`, `live`, `ended`. A
    /// separate pair from `sessionId`/`linkState` because a refining card
    /// stays in Prep — `bind_session` moves cards, and the refinement must
    /// never ride it. Dark Army's own bookkeeping, not writable from here.
    var refineState = ""
    var refineSessionId = ""
    /// In-column order. Default 0 on a daemon that predates the field, which
    /// keeps the old created-at sort as a tie-break rather than blanking.
    var position: Double = 0
    /// `""` or `"queued"` — this card is holding a place in its project's
    /// work queue because somebody pressed Start (or dropped it into In
    /// progress) while colliding work was already running there. Dark Army's own
    /// bookkeeping: the API's field allow-list excludes it, so a surface can
    /// only ever *clear* a slot (`board_unqueue`), never claim one.
    var queueState = ""
    /// When the person made that gesture. The record of the press, not the
    /// drain's order — a drag writes `queueRank` beside this and leaves
    /// the stamp alone.
    var queuedAt: Double?
    /// The person's preferred place in line, on the same axis as `queuedAt`
    /// (epoch seconds). Nil means never dragged, so the stamp is the order.
    /// Decoded with `maybe`: JSON `null` and a missing key both become nil.
    var queueRank: Double?
    /// The queue gate's own per-card answer — whether this card is holding
    /// its project's files — kept on the wire for older panels and as the
    /// fallback for `runActive`. Not the band's split any more: the gate
    /// releases on a manual-check flag (so queued work flows past it) while
    /// the flagged card's assistant may still be working.
    ///
    /// Defaults **true**, which is the direction that degrades safely: a
    /// daemon from before this field shipped says nothing, and the band goes
    /// on drawing what it always drew.
    var workActive = true
    /// Whether an assistant is *working* this card, right now — the daemon's
    /// `board_queue.run_active`, the band's RUN/DONE split. It differs from
    /// `workActive` on exactly one term: a card flagged for a hand-check
    /// stops holding its files but its session may still be busy, and this
    /// field is what keeps it in RUN while it is.
    ///
    /// Decoded with a fallback to `workActive`, never to a literal: an older
    /// daemon sends only `work_active`, and falling back to it reproduces
    /// that daemon's behaviour exactly — a constant `true` would over-fill
    /// RUN with genuinely finished cards, a constant `false` would empty it.
    var runActive = true
    /// What this card is waiting on, in the daemon's own words — the whole
    /// sentence, not a fragment to interpolate. Published per queued card and
    /// shown verbatim, so the refusal the press answered with and the badge
    /// the card wears a moment later are the same words.
    ///
    /// Empty is an **older daemon**, not a card with no reason: the fallback
    /// is a plain queued line. See `queuedLine(autostart:)`.
    var queueReason = ""
    /// What somebody still has to check by hand, in the words the session that
    /// did the work wrote. Non-empty **is** the flag — there is no separate
    /// boolean, because a flag and a note are two things that can disagree and
    /// a badge with no steps behind it is the state the column exists to be
    /// incapable of.
    ///
    /// Read-only here in one direction only: the API's field allow-list
    /// excludes it, so a surface can say the check has been done
    /// (`boardManualClear`) and can never claim one is outstanding.
    var manualSteps = ""
    /// The check file the flag named — the realpath of a
    /// `manual-check/<date>-<slug>/check.md` under the card's root — or `""`
    /// for a card flagged with steps alone. With a file, the card window draws
    /// the file and Passed / Failed instead of Mark checked. Read-only here:
    /// written by the daemon's `flag_manual` alone.
    var manualCheckPath = ""
    /// When a person pressed Reviewed on an assistant's close. `nil` means
    /// nobody has acknowledged it yet, which is what pins the card to the top
    /// of Done wearing its "FINISHED · REVIEW" banner. Read-only here: the
    /// API's field allow-list excludes it — a card-sheet Save must never
    /// silently acknowledge a review — and the gesture is the named verb
    /// `boardReview`.
    var reviewedAt: Double?
    /// Whether this finished card reached the frame through the daemon's
    /// *recent preview* read alone — that is, whether it is one of the ones
    /// a client asking for `?done=review` stops receiving. A record of which
    /// store read produced the row, never a second judgment about who is
    /// waiting: `awaitsReview` is still the panel's own reading, and the
    /// daemon's `done_awaiting_review` SQL is still the only place it is
    /// decided. `false` from an older daemon, which withholds nothing.
    var donePreview = false
    /// How many messages the card's thread holds. Count only — the text
    /// rides the per-card fetch as `messages`. Default 0 on a daemon that
    /// predates the field, so one absent key cannot blank the board.
    var threadCount = 0
    /// The thread itself, populated only on the per-card fetch. Empty on
    /// snapshot cards; the sheet fetches rather than reading this off the
    /// live board.
    var messages: [CardMessage] = []

    /// Whether the amber `MANUAL CHECK NEEDED` line is drawn — the note is
    /// stored *and* it is a person's turn. Composed by the daemon per card
    /// beside `work_active` / `run_active`, never re-derived here: the rail's
    /// own rows call a roster-only stub `running`, so a join against them
    /// would hide the badge on exactly the finished-and-quiet card that most
    /// needs it. Only the daemon holds the hook-stream freshness reading.
    var manualCheckDue = false
    /// The daemon decides which cards have lost their session.
    var needsYou = false
    /// Mission Control asked for this card to be started (the daemon's
    /// `start_ask_id`, fresh and still startable). The ask starts nothing:
    /// it puts the card on Needs you, and the person's press on START is the
    /// start. `""` for no ask; the id is the Dismiss fingerprint's material.
    var startAskId = ""
    var startAskedAt: Double = 0
    /// How many agents may work at once in **this card's project** — the
    /// daemon's own resolution of the machine-wide dial against the project's
    /// own override, published per card beside `workActive` / `runActive` for
    /// their reason: the rule lives in one place and the panel joins nothing.
    ///
    /// **0 means an older daemon** sent no key, and is never drawn: the band
    /// falls back to `Board.parallelLimit`, the machine default, which is
    /// exactly what that daemon's own panel drew.
    var parallelLimit = 0
    /// What Dark Army observed this card's last finished run do — the headline
    /// alone. **nil is no record**, decoded with `maybe`: an empty value here
    /// would make "nobody has run this" and "this run changed nothing" the
    /// same thing on screen.
    var workRecord: WorkRecordHead?
    /// The whole record, present only on the per-card fetch (`boardCard`).
    /// The closing words and the file list never ride an SSE frame.
    var workRecordFull: WorkRecord?
    /// What this card has cost so far and how long its assistant has
    /// actually worked, composed by the daemon off its two ledgers. **nil
    /// is no run** — `workRecord`'s rule: a card nobody has ever run sends
    /// no key and draws no line, not a zero and not a dash.
    var runFigures: RunFigures.Figures?
    /// How the run is going — the daemon's own reading, drawn verbatim by
    /// `RunHealthLine`. **nil is no run**, `workRecord`'s rule: a card
    /// nobody has started sends no key and draws no line.
    var runHealth: RunHealth?
    /// Where this card stands in a batch-implement session — the card the
    /// session is on, or one waiting its turn. **nil is no batch**,
    /// `workRecord`'s rule: the daemon sends the key only for those two, and
    /// an absent or malformed one decodes to nil rather than blanking the
    /// board.
    var batch: BatchMark?
    /// The one line the tile draws for `batch`: `BATCH 2/3 · working`, or
    /// `""` where there is no batch. Composed from the daemon's three
    /// fields; nothing is counted here.
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
    /// The cards this one waits on, as the store holds them: card ids joined
    /// by newlines. A thing a person states — written back whole through
    /// `board_update` with `expected_revision` — and gated at Start by the
    /// daemon, which queues the card until each one is done or finished and
    /// waiting only on a manual check. `""` from an older daemon.
    var blockedBy = ""
    /// Those cards as the daemon resolved them this frame — title, column
    /// and whether each counts as finished — in the stored order. A card
    /// that no longer exists is left out. `[]` where there are none.
    var dependencies: [CardDependency] = []
    /// The cards waiting on this one, off the same frame. `[]` for none.
    var dependents: [CardLink] = []
    /// `Waits on: "X" (done) · "Y" (not yet)` and `Unblocks: "A"` — the
    /// daemon's sentences, drawn verbatim and only where non-empty.
    var dependencyLine = ""
    var dependentsLine = ""

    /// `blockedBy` as a list of ids, the store's own split.
    var dependencyIds: [String] { BoardCard.stages(blockedBy) }

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

    /// Somebody still has to check this by hand.
    var needsManualCheck: Bool { !manualSteps.isEmpty }

    /// An assistant declared this card finished and no human has looked yet.
    /// On a daemon from before `reviewed_at` shipped, every agent-closed card
    /// reads as pending — the banner draws with a Reviewed press that fails
    /// into the card's refusal line. Accepted degradation, stated in the plan
    /// rather than papered over with a capability flag.
    var awaitsReview: Bool { isAgentClosed && reviewedAt == nil }

    /// Waiting its turn in this project's pipeline.
    var isQueued: Bool { queueState == "queued" }

    /// Where "Send back" returns a card whose run ended: Backlog with a plan,
    /// Prep without — the daemon's own bind-window expiry rule, pinned
    /// against `daemon_board.py` by `host/tests/test_needs_you_cards.py`.
    var sendBackColumn: BoardColumn { planPath.isEmpty ? .prep : .backlog }

    /// Whether Done arms before confirming: wherever the press could reach a
    /// live session.
    var doneArms: Bool { linkState == "live" }

    /// What a queued card says about its wait.
    ///
    /// **The daemon's sentence, verbatim, whenever it sent one.** Under the
    /// slot rule the wait has no single holder to name — the project is full,
    /// not one card in the way — and the count that explains it lives on the
    /// daemon, so composing the sentence here would be the panel guessing at
    /// arithmetic it cannot see. Publishing it also means the refusal a press
    /// answers with and this badge are the same words.
    ///
    /// The two plain strings below are the **older-daemon fallback** and
    /// nothing else: a daemon from before `queue_reason` shipped sends no
    /// sentence, and a bare promise is the only honest thing to draw when the
    /// arithmetic behind the wait is not on this side. It is a function
    /// rather than a view's private property so the preference order is a
    /// test.
    func queuedLine(autostart: Bool) -> String {
        let published = queueReason.trimmingCharacters(
            in: .whitespacesAndNewlines)
        if !published.isEmpty { return published }
        return autostart ? "Queued — Dark Army will start it"
                         : "Queued — press Start"
    }

    /// When this card's run stopped mattering, best available. Three stamps
    /// rather than one because none of them is always there: a card closed by
    /// its agent has `doneAt`, one whose session died has `sessionEndedAt`,
    /// and one that simply went quiet has neither — it falls back to when the
    /// card was written, which at least keeps the order stable instead of
    /// reshuffling the group on every frame.
    ///
    /// This is the **ordering** key and only that. Its `createdAt` fallback is
    /// a sort key, not a finish time — a card written two days ago and
    /// finished a minute ago would read as two days old. The **age** key is
    /// `knownFinishedAt`, which states nothing it was not told; do not merge
    /// the two back together.
    var finishedClock: Double { doneAt ?? sessionEndedAt ?? createdAt }

    /// When this run actually finished, in the daemon's own words — `nil`
    /// where it said nothing. The age key beside `finishedClock`'s ordering
    /// key: no `createdAt` fallback, because a shelf life measured off when
    /// the card was *written* would hide work that finished a minute ago.
    var knownFinishedAt: Double? { doneAt ?? sessionEndedAt }

    static func stages(_ raw: String) -> [String] {
        raw.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    /// Written by an assistant through Dark Army's channel rather than by hand.
    var isAgentAuthored: Bool { !author.isEmpty && author != "user" }
    var isDispatching: Bool { linkState == "dispatching" }
    /// Panel approximation of whether Delete will close a terminal.
    /// Done is not excluded (2026-08-24): deleting a finished card is the
    /// same cancel as any other column. Dispatching over-warns because the
    /// spawn receipt is not on the snapshot — a label promising more caution
    /// than needed, never a close the label hid. The daemon's
    /// `_session_provider` check is authoritative: a rare non-codex tool
    /// bound to a codex-provider session over-warns here and the daemon
    /// silently skips, same direction.
    var deleteClosesTerminal: Bool {
        (linkState == "live" && !sessionId.isEmpty && tool != "codex")
        || (linkState == "dispatching" && tool != "codex")
        || (refineState == "live" && !refineSessionId.isEmpty)
    }
    /// Dark Army has a session on this card, or is about to. The set the pipeline
    /// band starts from before `runActive` splits it into working and over.
    var isInFlight: Bool { linkState == "live" || linkState == "dispatching" }
    var sessionEnded: Bool { linkState == "ended" }
    /// A refinement is underway (binding or bound). What draws "refining…" and
    /// withholds Refine.
    var isRefining: Bool { refineState == "dispatching" || refineState == "live" }
    /// Closed by the assistant that was doing it, rather than dragged across by
    /// hand.
    var isAgentClosed: Bool { !closedBy.isEmpty }

    enum CodingKeys: String, CodingKey {
        case outcomeRevision = "outcome_revision"
        case outcomeStatus = "outcome_status"
        case id, project, root, title, summary, prompt, tool, author
        case column = "column_name"
        case sessionId = "session_id"
        case linkState = "link_state"
        case dispatchError = "dispatch_error"
        case createdAt = "created_at"
        case sessionEndedAt = "session_ended_at"
        case doneAt = "done_at"
        case model
        case workflow
        case agentTrail = "agent_trail"
        case crew
        case leadFace = "lead_face"
        case promptTruncated = "prompt_truncated"
        case summaryTruncated = "summary_truncated"
        case closedBy = "closed_by"
        case closeNote = "close_note"
        case authorName = "author_name"
        case closedByName = "closed_by_name"
        case position
        case planPath = "plan_path"
        case planApproved = "plan_approved"
        case planApprovedAt = "plan_approved_at"
        case startWhenPlanned = "start_when_planned"
        case priority, area, kind
        case reportPath = "report_path"
        case reportVerdict = "report_verdict"
        case reportRecommendation = "report_recommendation"
        case revision
        case refineState = "refine_state"
        case refineSessionId = "refine_session_id"
        case queueState = "queue_state"
        case queuedAt = "queued_at"
        case queueRank = "queue_rank"
        case queueReason = "queue_reason"
        case workActive = "work_active"
        case runActive = "run_active"
        case manualSteps = "manual_steps"
        case manualCheckPath = "manual_check_path"
        case manualCheckDue = "manual_check_due"
        case needsYou = "needs_you"
        case startAskId = "start_ask_id"
        case startAskedAt = "start_asked_at"
        case parallelLimit = "parallel_limit"
        case reviewedAt = "reviewed_at"
        case donePreview = "done_preview"
        case attachments
        case threadCount = "thread_count"
        case messages
        case workRecord = "work_record"
        case workRecordFull = "work_record_full"
        case runFigures = "run_figures"
        case runHealth = "run_health"
        case batch
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
        project = c.value(.project, "")
        root = c.value(.root, "")
        title = c.value(.title, "")
        summary = c.value(.summary, "")
        prompt = c.value(.prompt, "")
        tool = c.value(.tool, "")
        model = c.value(.model, "")
        column = c.value(.column, "backlog")
        sessionId = c.value(.sessionId, "")
        linkState = c.value(.linkState, "")
        dispatchError = c.value(.dispatchError, "")
        author = c.value(.author, "user")
        createdAt = c.value(.createdAt, 0)
        // Newline-separated in the store, so a plain string is what arrives.
        // Split here rather than at each view, or three call sites end up with
        // three ideas of what an empty list looks like.
        workflow = BoardCard.stages(c.value(.workflow, ""))
        agentTrail = BoardCard.stages(c.value(.agentTrail, ""))
        // Absent on every card with no crew, and on every frame an older
        // daemon sends — the tolerant helper, never `decode`.
        crew = c.value(.crew, [:])
        leadFace = c.value(.leadFace, "")
        promptTruncated = c.value(.promptTruncated, false)
        summaryTruncated = c.value(.summaryTruncated, false)
        // Through the tolerant helper, never `decode`: a daemon that has not
        // been restarted after a panel upgrade sends cards without these keys,
        // and Swift's synthesized `Decodable` throws on a missing key even
        // where the property has a default — one absent field would blank the
        // whole board.
        closedBy = c.value(.closedBy, "")
        closeNote = c.value(.closeNote, "")
        authorName = c.value(.authorName, "")
        closedByName = c.value(.closedByName, "")
        position = c.value(.position, 0)
        // Three new fields on a daemon not yet restarted: the tolerant helper
        // with `""` defaults, never `decode` — Models.swift's documented trap.
        planPath = c.value(.planPath, "")
        planApproved = c.value(.planApproved, "")
        planApprovedAt = c.value(.planApprovedAt, 0)
        startWhenPlanned = c.value(.startWhenPlanned, "") == "1"
        priority = c.value(.priority, "")
        area = c.value(.area, "")
        kind = c.value(.kind, "")
        reportPath = c.value(.reportPath, "")
        // An absent key — a daemon older than the column — decodes "".
        reportVerdict = c.value(.reportVerdict, "")
        reportRecommendation = c.value(.reportRecommendation, "")
        revision = c.value(.revision, 0)
        refineState = c.value(.refineState, "")
        refineSessionId = c.value(.refineSessionId, "")
        // The queue pair, same trap, same answer. `queuedAt` is `maybe`
        // rather than a defaulted number because there is no zero time that
        // means "not queued" — nil is the state, and a 0 would sort the card
        // to the head of a queue it is not in.
        queueState = c.value(.queueState, "")
        queuedAt = c.maybe(.queuedAt)
        queueRank = c.maybe(.queueRank)
        // Same trap, same answer: `""` means the daemon predates the field,
        // and `queuedLine(autostart:)`'s plain fallback stands in for it.
        queueReason = c.value(.queueReason, "")
        workActive = c.value(.workActive, true)
        // `maybe` with a fallback to the field just decoded, never
        // `value(.runActive, true)`: on a daemon that predates the split the
        // key is absent, and the old single answer is the only honest one —
        // a literal default would over- or under-fill RUN on every frame.
        runActive = c.maybe(.runActive) ?? workActive
        // Same trap, same answer: a daemon not yet restarted after this
        // shipped sends cards with no `manual_steps` key at all, and Swift's
        // synthesized `Decodable` would throw on the absence even though the
        // property has a default — one missing field must never blank the board.
        manualSteps = c.value(.manualSteps, "")
        manualCheckPath = c.value(.manualCheckPath, "")
        needsYou = c.value(.needsYou, false)
        startAskId = c.value(.startAskId, "")
        startAskedAt = c.value(.startAskedAt, 0)
        // `maybe` with a sibling-shaped fallback, `runActive`'s documented
        // pattern: an absent key means a daemon from before the badge learned
        // to wait its turn, and `true` there reproduces today's always-show
        // behaviour exactly — a half-upgraded machine never hides a check it
        // should be showing. The steps term is folded in so the invariant
        // "non-empty steps *is* the flag, a badge with nothing behind it
        // cannot exist" survives any wire value.
        manualCheckDue = !manualSteps.isEmpty && (c.maybe(.manualCheckDue) ?? true)
        // Tolerant, and defaulting to the sentinel rather than to 1: absent
        // must be distinguishable from a real limit of 1, because it is what
        // sends the band to the board-level scalar instead.
        parallelLimit = max(0, c.value(.parallelLimit, 0))
        // `maybe`, never `decode`, and never a defaulted number: nil is the
        // "not yet reviewed" state itself, and a daemon from before this
        // shipped sends cards with no key at all — one absent field must
        // never blank the board (Models.swift's documented trap).
        reviewedAt = c.maybe(.reviewedAt)
        donePreview = c.value(.donePreview, false)
        sessionEndedAt = c.maybe(.sessionEndedAt)
        doneAt = c.maybe(.doneAt)
        // Newline-separated in the store, `workflow`'s shape. The tolerant
        // helper with `""` so a daemon not yet restarted after this shipped
        // cannot blank the board.
        attachments = BoardCard.stages(c.value(.attachments, ""))
        threadCount = c.value(.threadCount, 0)
        messages = c.value(.messages, [])
        // `maybe`, deliberately, and the comment is the design: an absent key
        // must decode to **nil**, not to an empty record. A daemon that
        // predates this sends nothing; a card nobody has run sends nothing;
        // both mean "there is no record", and a zero-filled value would draw
        // as a run that changed nothing.
        workRecord = c.maybe(.workRecord)
        workRecordFull = c.maybe(.workRecordFull)
        runFigures = c.maybe(.runFigures)
        runHealth = c.maybe(.runHealth)
        // `maybe`, never `decode`: absent is no batch, and a malformed value
        // (`"batch": 3`) is nil rather than a thrown frame.
        batch = c.maybe(.batch)
        // The dependency five, each through the tolerant helper with an
        // empty default: an older daemon sends none of them, and a card with
        // no links sends none of the four derived ones either.
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
/// and a mark with no usable rank is no mark at all.
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

/// One card another waits on, as the daemon resolved it for this frame.
/// `met` is the daemon's word — Done, or finished and waiting only on a
/// manual check — and is never re-derived from `column` here.
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

    init(id: String = "", title: String = "", column: String = "",
         met: Bool = false) {
        self.id = id
        self.title = title
        self.column = column
        self.met = met
    }
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

    init(id: String = "", title: String = "") {
        self.id = id
        self.title = title
    }
}


/// How a card's run is going, as the daemon publishes it under
/// `run_health` (`run_health.py`): the size class against the project's own
/// finished runs, what the session asked and was refused, how full its
/// context is, and the attempt / return / fix-round counts off the store's
/// ledgers. Every figure is the daemon's; `RunHealthLine` spells them and
/// neither client re-derives one.
///
/// Every field decodes through `value` / `maybe` with a default, and the
/// struct holds no non-optional sub-object, so a ragged object from a newer
/// or older daemon still decodes. `turns`, `tokensK`, `ctxPct` and
/// `fixRounds` are **optionals**, not zeros: the daemon omits each where it
/// is unknown, and a zero there would say something it never said.
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
    /// The daemon's amber bit. Never the only signal: the word comes first.
    var attention = false
    /// Whether the reading came off a live row on this frame rather than
    /// the daemon's frozen record of a finished run.
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


/// The board as one payload: the cards, the per-column counts, and whether Dark Army
/// is allowed to start anything.
///
/// **No version markers.** The daemon publishes fourteen `*_supported` /
/// `*_writable` flags for the phone, which can meet an older Mac; the panel
/// ships in the same bundle as its daemon and never can, so it decodes none
/// of them (20 Sep 2026). `dispatchEnabled`, `ownTerminalEnabled` and
/// `autostartEnabled` are live preferences, not markers, and stay.
struct Board: Decodable, Equatable {
    var generatedAt: Double = 0
    var cards: [BoardCard] = []
    /// The `?cards=delta` marker: this board's `cards` holds only the cards
    /// that moved, and `cardOrder` names every card in the daemon's order.
    /// `DaemonClient.apply` merges it into the board it holds
    /// (`BoardCardDelta.merge`) and clears both before anything downstream
    /// reads the board, so no view ever sees a delta. False from a daemon
    /// that sends whole boards, and the merge never runs.
    var cardsDelta = false
    var cardOrder: [String] = []
    var counts: [String: Int] = [:]
    /// Exact store-wide Done membership. Empty from an older daemon, which
    /// keeps the destructive control hidden instead of degrading to count-only
    /// protection.
    var doneClearToken = ""
    /// What a *held* copy of the Done column can go stale against:
    /// membership, any revised column, and a review. Beside
    /// `doneClearToken` and never a substitute for it — `clear_done`
    /// compares membership and only membership, and passing this to it
    /// would refuse every Clear Done after any finished card was edited.
    /// Empty from an older daemon, which simply never asks for a refetch.
    var doneViewToken = ""
    /// Whether Start may be drawn *at all*. Absent rather than disabled when
    /// this is false — the rule the wrap-up button already follows: a
    /// button that is present and inert is a promise the app cannot keep.
    var dispatchEnabled = false
    /// Whether the next Start opens Dark Army's own terminal. Live fact from
    /// the snapshot, not the ⋯ menu: START HERE is drawn only while this is
    /// off, so the two buttons cannot do the same thing.
    var ownTerminalEnabled = false
    var available = false
    /// The assistants Dark Army can actually start, in the daemon's own words.
    var tools: [String] = []
    var installed: [String: Bool] = [:]
    func isInstalled(_ tool: String) -> Bool { installed[tool] ?? true }
    /// And which models each of them may be run on — the daemon's own
    /// `dispatch.MODELS`, published rather than duplicated here so the panel
    /// re-derives nothing. Empty from an older daemon, which is exactly the
    /// gate every model chooser keys off: absent, never blank.
    var models: [String: [String]] = [:]

    /// The models on offer for one assistant, and the whole of the panel's
    /// model logic. Empty for `""` (no assistant chosen), for a tool the
    /// daemon ships no catalogue for, and for an older daemon that sends none
    /// — in all three cases the chooser is *absent* rather than empty.
    static func modelOptions(models: [String: [String]], tool: String) -> [String] {
        models[tool] ?? []
    }

    /// Presentation only; selection and dispatch keep the daemon's raw id.
    static func modelLabel(_ model: String) -> String {
        model == "gpt-6-astra" ? "Astra 6" : model
    }

    /// Every open VS Code window as name + folder, which is the same list the
    /// dispatch guard checks a card's root against. Offered rather than typed:
    /// a folder Dark Army does not recognise is refused at the moment somebody
    /// presses Start, which is the worst place to learn it was wrong.
    var projects: [BoardProject] = []
    /// Whether Dark Army will start the head of a project's queue by itself. Drives
    /// a queued card's *wording*, not a button: "Dark Army will start it when the
    /// files are free" against "press Start when the files are free".
    ///
    /// Defaults **false**, which is the conservative direction and the point
    /// of decoding it tolerantly: a daemon that predates this field promises
    /// nothing, and a card must never promise an action Dark Army will not take.
    var autostartEnabled = false
    /// How many agents may work at once in one project — the daemon's own
    /// clamped number, which is what the Pipeline readout draws as the
    /// denominator of "RUN 2/3".
    ///
    /// Defaults to **1** and is floored at 1 on decode. Both matter: an older
    /// daemon sends no key at all, and a zero — from a hand-edited
    /// preferences file that somehow reached the wire — would render "RUN
    /// 2/0", a readout stating a rule nothing obeys.
    var parallelLimit = 1
    /// Which projects have said otherwise: canonical project root → the
    /// number that project runs on. **Tick-state only** — what a project
    /// actually runs on arrives already resolved on each card, and this map
    /// is never a denominator. `[:]` on a daemon that predates the field.
    var parallelOverrides: [String: Int] = [:]

    /// Only the published flag determines membership. Newest finishes first.
    func needsYouCards() -> [BoardCard] {
        cards.filter(\.needsYou).sorted { a, b in
            a.finishedClock == b.finishedClock
                ? a.id < b.id : a.finishedClock > b.finishedClock
        }
    }

    /// This project's queue, in the order the daemon's drain obeys — the
    /// coalesced key (`queueRank ?? queuedAt ?? 0`, then id), derived here
    /// rather than sent, so no per-tick position exists on the wire to
    /// defeat the quiet-frame floor.
    func queued(in project: String) -> [BoardCard] {
        cards.filter { $0.isQueued && $0.project == project }
            .sorted { a, b in
                let x = a.queueRank ?? a.queuedAt ?? 0
                let y = b.queueRank ?? b.queuedAt ?? 0
                return x == y ? a.id < b.id : x < y
            }
    }

    /// The cards this project has in flight right now — the same two link
    /// states the daemon's gate starts from, so the RUN heading and the gate
    /// are reading one list rather than two kept in step.
    ///
    /// The narrowing rules — a card in Done holds nothing, a `live` card whose
    /// session the daemon can no longer hear holds nothing — are facts about
    /// the fleet, not about the card, so they are not re-derived here: they
    /// arrive already decided as `runActive`, and a second opinion computed
    /// in the panel is the two-surfaces-disagree failure in its purest form.
    /// `runActive`, not `workActive`: a card flagged for a hand-check stops
    /// holding its files, but while its assistant is still working it belongs
    /// in RUN — the gate's answer filed every flagged live card under done.
    func running(in project: String) -> [BoardCard] {
        cards.filter { $0.project == project && $0.isInFlight && $0.runActive }
    }

    /// This project's Backlog, in the snapshot's own order — which is the
    /// store's `CARD_ORDER_SQL`: `column_name`, then
    /// `CAST(priority AS INTEGER) DESC`, then `position`, then `created_at`.
    /// So it is the order the board itself draws — most important first — and
    /// the order one press would walk.
    ///
    /// Deliberately the whole column: which of these cards will actually
    /// start is the daemon's judgment (the plan gate, `dispatch.guard`, the
    /// slot rule), and a filter here would be a second copy of it that
    /// drifts. The press reports what happened to each one.
    func backlog(in project: String) -> [BoardCard] {
        cards.filter { $0.project == project && $0.column == "backlog" }
    }

    func cards(in column: String) -> [BoardCard] {
        cards.filter { $0.column == column }
    }

    /// The card this session is bound to — the fleet→board mirror of
    /// `Snapshot.row(session:)`, resolved against the daemon's own bookkeeping
    /// (`session_id` / `refine_session_id`) and never a second notion computed
    /// here.
    ///
    /// The card being **executed** outranks the card being **refined**: the
    /// work a session is doing is a stronger answer to "which card is this?"
    /// than the plan a session is writing *for* a card, and `dispatch.py`'s
    /// one-session-per-card bound makes at most one of each exist anyway.
    ///
    /// An empty id resolves to nothing rather than to the first unbound card —
    /// `board.py`'s `by_session()` guard, for the same reason: `sessionId`
    /// defaults to `""` on every card nobody has started.
    ///
    /// **The refine rung is gated on `isRefining`, and that gate is the whole
    /// point of the rung.** `refine_session_id` is not a live link — it is an
    /// *audit trail*. `BoardStore.attach_plan` clears `refine_state` and
    /// deliberately leaves `refine_session_id` standing, so the session that
    /// wrote a plan keeps that field for the life of the card, long after the
    /// card has moved to Backlog and been dispatched to somebody else. Reading
    /// it ungated makes a `/ship` session claim a card another agent is now
    /// working, for ever. Every other reader here already gates on
    /// `refineState` (`Board.refiningAgent`, `BoardCardSheet`,
    /// `PipelineBand`); this one is the outlier, not the precedent.
    func card(forSession id: String) -> BoardCard? {
        guard !id.isEmpty else { return nil }
        return cards.first { $0.sessionId == id }
            ?? cards.first { $0.isRefining && $0.refineSessionId == id }
    }

    /// Cards not started: Prep + Backlog, the same sum the menu bar's to-do
    /// figure makes. `backlog` is the store's name; `ready` is the
    /// one-generation alias so an older daemon does not blank the badge, and
    /// `prep` is simply absent from one.
    var todo: Int {
        (counts["backlog"] ?? counts["ready"] ?? 0) + (counts["prep"] ?? 0)
    }

    /// Store-wide, unlike the bounded Done preview in `cards`.
    var doneCount: Int { max(0, counts["done"] ?? 0) }

    var hasSafeDoneClearToken: Bool {
        doneClearToken.count == 64
            && doneClearToken.allSatisfy { "0123456789abcdef".contains($0) }
    }

    /// The board is empty only when every canonical store count is zero. Done
    /// previews can be absent because they are old or filtered out.
    var isStoreEmpty: Bool {
        ["prep", "backlog", "in_progress", "done"].allSatisfy {
            max(0, counts[$0] ?? 0) == 0
        }
    }

    /// Every project that has a card, plus every project with an open window —
    /// the union, so a project you could file work against shows up in the
    /// filter before the first card exists in it.
    var projectNames: [String] {
        var seen: [String] = []
        for card in cards where !seen.contains(card.project) {
            seen.append(card.project)
        }
        for project in projects where !seen.contains(project.name) {
            seen.append(project.name)
        }
        return seen.sorted { a, b in
            if a.isEmpty != b.isEmpty { return !a.isEmpty }
            return a.localizedCaseInsensitiveCompare(b) == .orderedAscending
        }
    }

    enum CodingKeys: String, CodingKey {
        case cards, counts, available, tools, installed, models, projects
        case doneClearToken = "done_clear_token"
        case doneViewToken = "done_view_token"
        case generatedAt = "generated_at"
        case dispatchEnabled = "dispatch_enabled"
        case ownTerminalEnabled = "own_terminal_enabled"
        case autostartEnabled = "autostart_enabled"
        case parallelLimit = "parallel_limit"
        case parallelOverrides = "parallel_overrides"
        case cardsDelta = "cards_delta"
        case cardOrder = "card_order"
    }

    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = c.value(.generatedAt, 0)
        cards = c.value(.cards, [])
        cardsDelta = c.value(.cardsDelta, false)
        cardOrder = c.value(.cardOrder, [])
        counts = c.value(.counts, [:])
        doneClearToken = c.value(.doneClearToken, "")
        doneViewToken = c.value(.doneViewToken, "")
        dispatchEnabled = c.value(.dispatchEnabled, false)
        ownTerminalEnabled = c.value(.ownTerminalEnabled, false)
        autostartEnabled = c.value(.autostartEnabled, false)
        // Tolerant helper *and* a floor: absent is an older daemon, and a
        // zero is a denominator no readout may draw.
        parallelLimit = max(1, c.value(.parallelLimit, 1))
        parallelOverrides = c.value(.parallelOverrides, [:])
        available = c.value(.available, false)
        tools = c.value(.tools, [])
        installed = c.value(.installed, [:])
        models = c.value(.models, [:])
        projects = c.value(.projects, [])
    }

    init() {}
}

/// The five board-wide facts a card face reads, and nothing else: whether
/// Start may be drawn, whether it opens Dark Army's own terminal, whether the
/// queue drains by itself, and which assistants are offered and installed.
/// Handed to every `BoardCardView` as a value so a card can be compared —
/// holding the whole `Board` (or the client) would make every card news
/// whenever any card moved.
struct BoardChrome: Equatable {
    var dispatchEnabled = false
    var ownTerminalEnabled = false
    var autostartEnabled = false
    var tools: [String] = []
    var installed: [String: Bool] = [:]

    init(_ board: Board) {
        dispatchEnabled = board.dispatchEnabled
        ownTerminalEnabled = board.ownTerminalEnabled
        autostartEnabled = board.autostartEnabled
        tools = board.tools
        installed = board.installed
    }
}

/// One open VS Code window: what to call it, and the folder a card filed against
/// it would launch into.
struct BoardProject: Decodable, Equatable, Identifiable, Hashable {
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
