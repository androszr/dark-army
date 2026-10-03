import Foundation

// The board verbs, as `Fetchers.swift` and `OutcomeClient.swift` already are:
// one extension per concern on the one client. Split out of
// `DaemonClient.swift` on 20 Sep 2026. `post` / `postAny` / `noteAuthRefused`
// are module-internal for these extensions' sake.
extension DaemonClient {
    // MARK: - The board
    //
    // All five go through `post(_:)`, which is why every value here is a
    // `String`: the header it sets is `X-Bob-Token`, not `Authorization:
    // Bearer` — reads are ungated, so the wrong header looks like it works and
    // every write silently 403s.

    /// `refine` is the composer's one-press mark — write the card, then
    /// open a planning session on it. Envelope, not a card field, in
    /// `skipPlanGate`'s shape: the daemon's `_BOARD_ENVELOPE` lists it and
    /// `_board_fields` never lets it near the store. A refused refinement
    /// still answers 200; its reason lands on the card's `dispatch_error`.
    func boardCreate(title: String, summary: String, prompt: String,
                     project: String, root: String, tool: String,
                     column: String, workflow: String = "",
                     attachments: String = "",
                     model: String = "",
                     createToken: String = "",
                     refine: Bool = false,
                     beneficiary: String = "",
                     intendedBenefit: String = "",
                     successCriterion: String = "",
                     priority: String = "",
                     area: String = "",
                     kind: String = "",
                     startWhenPlanned: Bool = false) async -> ActionResult {
        var body = ["action": "board_create", "title": title,
                    "summary": summary, "prompt": prompt,
                    "project": project, "root": root, "tool": tool,
                    "column_name": column, "workflow": workflow,
                    "attachments": attachments, "model": model,
                    "start_when_planned": startWhenPlanned ? "1" : ""]
        if !createToken.isEmpty { body["create_token"] = createToken }
        if refine { body["refine"] = "true" }
        // The objective, sent only when typed: an unfilled composer's
        // payload is byte-identical to before, and the store bounds what
        // does arrive (`objective_refusal`) rather than this client.
        if !beneficiary.isEmpty { body["beneficiary"] = beneficiary }
        if !intendedBenefit.isEmpty { body["intended_benefit"] = intendedBenefit }
        if !successCriterion.isEmpty { body["success_criterion"] = successCriterion }
        // The priority, the same rule: a string, never a number, because
        // `_board_fields` coerces a JSON `0` into "no opinion". The store
        // refuses an off-range value in `PRIORITY_REFUSAL`'s words.
        if !priority.isEmpty { body["priority"] = priority }
        if !area.isEmpty { body["area"] = area }
        if !kind.isEmpty { body["kind"] = kind }
        return await post(body)
    }

    /// `skipPlanGate` on the three verbs that can move a card into In
    /// progress is the plan gate's confirmed-press flag: the daemon refuses
    /// an unplanned arrival there unless the payload carries it, and the
    /// panel sends it only from the confirmation dialog / the armed "Start
    /// unplanned?" press. Envelope, not a card field — the daemon's
    /// `_BOARD_ENVELOPE` says so.
    func boardUpdate(_ cardId: String, fields: [String: String],
                     skipPlanGate: Bool = false) async -> ActionResult {
        var body = fields
        body["action"] = "board_update"
        body["card_id"] = cardId
        if skipPlanGate { body["skip_plan_gate"] = "true" }
        return await post(body)
    }

    /// Forget the card's session and any dispatch note, optionally moving it in
    /// the same write. The panel cannot set `session_id` or `link_state` — they
    /// are the daemon's bookkeeping and are not in the API's field allow-list —
    /// so clearing them is a verb rather than an update, and it is atomic: a
    /// card never lands back in Backlog still carrying a dead session id, which
    /// made it permanently unstartable.
    func boardReset(_ cardId: String,
                    fields: [String: String] = [:]) async -> ActionResult {
        var body = fields
        body["action"] = "board_reset"
        body["card_id"] = cardId
        return await post(body)
    }

    /// One card, with its prompt in full.
    ///
    /// The live snapshot carries only a preview of each prompt — the whole text
    /// on every SSE frame is what put that payload an order of magnitude over
    /// the limiter's budget — so anything that needs the real instructions has
    /// to ask. Returns nil on any failure, and the caller keeps the editor shut
    /// rather than opening it on a body it must not save.
    func boardCard(_ cardId: String) async -> BoardCard? {
        await boardCardReport(cardId)?.cards.first
    }

