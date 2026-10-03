import Foundation

/// Chosen action names matching `ApiServer.LAN_ACTIONS`. Membership, not a
/// prefix — a verb that is not here is not a phone verb.
///
/// Every home request is a sealed frame under the pairing's home key, posted
/// to `/api/home` with `X-Bob-Channel` naming the channel (derived from the
/// key, not a credential). No device token travels, and the loopback secret
/// is never sent from this app.
enum PhoneActions {
    static let boardCreate = "board_create"
    static let boardUpdate = "board_update"
    static let boardReset = "board_reset"
    static let boardDelete = "board_delete"
    /// Route: `BobDaemon.clear_done_cards` — the Mac's store-wide Done sweep,
    /// confirmed against the count and membership token that were on screen
    /// when the button armed. Same verb as the panel; one arm, not two.
    static let boardClearDone = "board_clear_done"
    static let boardDispatch = "board_dispatch"
    static let boardRefine = "board_refine"
    /// Route: `BobDaemon.refine_cards` — Refine on several Prep cards with
    /// one planning session. `card_ids` is comma-joined in board order; the
    /// Mac runs every Refine guard per card and opens one planning session,
    /// or refuses the whole press in its own words. Armed then confirmed on
    /// the Board tab's Prep row, drawn only where the board says
    /// `refine_batch_supported`.
    static let boardRefineBatch = "board_refine_batch"
    /// Route: `BobDaemon.start_cards` — Start on several planned Backlog
    /// cards with one session, worked one at a time. `card_ids` comma-joined
    /// in board order; the Mac re-runs every Start guard per card, skips and
    /// names a failure, opens one session or refuses the whole press in its
    /// own words. Armed then confirmed on the Board tab's Backlog row, sent
    /// synchronously, drawn only where the board says `start_batch_supported`.
    static let boardStartBatch = "board_start_batch"
    /// Route: `BobDaemon.approve_card_plan` — "yes, this wording". The Mac
    /// hashes the plan file again and refuses unless the digest this echoes
    /// back still matches, so an approval is always of a version somebody
    /// actually read. It starts nothing; it only ever narrows what a later
    /// Start does without a confirmation.
    static let boardApprovePlan = "board_approve_plan"
    /// Route: `BobDaemon.unqueue_card` — take a waiting card out of the
    /// line. Clear-never-set: the phone can empty a slot and can never
    /// claim one, because `queue_state` is outside `_BOARD_FIELDS`. Unarmed:
    /// clearing a slot destroys nothing, and the undo is pressing Start.
    static let boardUnqueue = "board_unqueue"
    /// Route: `BobDaemon.start_project` — press Start on every startable card
    /// in one project's Backlog, in board order. Not a new capability: the
    /// Mac runs each card through the same `_dispatch_card_locked` a hand
    /// press takes, so the plan gate, the guard and the slot rule all still
    /// apply, and it carries no `skip_plan_gate`. `root` verbatim from the
    /// snapshot; the reply's `detail` is a report meant to be read.
    static let boardStartProject = "board_start_project"
    /// Route: `BobDaemon.move_queued_card` — place one queued card before
    /// another (`before_id`; empty appends). The store's own guard refuses a
    /// card that is not queued, which is what makes a stale screen safe.
    static let boardQueueMove = "board_queue_move"
    /// Route: `BobDaemon.request_preference` → the menu-bar app's
    /// `_set_board_autostart`. `enabled` is `"on"` / `"off"`. The 200 means
    /// *accepted*, not applied: the Mac stores and republishes on its own
    /// thread, and the board snapshot carrying the new value is the proof.
    static let setBoardAutostart = "set_board_autostart"
    /// The sibling dial: how many assistants may work at once in one
    /// project. `root` verbatim from the snapshot (the stored map is keyed
    /// by the string it is sent) and `limit` `"0"`…`"4"`, where `0` is
    /// "back to the shared default".
    static let setBoardParallelRoot = "set_board_parallel_root"
    static let reply = "reply"
    static let permissionVerdict = "permission_verdict"
    static let dismiss = "dismiss"
    static let closeTerminal = "close_terminal"
    /// Route: `BobDaemon.low_priority_session` — `/low-priority` typed onto
    /// a rate-limited Claude session's own input line, then its card
    /// dropped. Offered only where the row says `can_low_priority`; the
    /// Mac's four refusals (open prompt, not Claude, not rate-limited,
    /// switched within the cooldown) are the safety of this verb, and the
    /// command is a toggle, so it is armed then confirmed.
    static let lowPriority = "low_priority"
    /// Route: `BobDaemon.terminal_input` — keys into a terminal the Mac's
    /// Dark Army itself hosts for a board-dispatched session. **Raw bytes ride
    /// `bytes`** (base64): the phone's own emulator typing from away, under
    /// the desk's rules — no prompt refusal, no control-character refusal,
    /// because the dialog is on this screen. At home keys go up the sealed
    /// stream instead and this verb is not sent. The `text` route — one
    /// line, then Enter, with the Mac's prompt and control refusals — is
    /// for older phones and is not sent by this build. Offered only where
    /// the row says `can_terminal_input` **and** the board says
    /// `terminal_stream_supported`. A keystroke is not idempotent, so this
    /// verb always rides `post`, which mints the receipt token every press
    /// carries.
    static let terminalInput = "terminal_input"
    /// Route: `BoardVerbsMixin.open_mission` — start Mission Control, the
    /// Mac's one standing read-only chief-of-staff session, or find it
    /// alive. The Comm tab posts it on appearance; the Mac spawns nothing
    /// while one is alive and refuses in its own words otherwise (the
    /// launcher switch, the cooldown, the launch bounds, no checkout). No
    /// payload field; rides `post` with the receipt token like every write
    /// and is not in `settlingActions` — nothing is leaving.
    static let missionOpen = "mission_open"
    /// Route: `BoardVerbsMixin.end_mission` — close Mission Control's
    /// terminal, the one thing that ends it. Armed then confirmed; rides
    /// `post`; not in `settlingActions`.
    static let missionEnd = "mission_end"
    /// Route: `BobDaemon.request_rebuild` — ask the Mac to rebuild and
    /// restart Dark Army. **Home Wi-Fi only**: the verb is on the Mac's
    /// `LAN_ACTIONS` and deliberately not on `REMOTE_ACTIONS`, so the screens
    /// never offer it from away. Armed then confirmed, and rides `post`, never
    /// `enqueue`: a rebuild banked in the queue and fired later would restart
    /// the Mac on the phone's clock, not the person's. Not in
    /// `settlingActions` — nothing is leaving. No payload field.
    static let rebuildApp = "rebuild_app"
    /// Route: `BoardVerbsMixin.message_card` — a person's own words typed
    /// onto the input line of the session working one named card, the way
    /// `/compact` and `/clear` already go. Card-scoped, never session-scoped:
    /// the reach is exactly "a session Dark Army started for this card, while that
    /// card's link is live". Offered only where the row says `can_message`
    /// **and** the board says `card_message_writable`; the Mac's seven
    /// refusals (nothing working the card, Codex, an open permission prompt,
    /// empty, too long, a leading slash, more than one line) are the safety
    /// of this verb, and its words are shown verbatim.
    static let boardMessage = "board_message"
    static let hideSession = "hide_session"
    static let stopSession = "stop_session"
    static let deleteAgent = "delete_agent"
    /// Route: `BobDaemon.answer_question` — digit-then-Enter typed into the
    /// session's own VS Code terminal, in front of the dialog rather than
    /// behind it. Its guards (permission-prompt refusal, the `waiting`
    /// re-check, the `question_id` match) are the safety of this verb.
    static let answerQuestion = "answer_question"
    /// The batch sibling, for the dialogs that ask more than once: one
    /// ordered burst answering every question (`BobDaemon`'s plural verb).
    /// The choices ride comma-joined ("1,0,2"), 0-based, one per question in
    /// dialog order, because `post` is `[String: String]`; the Mac re-checks
    /// the count and every index against the dialog it holds before typing.
    static let answerQuestions = "answer_questions"
    /// Route: `BobDaemon.prepare_card_text` — one `claude -p` writing the
    /// card's instructions from its title and summary. Slow by design (up to
    /// two minutes with attachments), so it never rides `PhoneClient.post`.
    static let prepareCard = "prepare_card"
    /// Route: `ApiServer._register_push_token` — record this phone's APNs
    /// token on its own away channel, so the Mac's alerts can buzz it. The
    /// device is the verified caller, never a payload field; an empty token
    /// unregisters (the forget path).
    static let registerPushToken = "register_push_token"
    /// Route: `ApiServer._register_activity_token` — record this phone's
    /// Live Activity update token on its own away channel, so the Mac can
    /// update and end the Lock Screen card with the app closed. The push
    /// token's twin: the device is the verified caller, an empty token
    /// unregisters, and it is posted only where the Mac states
    /// `live_activity_supported`.
    static let registerActivityToken = "register_activity_token"
    /// Route: `BobDaemon.ack_inbox` — hide one Needs you subject until it
    /// changes. On a session subject the Mac also drops the notification
    /// card, so the row goes quiet everywhere; never an answer, never Mark
    /// checked.
    static let inboxAck = "inbox_ack"
    /// Route: `BobDaemon.set_bot_access` — put the Mac's bot's Read or
    /// Write access in one position (`off`, `1h`, `6h`, `24h`, `forever`);
    /// the same timer again restarts it. The payload's `device_id` is the
    /// bot, the *target*; the Mac refuses any other target and refuses the
    /// bot itself by its verified identity. Drawn on the profile screen only
    /// where the Mac publishes `bot_access`.
    static let setBotAccess = "set_bot_access"
    /// Route: `BobDaemon.ack_access_alert` — close one burst alert off the
    /// phone doors' access log. Clear-never-set: the log entry stays, and
    /// the verb can raise nothing. Its own verb, never `inbox_ack`.
    static let accessAlertAck = "access_alert_ack"
    /// Route: `BobDaemon.clear_manual_check` — "I did the leftover check".
    /// Clear-never-set: `manual_steps` is outside `_BOARD_FIELDS`, so the
    /// phone can empty a check and never hang one on a card. The press
    /// echoes the steps it drew (`PhoneCardAck.manualClearFields`) and the
    /// Mac ANDs that into its own one-shot WHERE, so a stale screen clears
    /// nothing else. Armed then confirmed here — the Mac's own press is
    /// unarmed — because a thumb must not fire on a card it has not read.
    static let boardManualClear = "board_manual_clear"
    /// Route: `BobDaemon.record_manual_outcome` — Passed or Failed on a
    /// check file, with a note. Keyed on the file's path, never a card: the
    /// Mac writes the file's three status lines only while it still says
    /// open, and clears every card flagged with it. Armed then confirmed
    /// here, `boardManualClear`'s thumb rule.
    static let boardManualOutcome = "board_manual_outcome"
    /// Route: `BobDaemon.review_card` — "I have read this close". The field
    /// is closed, the act is open: `reviewed_at` is stamped only by this
    /// named verb. The press echoes `closed_by` and `close_note`
    /// (`PhoneCardAck.reviewFields`); a mismatch, half a pair or a second
    /// press is refused in the Mac's words. Reading a result never accepts
    /// an outcome: accepting and asking for a revision stay Mac-only verbs.
    static let boardReview = "board_review"
    /// Route: `BobDaemon.promote_card` — turn a Done scout's report into a
    /// Prep build card whose instructions lead with the report's path. It
    /// creates one card off the scout's own row, dispatches nothing, and
    /// the store refuses a second press ("already promoted"). Unarmed, as
    /// on the Mac: it destroys nothing. Drawn only against a Mac whose
    /// board says `promote_supported`; an older Mac 404s the verb.
    static let boardPromote = "board_promote"
    /// Route: `BobDaemon.merge_card` — land a Done card's branch on the
    /// local main line as a merge commit, after the project's checks, with
    /// nothing pushed. The press echoes the branch tip the CHANGES list was
    /// read at (`PhoneCardAck.mergeFields`), the Mac refuses a branch that
    /// moved, answers at once and works in the background. Armed then
    /// confirmed here, `boardManualClear`'s thumb rule. Drawn only against
    /// a Mac whose board says `merge_writable`.
    static let boardMerge = "board_merge"
    /// Route: `BobDaemon.fix_merge_card` — start the card's own assistant in
    /// the card's folder to fix a merge that stopped. Armed then confirmed:
    /// it opens a terminal on the Mac.
    static let boardMergeFix = "board_merge_fix"
    /// Route: `BobDaemon.run_card_review` — an assistant reads the branch
    /// and puts SHIP or STOP on the card. One press, changes no files.
    /// Drawn only against a Mac whose board says `review_run_writable`.
    static let boardReviewRun = "board_review_run"

    /// The sealed home route every read and action rides. Named here so no
    /// other file spells it.
    static let homePath = "/api/home"
    /// The one LAN route that is not `/api/home`: a sealed header frame in
    /// `frameHeader` and a sealed blob as the body, one staged attachment.
    static let uploadPath = "/api/upload"

    /// Which paired phone is talking — the home channel id, derived from the
    /// key. Knowing it buys nothing: the seal is the proof.
    static let channelHeader = "X-Bob-Channel"
    /// The upload's header frame rides here; the HTTP body is the blob.
    static let frameHeader = "X-Bob-Frame"
    static let olderMac = "this Mac cannot take commands from the phone yet"
    /// This record has no home key — it was paired before the home path was
    /// sealed, and the Mac now refuses its every request. Shown in the
    /// unpaired bar instead of failing quietly.
    static let pairAgain = "This Mac now seals phone traffic — pair it again."
    /// A typed-address pair whose answer carried no home key: the Mac is a
    /// build from before sealed home access, and this app has no plaintext
    /// leg to fall back to.
    static let olderMacPair = "This Mac is older than this app — update Dark Army "
        + "on the Mac, then pair again."
    /// A typed-address pair whose reply was not the shape this phone asked
    /// for — a start answered without `pairing` / `salt` / `B`, a finish
    /// whose sealed reply carries no verifying `M2`, or a `B` out of shape.
    /// The phone's own words; the Mac's refusals are shown verbatim instead.
    static let pakeOutOfStep = "The Mac's pairing answer was not the one this phone "
        + "asked for — start a new code on the Mac and try again."
    static let planGatePrefix = "this card has no plan yet"
    /// The gate's second rung: a card whose *approved* plan has since been
    /// edited. A separate sentence from the one above, and deliberately not a
    /// prefix of it — the confirmation it raises says "start with a changed
    /// plan", never "start unplanned", and a pytest pins both constants
    /// against the daemon's and against each other.
    static let planChangedPrefix = "this card's plan has changed"
    /// The opening words of `board.CARD_CHANGED_REFUSAL` — a save judged
    /// against a change number the Mac has already moved past. Recognised by
    /// prefix exactly as the two above are, and pinned byte-for-byte against
    /// the daemon's constant by a pytest, so the sentence may only ever be
    /// extended at its end.
    static let cardChangedPrefix = "this card changed on the Mac"
    /// The opening words of the Mac's own lease refusal (`relay.LEASE_REFUSAL`)
    /// — shared here so views can recognise the sentence, never re-say it.
    static let leaseRefusalPrefix = "away access has lapsed"
    /// Photo upload is deliberately LAN-only (mailbox size and TTL make a
    /// multi-frame reassembly protocol a new thing to get wrong).
    static let photosNeedHome = "Photos need home Wi-Fi."