    /// The whole single-card reply — the card and, beside it, the timeline
    /// the on-open read carries. `boardCard`'s pattern: 5 s timeout, nil on
    /// any transport or decode failure.
    func boardCardReport(_ cardId: String) async -> BoardReport? {
        guard !cardId.isEmpty,
              let encoded = cardId.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) else { return nil }
        var req = request("/api/board?card=\(encoded)")
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let report = try? JSONDecoder().decode(BoardReport.self, from: data)
        else { return nil }
        return report
    }

    /// The whole finished column, in the frame's own shape.
    ///
    /// `boardCard`'s pattern exactly — 5 s timeout, nil on any transport or
    /// decode failure — because a failed fetch must leave whatever is already
    /// held on screen rather than blanking a column.
    func doneArchiveReport() async -> BoardReport? {
        var req = request("/api/board?column=done")
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let report = try? JSONDecoder().decode(BoardReport.self, from: data)
        else { return nil }
        return report
    }

    /// One session's durable history record, on demand.
    ///
    /// Same shape as `boardCard`: percent-encode, 5s timeout, nil on any
    /// transport or decode failure. A card sheet asks once per open; nothing
    /// here rides the SSE frame.
    func sessionRecord(_ sessionId: String) async -> SessionRecordReport? {
        guard !sessionId.isEmpty,
              let encoded = sessionId.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) else { return nil }
        var req = request("/api/history?session=\(encoded)")
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let report = try? JSONDecoder().decode(SessionRecordReport.self,
                                                     from: data)
        else { return nil }
        return report
    }

    /// What Dark Army observed one card's last run do — the whole record, or one
    /// file's changes.
    ///
    /// `boardCard`'s shape: percent-encode, 5s timeout, nil on any transport
    /// or decode failure. The record itself normally arrives free with the
    /// sheet's existing card fetch (`work_record_full`); this is the route
    /// for one file's diff, and the fallback where an older per-card reply
    /// carried no record at all.
    func workRecord(_ cardId: String, file: Int? = nil) async -> Data? {
        guard !cardId.isEmpty,
              let encoded = cardId.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) else { return nil }
        var path = "/api/work-record?card=\(encoded)"
        if let file { path += "&file=\(file)" }
        var req = request(path)
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200
        else { return nil }
        return data
    }

    /// The record for one card. Absent is nil, never an empty record.
    func workRecordReport(_ cardId: String) async -> WorkRecordReport? {
        guard let data = await workRecord(cardId) else { return nil }
        return try? JSONDecoder().decode(WorkRecordReport.self, from: data)
    }

    /// One file's changes, by its index in the record's own list. The caller
    /// never supplies a path — the daemon resolves the index against the row
    /// it stored, which is what keeps the containment argument short.
    func workRecordDiff(_ cardId: String, file: Int) async -> WorkRecordDiff? {
        guard let data = await workRecord(cardId, file: file) else { return nil }
        return try? JSONDecoder().decode(WorkRecordDiff.self, from: data)
    }

    func boardDelete(_ cardId: String) async -> ActionResult {
        await post(["action": "board_delete", "card_id": cardId])
    }

    /// Clear the store-wide Done set confirmed by the user. One request no
    /// matter how many Done cards exist; the daemon re-checks the count inside
    /// the store transaction before deleting anything.
    func boardClearDone(expectedCount: Int,
                        doneToken: String) async -> ActionResult {
        await post(["action": "board_clear_done",
                    "expected_count": String(expectedCount),
                    "expected_done_token": doneToken])
    }

    /// Start the assistant a card names. The daemon refuses far more often than
    /// it accepts — dispatch off, a window closed, the card moved — and every
    /// refusal comes back in `detail` as words to put on the card, because
    /// nothing else on screen moves when a launch does not happen.
    func boardAsk(cardId: String, text: String) async -> ActionResult {
        await post(["action": "board_ask", "card_id": cardId, "text": text])
    }

    /// Type a person's own words onto the input line of the session working
    /// this card — `boardAsk`'s sibling and a different act. `boardAsk`
    /// reaches a session down its channel or starts a helper; this reaches a
    /// board-dispatched session, which has no channel, by keystroke.
    ///
    /// Card-scoped: no session id crosses the wire. Every bound is the
    /// daemon's (empty, one line, a leading slash, the length, an open
    /// permission prompt) and every refusal comes back in `detail` as words
    /// to put under the box.
    func boardMessage(cardId: String, text: String) async -> ActionResult {
        await post(["action": "board_message", "card_id": cardId,
                    "text": text])
    }

    func boardDispatch(_ cardId: String,
                       skipPlanGate: Bool = false,
                       ownTerminal: Bool = false) async -> ActionResult {
        var body = ["action": "board_dispatch", "card_id": cardId]
        if skipPlanGate { body["skip_plan_gate"] = "true" }
        if ownTerminal { body["own_terminal"] = "true" }
        return await post(body)
    }

    /// Open an assistant terminal in `root` with **no card behind it** —
    /// + TERMINAL on the Agents rail.
    ///
    /// `boardDispatch`'s shape and the same door, but not a board write: the
    /// daemon creates no card and moves nothing. Every bound is the daemon's
    /// (the launcher preference, the enrolment ledger, the one-per-project
    /// launch bound, the cooldown) and every refusal comes back in `detail`
    /// as words to draw verbatim under the Start button.
    func spawnTerminal(root: String, tool: String) async -> ActionResult {
        await post(["action": "spawn_terminal", "root": root, "tool": tool])
    }

    /// Open Mission Control — the one standing chief-of-staff session — or
    /// find it alive. `spawnTerminal`'s shape and door (`X-Bob-Token`
    /// through `post`); the daemon starts nothing while one is alive and
    /// refuses in `detail`'s words otherwise.
    func missionOpen() async -> ActionResult {
        await post(["action": "mission_open"])
    }

    /// End Mission Control: the one thing that closes its terminal.
    func missionEnd() async -> ActionResult {
        await post(["action": "mission_end"])
    }

    /// Dispatch a *planning* session onto a Prep card — the Refine button.
    /// The daemon guards it exactly as a dispatch (same preference, same
    /// bounds), and the card does not move: `refine_state` is what changes.
    func boardRefine(_ cardId: String) async -> ActionResult {
        await post(["action": "board_refine", "card_id": cardId])
    }

    /// Refine several Prep cards with **one** planning session — the Prep
    /// row's REFINE n TOGETHER. `post` is `[String: String]`, so the ids ride
    /// as one comma-joined `card_ids`, in the order given (the board's own);
    /// the daemon re-runs every Refine guard per card and opens one terminal.
    func boardRefineBatch(_ ids: [String]) async -> ActionResult {
        await post(["action": "board_refine_batch",
                    "card_ids": ids.joined(separator: ",")])
    }

    /// Start several planned Backlog cards with **one** session, worked one
    /// at a time — the Backlog row's START n TOGETHER. `boardRefineBatch`'s
    /// shape: one comma-joined `card_ids`; the daemon orders the members by
    /// the board, runs every Start guard per card and opens one terminal.
    func boardStartBatch(_ ids: [String]) async -> ActionResult {
        await post(["action": "board_start_batch",
                    "card_ids": ids.joined(separator: ",")])
    }

    /// Take a card out of its project's work queue.
    ///
    /// Clear-never-set: `queue_state` and `queued_at` are outside the API's
    /// field allow-list, so this is the only route a surface has to either of
    /// them and it can only ever empty the slot. Unarmed on every surface that
    /// offers it — clearing a queue slot destroys nothing, and the undo is
    /// pressing Start again.
    func boardUnqueue(_ cardId: String) async -> ActionResult {
        await post(["action": "board_unqueue", "card_id": cardId])
    }

    /// Tick or clear "start this by itself the moment its plan lands".
    ///
    /// Deliberately sends **no** `expected_revision` — `boardUnqueue`'s shape.
    /// A one-field toggle is not a form save, and guarding it would refuse the
    /// press whenever anything unrelated on the card had moved since the last
    /// frame. Unarmed for the same reason: setting it destroys nothing and the
    /// undo is pressing it again.
    func boardStartWhenPlanned(_ cardId: String, on: Bool) async -> ActionResult {
        await post(["action": "board_update", "card_id": cardId,
                    "start_when_planned": on ? "1" : ""])
    }

    /// Press Start on every startable card in this project's Backlog, in
    /// board order.
    ///
    /// One gesture, N presses — and not a new capability: the daemon runs
    /// every card through the same `_dispatch_card_locked` a hand press
    /// takes, so the plan gate, the enrolment refusal, `dispatch.guard` and
    /// the slot rule all still apply. There is deliberately no
    /// `skip_plan_gate`: an unplanned card is skipped and named in the reply,
    /// because a confirmation is a thing a person gives about one card.
    ///
    /// `detail` is the report and is worth showing whether or not `ok` — a
    /// press where everything was left alone is exactly the case a person
    /// needs the words for.
    func boardStartProject(root: String) async -> ActionResult {
        await post(["action": "board_start_project", "root": root])
    }

    /// Reorder a card already in its project's queue. `beforeId` empty
    /// appends. The store refuses any card that is not queued — a surface
    /// may reorder the line and may never put a card in it.
    func boardQueueMove(_ cardId: String, beforeId: String) async -> ActionResult {
        await post(["action": "board_queue_move", "card_id": cardId,
                    "before_id": beforeId])
    }

    /// Say the hand-check this card was flagged for has been done.
    ///
    /// Clear-never-set, `boardUnqueue`'s shape: `manual_steps` is outside the
    /// API's field allow-list, so this is the only route a surface has to it
    /// and it can only ever empty the field. Unarmed — it destroys a note, but
    /// the undo is that the session can say it again, and an arm on a verb
    /// somebody presses *after* doing the chore is ceremony.
    func boardManualClear(_ cardId: String) async -> ActionResult {
        await post(["action": "board_manual_clear", "card_id": cardId])
    }

    /// Record Passed or Failed on a manual check file, with the person's
    /// note. Keyed on the file, never a card: the daemon writes the file's
    /// three status lines and clears every card flagged with it, and a
    /// second press is refused in words. `X-Bob-Token` rides `post`.
    func boardManualOutcome(path: String, status: String,
                            note: String) async -> ActionResult {
        await post(["action": "board_manual_outcome", "path": path,
                    "status": status,
                    "note": String(note.prefix(ManualCheckRules.noteLimit))])
    }

    /// Acknowledge an assistant's close — the Reviewed press. Drops the
    /// "FINISHED · REVIEW" banner and lets the card sink into the ordinary
    /// Done order; the closer's name and note stay on the card. One click, no
    /// arm: nothing is destroyed, and the whole cost of a slipped press is a
    /// dropped highlight. `reviewed_at` is outside the API's field allow-list,
    /// so this named verb is the only route a surface has to it.
    func boardReview(_ cardId: String) async -> ActionResult {
        await post(["action": "board_review", "card_id": cardId])
    }

    /// Promote a finished scout into a Prep build card carrying its report.
    /// Loopback only: the daemon re-checks, and a second press is refused
    /// in words. `ActionResult` stays `ok` + `detail`; the reply's
    /// `card_id` is for the daemon's tests and a later reveal, not read here.
    func boardPromote(_ cardId: String) async -> ActionResult {
        await post(["action": "board_promote", "card_id": cardId])
    }

    /// Land a Done card's branch on the local main line
    /// (`docs/card-worktrees.md`, *Review and merge*). The daemon answers at
    /// once and merges in the background; every refusal comes back in
    /// `detail`. `expectedTip` is the branch tip the Changes list was read at
    /// — absent on the Mac's plain press, which sends no guard.
    func boardMerge(_ cardId: String, expectedTip: String = "") async -> ActionResult {
        var body = ["action": "board_merge", "card_id": cardId]
        if !expectedTip.isEmpty { body["expected_tip"] = expectedTip }
        return await post(body)
    }

    /// Start the card's own assistant in the card's folder to fix a merge
    /// that stopped on a conflict or failed checks.
    func boardMergeFix(_ cardId: String) async -> ActionResult {
        await post(["action": "board_merge_fix", "card_id": cardId])
    }

    /// Start an assistant that reviews the card's branch and answers onto the
    /// card. Unarmed: it changes no files.
    func boardReviewRun(_ cardId: String) async -> ActionResult {
        await post(["action": "board_review_run", "card_id": cardId])
    }

    /// A Done card's branch against the main line — commits and files with
    /// counts. Fetched when the CHANGES section opens, never on a poll.
    /// `workRecord`'s shape: percent-encode, nil on any transport or decode
    /// failure; a longer timeout, because the daemon runs a few git reads.
    func cardChanges(_ cardId: String) async -> CardChangesReport? {
        guard let data = await cardChangesData(cardId, query: "") else { return nil }
        return try? JSONDecoder().decode(CardChangesReport.self, from: data)
    }

    /// One file's changes, by its index in the list the daemon sent and the
    /// branch tip that list was read at (a moved branch is refused, not
    /// drawn against the wrong listing).
    func cardChangeDiff(_ cardId: String, file: Int, tip: String) async -> CardChangeDiff? {
        guard let encoded = tip.addingPercentEncoding(
            withAllowedCharacters: .alphanumerics),
              let data = await cardChangesData(
                cardId, query: "&file=\(file)&tip=\(encoded)") else { return nil }
        return try? JSONDecoder().decode(CardChangeDiff.self, from: data)
    }

    private func cardChangesData(_ cardId: String, query: String) async -> Data? {
        guard !cardId.isEmpty,
              let encoded = cardId.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) else { return nil }
        var req = request("/api/card-changes?card=\(encoded)\(query)")
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return data
    }

    /// Say "yes, this wording" about the plan a card points at. The digest is
    /// the panel's own hash of the text it just read and drew, and the daemon
    /// re-reads the file and refuses unless the two still agree — so an
    /// approval is always of a version somebody actually saw. `plan_approved`
    /// is outside the API's field allow-list, so this named verb is the only
    /// route to it.
    func approvePlan(cardId: String, planPath: String,
                     digest: String) async -> ActionResult {
        await post(["action": "board_approve_plan", "card_id": cardId,
                    "plan_path": planPath, "plan_digest": digest])
    }

    /// Place a card in a column, before `beforeId` (empty = append). The
    /// daemon computes the midpoint so this never sends a raw float.
    /// `fields` rides the same store write, so a drop that changes a field
    /// and the slot lands as one statement, never a card left half-moved.
    func boardReorder(_ cardId: String, column: String, beforeId: String,
                      skipPlanGate: Bool = false,
                      fields: [String: String] = [:]) async -> ActionResult {
        var body = fields
        body["action"] = "board_reorder"
        body["card_id"] = cardId
        body["column_name"] = column
        body["before_id"] = beforeId
        if skipPlanGate { body["skip_plan_gate"] = "true" }
        return await post(body)
    }

    /// Fill a new card's instructions from the plain-words description.
    ///
    /// Does not go through `post(_:)`: that returns only `ok`/`detail` and
    /// inherits the default 60s URLSession timeout, which is the daemon's own
    /// ceiling. Own request off `request(_:)` so the token header is unchanged,
    /// with `timeoutInterval = 75` so the panel outlives the helper.
    func prepareCard(title: String, summary: String, tool: String,
                     project: String, root: String,
                     attachments: String = "",
                     idea: String = "") async -> PrepareResult {
        var req = request("/api/action")
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // 135 outlives the daemon's 120s attachment ceiling the way 75
        // outlived the 60s no-attachment one.
        req.timeoutInterval = 135
        let body: [String: String] = [
            "action": "prepare_card",
            "title": title, "summary": summary,
            "tool": tool, "project": project, "root": root,
            "attachments": attachments,
            // Additive: an older daemon ignores the key entirely and answers
            // exactly as it does today.
            "idea": idea,
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return PrepareResult(ok: false, detail: "The daemon did not answer.",
                                 prompt: "", workflow: "")
        }
        guard AreaWireFields.accepts(data) else {
            return PrepareResult(ok: false, detail: "The reply could not be read.",
                                 prompt: "", workflow: "")
        }
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let prompt = obj?["prompt"] as? String ?? ""
        let workflow = obj?["workflow"] as? String ?? ""
        // Absent from an older daemon's answer, which is empty, which the
        // composer reads as "leave that field as the person typed it".
        let newTitle = obj?["title"] as? String ?? ""
        let newSummary = obj?["summary"] as? String ?? ""
        let suggested = obj?["suggested_root"] as? String ?? ""
        // The drafted objective, idea mode only; absent from an older
        // daemon, which is empty, which the composer never applies.
        let beneficiary = obj?["beneficiary"] as? String ?? ""
        let intendedBenefit = obj?["intended_benefit"] as? String ?? ""
        let successCriterion = obj?["success_criterion"] as? String ?? ""
        let detail = obj?["detail"] as? String ?? ""
        let okFlag = obj?["ok"] as? Bool ?? false
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 200 && okFlag {
            return PrepareResult(ok: true, detail: "",
                                 prompt: prompt, workflow: workflow,
                                 title: newTitle, summary: newSummary,
                                 suggestedRoot: suggested,
                                 suggestedArea: obj?["suggested_area"] as? String ?? "",
                                 beneficiary: beneficiary,
                                 intendedBenefit: intendedBenefit,
                                 successCriterion: successCriterion)
        }
        Trace.log("prepare refused http=\(code)")
        let reason = ActionResult.refusalText(detail: detail)
        return PrepareResult(ok: false, detail: reason,
                             prompt: "", workflow: "")
    }

    /// Enrol a folder: Dark Army writes a private key file inside it, gitignores it,
    /// and records it. Only enrolled projects are visible to Dark Army or allowed to
    /// write to its board.
    func enrollProject(_ root: String) async -> ActionResult {
        await post(["action": "enroll_project", "root": root])
    }

    /// Take a folder back out. It goes quiet on the next thing it says — the
    /// daemon re-checks the ledger on every message rather than once a session.
    func unenrollProject(_ root: String) async -> ActionResult {
        await post(["action": "unenroll_project", "root": root])
    }

    /// RFC 3986 unreserved, ASCII only. Unicode letters must not ride the
    /// GET URL — `request(_:)` force-unwraps `URL(string:)`.
    static let knowledgeRootUnreserved = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    func knowledgeReport(root: String) async -> KnowledgeReport? {
        guard !root.isEmpty,
              let encoded = root.addingPercentEncoding(
                withAllowedCharacters: Self.knowledgeRootUnreserved)
        else { return nil }
        var req = request("/api/knowledge?root=\(encoded)")
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return try? JSONDecoder().decode(KnowledgeReport.self, from: data)
    }

    /// Every scout report the daemon lists, newest first, no bodies — or
    /// one enrolled project's; with a `query`, only the reports whose body
    /// holds it, each with a snippet (`?q=`, `manualChecks`' encoding).
    /// `knowledgeReport(root:)`'s shape: the token rides `request(_:)`, a
    /// 403 is noted, anything else is `nil`.
    func scoutReportsIndex(root: String = "", query: String = "") async -> ScoutReportIndex? {
        var parts: [String] = []
        for (key, value) in [("root", root), ("q", query)] where !value.isEmpty {
            guard let encoded = value.addingPercentEncoding(
                withAllowedCharacters: Self.knowledgeRootUnreserved)
            else { return nil }
            parts.append("\(key)=\(encoded)")
        }
        let path = "/api/scout-reports"
            + (parts.isEmpty ? "" : "?" + parts.joined(separator: "&"))
        var req = request(path)
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return try? JSONDecoder().decode(ScoutReportIndex.self, from: data)
    }

    /// One report's text, split into its answer block and body. The daemon
    /// re-checks the path against the set it lists; outside it the answer
    /// is `available: false` with the reason in words.
    func scoutReportBody(path: String) async -> ScoutReportBody? {
        guard !path.isEmpty,
              let encoded = path.addingPercentEncoding(
                withAllowedCharacters: Self.knowledgeRootUnreserved)
        else { return nil }
        var req = request("/api/scout-report?path=\(encoded)")
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return try? JSONDecoder().decode(ScoutReportBody.self, from: data)
    }

    func knowledgeConfirm(root: String, key: String) async -> ActionResult {
        await post(["action": "knowledge_confirm", "root": root, "key": key])
    }

    func knowledgeStale(root: String, key: String) async -> ActionResult {
        await post(["action": "knowledge_stale", "root": root, "key": key])
    }

    func knowledgeEdit(root: String, key: String, question: String,
                       answer: String) async -> ActionResult {
        await post(["action": "knowledge_edit", "root": root, "key": key,
                    "question": question, "answer": answer])
    }

    /// Ask the daemon for a two-minute pairing code. The QR is drawn from
    /// the extra fields; `ActionResult` stays the two-field shape every
    /// other press uses.
    func beginPairing(allowTyped: Bool = false) async -> PairingResult {
        var req = request("/api/action")
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(
            withJSONObject: PairingSession.beginPayload(allowTyped: allowTyped))
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return PairingResult(ok: false, detail: "The daemon did not answer.")
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let detail = obj?["detail"] as? String ?? ""
        if code == 200, let obj, (obj["ok"] as? Bool) ?? true {
            let port = (obj["port"] as? Int)
                ?? (obj["port"] as? NSNumber)?.intValue
                ?? 0
            let expires = (obj["expires_at"] as? Double)
                ?? (obj["expires_at"] as? NSNumber)?.doubleValue
                ?? 0
            let host = obj["host"] as? String ?? ""
            // Tolerant decode, the house rule: an older daemon sends no
            // `hosts`, and absent must never blank the QR — it falls back
            // to the single address that key has always carried.
            var hosts = (obj["hosts"] as? [String] ?? [])
                .filter { !$0.isEmpty }
            if hosts.isEmpty { hosts = host.isEmpty ? [] : [host] }
            return PairingResult(
                ok: true,
                detail: "",
                code: obj["code"] as? String ?? "",
                host: host,
                hosts: hosts,
                port: port,
                expiresAt: expires,
                // Absent on an older daemon; empty means the QR carries no
                // key and the phone pairs plainly.
                homeKey: obj["home_key"] as? String ?? "",
                // Absent on an older daemon: the tick draws off, and the
                // window still opens.
                allowTyped: obj["allow_typed"] as? Bool ?? false)
        }
        Trace.log("pairing refused http=\(code)")
        let reason = ActionResult.refusalText(detail: detail)
        return PairingResult(ok: false, detail: reason)
    }

    func unpairDevice(_ deviceId: String) async -> ActionResult {
        await post(["action": "unpair_device", "device_id": deviceId])
    }

    /// Record the mailbox address for the away path. Loopback only, behind
    /// the same gate as pairing; the refusal (a non-https address) is the
    /// daemon's own sentence, shown verbatim in the relay sheet.
    func setRelay(_ url: String, pushSecret: String = "") async -> ActionResult {
        await post(["action": "set_relay", "url": url,
                    "push_secret": pushSecret])
    }

    /// The socket relay's address (`wss://…`), the mailbox address's twin:
    /// loopback only, behind the same gate; the refusal is the daemon's own
    /// sentence. A phone copies it at pairing time.
    func setRelayWS(_ url: String) async -> ActionResult {
        await post(["action": "set_relay_ws", "url": url])
    }

    /// Grant one phone an away window of `days`, or end it at `0`. Loopback
    /// only, behind the same gate as pairing; the refusal is the daemon's
    /// own sentence. The daemon may only *shorten* a running window from
    /// here — lengthening waits for the phone to be seen at home.
    func setAwayDays(_ deviceId: String, days: Int) async -> ActionResult {
        await postAny(["action": "set_away_days", "device_id": deviceId,
                       "days": days])
    }

    /// Put one side (`read` / `write`) of the bot's access in one position
    /// (`off`, `1h`, `6h`, `24h`, `forever`). Loopback only, `setAwayDays`'
    /// gate; a timed position runs from now, so the same one again is a
    /// refresh. The refusal is the daemon's own sentence.
    func setBotAccess(_ deviceId: String, side: String, mode: String) async -> ActionResult {
        await postAny(["action": "set_bot_access", "device_id": deviceId,
                       "side": side, "mode": mode])
    }

    /// Let one phone answer a permission or acknowledge a waiting agent
    /// from its lock screen, or stop. Loopback only, `setAwayDays`' gate.
    func setLockScreenActions(_ deviceId: String, enabled: Bool) async -> ActionResult {
        await postAny(["action": "set_lock_screen_actions", "device_id": deviceId,
                       "enabled": enabled])
    }

    func inboxAck(key: String, kind: String, fingerprint: String) async -> ActionResult {
        await post(["action": "inbox_ack", "key": key, "kind": kind,
                    "fingerprint": fingerprint])
    }
}