    /// Build `http://<host>:<port>/api/home`. A bad host refuses in words,
    /// never force-unwraps — `HostAddress.check`, then `URL(string:)`.
    enum ActionURL {
        case ok(URL)
        case refused(String)
    }

    static func homeURL(host: String, port: Int) -> ActionURL {
        lanURL(host: host, port: port, path: homePath)
    }

    /// The same shape for the upload route.
    static func uploadURL(host: String, port: Int) -> ActionURL {
        lanURL(host: host, port: port, path: uploadPath)
    }

    private static func lanURL(host: String, port: Int, path: String) -> ActionURL {
        switch HostAddress.check(host: host, typedPort: port) {
        case .refused(let message):
            return .refused(message)
        case .ok(let cleaned):
            let text = "http://\(cleaned.host):\(cleaned.port)\(path)"
            guard let url = URL(string: text) else {
                return .refused("That isn't a usable address — type the host shown on the Mac's pairing sheet.")
            }
            return .ok(url)
        }
    }
}

/// Whether a Needs you entry offers Dismiss: the reducer's own
/// `dismissable` (every wire kind but the permission ask) against a Mac
/// that keeps a hide list at all.
enum PhoneInboxAck {
    static func showsDismiss(wire: PhoneInboxWireKind, available: Bool) -> Bool {
        available && wire.dismissable
    }
}

/// The two card acknowledgements a phone may send — Mark checked and Mark
/// reviewed — as pure rules over the **live snapshot card** and the board's
/// two version markers. Each control is gated on its own marker so a Mac
/// that honours one and not the other hides exactly one button; an older
/// Mac sends neither key and draws neither. The fields are the
/// current-state echo the Mac's store re-checks in its UPDATE WHERE: they
/// are a confirmation of what was on screen, never a capability — pairing,
/// the seal and the lease still decide whether the write is heard. Never
/// built from `CardCacheStore`: a cached copy is not what the person is
/// looking at, and the store guard is the real answer if it has moved on.
enum PhoneCardAck {
    static func showsManualClear(card: BoardCard, board: Board) -> Bool {
        board.manualClearWritable && card.manualCheckDue
            && !card.manualSteps.isEmpty && card.manualCheckPath.isEmpty
    }

    /// Mark checked on a card flagged with a check file the Mac will no
    /// longer serve (moved, edited out of shape, outside the project's
    /// folder) — Passed / Failed would be refused, so the badge needs its
    /// old way off. The caller supplies "refused" from the on-open read.
    static func showsManualClearOverRefusedFile(card: BoardCard,
                                                board: Board) -> Bool {
        board.manualClearWritable && card.manualCheckDue
            && !card.manualSteps.isEmpty && !card.manualCheckPath.isEmpty
    }

    /// Passed / Failed, on a card flagged with a check file, against a Mac
    /// that takes the press. Mark checked is hidden there.
    static func showsManualOutcome(card: BoardCard, board: Board) -> Bool {
        board.manualOutcomeWritable && !card.manualCheckPath.isEmpty
            && !card.manualSteps.isEmpty
    }

    static func manualOutcomeFields(_ card: BoardCard, status: String,
                                    note: String) -> [String: String] {
        ManualOutcomeFields.fields(path: card.manualCheckPath, status: status,
                                   note: note)
    }

    static func showsReview(card: BoardCard, board: Board) -> Bool {
        board.reviewWritable && card.awaitsReview && !card.closeNote.isEmpty
    }

    static func manualClearFields(_ card: BoardCard) -> [String: String] {
        ["card_id": card.id, "expected_manual_steps": card.manualSteps]
    }

    /// `card_id`, and the branch tip the list was read at when there is one:
    /// the Mac refuses a MERGE whose branch moved since. Absent tip means no
    /// guard, `expected_revision`'s rule.
    static func mergeFields(_ card: BoardCard, tip: String) -> [String: String] {
        var fields = ["card_id": card.id]
        if !tip.isEmpty { fields["expected_tip"] = tip }
        return fields
    }

    static func reviewFields(_ card: BoardCard) -> [String: String] {
        ["card_id": card.id,
         "expected_closed_by": card.closedBy,
         "expected_close_note": card.closeNote]
    }
}

/// The body of one Passed / Failed press, from the card screen or the
/// Checks screen alike: the file, the word, the note on one line and
/// clamped to the Mac's own limit (`MAX_MANUAL_OUTCOME_CHARS`, 400 —
/// `ManualCheckRules.noteLimit`). Self-contained, so every harness that
/// compiles this file alone still builds.
enum ManualOutcomeFields {
    static let noteLimit = 400

    static func fields(path: String, status: String,
                       note: String) -> [String: String] {
        let flat = note.split(whereSeparator: \.isNewline).joined(separator: " ")
        let clamped = String(flat.trimmingCharacters(in: .whitespaces)
            .prefix(noteLimit))
        return ["path": path, "status": status, "note": clamped]
    }
}

/// Outcome of one `POST /api/action`. Empty `detail` on 200 is success.
struct PhoneActionResult {
    var ok: Bool
    var detail: String
    /// The card's change number as the Mac holds it after a successful
    /// `board_update`. `nil` from every other verb and from every older Mac,
    /// which is why the card screen keeps its own copy rather than requiring
    /// this.
    var revision: Int?
    /// What the Mac holds, sent back with `CARD_CHANGED_REFUSAL` alone. The
    /// refusal is routinely the first thing a phone hears after being out of
    /// signal and a second fetch may not be possible, so the answer carries
    /// the other side of the conflict with it.
    var current: CardStated?

    init(ok: Bool, detail: String, revision: Int? = nil,
         current: CardStated? = nil) {
        self.ok = ok
        self.detail = detail
        self.revision = revision
        self.current = current
    }

    var isPlanGateRefusal: Bool {
        detail.hasPrefix(PhoneActions.planGatePrefix)
    }

    var isPlanChangedRefusal: Bool {
        detail.hasPrefix(PhoneActions.planChangedPrefix)
    }

    var isCardChangedRefusal: Bool {
        detail.hasPrefix(PhoneActions.cardChangedPrefix)
    }

    static func parse(data: Data, code: Int) -> PhoneActionResult {
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let detail = (obj?["detail"] as? String)
            ?? (obj?["error"] as? String)
            ?? ""
        guard obj == nil || AreaWireFields.accepts(data) else {
            return PhoneActionResult(ok: false,
                detail: detail.isEmpty ? "The reply could not be read." : detail)
        }
        let revision = obj?["revision"] as? Int
        let current = CardStated(json: obj?["current"] as? [String: Any])
        if code == 200 {
            // A 200's `detail` is the Mac's report of what it did — START n
            // TOGETHER's "started 4 cards (1 skipped)", START PROJECT's — and
            // the screens that draw it read it here. Only `detail`: an
            // `error` key on a success is not a report.
            return PhoneActionResult(ok: true,
                                     detail: (obj?["detail"] as? String) ?? "",
                                     revision: revision, current: current)
        }
        return PhoneActionResult(ok: false, detail: detail, revision: revision,
                                 current: current)
    }
}

/// The Mac's own copy of the fields a save may name, as it reported them in
/// a refusal (`BoardVerbsMixin.STATED_FIELDS`). Read out of the JSON by hand
/// rather than decoded, because the whole object is absent on every answer
/// but one and an absent object must be `nil`, never a blank card.
struct CardStated: Equatable {
    var revision = 0
    var title = ""
    var summary = ""
    var prompt = ""
    /// The Mac's importance number, `""` where nobody has scored the card —
    /// and `""` too where the Mac is older than the field, which is the same
    /// thing to draw.
    var priority = ""
    var area = ""

    init?(json: [String: Any]?) {
        guard let json else { return nil }
        revision = json["revision"] as? Int ?? 0
        title = json["title"] as? String ?? ""
        summary = json["summary"] as? String ?? ""
        prompt = json["prompt"] as? String ?? ""
        priority = json["priority"] as? String ?? ""
        area = json["area"] as? String ?? ""
    }
}

/// Outcome of one `prepare_card`. `PhoneActionResult` throws away every 200
/// body, and this verb's whole point *is* the body.
struct PhonePrepareResult {
    var ok: Bool
    var detail: String
    var prompt: String
    var workflow: String
    /// Written only by the Mac's one-box idea mode. An older Mac answers
    /// without these two keys, which reads as empty — and the composer
    /// applies a value only when it is non-empty, so empty never blanks
    /// something a person typed.
    var title: String = ""
    var summary: String = ""
    /// Which open project the Mac thinks the card belongs to, as a *root*, or
    /// `""` for no opinion. Same rule again: an older Mac sends no key, and
    /// empty never blanks the folder somebody already picked.
    var suggestedRoot: String = ""
    var suggestedArea: String = ""
    /// The drafted objective, idea mode only. The same rule one step
    /// stricter: applied only when non-empty **and** the box is empty.
    var beneficiary: String = ""
    var intendedBenefit: String = ""
    var successCriterion: String = ""
    /// Which side answered. Defaulted so `parse(data:code:)` — the Mac's
    /// reply — is untouched; `PhonePreparer` sets `.phone`.
    var preparedVia: PrepareRoute = .mac

    static func parse(data: Data, code: Int) -> PhonePrepareResult {
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let detail = (obj?["detail"] as? String)
            ?? (obj?["error"] as? String)
            ?? ""
        guard AreaWireFields.accepts(data) else {
            return PhonePrepareResult(ok: false, detail: "The reply could not be read.",
                                      prompt: "", workflow: "")
        }
        guard code == 200, (obj?["ok"] as? Bool) == true else {
            return PhonePrepareResult(ok: false, detail: detail,
                                      prompt: "", workflow: "")
        }
        return PhonePrepareResult(
            ok: true, detail: "",
            prompt: (obj?["prompt"] as? String) ?? "",
            workflow: (obj?["workflow"] as? String) ?? "",
            title: (obj?["title"] as? String) ?? "",
            summary: (obj?["summary"] as? String) ?? "",
            suggestedRoot: (obj?["suggested_root"] as? String) ?? "",
            suggestedArea: (obj?["suggested_area"] as? String) ?? "",
            beneficiary: (obj?["beneficiary"] as? String) ?? "",
            intendedBenefit: (obj?["intended_benefit"] as? String) ?? "",
            successCriterion: (obj?["success_criterion"] as? String) ?? "")
    }
}
