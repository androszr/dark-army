import SwiftUI

/// Where a plan's plain half ends. The `/ship` template writes one H2 between
/// the person's zone (header list, what / how / how-you-know) and the
/// implementer's zone; the card window and the phone fold everything from
/// that line down behind a chevron. A plan with no such heading — one written
/// before the template carried it — is shown whole. Lossless: `summary +
/// detail == source` whenever `detail` is non-nil. Byte-identical on the
/// Mac (`panel/Sources/BobPanel/PlanDiagram.swift`), pinned by
/// `host/tests/test_phone_plan_fold.py`.
enum PlanSplit {
    static let marker = "## Technical detail"
    static func split(_ source: String) -> (summary: String, detail: String?) {
        let lines = source.split(separator: "\n", omittingEmptySubsequences: false)
        guard let at = lines.firstIndex(where: {
            $0.trimmingCharacters(in: .whitespaces) == marker
        }) else { return (source, nil) }
        let summary = lines[..<at].joined(separator: "\n") + "\n"
        let detail = lines[at...].joined(separator: "\n")
        return (summary, detail)
    }
}

/// Card screen, pushed from a tap on a board card.
///
/// Asks the Mac for this one card in full the moment it opens (`fetchCard`),
/// because the live frame deliberately carries only a *preview* of a card's
/// instructions — a phone living off that frame could start work it could not
/// read. The full text, the plan document the card points at, the in-place
/// editors and the plan approval all hang off that one read; Save is held,
/// saying why, until it lands, so a correction can never overwrite good
/// instructions with a shortened copy of themselves.
/// `@MainActor`, and the reason is the press: `setTool`, `setModel` and
/// every other method here starts a `Task` for `send`, and a `Task` made
/// inside a method that is not isolated runs off the main actor — so the
/// `pressed` write that draws QUEUED landed on a background thread and no
/// redraw followed until the next unrelated one (a return to the app, on
/// 21 Sep 2026). With the whole view on the main actor, every task it
/// starts inherits it, and the mark is on screen at the tap.
@MainActor
struct PhoneCardDetailView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @StateObject private var retained: PhoneCardDraftState

    init(seed: BoardCard, client: PhoneClient, retained: PhoneCardDraftState? = nil) {
        self.seed = seed
        self.client = client
        _retained = StateObject(wrappedValue: retained ?? PhoneCardDraftState())
    }

    let seed: BoardCard
    @ObservedObject var client: PhoneClient
    @Environment(\.dismiss) private var dismiss
    @StateObject private var arm = Arm()
    private var note: String {
        get { retained.note }
        nonmutating set { retained.note = newValue }
    }
    @State private var pendingSkip = false
    /// Confirm should retry `board_update` (launcher off / already has a
    /// session), not `board_dispatch`. Mac's `pendingUnplanned` with
    /// `dispatches: false`.
    @State private var pendingPlainMove = false
    /// The confirmation on screen is the *changed plan* one, not the
    /// unplanned one. A separate slot because the two refusals mean different
    /// things and the armed label must say which — confirming "Start
    /// unplanned?" on a card that has a plan would be a button lying about
    /// what pressing it agrees to.
    @State private var pendingChangedPlan = false
    /// Which control made the press in play. The queue's mark says *a*
    /// press for this card is queued, on its way or landed; this says which
    /// control wears it. Held until the mark goes — a press is queued, not
    /// awaited — except for the synchronous Save, which clears it itself.
    @State private var pressed: Pressed?
    /// The receipt whose refusal this screen last acted on, so a plan-gate
    /// refusal arms Start once per press and not on every redraw.
    @State private var armedForNote = ""
    /// A confirmed Delete is in the queue. Written down is not landed: the
    /// screen stays while the press is in play, so a refusal from the Mac's
    /// re-check has a screen to land on, and pops once the board stops
    /// listing the card or the mark leaves with nothing said against it.
    @State private var deleteQueued = false
    /// This card as the Mac holds it, with its plan. Nil until the read
    /// lands, and on an older Mac that 404s the `card` kind it stays nil for
    /// ever — which is exactly what holds Save and hides Approve.
    private var cardFull: CardFull? {
        get { retained.cardFull }
        nonmutating set { retained.cardFull = newValue }
    }
    /// The three fields this screen may correct, seeded once per card.
    private var draftTitle: String {
        get { retained.draftTitle }
        nonmutating set { retained.draftTitle = newValue }
    }
    private var draftSummary: String {
        get { retained.draftSummary }
        nonmutating set { retained.draftSummary = newValue }
    }
    private var draftPrompt: String {
        get { retained.draftPrompt }
        nonmutating set { retained.draftPrompt = newValue }
    }
    /// The importance number, as typed: `""` is "no opinion", `"0"` is the
    /// lowest score, and the two are different. A string, never an `Int`,
    /// because the Mac's `_board_fields` turns a JSON `0` into `""`.
    private var draftArea: String {
        get { retained.draftArea }
        nonmutating set { retained.draftArea = newValue }
    }
    private var draftPriority: String {
        get { retained.draftPriority }
        nonmutating set { retained.draftPriority = newValue }
    }
    /// Which card id the drafts above belong to, and whether anybody has
    /// typed into them. Both guard the re-seed when the full read lands: the
    /// arriving text must never overwrite what a person is in the middle of
    /// writing, and must never land on a different card's screen.
    private var draftFor: String {
        get { retained.draftFor }
        nonmutating set { retained.draftFor = newValue }
    }
    private var draftTouched: Bool {
        get { retained.draftTouched }
        nonmutating set {
            // The first keystroke pins the revision the text was typed
            // against; a restored draft arrives with it already pinned
            // (`PhonePlace.CardDraft.baseRevision`). Untouched, nothing is.
            if newValue, !retained.draftTouched, retained.editRevision == nil {
                retained.editRevision = seedRevision
            }
            if !newValue { retained.editRevision = nil }
            retained.draftTouched = newValue
        }
    }
    private var editing: Bool {
        get { retained.editing }
        nonmutating set { retained.editing = newValue }
    }
    /// The Mac's own copy, reported inside a `CARD_CHANGED_REFUSAL`. While
    /// this is set the editors keep every typed word and the screen draws
    /// both versions with two plainly labelled ways out. There is no third,
    /// silent path.
    private var conflict: CardStated? {
        get { retained.conflict }
        nonmutating set { retained.conflict = newValue }
    }
    /// The cached plan document, read from disk **once** per card rather
    /// than from the `plan` computed property. `draftPrompt` is retained view state, so
    /// evaluating a 64 KiB `Data(contentsOf:)` in the view body was one file
    /// read per keystroke while editing a card out of reach — which is
    /// precisely the situation this whole feature exists for.
    private var cachedPlanText: String? {
        get { retained.cachedPlanText }
        nonmutating set { retained.cachedPlanText = newValue }
    }
    /// Whether the plan's technical half is unfolded. View state only:
    /// never cached, never sent, reset per card in `loadFull`.
    private var planDetailOpen: Bool {
        get { retained.planDetailOpen }
        nonmutating set { retained.planDetailOpen = newValue }
    }
    /// Whether the Send-a-message field is disclosed, and what is in it. The
    /// button *is* the deliberate gesture — no arm-then-confirm, which on
    /// this screen guards only the acts that destroy something.
    private var messageOpen: Bool {
        get { retained.messageOpen }
        nonmutating set { retained.messageOpen = newValue }
    }
    private var messageText: String {
        get { retained.messageText }
        nonmutating set { retained.messageText = newValue }
    }
    /// Whether a message has been written into the queue from this screen
    /// — its own slot rather than `note` because `note` is the orange
    /// refusal line. The phone carries no card conversation, so without
    /// this a press leaves no positive evidence at all that the keystrokes
    /// reached a terminal. Set on the press; what is drawn is `sendLine`,
    /// read off the receipt, never a stored word.
    private var sendNote: String {
        get { retained.sendNote }
        nonmutating set { retained.sendNote = newValue }
    }

    /// The message box's confirmation, read off the message's own receipt
    /// each draw so it moves with the press — the queue's line while it is
    /// queued or going out, "Sent." once it landed or aged off — and says
    /// nothing while the Mac's words about it are the note. A stored
    /// "queued" that never changed was the reported bug.
    private var sendLine: String {
        guard !sendNote.isEmpty else { return "" }
        switch client.queuedState(action: PhoneActions.boardMessage,
                                  for: card.id) {
        case .queued: return ReceiptLedger.queuedLine
        case .sending: return ReceiptLedger.sendingLine
        case .sent, .accepted: return ReceiptLedger.sentLine
        case .stuck, .refused: return ""
        case .done, nil: return "Sent."
        }
    }
    /// The tick as this phone has just set it, until the Mac's own board
    /// says the same thing. A one-field toggle is answered by the next
    /// ordinary check-in — up to a whole poll cycle away, and through the
    /// relay longer still — and a checkbox that does not move under the
    /// thumb reads as a press that missed. Set on the press, cleared when a
    /// board frame agrees or when the Mac refuses. View state only: never
    /// sent, never cached, and the card is still the authority.
    @State private var pendingStartWhenPlanned: Bool?
    /// The note typed under Passed / Failed on a card flagged with a check
    /// file, and which of the two the arm is holding. View state only.
    @State private var manualNote = ""
    @State private var manualOutcomeArmed = ""
    /// The assistant and the model the person just tapped, held on screen
    /// as the selection while the press is on its way. The same idiom as
    /// the tick above and for the same reason: the receipt for either
    /// pick lasts until a board frame names the value asked for, which
    /// through the relay is long enough that a tile still drawn on the
    /// Mac's old value read as a tap that missed — and a person tapped
    /// again, or could not say which one they had chosen. Cleared when
    /// the mark leaves (landed or refused), on a refusal's note, and
    /// when another card comes behind the same view. View state only.
    @State private var pendingTool: String?
    @State private var pendingModel: String?
    /// Which folded sections the person has opened on *this* screen. Plain
    /// screen state, deliberately **not** on `PhoneCardDraftState`, which the
    /// router keeps per card across opens: `CardSections` decides what is
    /// open by default per stage and this holds only the presses on top of
    /// it, cleared on a card swap and on a stage change so every open starts
    /// back at the rule. Never cached, never sent.
    @State private var openedSections: Set<CardSections.Section> = []

    /// The acting controls on this screen, one slot each.
    private enum Pressed {
        case start, startHere, done, refine, delete, move, tool, model
        case save, approve, message, startWhenPlanned
        case manualClear, manualOutcome, review, promote, dependencies
        case merge, mergeFix, reviewRun
    }

    /// The CHANGES page (`card_changes`), fetched when its fold row opens and
    /// held for this screen's life; the one file whose changes are open, by
    /// its index in that page and the tip the page was read at. View state
    /// only: on no card, in no cache, never read by the poll.
    @State private var changes: CardChangesReport?
    @State private var changesFailed = false
    @State private var openChangeFile: Int?
    @State private var changeDiff: CardChangeDiff?

    private var board: Board { client.snapshot.board }

    /// This card as the phone itself holds it. The complete instructions and
    /// the complete plan, kept on disk by `CardCacheStore`, so a card opened
    /// out of reach reads exactly as it does at the desk.
    private var cached: CachedCard? { client.cardCache.card(seed.id) }

    private var card: BoardCard {
        board.cards.first { $0.id == seed.id } ?? seed
    }

    /// Whether `card` is the live frame's copy rather than the `seed`
    /// fallback. The two acknowledgments echo what the Mac is showing
    /// *now*, so they draw only off a live card: a reviewed Done card
    /// leaves a `done=review` frame the moment `reviewed_at` lands and
    /// would otherwise fall back to a seed that still reads unreviewed,
    /// re-offering Mark reviewed until the Done archive is refetched.
    private var cardIsLive: Bool {
        board.cards.contains { $0.id == seed.id }
    }

    /// What the tick should draw: this phone's un-landed press where there
    /// is one, else the card.
    private var startWhenPlannedShown: Bool {
        pendingStartWhenPlanned ?? card.startWhenPlanned
    }

    private var isBusy: Bool {
        card.linkState == "dispatching" || card.linkState == "live"
            || card.refineState == "dispatching" || card.refineState == "live"
    }

    /// The word the pressed control wears — QUEUED, SENDING…, SENT — while
    /// its press is in play; `SENDING…` for the one synchronous press
    /// (Save) that is still awaited.
    private var mark: String { client.queueMark(for: card.id) ?? "SENDING…" }

    /// The same word as a screen reader says it — "Queued", "Sending",
    /// "Sent" — never "Sending" for a control drawn QUEUED.
    private var spokenMark: String { Receipt.spoken(mark: mark) }

    /// A confirmed Delete for this card, from the instant it goes out until
    /// the board stops listing the card. The board list's own predicate, so
    /// this screen and the row behind it cannot disagree about whether a
    /// card is on its way out. A superset of what this read used to be —
    /// `outAction != nil` below already covered the in-flight half.
    private var settlingHere: Bool {
        client.cardLeaving(card.id)
    }

    /// The card is on its way out — a confirmed Delete the Mac accepted,
    /// until the board stops listing it. Every acting control dims with
    /// it, and with **nothing else**: a press is queued, not awaited, so
    /// the controls are the person's again the moment a press is written
    /// down, and Approve then Start go in two quick taps. A new name rather
    /// than `isBusy`, which already means "the card's session is live".
    private var sending: Bool { settlingHere }

    /// The one seam every acting control sends through. Disarms first, so a
    /// stale confirmation cannot fire against a card the Mac has just
    /// changed; every caller captures what it needs before confirming.
    ///
    /// Two routes. A press whose *reply body* this screen reads — the
    /// guarded Save (`expected_revision`, whose 409 carries the Mac's copy;
    /// Prepare, the composer's, is the other and is never sent from here)
    /// — stays on the synchronous `post`, `refreshAfter` passed straight
    /// through. Every other verb is written into the queue and comes back
    /// at once; the Mac's own later words arrive through `queueNote(for:)`.
    private func send(_ action: String, _ fields: [String: String],
                      as slot: Pressed,
                      refreshAfter: Bool = true) async -> PhoneActionResult {
        arm.disarm()
        pressed = slot
        let scope = card.id
        if fields["expected_revision"] != nil {
            defer { pressed = nil }
            return await client.post(action: action, fields: fields,
                                     scope: scope, refreshAfter: refreshAfter)
        }
        let result = await client.enqueue(action: action, fields: fields,
                                          scope: scope)
        // A refused enqueue — the duplicate or the Face ID sentence — never
        // entered the queue, so there is no mark to wait for; nor is there
        // one if the press already left it.
        if !result.ok || client.queueMark(for: scope) == nil { pressed = nil }
        return result
    }

    /// The Board tab's swipe reads this same rule, so it lives in
    /// `PhoneCardSwipe.canStart` (`CardSwipe.swift`) and both surfaces call
    /// the one body.
    private var canStart: Bool {
        PhoneCardSwipe.canStart(card: card, dispatchEnabled: board.dispatchEnabled)
    }

    /// START HERE: spawn Dark Army's own terminal for a card that has none
    /// connected. Absent when Start already does that, or against a Mac
    /// that ignores the flag.
    private var canSpawnHere: Bool {
        canStart
            && board.ownTerminalSpawnSupported
            && !board.ownTerminalEnabled
    }

    /// The Board tab's tick rule is this button's rule: one function, so a
    /// card the Prep row lets you tick for a batch is exactly a card whose
    /// own screen offers Refine (`PhoneRowSelection.tickable`).
    private var canRefine: Bool {
        PhoneRowSelection.tickable("prep", card: card,
                                   dispatchEnabled: board.dispatchEnabled)
    }

    /// Same as the Mac's `doneClears`: a live (or dispatching) arrival in
    /// Done disposes the bound tab. Codex never arms — Dark Army cannot type there.
    private var doneClears: Bool {
        guard card.tool != "codex" else { return false }
        return card.linkState == "dispatching"
            || (card.linkState == "live" && !card.sessionId.isEmpty)
    }

    private var startsUnplanned: Bool { card.planPath.isEmpty && !card.isScout }

    /// Whether the assistant (and so the model) is still the person's to
    /// choose. The Mac's `canRetool` — no bound session, not dispatching —
    /// plus the Done exclusion: a finished card states what ran, it does not
    /// offer to change it.
    private var canRetool: Bool {
        card.sessionId.isEmpty
            && card.linkState != "dispatching"
            && card.column != "done"
    }

    /// The card's own column decides the lead order through the phone
    /// sheet's rule table, the same rule that opens it at half height.
    private var statusFirst: Bool { PhoneSheet.answers(.card, card.column) }

    /// The portrait, the card's name, the SCOUT tag, the summary, the meta
    /// line and the assistant row: drawn first on most cards, under the
    /// pinned sections on a card answered from half height.
    @ViewBuilder private var identityLead: some View {
        if let face = sheetFace {
            PixelMark(character: face.character, state: face.state,
                      size: PixelMark.sheetSize)
                .frame(maxWidth: .infinity)
        }
        Text(card.title.isEmpty ? "untitled" : card.title)
            .font(Theme.prose(20, weight: .semibold))
            .foregroundStyle(Theme.text)
        if card.isScout {
            Text("SCOUT")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.phosphor)
                .padding(.horizontal, 5).padding(.vertical, 2)
                .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
        }
        // The summary in full — `fullSummary` prefers the fetched
        // row, then the phone's own copy, over the frame's, which
        // the Mac clips at `BOARD_SNAPSHOT_SUMMARY_CHARS` with no
        // ellipsis. Drawn here once; Instructions carries the
        // prompt alone.
        if !fullSummary.isEmpty {
            Text(fullSummary)
                .font(Theme.prose(16))
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        if !metaLine.isEmpty {
            Text(metaLine)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        }
        if canRetool {
            assistantPicker
        } else if !card.tool.isEmpty {
            assistantRecord
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                // The lead first — the portrait and the card's name, its
                // summary, the meta line and the assistant row — then where
                // it stands and the one next action, then its sections in
                // the order `CardSections` gives this stage: one MORE row
                // with everything else behind it, Delete last. A card in In
                // progress is answered from half height, so its lead sits
                // under the pinned sections instead (`statusFirst`, the
                // sheet table's own rule): the status and the message box
                // are the first screen. The order follows the column, never
                // the detent, so nothing reshuffles under a drag. The
                // refusal lines stay under the list whatever the stage.
                if !statusFirst {
                    identityLead
                }
                ForEach(CardSections.order(for: stage), id: \.self) { s in
                    foldedSection(s)
                    // Under the status and its run figures, above every
                    // fold: the one press that ends a finished run.
                    if s == .status, showsDoneClose {
                        doneCloseButton
                    }
                    // After the last pinned section (WAITS ON, in every
                    // order), above MORE: the lead of a card being
                    // answered from half height.
                    if s == .dependencies, statusFirst {
                        identityLead
                    }
                }
                if !note.isEmpty {
                    Text(note)
                        .font(Theme.mono(12))
                        .foregroundStyle(.orange)
                }
                if let notice = client.boardNotices[card.id], !notice.isEmpty {
                    Text(notice)
                        .font(Theme.mono(12))
                        .foregroundStyle(.orange)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        // An inline bar title ellipsises and cannot wrap, so the bar says
        // what kind of screen this is. The card's whole title is drawn
        // above, in the body, and always has been.

        .decryptSurface("CardDetailView")
        .onAppear {
            arm.onDisarm = {
                pendingSkip = false
                pendingPlainMove = false
                pendingChangedPlan = false
            }
            // A refusal that landed while this screen was not up — the
            // plan gate's words included — is read on arrival, not only on
            // a change seen while drawn; `Arm`'s window would otherwise
            // have run out unseen.
            noteArrived(client.queueNote(for: card.id))
        }
        .onDisappear { arm.disarm() }
        // The Mac has answered: the card is the authority again. Also fires
        // for a change somebody made at the desk, which is the right answer
        // too — the board wins, and the tick stops holding a value nobody
        // is waiting on.
        .onChange(of: client.queueMark(for: card.id)) { _, mark in
            // The press left the queue — landed or refused — so the
            // pressed control goes back to its own words.
            if mark == nil && pressed != .save { pressed = nil }
            if mark == nil {
                pendingTool = nil
                pendingModel = nil
                leaveDeletedCard()
            }
        }
        .onChange(of: cardIsLive) { _, _ in leaveDeletedCard() }
        .onChange(of: client.queueNote(for: card.id)) { _, queued in
            noteArrived(queued)
        }
        .onChange(of: card.startWhenPlanned) { _, landed in
            if pendingStartWhenPlanned == landed { pendingStartWhenPlanned = nil }
        }
        // A different card behind the same view identity keeps none of the
        // last one's un-landed press.
        .onChange(of: seed.id) { _, _ in
            pendingStartWhenPlanned = nil
            pendingTool = nil
            pendingModel = nil
            openedSections = []
            changes = nil
            changesFailed = false
            openChangeFile = nil
            changeDiff = nil
        }
        // A card that moves column while its screen is up starts back at the
        // rule for where it is now.
        .onChange(of: stage) { _, _ in openedSections = [] }
        .task(id: seed.id) { await loadFull() }
        // The branch against the main line, read when CHANGES opens and
        // again only when a merge or a review moves the card — never from
        // the poll, `backgroundRefresh` or the widget.
        .task(id: changesFetchKey) { await loadChanges() }
    }

    // MARK: - Which sections lead, and which fold

    /// Where this card is in its life, off the snapshot's copy.
    private var stage: CardSections.Stage {
        CardSections.stage(column: card.column, linkState: card.linkState,
                           manualCheckDue: card.manualCheckDue,
                           runActive: card.runActive)
    }

    /// What a closed row may say, all of it already on the card.
    private var foldFacts: CardSections.Facts {
        var f = CardSections.Facts()
        if let head = card.workRecord {
            f.files = head.files
            f.filesTotal = head.filesTotal
            f.filesAvailable = head.filesAvailable
            f.hasReport = head.report
        }
        f.planAttached = !card.planPath.isEmpty
        f.planApproved = !card.planApproved.isEmpty
        f.planMissing = plan.map { !$0.available } ?? false
        f.reportAttached = !card.reportPath.isEmpty
        f.isScout = card.isScout
        f.manualNote = !card.manualSteps.isEmpty
        f.crewStages = Specialists.parse(card.workflow).count
        if let report = cardFull?.timeline, let first = report.steps.first,
           let at = first.at {
            f.age = CardTimeline.elapsed(report.generatedAt - at)
        }
        f.hidden = CardSections.hidden(for: stage).filter { hasContent($0) }.count
        return f
    }

    /// Whether a section has anything to draw on this card — each section's
    /// own presence gate, restated so a fold row is never a chevron that
    /// opens nothing. The phone draws no run record, no documents list and no
    /// attachments, and its session is the OPEN TERMINAL line under status.
    private func hasContent(_ s: CardSections.Section) -> Bool {
        switch s {
        case .status: return true
        case .manualCheck: return !card.manualSteps.isEmpty
        case .closeSignature:
            return card.column == "done" && !card.closeNote.isEmpty
        // Every scout: the report once it is attached, and until then the
        // line saying the scout is still out.
        case .report: return card.isScout
        case .objective: return board.outcomesSupported
        // DONE & CLOSE stands above the folds in the band's place, so the
        // band does not draw a second Done under it.
        case .verbs: return (nextAction != .none && !(nextAction == .done && showsDoneClose)) || armedOffScreen
        case .otherVerbs:
            return canSpawnHere || arm.startHere != nil
                || (canRefine && board.startWhenPlannedSupported)
                || (canStart && nextAction != .start)
                || (arm.done != nil && nextAction != .done)
                || mergeControlsShown
        case .collaboration: return client.snapshot.collaboration != nil
        case .more: return CardSections.hidden(for: stage).contains { hasContent($0) }
        case .plan: return !card.planPath.isEmpty && plan != nil
        case .workRecord: return card.workRecord != nil
        case .changes:
            return cardIsLive && board.cardChangesSupported
                && !card.worktreeBranch.isEmpty
                && (stage == .done || stage == .ended)
        case .run, .documents, .attachments, .session: return false
        case .editor: return true
        case .instructions: return !fullPrompt.isEmpty
        case .crew:
            return !Specialists.parse(card.workflow).isEmpty
                || !Specialists.parse(card.agentTrail).isEmpty
        // The band draws the box while the card is being worked
        // (`messageSection`'s own gate), so the row would open on nothing.
        case .thread: return canMessageSession && nextAction != .reply
        case .queue: return card.queueState == "queued"
        // The rows and the Unblocks line; the Add picker lives under EDIT
        // CARD, so a card with no links draws no empty section here.
        case .dependencies:
            return !card.dependencies.isEmpty || !card.dependents.isEmpty
        case .timeline: return board.cardTimelineSupported && cardFull?.timeline != nil
        case .danger: return true
        }
    }

    /// The fold row and, when open, the section under it. A section the rule
    /// opens — pinned or a lead for this stage — draws its body with no row:
    /// a chevron is a promise that a press folds something, and
    /// `CardSections.isOpen` keeps those open whatever is pressed. Everything
    /// else is a labelled row behind the one MORE row (`CardSections.shown`);
    /// MORE's own open state rides `openedSections` like any section's, so
    /// the card-swap and stage-change resets cover it.
    @ViewBuilder
    private func foldedSection(_ s: CardSections.Section) -> some View {
        if hasContent(s) {
            if CardSections.isOpen(s, stage: stage, opened: []) {
                section(s)
            } else if CardSections.shown(s, stage: stage, opened: openedSections) {
                let open = CardSections.isOpen(s, stage: stage, opened: openedSections)
                PhoneCardFoldRow(label: CardSections.rowText(s, facts: foldFacts),
                                 open: open) {
                    if open {
                        openedSections.remove(s)
                    } else {
                        openedSections.insert(s)
                    }
                }
                if open, s != .more {
                    section(s)
                }
            }
        }
    }

    /// One section's body; every piece keeps its own gate.
    @ViewBuilder
    private func section(_ s: CardSections.Section) -> some View {
        switch s {
        case .status: statusSection
        case .manualCheck: manualCheckSection
        case .closeSignature: closeSignatureSection
        case .objective:
            if board.outcomesSupported {
                PhoneOutcomeCard(client: client, card: card).id(card.id)
            }
        case .verbs: verbs
        case .otherVerbs: otherVerbs
        case .plan: planSection
        case .report: reportSection
        case .workRecord:
            PhoneWorkRecordSection(client: client, card: card)
                .id(card.id)
        case .changes: changesSection
        case .run, .documents, .attachments, .session: EmptyView()
        case .editor:
            Rectangle().fill(Theme.hair).frame(height: 1)
            columnPicker
            // The assistant row is in the lead now, above the sections.
            if canRetool, !board.modelOptions(for: card.tool).isEmpty {
                modelPicker
            }
            editorSection
            dependencyPicker
        case .instructions: instructionsSection
        case .crew: crewSection
        case .thread: messageSection
        case .queue:
            Text(card.queueReason.isEmpty ? "queued" : card.queueReason)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        case .dependencies: dependenciesSection
        case .timeline: timelineSection
        case .danger: dangerSection
        case .collaboration:
            CollaborationView(client: client, focus: .card(card.id),
                              initiallyExpanded: true)
        case .more: EmptyView()
        }
    }

    // MARK: - Waits on

    /// The cards this one waits on, one row each — the title, the Mac's met
    /// bit as a word, and ✕ to stop waiting on it where this Mac takes the
    /// write — then the Mac's Unblocks line, verbatim.
    @ViewBuilder
    private var dependenciesSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(card.dependencies) { dep in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(dep.title.isEmpty ? "untitled" : dep.title)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                    Text(Self.dependencyWord(dep))
                        .font(Theme.mono(11))
                        .foregroundStyle(dep.met ? Theme.phosphor : Theme.dim)
                    Spacer(minLength: 8)
                    if board.dependenciesSupported {
                        DecryptButton {
                            setDependencies(card.linkedIds.filter { $0 != dep.id })
                        } label: {
                            Text("\u{2715}")
                                .font(Theme.mono(13))
                                .frame(minWidth: 44, minHeight: 44)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(Theme.phosphor)
                        .disabled(sending)
                        .accessibilityLabel("Stop waiting on \(dep.title.isEmpty ? "untitled" : dep.title)")
                    }
                }
            }
            if !card.dependentsLine.isEmpty {
                Text(card.dependentsLine)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
        }
    }

    /// **Add…** under EDIT CARD: this project's other cards, none in Done,
    /// none already listed, and nothing once the list holds the store's
    /// eight. Drawn only where this Mac takes the write
    /// (`dependencies_supported`), so an older Mac never drops a list at
    /// its door while the screen says it was saved.
    @ViewBuilder
    private var dependencyPicker: some View {
        let choices = dependencyChoices
        if board.dependenciesSupported, card.column != "done", !choices.isEmpty {
            Menu {
                ForEach(choices) { other in
                    DecryptButton(other.title.isEmpty ? "untitled" : other.title) {
                        setDependencies(card.linkedIds + [other.id])
                    }
                }
            } label: {
                // The press's mark while a link is on its way, the column
                // and model choosers' relabel rule.
                Text(pressed == .dependencies ? mark : "WAITS ON \u{00b7} Add\u{2026}")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphor)
                    .frame(minHeight: 44, alignment: .leading)
            }
            .disabled(sending)
            .accessibilityLabel("Add a card this one waits on")
        }
    }

    private var dependencyChoices: [BoardCard] {
        BoardCard.dependencyChoices(for: card, in: board.cards)
    }

    /// The Mac's met bit in words — the three `dependency_line` uses. `met`
    /// is never re-derived here; the column only says *why*.
    static func dependencyWord(_ dep: CardDependency) -> String {
        if dep.column == "done" { return "done" }
        return dep.met ? "check pending" : "not yet"
    }

    /// Write the whole list, guarded at the revision of the copy on screen,
    /// so a card changed underneath is refused in the Mac's words rather
    /// than overwritten. The Mac refuses a loop, a self-wait and another
    /// project in words, drawn in the note.
    private func setDependencies(_ ids: [String]) {
        let live = card
        Task {
            let result = await send(PhoneActions.boardUpdate,
                                    ["card_id": live.id,
                                     "blocked_by": ids.joined(separator: "\n"),
                                     "expected_revision": String(live.revision)],
                                    as: .dependencies)
            apply(result)
        }
    }

    // MARK: - Timeline

    /// The moments in the card's life as the Mac composed them, off the one
    /// `card` read this screen already makes on open — no second request,
    /// nothing on the poll. Each row is one spoken string; the decoration is
    /// hidden from VoiceOver; nothing wraps under a `.lineLimit`. The last
    /// line says where the card is now, formatted once against the report's
    /// own clock.
    @ViewBuilder
    private var timelineSection: some View {
        if let report = cardFull?.timeline {
            VStack(alignment: .leading, spacing: 8) {
                if !report.available {
                    Text(report.reason.isEmpty ? "the timeline could not be read"
                                               : report.reason)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.amber)
                } else {
                    let rows = CardTimeline.rows(report)
                    if rows.isEmpty {
                        Text("nothing recorded yet")
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                    }
                    ForEach(rows) { row in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(row.when)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(row.when == CardTimeline.notObserved
                                                     ? Theme.dim : Theme.phosphor)
                                Spacer(minLength: 8)
                                if let gap = row.gap, !gap.isEmpty {
                                    Text(gap)
                                        .font(Theme.mono(11))
                                        .foregroundStyle(Theme.dim)
                                }
                            }
                            Text(row.label)
                                .font(Theme.mono(13, weight: row.durable ? .medium : .regular))
                                .foregroundStyle(Theme.phosphor)
                            if !row.note.isEmpty {
                                Text(row.note)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.dim)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel(timelineSpoken(row))
                    }
                    if report.truncated {
                        Text("older moments not listed")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                    }
                    if !report.recentOnlyNote.isEmpty {
                        Text(report.recentOnlyNote)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                    }
                    if let line = CardTimeline.openLine(report, now: report.generatedAt) {
                        Rectangle().fill(Theme.hair).frame(height: 1)
                            .accessibilityHidden(true)
                        Text(line)
                            .font(Theme.mono(13, weight: .medium))
                            .foregroundStyle(Theme.phosphor)
                    }
                }
            }
        }
    }

    private func timelineSpoken(_ row: CardTimeline.Row) -> String {
        var parts = [row.label]
        if !row.note.isEmpty { parts.append(row.note) }
        parts.append(row.when)
        if let gap = row.gap, !gap.isEmpty { parts.append("after " + gap) }
        return parts.joined(separator: ", ")
    }

    /// Where the card stands, in a line or two: the shared sentence for a
    /// card waiting on a check, the starting / refining caret, the way
    /// through to a hosted terminal, and a dispatch refusal. Absent where
    /// there is nothing to say.
    @ViewBuilder
    private var statusSection: some View {
        Text(stage == .manualCheck ? CardSections.statusLine
                                   : CardSections.stateWords(
                                        for: stage, linked: !card.linkState.isEmpty))
            .font(Theme.mono(13, weight: .medium))
            .foregroundStyle(stage == .manualCheck ? Theme.amber : Theme.phosphor)
            .fixedSize(horizontal: false, vertical: true)
        // Its place in a batch — the Mac's line, the tile's words.
        if !card.batchLine.isEmpty {
            Text(card.batchLine)
                .font(Theme.mono(12))
                .foregroundStyle(card.isBatchWaiting ? Theme.faint : Theme.phosphor)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(card.batchLine)
        }
        // What the last MERGE press came to and the review's verdict, the
        // Mac's words verbatim (`merge_line`, `CardMerge.verdictLine`) —
        // drawn only against a Mac that publishes them, amber where it
        // needs you.
        if board.mergeWritable, !card.mergeLine.isEmpty {
            Text(card.mergeLine)
                .font(Theme.mono(12))
                .foregroundStyle(["blocked", "conflict", "checks_failed"]
                    .contains(card.mergeState) ? Theme.amber : Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(card.mergeLine)
        }
        if board.reviewRunWritable, let verdict = CardMerge.verdictLine(
            verdict: card.reviewVerdict, running: card.reviewRunning,
            current: changes?.review.current ?? true) {
            Text(verdict)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
        liveState
        // How the run is going — `RunHealthLine.text`, byte-equal with the
        // panel's, the Mac's own reading drawn verbatim with the size word
        // first. Drawn only where this Mac says it publishes the reading
        // (`runHealthSupported`): against an older Mac the line is absent,
        // never broken. Amber on the Mac's `attention` bit, never colour
        // alone. Wraps; the phone clips no prose.
        if board.runHealthSupported, let health = card.runHealth {
            Text(RunHealthLine.text(health))
                .font(Theme.mono(12))
                .foregroundStyle(health.attention ? Theme.amber : Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(RunHealthLine.spoken(health))
        }
        terminalLink
        // The Mac's cost-and-time line (`RunFigures.line`, byte-equal with
        // the panel's), drawn only where this Mac says it publishes the
        // figures — an older one sends no marker and the line is absent,
        // never blank. Wraps; the phone clips no prose.
        if board.runFiguresSupported, let figures = RunFigures.line(card.runFigures) {
            Text(figures)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(figures)
        }
        if !card.dispatchError.isEmpty {
            Text(card.dispatchError)
                .font(Theme.mono(12))
                .foregroundStyle(.orange)
        }
    }

    /// The steps the assistant left, verbatim. The badge says whether the
    /// check is *due* — the daemon's word — while the steps are drawn
    /// whenever there are any, so a stale note can still be read. Under
    /// them, where this Mac honours the verb from a phone
    /// (`PhoneCardAck.showsManualClear`), **Mark checked**: armed then
    /// confirmed, the Mac's own words on the button, and the steps that
    /// were on screen echoed back so a stale picture clears nothing else.
    @ViewBuilder
    private var manualCheckSection: some View {
        if !card.manualSteps.isEmpty {
            if card.manualCheckDue {
                Text("MANUAL CHECK NEEDED")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .tracking(0.8)
            }
            MarkdownText(source: card.manualSteps, base: 13, mono: true)
                .equatable()
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            if !card.manualCheckPath.isEmpty && !manualCheckRefused {
                manualCheckFile
            } else {
                if manualCheckRefused {
                    Text(manualCheckRefusal)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // The band draws the press on the manual-check stage; this
                // copy is for a stale note read at any other stage.
                if nextAction != .markChecked {
                    manualClearButton
                }
            }
        }
    }

    /// The check file under the steps, off the on-open read (`reportSection`'s
    /// branches), then — where this Mac takes the press from a phone —
    /// **Passed** / **Failed**, each armed then confirmed, with a note. The
    /// Mac writes the outcome into the file and clears the card's steps.
    @ViewBuilder
    private var manualCheckFile: some View {
        if let check = cardFull?.manualCheck, fetched != nil {
            if check.available {
                MarkdownText(source: check.text, base: 12, mono: true)
                    .equatable()
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(check.reason.isEmpty
                     ? "that check could not be read" : check.reason)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
        } else {
            Text(fetched == nil ? "the check is on the Mac — fetching"
                                : "this Mac does not send the check yet")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        }
        Text(card.manualCheckPath)
            .font(Theme.mono(11))
            .foregroundStyle(Theme.faint)
            .textSelection(.enabled)
        if cardIsLive && PhoneCardAck.showsManualOutcome(card: card, board: board) {
            TextField("note (optional)", text: $manualNote, axis: .vertical)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphor)
                .padding(8)
                .background(Theme.well)
                .hidesKeyboard()
                .onChange(of: manualNote) { _, value in
                    if value.count > ManualCheckRules.noteLimit {
                        manualNote = String(value.prefix(ManualCheckRules.noteLimit))
                    }
                }
            HStack(spacing: 12) {
                DecryptButton(manualOutcomeLabel("passed")) {
                    pressManualOutcome("passed")
                }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                DecryptButton(manualOutcomeLabel("failed")) {
                    pressManualOutcome("failed")
                }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
            }
        }
    }

    private func manualOutcomeLabel(_ status: String) -> String {
        let word = status == "passed" ? "Passed" : "Failed"
        if pressed == .manualOutcome && manualOutcomeArmed == status { return mark }
        return arm.manualOutcome != nil && manualOutcomeArmed == status
            ? word + "?" : word
    }

    /// Arm, then send — `pressManualClear`'s discipline: the path comes off
    /// the live card before `confirm`, and the Mac re-checks that the file
    /// still says open.
    private func pressManualOutcome(_ status: String) {
        let fields = PhoneCardAck.manualOutcomeFields(
            card, status: status, note: manualNote)
        if manualOutcomeArmed == status
            && arm.confirm(.manualOutcome, id: card.id) {
            Task {
                let result = await send(PhoneActions.boardManualOutcome, fields,
                                        as: .manualOutcome)
                if result.ok { manualNote = "" }
                apply(result)
            }
        } else {
            manualOutcomeArmed = status
            arm.arm(.manualOutcome, id: card.id)
        }
    }

    /// The Mac's on-open read says it will not serve this card's check file
    /// any more, so Passed / Failed would be refused: Mark checked comes
    /// back in their place.
    private var manualCheckRefused: Bool {
        fetched != nil && cardFull?.manualCheck?.available == false
    }

    private var manualCheckRefusal: String {
        let reason = cardFull?.manualCheck?.reason ?? ""
        return reason.isEmpty ? "the check file cannot be read"
                              : "the check file cannot be read — \(reason)"
    }

    /// Mark checked: a card flagged with steps alone, or with a check file
    /// the Mac will no longer serve.
    private var offersManualClear: Bool {
        PhoneCardAck.showsManualClear(card: card, board: board)
            || (manualCheckRefused
                && PhoneCardAck.showsManualClearOverRefusedFile(card: card, board: board))
    }

    @ViewBuilder
    private var manualClearButton: some View {
        if cardIsLive && offersManualClear {
            DecryptButton(manualClearLabel) { pressManualClear() }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                .accessibilityLabel(pressed == .manualClear
                    ? spokenMark
                    : (arm.manualClear != nil
                        ? "Mark checked, press again to confirm"
                        : "Mark checked"))
        }
    }

    private var manualClearLabel: String {
        if pressed == .manualClear { return mark }
        return arm.manualClear != nil ? "Mark checked?" : "Mark checked"
    }

    /// Arm, then send. The echo is captured from the **live snapshot card**
    /// before `confirm` — never the cache, never the seed — so what goes to
    /// the Mac is what the person was looking at when they pressed twice;
    /// the store's WHERE is the answer if even that had moved on.
    private func pressManualClear() {
        let fields = PhoneCardAck.manualClearFields(card)
        if arm.confirm(.manualClear, id: card.id) {
            Task {
                let result = await send(PhoneActions.boardManualClear, fields,
                                        as: .manualClear)
                apply(result)
            }
        } else {
            arm.arm(.manualClear, id: card.id)
        }
    }

    @ViewBuilder
    private var instructionsSection: some View {
        // The prompt alone: the summary is the lead's (`fullSummary`,
        // drawn once under the title).
        if !fullPrompt.isEmpty {
            // Prepare writes the instructions as Markdown; drawn as
            // its bytes, a card read as a list of `**` and `- `.
            MarkdownText(source: fullPrompt, base: 12, mono: true)
                .equatable()
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The assistant's close, verbatim, and under it — where this Mac
    /// honours the verb from a phone and the close is still unread
    /// (`PhoneCardAck.showsReview`) — **Mark reviewed**, armed then
    /// confirmed, echoing the close identity that was on screen. Reading a
    /// result never accepts an outcome; that decision stays on the Mac.
    @ViewBuilder
    private var closeSignatureSection: some View {
        if card.column == "done" && !card.closeNote.isEmpty {
            if !card.closedByName.isEmpty {
                Text("closed by \(card.closedByName)")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
            Text(card.closeNote)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.dim)
                .textSelection(.enabled)
            // The band draws the press on a Done card; this copy is for the
            // ended-stage row under MORE.
            if nextAction != .review {
                reviewButton
            }
        }
    }

    /// A scout's report, as a document — the whole point of a scout card,
    /// which until 21 Sep 2026 showed its report as a path in a line of
    /// text. `MarkdownText` is the plan's renderer (`Markdown.swift`, byte-
    /// pinned to the panel's). Its own section (`CardSections.Section.report`),
    /// a lead once the scout has come back. Before the attach the section
    /// says the scout is still out, so the wait between Start and the
    /// report reads as expected rather than as a miss. With a
    /// `report_path`: on the Mac's on-open read it is the text; before that
    /// read lands, or from an older Mac, the path and a line saying so.
    /// Under a Done scout's report, **Promote** — one press, unarmed as on
    /// the Mac — against a Mac that honours the verb from a phone.
    @ViewBuilder
    private var reportSection: some View {
        if card.reportPath.isEmpty {
            Text(card.column == "in_progress"
                 ? "Scouting — the report lands here when the investigation finishes."
                 : "No report yet — the scout writes one when it is started.")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        } else {
            // The verdict the Mac stored at the attach, in full above the
            // report (`ScoutVerdictLine`, byte-pinned with the Mac). An
            // older Mac sends no verdict, and nothing is drawn.
            if let verdict = ScoutVerdictLine.text(
                verdict: card.reportVerdict,
                recommendation: card.reportRecommendation) {
                Text(verdict)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(ScoutVerdictLine.spoken(
                        verdict: card.reportVerdict,
                        recommendation: card.reportRecommendation))
            }
            if let report = cardFull?.report, fetched != nil {
                if report.available {
                    MarkdownText(source: report.text, base: 12, mono: true)
                        .equatable()
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(report.reason.isEmpty
                         ? "that report could not be read" : report.reason)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                    Text(card.reportPath)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .textSelection(.enabled)
                }
            } else {
                Text(fetched == nil ? "the report is on the Mac — fetching"
                                    : "this Mac does not send the report yet")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                Text(card.reportPath)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .textSelection(.enabled)
            }
            if canPromote {
                DecryptButton(promoteLabel) { pressPromote() }
                    .buttonStyle(AlarmOutline())
                    .disabled(sending)
                    .accessibilityLabel("Promote this report to a build card")
            }
        }
    }

    /// `BoardCardSheet.canPromote`'s terms: a Done scout with a report, on a
    /// Mac that takes the verb from a phone. Live card only — a seed could
    /// still read Done after the Mac has already promoted it.
    private var canPromote: Bool {
        cardIsLive && board.promoteSupported && card.isScout
            && !card.reportPath.isEmpty && card.column == "done"
    }

    private var promoteLabel: String {
        pressed == .promote ? mark : "PROMOTE TO A BUILD CARD"
    }

    /// One press, no arm: promoting creates a card and destroys nothing,
    /// and the store refuses a second press in its own words.
    private func pressPromote() {
        Task {
            let result = await send(PhoneActions.boardPromote,
                                    ["card_id": card.id], as: .promote)
            apply(result)
        }
    }

    @ViewBuilder
    private var reviewButton: some View {
        if cardIsLive && PhoneCardAck.showsReview(card: card, board: board) {
            DecryptButton(reviewLabel) { pressReview() }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                .accessibilityLabel(pressed == .review
                    ? spokenMark
                    : (arm.review != nil
                        ? "Mark reviewed, press again to confirm"
                        : "Mark reviewed"))
        }
    }

    private var reviewLabel: String {
        if pressed == .review { return mark }
        return arm.review != nil ? "Mark reviewed?" : "Mark reviewed"
    }

    /// `pressManualClear`'s twin: the close identity is captured from the
    /// live snapshot card before `confirm`.
    private func pressReview() {
        let fields = PhoneCardAck.reviewFields(card)
        if arm.confirm(.review, id: card.id) {
            Task {
                let result = await send(PhoneActions.boardReview, fields,
                                        as: .review)
                apply(result)
            }
        } else {
            arm.arm(.review, id: card.id)
        }
    }

    // MARK: - Review and merge

    /// MERGE: the shared rule (`CardMerge`) and this Mac's marker, on a live
    /// card — a seed could still read Done after the Mac merged it. Once
    /// the CHANGES page is read, the Mac's own `merge_offered` also has its
    /// say (a Failed hand-check shows nowhere else on the card).
    private var showsMerge: Bool {
        cardIsLive && board.mergeWritable
            && CardMerge.offered(column: card.column, branch: card.worktreeBranch,
                                 mergeState: card.mergeState,
                                 manualDue: !card.manualSteps.isEmpty,
                                 daemonOffers: card.mergeOffered)
    }

    private var showsMergeFix: Bool {
        cardIsLive && board.mergeWritable
            && CardMerge.fixOffered(mergeState: card.mergeState,
                                    daemonOffers: card.mergeOffered)
    }

    private var showsReviewRun: Bool {
        cardIsLive && board.reviewRunWritable
            && CardMerge.reviewOffered(column: card.column,
                                       branch: card.worktreeBranch,
                                       reviewRunning: card.reviewRunning,
                                       manualDue: !card.manualSteps.isEmpty,
                                       daemonOffers: card.mergeOffered)
    }

    private var mergeControlsShown: Bool {
        showsMerge || showsMergeFix || showsReviewRun
            || (board.mergeWritable && card.mergeState == "merging")
    }

    /// The three presses. MERGE and Fix are armed then confirmed, each in
    /// its own `Arm` slot; Run review is one press. `queueMark` and
    /// `queueNote` speak for them as for every verb.
    @ViewBuilder
    private var mergeVerbs: some View {
        if showsMerge {
            DecryptButton(mergeLabel) { pressMerge() }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                .accessibilityLabel(pressed == .merge
                    ? spokenMark
                    : (arm.merge != nil ? "Merge into main, press again to confirm"
                                        : "Merge into main"))
        } else if board.mergeWritable && card.mergeState == "merging" {
            Text(CardMerge.mergeLabel(mergeState: "merging", armed: false))
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        }
        if showsMergeFix {
            DecryptButton(fixLabel) { pressMergeFix() }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                .accessibilityLabel(pressed == .mergeFix
                    ? spokenMark
                    : (arm.mergeFix != nil ? "Fix the merge, press again to confirm"
                                           : "Fix the merge"))
        }
        if showsReviewRun {
            DecryptButton(pressed == .reviewRun ? mark : "RUN REVIEW") { pressReviewRun() }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                .accessibilityLabel(pressed == .reviewRun ? spokenMark : "Run a review")
        }
    }

    private var mergeLabel: String {
        if pressed == .merge { return mark }
        return CardMerge.mergeLabel(mergeState: card.mergeState,
                                    armed: arm.merge != nil)
    }

    private var fixLabel: String {
        if pressed == .mergeFix { return mark }
        return CardMerge.fixLabel(armed: arm.mergeFix != nil)
    }

    /// Echoes the branch tip the CHANGES page was read at when it was read;
    /// the Mac refuses a branch that moved since, in words.
    private func pressMerge() {
        let fields = PhoneCardAck.mergeFields(card, tip: changes?.branchTip ?? "")
        if arm.confirm(.merge, id: card.id) {
            Task {
                let result = await send(PhoneActions.boardMerge, fields,
                                        as: .merge)
                apply(result)
            }
        } else {
            arm.arm(.merge, id: card.id)
        }
    }

    private func pressMergeFix() {
        if arm.confirm(.mergeFix, id: card.id) {
            Task {
                let result = await send(PhoneActions.boardMergeFix,
                                        ["card_id": card.id], as: .mergeFix)
                apply(result)
            }
        } else {
            arm.arm(.mergeFix, id: card.id)
        }
    }

    /// One press, no arm: a review changes no files and a second press is
    /// refused in words while one is running.
    private func pressReviewRun() {
        Task {
            let result = await send(PhoneActions.boardReviewRun,
                                    ["card_id": card.id], as: .reviewRun)
            apply(result)
        }
    }

    /// One request per open of the fold row and per moment the page can go
    /// stale (a merge landing, a verdict arriving). `"closed"` while the row
    /// is shut, so nothing is read for a card nobody is looking at.
    private var changesFetchKey: String {
        guard openedSections.contains(.changes), cardIsLive,
              board.cardChangesSupported else { return "closed" }
        return "\(card.id)-\(card.mergeState)-\(card.reviewVerdict)-\(card.reviewRunning)"
    }

    private func loadChanges() async {
        guard changesFetchKey != "closed" else { return }
        let wanted = card.id
        let fetched = await client.fetchCardChanges(wanted)
        guard wanted == card.id else { return }
        if let fetched {
            changes = fetched
            changesFailed = false
        } else {
            changesFailed = changes == nil
        }
        openChangeFile = nil
        changeDiff = nil
    }

    private func openChange(_ index: Int) {
        guard let tip = changes?.branchTip, !tip.isEmpty else { return }
        if openChangeFile == index {
            openChangeFile = nil
            changeDiff = nil
            return
        }
        openChangeFile = index
        changeDiff = nil
        let wanted = card.id
        Task {
            let fetched = await client.fetchCardChangeDiff(wanted, file: index,
                                                           tip: tip)
            guard wanted == card.id, openChangeFile == index else { return }
            changeDiff = fetched ?? CardChangeDiff.unreadable
        }
    }

    /// The branch against the main line: commits, then files with their
    /// counts, a file's changes on a tap. Every sentence is the Mac's; the
    /// phone counts and words nothing itself.
    @ViewBuilder
    private var changesSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let report = changes {
                if !report.available {
                    Text(report.reason)
                        .font(Theme.mono(12))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("\(report.ahead) ahead of \(report.trunk), \(report.behind) behind")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                    ForEach(report.commits) { commit in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(commit.sha8)
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.faint)
                            Text(commit.subject)
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.phosphor)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    if report.commitsTruncated {
                        Text("· more commits than are listed")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                    }
                    ForEach(Array(report.files.enumerated()), id: \.offset) { pair in
                        changeFileRow(pair.offset, pair.element)
                    }
                    if report.filesTruncated {
                        Text("· \(report.filesTotal) files changed, not all listed")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                    }
                    if !report.mergeOffered, !report.mergeRefusal.isEmpty {
                        Text(report.mergeRefusal)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            } else if changesFailed {
                Text("Dark Army could not be reached for this branch's changes.")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            } else {
                Text("Reading the branch…")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func changeFileRow(_ index: Int, _ file: CardChangeFile) -> some View {
        let counts = file.binary ? "binary" : "+\(file.added) \u{2212}\(file.removed)"
        DecryptButton(action: { openChange(index) }) {
            HStack(spacing: 8) {
                Text(file.path)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.phosphor)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .multilineTextAlignment(.leading)
                Text(counts)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            .frame(minHeight: 44)
            .accessibilityElement(children: .combine)
        }
        .buttonStyle(.plain)
        if openChangeFile == index {
            if let diff = changeDiff {
                if diff.available {
                    ScrollView(.horizontal) {
                        Text(diff.text)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.dim)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if diff.truncated {
                        Text("· only the start of this file's changes")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                    }
                } else {
                    Text(diff.reason)
                        .font(Theme.mono(12))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text("Reading that file…")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
        }
    }

    /// Delete, armed then confirmed, on its own at the very bottom — out of
    /// `verbs`, so the acting row never ends in the one press that destroys.
    @ViewBuilder
    private var dangerSection: some View {
        DecryptButton(deleteLabel) {
            if arm.confirm(.deleteCard) {
                Task {
                    let result = await send(PhoneActions.boardDelete,
                                            ["card_id": card.id],
                                            as: .delete)
                    apply(result, pop: true)
                }
            } else {
                arm.arm(.deleteCard, id: card.id)
            }
        }
        .buttonStyle(AlarmOutline(color: Theme.alarm))
        .disabled(sending)
        // The typed line under the verbs for as long as `deleteLabel`
        // reads SENDING… — the request plus the settling hold, until the
        // board stops listing the card. The label swap stays.
        if pressed == .delete || settlingHere {
            AgentChatterView(.line, wait: .deleting, seed: card.id,
                             spoken: "Deleting this card")
                .id(card.id)
        }
    }

    /// The panel's `BoardCardView.liveState`, on the phone: a blinking
    /// cursor and the panel's own caption while a session is being started
    /// for this card, or while a refinement is at work on it. The Start
    /// half reads the press *and* the daemon's `linkState`, so the cursor
    /// appears the instant the button is pressed and stays until the card
    /// binds; the Refine half is the daemon's `refineState` alone, because
    /// the card never leaves Prep for it and nothing else says it is live.
    @ViewBuilder
    private var liveState: some View {
        if pressed == .start || pressed == .startHere
            || card.linkState == "dispatching" {
            HStack(spacing: 5) {
                AgentChatterView(.caret, wait: .starting, seed: card.id,
                                 spoken: "Starting this card")
                    .id(card.id)
                Text("starting — waiting for the session to appear")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            }
        } else if card.isRefining {
            HStack(spacing: 5) {
                AgentChatterView(.caret, wait: .refining, seed: card.id,
                                 spoken: "Refining this card")
                    .id(card.id)
                Text(card.refineState == "live"
                     ? "refining — answer the interview in the terminal"
                     : "refining — waiting for the session to appear")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            }
        }
    }

    /// The live screen lives on the agent, not on this card. Drawn only
    /// when Dark Army hosts that terminal, so an editor-run session does not
    /// grow a second way through to a screen this phone cannot draw.
    @ViewBuilder
    private var terminalLink: some View {
        if let (agent, category) = client.snapshot.agents.hostedAgent(for: card) {
            DecryptButton(action: { sheets.show(.agent(agent, category)) }) {
                Text("OPEN TERMINAL")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.phosphor)
                    .frame(minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open this session's terminal")
        }
    }

    /// Project and model only: the assistant is on the switcher above and
    /// the column and link are the state line's words.
    private var metaLine: String {
        var parts: [String] = []
        if !card.project.isEmpty { parts.append(card.project) }
        if !card.model.isEmpty { parts.append(card.model) }
        return parts.joined(separator: " · ")
    }

    /// The crew with names and room to breathe — the Mac card window's block,
    /// the same view one style along. Drawn nothing at all where the card has
    /// neither declared nor run anything; the heading follows the band rather
    /// than standing over an empty space.
    @ViewBuilder
    private var crewSection: some View {
        let workflow = Specialists.parse(card.workflow)
        let trail = Specialists.parse(card.agentTrail)
        if !workflow.isEmpty || !trail.isEmpty || !card.area.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("CREW")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .accessibilityHidden(true)
                CrewBand(workflow: workflow, trail: trail, live: liveStageNames,
                         crew: card.crew, lead: card.area, promised: card.leadFace,
                         style: .grid)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(CrewBand.caption(workflow: workflow, trail: trail,
                                                 live: liveStageNames,
                                                 crew: card.crew, lead: card.area,
                                                 promised: card.leadFace))
        }
    }

    /// The person this card is about, when one is known. The bound agent
    /// (the same session pick as `liveStageNames`), else the area's usual
    /// lead. Nil when neither is known — no hashed stranger.
    private var sheetFace: (character: String, state: Cast.State)? {
        let sid = !card.sessionId.isEmpty
            ? card.sessionId
            : (card.isRefining ? card.refineSessionId : "")
        if let (agent, bucket) = client.snapshot.agents.row(session: sid),
           let category = Category(rawValue: bucket) {
            return (Cast.character(for: agent), Cast.state(for: agent, category: category))
        }
        let lead = card.leadFace.isEmpty ? Areas.anchor(card.area) : card.leadFace
        guard !lead.isEmpty else { return nil }
        return (lead, .sleep)
    }

    /// The stages running under this card's session right now, through the
    /// fleet's own `row(session:)` — the board face's helper, same rule.
    private var liveStageNames: Set<String> {
        let sid = !card.sessionId.isEmpty
            ? card.sessionId
            : (card.isRefining ? card.refineSessionId : "")
        guard let (agent, _) = client.snapshot.agents.row(session: sid) else {
            return []
        }
        return Set(agent.subagentRows.map(\.label).filter { !$0.isEmpty })
    }

    private var columnPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("column")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                // The menu below carries this name in its own label now, and
                // a caption that is its own element makes VoiceOver say it
                // twice.
                .accessibilityHidden(true)
            Menu {
                DecryptButton("PREP") { move(to: "prep") }
                DecryptButton("BACKLOG") { move(to: "backlog") }
                DecryptButton("IN PROGRESS") { move(to: "in_progress") }
                DecryptButton("DONE") { move(to: "done") }
            } label: {
                Text(pressed == .move ? mark
                     : (card.column.isEmpty ? "move to…" : card.column.replacingOccurrences(of: "_", with: " ")))
                    .font(Theme.mono(13))
                    .foregroundStyle(Theme.phosphor)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 6)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
            }
            .disabled(sending)
            .accessibilityLabel("Move this card to another column")
            .accessibilityValue(pressed == .move ? spokenMark : (card.column.isEmpty ? "not set" : card.column.replacingOccurrences(of: "_", with: " ")))
        }
    }

    /// The assistant, as the tile row. Sends `tool` alone:
    /// the store clears the model in the same write, and `post` re-polls
    /// before it returns, so the reset is on screen without a second writer
    /// of that rule here.
    private var assistantPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("assistant")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                // The press's own word — QUEUED, SENDING…, SENT — beside
                // the caption while the pick is on its way: the tiles
                // below are dimmed and still show the Mac's value, so
                // without this line a tap looked like nothing happened
                // until the next board frame carried the new tool.
                if pressed == .tool {
                    Text(mark)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.phosphor)
                }
            }
            // The group below is named Assistant, and a caption that is
            // its own element makes VoiceOver say it twice; the group's
            // value already reads "Sending" while the pick is in play.
            .accessibilityHidden(true)
            // The same row of marks the composer draws, dimmed while this
            // card's pick is on its way — which lasts until the card
            // names the tool asked for (`ReceiptEffect.cardTool`), not
            // merely until the Mac said 200 — and drawn on the tile the
            // person tapped for that whole stretch, so the row says which
            // one is coming rather than which one the Mac last had.
            PhoneProviderSwitch(tools: board.tools,
                                installed: board.installed,
                                selected: pendingTool ?? card.tool,
                                sending: pressed == .tool,
                                pick: setTool)
                .disabled(sending)
        }
    }

    /// Which assistant has the card once the switcher is gone — a bound,
    /// dispatching or finished card states what ran and does not offer to
    /// change it. The tile row's idiom: the mark and its name, inert, one
    /// spoken element named as the switcher's group is.
    private var assistantRecord: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("assistant")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .accessibilityHidden(true)
            // The mark is decoration beside its own name, as the agent
            // detail draws it; the name is the one spoken element.
            HStack(spacing: 5) {
                PhoneProviderMark(provider: card.tool, size: 11)
                    .accessibilityHidden(true)
                Text(PhoneProviderMark.label(for: card.tool))
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .accessibilityLabel("Assistant")
                    .accessibilityValue(PhoneProviderMark.label(for: card.tool))
            }
        }
    }

    /// The model, drawn only where the chosen assistant offers any. The first
    /// row is Default, which sends `""` — "let the assistant decide".
    private var modelPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("model")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                // The menu below carries this name in its own label now, and
                // a caption that is its own element makes VoiceOver say it
                // twice.
                .accessibilityHidden(true)
            Menu {
                DecryptButton("Default") { setModel("") }
                ForEach(board.modelOptions(for: card.tool), id: \.self) { name in
                    DecryptButton(Board.modelLabel(name)) { setModel(name) }
                }
            } label: {
                // The name the person chose, with the press's word beside
                // it while the pick is on its way — never the mark alone,
                // which hid what had been chosen until the board agreed.
                HStack(spacing: 8) {
                    Text(shownModelLabel)
                        .foregroundStyle(pressed == .model ? Theme.dim : Theme.phosphor)
                    if pressed == .model {
                        Text(mark)
                            .foregroundStyle(Theme.phosphor)
                    }
                }
                .font(Theme.mono(13))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 6)
                .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
            }
            .disabled(sending)
            // Held while its own pick is on its way, the assistant row's
            // rule: a second choice queued behind the first is two writes
            // racing for one field.
            .disabled(pressed == .model)
            .accessibilityLabel("Model")
            .accessibilityValue(pressed == .model
                                ? "\(shownModelLabel), \(spokenMark)"
                                : shownModelLabel)
        }
    }

    /// The model name on the menu's face: the one just chosen while its
    /// press is in play, the card's own otherwise.
    private var shownModelLabel: String {
        let name = pendingModel ?? card.model
        return name.isEmpty ? "Default" : Board.modelLabel(name)
    }

    private func setTool(_ name: String) {
        let live = card
        if name == live.tool { return }
        // Acknowledged on the tap: the tile moves now and stays there
        // until the board names it; a refused enqueue never entered the
        // queue, so the tile goes back at once.
        pendingTool = name
        Task {
            let result = await send(PhoneActions.boardUpdate,
                                    ["card_id": live.id, "tool": name],
                                    as: .tool)
            if !result.ok { pendingTool = nil }
            apply(result)
        }
    }

    /// `setTool`'s shape exactly: immediate, unguarded, one field. No
    /// `expected_revision` — a one-field toggle is not a form save — and
    /// deliberately **not** in `settlingActions`: the row is not leaving, and
    /// the next board frame carries the new value.
    private func setStartWhenPlanned() {
        let live = card
        let wanted = !startWhenPlannedShown
        // Acknowledged on send: the tick moves now, and `sending` never
        // covers this press, so the card's other buttons do not grey behind
        // a value that is already on screen.
        pendingStartWhenPlanned = wanted
        Task {
            let result = await send(
                PhoneActions.boardUpdate,
                ["card_id": live.id,
                 "start_when_planned": wanted ? "1" : ""],
                as: .startWhenPlanned,
                refreshAfter: false)
            // Turned away before it was queued (a duplicate, a declined
            // Face ID sheet): the card never changed, so neither may the
            // tick. A refusal from the Mac arrives later, on the receipt,
            // and `noteArrived` resets the tick the same way.
            if !result.ok { pendingStartWhenPlanned = nil }
            apply(result)
        }
    }

    private func setModel(_ name: String) {
        let live = card
        if name == live.model { return }
        pendingModel = name
        Task {
            let result = await send(PhoneActions.boardUpdate,
                                    ["card_id": live.id, "model": name],
                                    as: .model)
            if !result.ok { pendingModel = nil }
            apply(result)
        }
    }

    // MARK: - The one next action

    /// What this screen can reach from where it is, filled from the gates it
    /// already computes; the rule turns it into the one verb the card leads
    /// with.
    private var reach: CardSections.Reach {
        CardSections.Reach(
            canRefine: canRefine,
            canStart: canStart,
            canReply: canMessageSession,
            canMarkChecked: cardIsLive
                && offersManualClear
                && card.manualCheckDue,
            canMarkDone: card.column == "in_progress" && !isBusy,
            canReview: cardIsLive && PhoneCardAck.showsReview(card: card, board: board))
    }

    /// The one call into the rule; every guard below reads this.
    private var nextAction: CardSections.NextAction {
        CardSections.nextAction(stage: stage, reach: reach)
    }

    /// The band: one verb, armed then confirmed where the press spends money
    /// or reaches a session. Every verb drawn here is *not* drawn again in
    /// its own section (`nextAction != .x` guards there), so none appears
    /// twice and none is lost; the rest are under OTHER ACTIONS.
    @ViewBuilder
    private var verbs: some View {
        armedElsewhere
        switch nextAction {
        case .refine:
            refineButton
        case .start:
            startButton
        case .reply:
            messageBox
        case .markChecked:
            manualClearButton
        case .done:
            if !showsDoneClose {
                DecryptButton(pressed == .move ? mark : "DONE") { move(to: "done") }
                    .buttonStyle(AlarmOutline())
                    .disabled(sending)
            }
        case .review:
            reviewButton
        case .none:
            EmptyView()
        }
    }

    /// Whether OTHER ACTIONS is open on screen — MORE pressed and the row
    /// pressed — so an armed press made *there* is confirmed there.
    private var otherVerbsOnScreen: Bool {
        CardSections.shown(.otherVerbs, stage: stage, opened: openedSections)
            && CardSections.isOpen(.otherVerbs, stage: stage, opened: openedSections)
    }

    /// An arm this card holds whose confirming button is not the band's
    /// own verb and is not on screen: the column mover under EDIT CARD arms
    /// Done and Start, a plan-gate refusal after a move arms Start or
    /// Start here, and `Arm.timeoutSeconds` would disarm before a person
    /// found the closed OTHER ACTIONS row. The band draws it instead.
    private var armedOffScreen: Bool {
        !otherVerbsOnScreen
            && ((arm.start != nil && nextAction != .start)
                || arm.startHere != nil
                || (arm.done != nil && nextAction != .done))
    }

    /// The armed buttons `armedOffScreen` names, above the band's own verb.
    /// Each is the same button OTHER ACTIONS draws, so a press confirms
    /// the same arm; that row is closed whenever these draw, so none
    /// appears twice.
    @ViewBuilder
    private var armedElsewhere: some View {
        if armedOffScreen {
            if arm.start != nil && nextAction != .start {
                startButton
            }
            if arm.startHere != nil {
                startHereButton
            }
            if arm.done != nil && nextAction != .done {
                doneArmedButton
            }
        }
    }

    /// The verbs that are not the one next action, behind MORE: START HERE,
    /// the start-when-planned tick, Start where Refine leads, and the armed
    /// Done the column mover raises.
    @ViewBuilder
    private var otherVerbs: some View {
        if canStart && nextAction != .start {
            startButton
        }
        if canSpawnHere || arm.startHere != nil {
            startHereButton
        }
        if arm.done != nil && nextAction != .done {
            doneArmedButton
        }
        mergeVerbs
        // "Start the work by itself the moment the plan lands." Unarmed:
        // setting it destroys nothing and the undo is pressing it again. Two
        // gates, `canRefine` and the version marker, so it sits exactly where
        // a Refine could still happen and is drawn *absent* — never present
        // and refused — against a Mac that predates the column.
        if canRefine && board.startWhenPlannedSupported {
            DecryptButton(startWhenPlannedShown
                   ? "[x] START WHEN PLANNED"
                   : "[ ] START WHEN PLANNED") {
                setStartWhenPlanned()
            }
            .buttonStyle(AlarmOutline())
            // Deliberately **not** `sending`: this press is drawn the
            // instant it is made, so there is nothing to wait behind, and
            // `post`'s own per-card dedupe still refuses a second one in
            // flight. `settlingHere` stays — a card on its way out is not
            // a card to tick.
            .disabled(settlingHere)
            .accessibilityLabel(startWhenPlannedShown
                ? "Start the work when the plan lands, on. Press to turn off."
                : "Start the work when the plan lands, off. Press to turn on.")
        }
    }

    private var startButton: some View {
        DecryptButton(startLabel) { pressStart() }
            .buttonStyle(AlarmOutline())
            .disabled(sending)
    }

    private var startHereButton: some View {
        DecryptButton(startHereLabel) { pressStartHere() }
            .buttonStyle(AlarmOutline())
            .disabled(sending)
            .accessibilityLabel("Start on Dark Army's own terminal")
    }

    /// The Done the column mover armed, confirmed here or under OTHER
    /// ACTIONS — whichever is on screen.
    private var doneArmedButton: some View {
        DecryptButton(doneLabel) { confirmDone() }
            .buttonStyle(AlarmOutline(color: Theme.alarm))
            .disabled(sending)
    }

    private var refineButton: some View {
        DecryptButton(refineLabel) {
            if arm.confirm(.refine) {
                Task { await dispatchRefine() }
            } else {
                arm.arm(.refine, id: card.id)
            }
        }
        .buttonStyle(AlarmOutline())
        .disabled(sending)
    }

    private var startLabel: String {
        if pressed == .start { return mark }
        if arm.start == nil { return "START" }
        if pendingChangedPlan { return "Start with changed plan?" }
        if startsUnplanned || pendingSkip { return "Start unplanned?" }
        return "Really start?"
    }

    private var startHereLabel: String {
        if pressed == .startHere { return mark }
        if arm.startHere == nil { return "START HERE" }
        if pendingChangedPlan { return "Start with changed plan here?" }
        if startsUnplanned || pendingSkip { return "Start unplanned here?" }
        return "Really start here?"
    }

    private var doneLabel: String {
        if pressed == .done { return mark }
        return "Done & clear?"
    }

    private var refineLabel: String {
        if pressed == .refine { return mark }
        return arm.refine != nil ? "Really refine?" : "Refine"
    }

    /// SENDING… outlives the request here: the card is in `settlingCards`
    /// until the Mac's board stops listing it, and a screen that has not
    /// popped yet must not offer Delete again in the meantime.
    private var deleteLabel: String {
        // The queue's own word while the press is in play; the literal only
        // once the Mac accepted it and the card is on its way out.
        if pressed == .delete { return mark }
        if settlingHere { return "SENDING…" }
        return arm.deleteCard != nil ? "Really delete?" : "Delete"
    }

    private func pressStart() {
        let skip = startsUnplanned || pendingSkip
        let plain = pendingPlainMove
        if arm.confirm(.start) {
            Task {
                if plain {
                    await updateInProgress(skipPlanGate: true, as: .start)
                } else {
                    await dispatchStart(skipPlanGate: skip)
                }
            }
        } else {
            pendingSkip = skip
            arm.arm(.start, id: card.id)
        }
    }

    private func pressStartHere() {
        let skip = startsUnplanned || pendingSkip
        if arm.confirm(.startHere) {
            Task {
                await dispatchStart(skipPlanGate: skip, ownTerminal: true,
                                    as: .startHere)
            }
        } else {
            pendingSkip = skip
            arm.arm(.startHere, id: card.id)
        }
    }

    private func confirmDone() {
        if arm.confirm(.done) {
            Task { await sendDone(as: .done) }
        }
    }

    private func move(to column: String) {
        let live = card
        if live.queueState == "queued" {
            note = live.queueReason.isEmpty ? "queued" : live.queueReason
            return
        }
        if column == live.column { return }
        if column == "prep" || column == "backlog" {
            Task {
                let result = await send(PhoneActions.boardReset,
                                        ["card_id": live.id,
                                         "column_name": column],
                                        as: .move)
                apply(result)
            }
            return
        }
        if column == "done" {
            if doneClears {
                if arm.done != nil, arm.confirm(.done) {
                    Task { await sendDone(as: .done) }
                } else {
                    arm.arm(.done, id: live.id)
                }
                return
            }
            Task { await sendDone(as: .move) }
            return
        }
        let hasSession = !live.sessionId.isEmpty || !live.linkState.isEmpty
        if live.tool.isEmpty && board.dispatchEnabled && !hasSession {
            note = "Choose an assistant on the card first — dropping it here starts one."
            return
        }
        if hasSession || !board.dispatchEnabled {
            // Menu never carries the flag — only pressStart after confirm.
            Task { await updateInProgress(skipPlanGate: false, as: .move) }
            return
        }
        if live.planPath.isEmpty && !live.isScout {
            pendingSkip = true
            note = ""
            arm.arm(.start, id: live.id)
            return
        }
        Task { await dispatchStart(skipPlanGate: false) }
    }

    private func dispatchStart(skipPlanGate: Bool,
                               ownTerminal: Bool = false,
                               as slot: Pressed = .start) async {
        var fields = ["card_id": card.id]
        if skipPlanGate { fields["skip_plan_gate"] = "true" }
        if ownTerminal { fields["own_terminal"] = "true" }
        // The plan gate's two confirmations arrive on the receipt, not the
        // return: `noteArrived` arms Start when the queued press is refused
        // in the gate's words.
        let result = await send(PhoneActions.boardDispatch, fields, as: slot)
        apply(result)
    }

    private func updateInProgress(skipPlanGate: Bool,
                                  as slot: Pressed) async {
        var fields = ["card_id": card.id, "column_name": "in_progress"]
        if skipPlanGate { fields["skip_plan_gate"] = "true" }
        // The gate's confirmations arrive on the receipt (`noteArrived`).
        let result = await send(PhoneActions.boardUpdate, fields, as: slot)
        apply(result)
    }

    /// The Mac's own words about this card's last queued press. A plan-gate
    /// refusal arms Start with the matching confirmation — the same two
    /// branches `dispatchStart` and `updateInProgress` used to take off the
    /// synchronous return, driven by the receipt instead — **once per
    /// receipt** (`armedForNote`), so a redraw cannot re-arm a confirmation
    /// the person already answered or let time out. Any other refusal is
    /// the note; a refused tick goes back to the card's own value. Every
    /// refusal drawn here is **read** (`readQueueNote`): the record stays
    /// the card's newest receipt — so an older refusal can never surface
    /// under a confirmation still armed — and ages off the QUEUE list as
    /// finished business rather than sitting there for ever.
    private func noteArrived(_ queued: QueueNote?) {
        guard let queued, queued.id != armedForNote else { return }
        armedForNote = queued.id
        pendingStartWhenPlanned = nil
        pendingTool = nil
        pendingModel = nil
        note = queued.text
        client.readQueueNote(for: card.id)
        let result = PhoneActionResult(ok: false, detail: queued.text)
        guard result.isPlanGateRefusal || result.isPlanChangedRefusal else {
            return
        }
        pendingSkip = true
        pendingPlainMove = queued.action == PhoneActions.boardUpdate
        pendingChangedPlan = result.isPlanChangedRefusal
        let ownTerminal = queued.action == PhoneActions.boardDispatch
            && (client.receipts.receipts.first { $0.id == queued.id }?
                .fields["own_terminal"] != nil)
        arm.arm(ownTerminal ? .startHere : .start, id: card.id)
    }

    // MARK: - Done & close

    /// A run that has ended on a card still In progress — the terminal may
    /// still be open, which is exactly when the band's Done was withheld
    /// (`isBusy`). Not while a run is working, dispatching or refining:
    /// closing then would cut work off, not tidy it away.
    private var showsDoneClose: Bool {
        cardIsLive
            && card.column == "in_progress"
            && stage == .ended
            && card.linkState != "dispatching"
            && card.refineState != "dispatching"
            && card.refineState != "live"
    }

    private var doneCloseButton: some View {
        DecryptButton(pressed == .done ? mark : "DONE & CLOSE") { pressDoneClose() }
            .buttonStyle(AlarmOutline())
            .disabled(sending)
            .accessibilityLabel("Mark done and close the terminal")
    }

    /// One press, both halves. With a live terminal it is the fleet's own
    /// Close terminal carrying `by_person`: the Mac closes the tab first
    /// and only then finishes the card (`close_session_terminal`), so a
    /// refused close — an open permission prompt — leaves the card where
    /// it is. With no terminal left it is the plain move to Done.
    private func pressDoneClose() {
        let live = card
        if live.linkState == "live" && !live.sessionId.isEmpty {
            Task {
                let result = await send(PhoneActions.closeTerminal,
                                        ["session_id": live.sessionId,
                                         "by_person": "1"],
                                        as: .done)
                apply(result)
            }
            return
        }
        Task { await sendDone(as: .done) }
    }

    private func sendDone(as slot: Pressed) async {
        let result = await send(PhoneActions.boardUpdate,
                                ["card_id": card.id, "column_name": "done"],
                                as: slot)
        apply(result)
    }

    private func dispatchRefine() async {
        let result = await send(PhoneActions.boardRefine,
                                ["card_id": card.id], as: .refine)
        apply(result)
    }

    // MARK: - The card in full, and the plan it points at

    /// The card's real instructions when the full read has landed, else the
    /// snapshot's preview. Never a blank: an older Mac, or a read still in
    /// flight, keeps drawing exactly what this screen drew before.
    private var fullPrompt: String {
        if let text = fetched?.prompt, !text.isEmpty { return text }
        if let text = cached?.prompt, !text.isEmpty { return text }
        return card.prompt
    }

    private var fullSummary: String {
        if let text = fetched?.summary, !text.isEmpty { return text }
        if let text = cached?.summary, !text.isEmpty { return text }
        return card.summary
    }

    /// Whether the whole of the instructions are on screen — the live read,
    /// or the phone's own copy of the *current* revision. What holds Save.
    private var haveFullText: Bool {
        if fetched != nil { return true }
        guard let held = cached else { return false }
        return held.revision == card.revision
    }

    /// The revision a Save is guarded against — **the revision of the text
    /// actually on screen**, which is not always the frame's.
    ///
    /// `fullPrompt` / `fullSummary` prefer the phone's own copy over the
    /// frame's preview, and the drafts are seeded from those before
    /// `fetchCard` returns (away, over the relay, that can be tens of
    /// seconds). So a cache at revision 3 against a frame at revision 4 —
    /// somebody edited on the Mac — would guard the person's typing with
    /// `expected_revision: 4`, the guard would *match*, and the Mac's
    /// revision-4 words would be silently overwritten by an edit made on
    /// top of revision 3. That is the exact overwrite this whole guard
    /// exists to prevent, so the number follows the text, never the frame.
    ///
    /// Typed text keeps the number it was typed against (`editRevision`) —
    /// also across a lock, when the restored rung fetches the card afresh
    /// and the fetched row would otherwise vouch for text it never showed.
    private var shownRevision: Int {
        if draftTouched, let base = retained.editRevision { return base }
        return seedRevision
    }

    /// The revision of the copy the editors were seeded from.
    private var seedRevision: Int {
        // **Both** branches, which is what the paragraph above already
        // claims. The fetched row seeds the drafts once, while nobody has
        // typed; a card edited on the Mac after that read landed moves the
        // *frame* to a newer revision while the editors still show the
        // fetched text, so guarding with `card.revision` there is the same
        // silent overwrite one branch over. `BoardState.applyFetchedCard`
        // is the panel's answer to exactly this, and it moves the number
        // with the text too.
        if let row = fetched { return row.revision }
        return cached?.revision ?? card.revision
    }

    /// The fetched card, but only while it is still *this* card — the read is
    /// keyed on the seed's id and a stale answer must never paint a new
    /// screen.
    private var fetched: BoardCard? {
        guard let full = cardFull, let row = full.card, row.id == card.id
        else { return nil }
        return row
    }

    /// The plan as the Mac just sent it, else the phone's own copy. The
    /// cached branch is the whole offline story: a card opened with no Mac
    /// in reach still shows its plan laid out as a document.
    private var plan: CardPlan? {
        if fetched != nil, let live = cardFull?.plan { return live }
        guard let held = cached, !held.planPath.isEmpty else { return nil }
        var offline = CardPlan()
        offline.path = held.planPath
        if held.planAvailable, let text = cachedPlanText, !text.isEmpty {
            offline.available = true
            offline.text = text
            offline.digest = held.planDigest
        } else {
            offline.available = false
            offline.reason = held.planReason.isEmpty
                ? "the plan is not on this phone yet" : held.planReason
        }
        return offline
    }

    /// The plan document, as a document. `MarkdownText` is the same renderer
    /// the instructions already use — `Markdown.swift` is byte-pinned to the
    /// panel's, so making a plan look better on a phone is not a thing this
    /// screen may do on its own.
    ///
    /// An unavailable plan draws the Mac's own `reason`, one line, never
    /// silence: a card pointing at a file that is gone is exactly the state a
    /// person needs told, because the Mac's plan gate treats it as unplanned.
    @ViewBuilder
    private var planSection: some View {
        if !card.planPath.isEmpty, let plan {
            Rectangle().fill(Theme.hair).frame(height: 1)
            Text("PLAN")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .tracking(0.8)
            if plan.available {
                let split = PlanSplit.split(plan.text)
                MarkdownText(source: split.summary, base: 12, mono: true)
                    .equatable()
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                if let detail = split.detail {
                    DecryptButton { planDetailOpen.toggle() } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "chevron.right")
                                .font(Theme.mono(11))
                                .rotationEffect(.degrees(planDetailOpen ? 90 : 0))
                                .accessibilityHidden(true)
                            Text("TECHNICAL DETAIL")
                                .font(Theme.mono(11))
                                .tracking(0.8)
                        }
                        .foregroundStyle(Theme.phosphor)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Technical detail")
                    .accessibilityValue(planDetailOpen ? "shown" : "hidden")
                    .accessibilityHint(planDetailOpen
                                       ? "Hides the technical half of the plan"
                                       : "Shows the technical half of the plan")
                    if planDetailOpen {
                        MarkdownText(source: detail, base: 12, mono: true)
                            .equatable()
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                approvalSection(plan)
            } else {
                Text(plan.reason.isEmpty
                     ? "that plan could not be read" : plan.reason)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                Text(card.planPath)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .textSelection(.enabled)
            }
        }
    }

    /// Approval as a section of its own: a rule, an `APPROVAL` heading, one
    /// sentence saying where the plan stands, and — where a press is still
    /// wanted — a full-width button at touch measure. Approved means *this
    /// wording*, so the sentence reads against the digest the Mac just sent
    /// rather than against a flag (`PlanApprovalStatus.decide`), and a plan
    /// edited after approval says so instead of quietly offering the button
    /// again. Absent — never inert — on a Mac that does not take an approval
    /// at all, and on an offline copy that holds no digest to approve.
    @ViewBuilder
    private func approvalSection(_ plan: CardPlan) -> some View {
        if board.planApprovalSupported && !plan.digest.isEmpty {
            let status = PlanApprovalStatus.decide(approved: card.planApproved,
                                                   digest: plan.digest,
                                                   approvedAt: card.planApprovedAt)
            Rectangle().fill(Theme.hair).frame(height: 1)
            Text("APPROVAL")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .tracking(0.8)
                .accessibilityAddTraits(.isHeader)
            Text(status.sentence)
                .font(Theme.mono(12))
                .foregroundStyle(status.ink)
                .fixedSize(horizontal: false, vertical: true)
            if status.offersButton {
                DecryptButton { Task { await approvePlan(plan) } } label: {
                    Text(pressed == .approve ? mark : "Approve this plan")
                        .frame(maxWidth: .infinity,
                               minHeight: PlanApprovalStatus.touchFloor
                                   - 2 * AlarmOutline.vPad)
                }
                .buttonStyle(AlarmOutline())
                .disabled(sending)
                .accessibilityLabel(pressed == .approve
                                    ? spokenMark : "Approve this plan")
                .accessibilityHint(status.hint)
            }
        }
    }

    private func approvePlan(_ plan: CardPlan) async {
        let result = await send(PhoneActions.boardApprovePlan,
                                ["card_id": card.id,
                                 "plan_path": plan.path,
                                 "plan_digest": plan.digest],
                                as: .approve)
        // No refetch: the approval lands on the snapshot's own
        // `plan_approved`, which the status above reads.
        apply(result)
    }

    /// Ask the Mac for this card in full, then seed the editors from it —
    /// **once**, and only while the person has not typed. A re-seed over a
    /// half-written correction is the sibling of the bug the panel already
    /// answered with `applyFetchedCard`.
    private func loadFull(force: Bool = false) async {
        let id = card.id
        if draftFor != id {
            draftFor = id
            draftTouched = false
            conflict = nil
            // One read per card, on the task rather than in the body.
            cachedPlanText = client.cardCache.planText(id)
            planDetailOpen = false
            draftTitle = card.title
            // The phone's own copy, where it holds one, so the editors are
            // seeded with the whole text rather than the frame's preview
            // even with no Mac in reach.
            draftSummary = fullSummary
            draftPrompt = fullPrompt
            draftPriority = card.priority
            draftArea = card.area
            // A different card: drop the read that belonged to the last one,
            // or the guard below would answer "already fetched" and this
            // screen would live off the frame's preview for ever.
            cardFull = nil
        }
        if cardFull != nil && !force { return }
        let answer = await client.fetchCard(id)
        guard card.id == id else { return }
        cardFull = answer
        // The sweep may have filled or replaced the cached document while
        // this screen was open; re-read it once, here, not in the body.
        if cachedPlanText == nil { cachedPlanText = client.cardCache.planText(id) }
        guard let row = answer?.card, row.id == id, !draftTouched else { return }
        draftTitle = row.title
        draftSummary = row.summary
        draftPrompt = row.prompt
        draftPriority = row.priority
        draftArea = row.area
    }

    // MARK: - Correcting the card

    /// Why Save is refused, or nil. **Held with a reason rather than absent**:
    /// `board_update` is honoured by every Mac, so this is a not-yet, not a
    /// capability the Mac lacks. The wording is the panel's own, verbatim.
    private var saveHoldReason: String? {
        if draftTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Name it first"
        }
        if !haveFullText && card.promptTruncated {
            return "Waiting for the full instructions"
        }
        if !haveFullText && card.summaryTruncated {
            return "Waiting for the full description"
        }
        // Belt to `shownRevision`'s braces. The guard above only fires on
        // the truncation flags, so a prompt under
        // `BOARD_SNAPSHOT_PROMPT_CHARS` never reached it — and a card whose
        // cached copy is behind the frame is a card whose editors are
        // showing words somebody has already changed. `shownRevision` makes
        // that save *refuse* rather than overwrite; this makes it not
        // happen at all, and says why.
        if !haveFullText && cached != nil {
            return "Waiting for the newer version"
        }
        return nil
    }

    /// Whether this card's assistant can be typed at from here. Every term
    /// is the Mac's own answer, re-checked at the press; the last is the
    /// version marker, so against an older Mac the button is absent rather
    /// than drawn and 404ed inside the sealed reply.
    private var canMessageSession: Bool {
        guard board.cardMessageWritable, card.linkState == "live",
              !card.sessionId.isEmpty,
              let (row, _) = client.snapshot.agents.row(session: card.sessionId)
        else { return false }
        return row.canMessage
    }

    /// Say something to the assistant already working this card — the Mac's
    /// `messagesTerminal` box, in `editorSection`'s own disclosure idiom.
    /// Single-line on purpose: the Mac refuses a message containing a line
    /// break, because the keystrokes end in a newline and the tail of a
    /// two-line message would be typed at a prompt that had already moved on.
    @ViewBuilder
    private var messageSection: some View {
        // The band draws the box while the card is being worked; here it
        // stays for a stage the band leads with something else.
        if canMessageSession, nextAction != .reply {
            Rectangle().fill(Theme.hair).frame(height: 1)
            messageBox
        }
    }

    @ViewBuilder
    private var messageBox: some View {
        if canMessageSession {
            DecryptButton(messageOpen ? "CLOSE" : "SEND A MESSAGE") {
                messageOpen.toggle()
            }
            .font(Theme.mono(11))
            .foregroundStyle(Theme.phosphor)
            .frame(minHeight: 44, alignment: .leading)
            .buttonStyle(.plain)
            Text("Typed straight onto that terminal's input line — the same "
                 + "as typing it there yourself.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            if messageOpen {
                TextField("", text: $retained.messageText)
                    .font(Theme.mono(13))
                    .foregroundStyle(Theme.phosphorBright)
                    .textFieldStyle(.plain)
                    .hidesKeyboard()
                    .padding(6)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                    // `TextField("", …)` has an *empty* label.
                    .accessibilityLabel("Message to this session")
                DecryptButton(pressed == .message ? mark : "SEND") {
                    Task { await sendMessage() }
                }
                .buttonStyle(AlarmOutline())
                .accessibilityLabel(pressed == .message
                                    ? spokenMark : "Send this message")
                .disabled(sending || messageText.trimmingCharacters(
                    in: .whitespacesAndNewlines).isEmpty)
            }
            if !sendLine.isEmpty {
                Text(sendLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
        }
    }

    /// On a refusal the typed text is left alone: every one of the Mac's
    /// seven refusals is something the person can fix by editing what they
    /// wrote or by waiting, and clearing the field would make them type it
    /// again. Deliberately not in `settlingCards` — the card is not leaving,
    /// which is `low_priority`'s own argument.
    private func sendMessage() async {
        let text = messageText.trimmingCharacters(
            in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sendNote = ""
        let result = await send(PhoneActions.boardMessage,
                                ["card_id": card.id, "text": text],
                                as: .message)
        apply(result)
        if result.ok {
            messageText = ""
            messageOpen = false
            // A message is in the queue: `sendLine` reads its receipt from
            // here on — the queue's line, then "Sent." — the whole
            // confirmation on this screen, since the phone carries no card
            // conversation to scroll back through.
            sendNote = "queued"
        }
    }

    /// The fields a person may correct here, in the Mac's four groups —
    /// what it is, where it runs, how important, what to do — each under a
    /// heading VoiceOver reads as one, every box named by the words above
    /// it (`editorField`), at touch measure, with the Save it needs right
    /// under the last box rather than at the foot of the screen. Hidden
    /// while the card's assistant is working, which is what the Mac's own
    /// saved-card editor allows.
    @ViewBuilder
    private var editorSection: some View {
        if !isBusy {
            Rectangle().fill(Theme.hair).frame(height: 1)
            DecryptButton(editing ? "CLOSE EDITOR" : "EDIT FIELDS") { editing.toggle() }
                .font(Theme.mono(11))
                .foregroundStyle(Theme.phosphor)
                .frame(minHeight: 44, alignment: .leading)
                .buttonStyle(.plain)
                .accessibilityLabel(editing ? "Close the editor" : "Edit this card's fields")
                .accessibilityHint(editing
                                   ? "Hides the boxes. Unsaved changes are kept until you leave the card."
                                   : "Shows boxes for the title, description, instructions, area and priority.")
            if editing {
                editorGroup("What it is") {
                    editorField("Title", "One line naming the work.") {
                        TextField("", text: $retained.draftTitle)
                            .font(Theme.mono(13))
                            .foregroundStyle(Theme.phosphorBright)
                            .textFieldStyle(.plain)
                            .hidesKeyboard()
                            .padding(10)
                            .frame(minHeight: 44)
                            .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                            .onChange(of: draftTitle) { _, _ in draftTouched = true }
                    }
                    editorField("In plain words",
                                "What this is for, so somebody who will never read "
                                + "the instructions can tell what the board is saying.") {
                        TextEditor(text: $retained.draftSummary)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                            .scrollContentBackground(.hidden)
                            .hidesKeyboard()
                            .frame(minHeight: 88)
                            .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                            .onChange(of: draftSummary) { _, _ in draftTouched = true }
                    }
                }
                // **Absent, never inert** against an older Mac: it would drop
                // the field and then 400 an update whose only named field was
                // dropped.
                if board.areasSupported {
                    editorGroup("Where it runs") {
                        editorField("Area", "Which part of the product this serves.") {
                            AreaGrid(selected: $retained.draftArea)
                                .onChange(of: draftArea) { _, _ in draftTouched = true }
                        }
                    }
                }
                if board.prioritySupported {
                    editorGroup("How important") {
                        editorField("Priority",
                                    "A whole number from 0 to 100; empty means no "
                                    + "opinion. The number decides the order in its column.") {
                            TextField("0–100", text: $retained.draftPriority)
                                .font(Theme.mono(13))
                                .foregroundStyle(Theme.phosphorBright)
                                .textFieldStyle(.plain)
                                .keyboardType(.numberPad)
                                .hidesKeyboard()
                                .padding(10)
                                .frame(minHeight: 44)
                                .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                                .onChange(of: draftPriority) { _, _ in draftTouched = true }
                            if let problem = PriorityInput.problem(draftPriority) {
                                Text(problem)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.amber)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .accessibilityLabel("Priority problem: " + problem)
                            }
                        }
                    }
                }
                editorGroup("What to do") {
                    editorField("Instructions",
                                "Handed to the assistant word for word when the card starts.") {
                        TextEditor(text: $retained.draftPrompt)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                            .scrollContentBackground(.hidden)
                            .hidesKeyboard()
                            .frame(minHeight: 200)
                            .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                            .onChange(of: draftPrompt) { _, _ in draftTouched = true }
                    }
                }
                if let reason = saveHoldReason {
                    Text(reason)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                } else if draftTouched {
                    Text("Unsaved changes — press Save to keep them.")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 12) {
                    DecryptButton(pressed == .save ? "SENDING…" : "SAVE") {
                        Task { await saveCard() }
                    }
                    .buttonStyle(AlarmOutline())
                    .accessibilityLabel(pressed == .save ? spokenMark : "Save this card")
                    .disabled(sending || saveHoldReason != nil)
                    if draftTouched {
                        DecryptButton("DISCARD") { discardEdits() }
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.phosphor)
                            .frame(minHeight: 44)
                            .buttonStyle(.plain)
                            .disabled(sending)
                            .accessibilityLabel("Discard changes")
                            .accessibilityHint("Puts the card's own words back in every box")
                    }
                }
                if let conflict { conflictSection(conflict) }
            }
        }
    }

    /// A titled group of boxes: a heading VoiceOver reads as a heading, a
    /// hairline, then the boxes — one accessibility container so a screen
    /// reader can skip it whole. The Mac's `editorGroup`, one style along.
    private func editorGroup<Content: View>(_ title: String,
                                            @ViewBuilder _ content: () -> Content)
        -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(Theme.mono(10))
                .foregroundStyle(Theme.dim)
                .tracking(0.8)
                .accessibilityAddTraits(.isHeader)
            Rectangle().fill(Theme.hair).frame(height: 1)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(title)
    }

    /// One labelled box: the words above it are its name and its help for a
    /// screen reader too — a bare `TextField("")` under a `Text` reads as
    /// "text field", which is every box on the card sounding the same.
    private func editorField<Content: View>(_ label: String, _ hint: String,
                                            @ViewBuilder _ content: () -> Content)
        -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.phosphor)
            content()
                .accessibilityLabel(label)
                .accessibilityHint(hint)
            Text(hint)
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityHidden(true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The card's own values back into every box — the full text where the
    /// phone holds it, the frame's preview otherwise, exactly as the editors
    /// were seeded. Sends nothing.
    private func discardEdits() {
        let row = cardFull?.card
        draftTitle = row?.title ?? card.title
        draftSummary = row?.summary ?? fullSummary
        draftPrompt = row?.prompt ?? fullPrompt
        draftPriority = row?.priority ?? card.priority
        draftArea = row?.area ?? card.area
        draftTouched = false
    }

    /// Save, guarded. `expected_revision` is the revision of the copy on
    /// screen, so a card somebody changed underneath is **refused in the
    /// Mac's words** rather than overwritten — and the refusal carries what
    /// the Mac holds, which is what lets this screen show both without a
    /// second fetch that may not be possible.
    private func saveCard(expecting: Int? = nil) async {
        guard saveHoldReason == nil else { return }
        var fields = ["card_id": card.id,
                      "title": draftTitle,
                      "summary": draftSummary,
                      "prompt": draftPrompt,
                      // Always a string: a JSON `0` would reach the Mac's
                      // `str(... or "")` coercion and store `""` — unscored
                      // rather than the lowest score.
                      "priority": draftPriority,
                      "expected_revision": String(expecting ?? shownRevision)]
        if board.areasSupported { fields["area"] = draftArea }
        let result = await send(PhoneActions.boardUpdate, fields, as: .save)
        if result.isCardChangedRefusal {
            // Every typed field is kept exactly as it was written. The
            // Mac's own words go in the note; its version is drawn beneath
            // the editors with two ways out.
            conflict = result.current
            note = result.detail
            return
        }
        apply(result)
        if result.ok {
            draftTouched = false
            conflict = nil
            await loadFull(force: true)
        }
    }

    /// What the Mac holds, beside what you typed, and the only two presses
    /// out of it. **Keep mine** re-sends the same fields at the revision the
    /// refusal reported; **Use the Mac's** replaces the editors' contents
    /// with the Mac's and clears the conflict, sending nothing.
    @ViewBuilder
    private func conflictSection(_ current: CardStated) -> some View {
        Rectangle().fill(Theme.hair).frame(height: 1)
        Text("WHAT THE MAC HOLDS")
            .font(Theme.mono(11))
            .foregroundStyle(Theme.amber)
            .tracking(0.8)
        conflictField("title", current.title)
        conflictField("description", current.summary)
        conflictField("instructions", current.prompt)
        // The number is on `STATED_FIELDS` expressly so a conflicted phone
        // sees the Mac's beside its own — and **USE THE MAC'S** below applies
        // it, so leaving it undrawn would replace a typed priority the person
        // never saw. `conflictField` no-ops on an empty value, so a card
        // nobody has scored still shows three lines.
        conflictField("priority", current.priority)
        if board.areasSupported { conflictField("area", current.area) }
        HStack(spacing: 12) {
            DecryptButton("KEEP MINE") {
                Task { await saveCard(expecting: current.revision) }
            }
            .buttonStyle(AlarmOutline())
            .disabled(sending)
            DecryptButton("USE THE MAC'S") {
                draftTitle = current.title
                draftSummary = current.summary
                draftPrompt = current.prompt
                draftPriority = current.priority
                draftArea = current.area
                // The editors now hold the Mac's copy, at its number.
                retained.editRevision = current.revision
                conflict = nil
                note = ""
            }
            .buttonStyle(AlarmOutline())
            .disabled(sending)
        }
    }

    @ViewBuilder
    private func conflictField(_ label: String, _ value: String) -> some View {
        if !value.isEmpty {
            Text(label)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            Text(value)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func apply(_ result: PhoneActionResult, pop: Bool = false) {
        if result.ok {
            note = ""
            pendingSkip = false
            pendingPlainMove = false
            pendingChangedPlan = false
            // Written down is not landed: the screen leaves when the card
            // does (`leaveDeletedCard`), never on the queue's own `ok`.
            if pop { deleteQueued = true }
        } else if !result.detail.isEmpty {
            note = result.detail
        }
    }

    /// A queued Delete's exit: the board no longer listing the card pops
    /// the screen; the mark leaving with nothing said pops it too; the mark
    /// leaving **with** the Mac's words keeps the screen, where they are
    /// drawn.
    private func leaveDeletedCard() {
        guard deleteQueued else { return }
        if !cardIsLive { dismiss(); return }
        if client.queueMark(for: card.id) == nil {
            if client.queueNote(for: card.id) == nil {
                dismiss()
            } else {
                deleteQueued = false
            }
        }
    }
}

/// One folded section's closed row at touch measure: a chevron, the label
/// and, where the card already knows it, a count or a word
/// (`CardSections.rowText`). The plan's `TECHNICAL DETAIL` idiom lifted to a
/// type so every section draws it the same; a header for VoiceOver with the
/// open state as its value.
struct PhoneCardFoldRow: View {
    let label: String
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        DecryptButton(action: toggle) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(Theme.mono(11))
                    .rotationEffect(.degrees(open ? 90 : 0))
                    .accessibilityHidden(true)
                Text(label)
                    .font(Theme.mono(11))
                    .tracking(0.8)
            }
            .foregroundStyle(Theme.phosphor)
            .frame(minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityValue(open ? "shown" : "hidden")
        .accessibilityHint(open ? "Hides this section" : "Shows this section")
        .accessibilityAddTraits(.isHeader)
    }
}

/// The card detail's per-stage rule. **Byte-identical to
/// `panel/Sources/BobPanel/CardSections.swift`** from the line below, pinned
/// by `host/tests/test_card_sections.py`: edit the panel's copy and paste.
enum CardSections {
    /// Every section a card screen may draw. The raw value is the fold row's
    /// label, so the two surfaces cannot word one differently.
    enum Section: String, CaseIterable {
        case status = "STATUS"
        case manualCheck = "MANUAL CHECK"
        case closeSignature = "CLOSED BY"
        case objective = "OBJECTIVE"
        case verbs = "ACTIONS"
        case plan = "PLAN"
        case workRecord = "WHAT CHANGED"
        case run = "WHAT RAN"
        case editor = "EDIT CARD"
        case instructions = "INSTRUCTIONS"
        case crew = "CREW"
        case session = "SESSION"
        case thread = "MESSAGES"
        case queue = "QUEUED"
        case timeline = "TIMELINE"
        case documents = "DOCUMENTS"
        case attachments = "ATTACHMENTS"
        case danger = "DELETE"
        /// The three added by `plans/2026-09-20-simplify-card-details.md`,
        /// after Delete so the earlier labels keep their lines: the
        /// evidence fold that used to sit outside the rule, the verbs that
        /// are not the one next action, and the one row everything folded
        /// sits behind.
        case collaboration = "COLLABORATION"
        case otherVerbs = "OTHER ACTIONS"
        case more = "MORE"
        /// A scout's written report — the deliverable of a scout card, so
        /// it is a section of its own rather than a path under DOCUMENTS
        /// (added 21 Sep 2026, after MORE so the earlier labels keep their
        /// lines). Drawn on every scout: the report once it is attached,
        /// and a line saying the scout is still out until then.
        case report = "REPORT"
        /// The cards this one waits on, whether each is done, and the cards
        /// it unblocks (added 24 Sep 2026, after REPORT so the earlier labels
        /// keep their lines). Pinned beside QUEUED, because a card held by a
        /// dependency says so in the queued line and the reason sits here.
        case dependencies = "WAITS ON"
        /// A finished card's branch against the main line: its commits,
        /// the files it changed and each one's changes (added 3 Oct 2026,
        /// after WAITS ON so the earlier labels keep their lines). Fetched
        /// on the row's open, never on a poll.
        case changes = "CHANGES"
    }

    /// Where the card is in its life. Four columns, with In progress split
    /// three ways by what the daemon says about the bound session.
    enum Stage: String, CaseIterable {
        case prep, backlog, running, manualCheck, ended, done
    }

    /// The one sentence drawn at the top of a card waiting on a check.
    static let statusLine = "Finished — waiting on your check"

    /// `manualCheckDue` is the daemon's `manual_check_due` — steps present
    /// *and* the assistant no longer typing — and it wins over `linkState`,
    /// because a check somebody has to do is the reason the card is open.
    /// `runActive` is the daemon's `run_active`, the pipeline band's own
    /// RUN/ENDED split: a `live` card whose session Dark Army can no longer hear
    /// never reaches `link_state == "ended"`, and drawing it as running
    /// while the band draws it ENDED is the two-surfaces-disagree bug. So
    /// a bound card (`linkState` non-empty) that is not running is `.ended`
    /// here too; a card with no link at all is `.running` by column and
    /// says so in `stateWords(for:linked:)`. An unknown column is Backlog,
    /// the panel's own `BoardColumn` fallback.
    static func stage(column: String, linkState: String,
                      manualCheckDue: Bool, runActive: Bool) -> Stage {
        switch column {
        case "prep": return .prep
        case "backlog": return .backlog
        case "done": return .done
        case "in_progress":
            if manualCheckDue { return .manualCheck }
            if linkState == "ended" { return .ended }
            if !linkState.isEmpty && !runActive { return .ended }
            return .running
        default: return .backlog
        }
    }

    /// Drawn open wherever they appear, with no chevron: a chevron is a
    /// promise that something is behind it, and these are never hidden.
    /// WAITS ON is pinned beside QUEUED and, like it, draws nothing on a
    /// card with nothing to say.
    static let pinned: Set<Section> = [.status, .verbs, .queue, .dependencies]

    /// The full order per stage: the pinned four and the stage's lead
    /// first, then MORE, then everything MORE hides, Delete always last.
    /// QUEUED is in every order because a queued card does not move — a
    /// Prep card pressed Start waits in Prep — and it draws nothing unless
    /// the card is queued. WAITS ON follows QUEUED in every order for the
    /// same reason. `.more` appears exactly once in every order and
    /// everything after it is behind that one row until it is pressed.
    static func order(for stage: Stage) -> [Section] {
        switch stage {
        case .prep:
            return [.status, .verbs, .queue, .dependencies, .more, .report, .editor, .objective,
                    .instructions, .crew, .otherVerbs, .collaboration, .timeline,
                    .attachments, .documents, .thread, .danger]
        case .backlog:
            return [.status, .verbs, .queue, .dependencies, .more, .plan, .report, .editor,
                    .objective, .instructions, .crew, .otherVerbs, .collaboration,
                    .timeline, .documents, .attachments, .thread, .danger]
        case .running:
            return [.status, .verbs, .queue, .dependencies, .more, .report, .session, .thread,
                    .manualCheck, .workRecord, .plan, .instructions, .editor,
                    .crew, .run, .otherVerbs, .objective, .collaboration,
                    .timeline, .attachments, .documents, .danger]
        case .manualCheck:
            return [.status, .manualCheck, .verbs, .queue, .dependencies, .more, .workRecord,
                    .report, .run, .plan, .instructions, .editor, .crew, .session,
                    .thread, .otherVerbs, .objective, .collaboration, .timeline,
                    .attachments, .documents, .danger]
        case .ended:
            return [.status, .report, .verbs, .queue, .dependencies, .more, .workRecord,
                    .changes, .run,
                    .manualCheck, .closeSignature, .plan, .instructions, .editor,
                    .session, .thread, .crew, .otherVerbs, .objective,
                    .collaboration, .timeline, .attachments,
                    .documents, .danger]
        case .done:
            return [.status, .closeSignature, .report, .verbs, .queue, .dependencies, .more,
                    .objective, .workRecord, .changes, .run, .plan, .instructions, .crew,
                    .session, .thread, .editor, .otherVerbs, .collaboration,
                    .timeline, .attachments, .documents, .danger]
        }
    }

    /// Open by default at this stage; the pinned four are implied. Three
    /// exceptions to "everything behind MORE", and only three: a card waiting
    /// on your check opens with the check's steps, because the steps are the
    /// next action; a finished card opens with its close note, because
    /// Reviewed is a judgment about that note; and a scout that has come
    /// back — its run ended or the card Done — opens with its report,
    /// because the report is what the card was for. REPORT leads only on a
    /// scout: on a build card the section has no content and draws nothing.
    /// `.more` is never here.
    static func leads(for stage: Stage) -> Set<Section> {
        switch stage {
        case .prep: return []
        case .backlog: return []
        case .running: return []
        case .manualCheck: return [.manualCheck]
        case .ended: return [.report]
        case .done: return [.closeSignature, .report]
        }
    }

    static func isOpen(_ s: Section, stage: Stage, opened: Set<Section>) -> Bool {
        pinned.contains(s) || leads(for: stage).contains(s) || opened.contains(s)
    }

    /// The sections MORE hides at this stage, in the order's own order: the
    /// order minus the pinned, the leads and the MORE row itself.
    static func hidden(for stage: Stage) -> [Section] {
        order(for: stage).filter { $0 != .more && !isOpen($0, stage: stage, opened: []) }
    }

    /// Whether a section is on screen at all — as a body or as a row. The
    /// MORE row and everything the rule opens always are; the rest only once
    /// MORE has been pressed. MORE's own open state rides `opened` like any
    /// other section's, so a screen needs no second slot for it and the
    /// card-swap and stage-change resets already cover it.
    static func shown(_ s: Section, stage: Stage, opened: Set<Section>) -> Bool {
        s == .more || isOpen(s, stage: stage, opened: []) || opened.contains(.more)
    }

    /// One sentence saying where the card stands, always present on the
    /// card screen, in words a person can read without knowing a column
    /// name. The manual-check stage keeps the sentence the screens already
    /// draw, so `statusLine` stays the one wording of it. `linked` is
    /// whether a session is bound at all (`link_state` non-empty): an In
    /// progress card nobody is working — moved there with the launcher
    /// off, or sent Back and dropped in again — is `.running` by column,
    /// and saying an assistant is working on it would be a lie.
    static func stateWords(for stage: Stage, linked: Bool) -> String {
        switch stage {
        case .prep: return "Not planned yet"
        case .backlog: return "Planned — ready to start"
        case .running:
            return linked ? "An assistant is working on it"
                          : "In progress — nobody working on it yet"
        case .manualCheck: return statusLine
        case .ended: return "The run ended — not marked done yet"
        case .done: return "Done"
        }
    }

    /// The bound reading, for a caller with no link to speak of.
    static func stateWords(for stage: Stage) -> String {
        stateWords(for: stage, linked: true)
    }

    /// What the screen drawing the card can actually do from where it is —
    /// filled by each surface from the gates it already computes (Dark Army's
    /// launcher on, a session it can type at, the daemon honouring the
    /// verb), all `false` by default so an older Mac offers nothing it
    /// cannot do. Never the tool.
    struct Reach: Equatable {
        var canRefine = false
        var canStart = false
        var canReply = false
        var canMarkChecked = false
        var canMarkDone = false
        var canReview = false
    }

    /// The one verb a card leads with. The raw value is the button's label
    /// where the surface draws a plain one; `none` draws no band at all.
    enum NextAction: String {
        case refine = "Refine"
        case start = "START"
        case reply = "SEND A MESSAGE"
        case markChecked = "Mark checked"
        case done = "Done"
        case review = "Reviewed"
        case none = ""
    }

    /// The one next action from the stage and what the screen can reach: a
    /// Prep card leads with Refine (Start where Refine is not offered), a
    /// planned card with Start, a card being worked with the message box, a
    /// card waiting on a check with Mark checked (the message box where that
    /// verb is not honoured), an ended run with Done, a finished card with
    /// Reviewed. Every other verb sits under OTHER ACTIONS.
    static func nextAction(stage: Stage, reach: Reach) -> NextAction {
        switch stage {
        case .prep:
            if reach.canRefine { return .refine }
            return reach.canStart ? .start : .none
        case .backlog:
            return reach.canStart ? .start : .none
        case .running:
            return reach.canReply ? .reply : .none
        case .manualCheck:
            if reach.canMarkChecked { return .markChecked }
            return reach.canReply ? .reply : .none
        case .ended:
            return reach.canMarkDone ? .done : .none
        case .done:
            return reach.canReview ? .review : .none
        }
    }

    /// What the closed row may say about its contents, all of it already on
    /// the card. Every member defaults, so a surface fills what it knows.
    struct Facts: Equatable {
        var files = 0
        var filesTotal = 0
        var filesAvailable = true
        var hasReport = false
        var planAttached = false
        var planApproved = false
        var planMissing = false
        /// `true` once `report_path` is set on a scout; the REPORT row reads
        /// `attached`, and `still out` on a scout that has not come back.
        var reportAttached = false
        var isScout = false
        var messages = 0
        var attachments = 0
        var documents = 0
        var manualNote = false
        var crewStages = 0
        /// How many cards this one waits on, for `WAITS ON · 2`.
        var dependencies = 0
        /// How long the card has existed, already formatted (`"3d 4h"`), so
        /// the closed TIMELINE row reads `TIMELINE · 3d 4h`. `""` unknown.
        var age = ""
        /// How many sections the MORE row hides on this card, so the closed
        /// row reads `MORE · 9`. 0 draws the bare label.
        var hidden = 0
    }

    /// The closed-row tail, `""` where nothing cheap is known. The row draws
    /// `label` and then `" · " + summary` only when this is non-empty.
    static func summary(_ s: Section, facts: Facts) -> String {
        switch s {
        case .workRecord:
            if !facts.filesAvailable { return "folder unreadable" }
            return facts.filesTotal == 1 ? "1 file" : "\(facts.filesTotal) files"
        case .plan:
            if facts.planMissing { return "missing" }
            if facts.planApproved { return "approved" }
            return facts.planAttached ? "attached" : ""
        case .report:
            if facts.reportAttached { return "attached" }
            return facts.isScout ? "still out" : ""
        case .thread: return facts.messages > 0 ? "\(facts.messages)" : ""
        case .attachments: return facts.attachments > 0 ? "\(facts.attachments)" : ""
        case .documents: return facts.documents > 0 ? "\(facts.documents)" : ""
        case .crew: return facts.crewStages > 0 ? "\(facts.crewStages)" : ""
        case .dependencies:
            return facts.dependencies > 0 ? "\(facts.dependencies)" : ""
        case .manualCheck: return facts.manualNote ? "note" : ""
        case .timeline: return facts.age
        case .more: return facts.hidden > 0 ? "\(facts.hidden)" : ""
        default: return ""
        }
    }

    /// The whole closed-row text, in one place so both surfaces draw it the
    /// same: `"WHAT CHANGED · 4 files"`, or the bare label.
    static func rowText(_ s: Section, facts: Facts) -> String {
        let tail = summary(s, facts: facts)
        return tail.isEmpty ? s.rawValue : s.rawValue + " · " + tail
    }
}

/// The card timeline's shared rule. **Byte-identical to
/// `panel/Sources/BobPanel/CardTimeline.swift`** from the line below, pinned
/// by `host/tests/test_card_timeline.py`: edit the panel's copy and paste.
enum CardTimeline {
    /// The words for a moment the daemon did not witness, and for the gap
    /// after it. The daemon's own constant, mirrored.
    static let notObserved = "not observed"
    /// The daemon's `RECENT_ONLY_NOTE`, mirrored for a client that wants to
    /// recognise it; drawn from the report verbatim, never from here.
    static let recentOnlyNote = "Permission asks older than a day are not kept."

    /// One drawn row. `when` is the moment as a short local date-time or
    /// `notObserved`; `gap` is the elapsed figure since the previous row,
    /// the daemon's gap words, or nil on the first row. Words only.
    struct Row: Identifiable, Equatable {
        let id: Int
        let label: String
        let note: String
        let when: String
        let gap: String?
        let durable: Bool
    }

    /// `"12s"`, `"5m"`, `"2h 10m"`, `"3d 4h"`; `""` for nil or a negative.
    /// Matches the daemon's `elapsed_text` exactly.
    static func elapsed(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "" }
        let total = Int(seconds)
        if total < 60 { return "\(total)s" }
        var minutes = total / 60
        if minutes < 60 { return "\(minutes)m" }
        var hours = minutes / 60
        minutes -= hours * 60
        if hours < 24 {
            return minutes > 0 ? "\(hours)h \(minutes)m" : "\(hours)h"
        }
        let days = hours / 24
        hours -= days * 24
        return hours > 0 ? "\(days)d \(hours)h" : "\(days)d"
    }

    /// A moment as a short local date-time, or `notObserved`.
    static func when(_ at: Double?) -> String {
        guard let at, at > 0 else { return notObserved }
        let formatter = DateFormatter()
        formatter.setLocalizedDateFormatFromTemplate("d MMM HH:mm")
        return formatter.string(from: Date(timeIntervalSince1970: at))
    }

    /// The report's steps as rows, in the daemon's order: the first row
    /// has no gap; a row whose `since_previous_seconds` is present draws
    /// its elapsed figure; otherwise the daemon's gap words, or nil where
    /// it named none.
    static func rows(_ report: CardTimelineReport) -> [Row] {
        var out: [Row] = []
        for (index, step) in report.steps.enumerated() {
            var gap: String? = nil
            if index > 0 {
                if let since = step.sincePreviousSeconds {
                    gap = elapsed(since)
                } else if !step.gap.isEmpty {
                    gap = step.gap
                }
            }
            out.append(Row(id: index,
                           label: step.label.isEmpty ? step.kind : step.label,
                           note: step.note,
                           when: step.observed ? when(step.at) : notObserved,
                           gap: gap,
                           durable: step.durable))
        }
        return out
    }

    /// `"Waiting for your review · 2d 4h"`, or the label with `notObserved`
    /// where the entry moment was never seen; nil where nothing is waiting.
    /// `now` is the report's own clock, so nothing here ticks.
    static func openLine(_ report: CardTimelineReport, now: Double) -> String? {
        guard let open = report.open, !open.label.isEmpty else { return nil }
        guard let since = open.since else {
            return open.label + " · " + notObserved
        }
        return open.label + " · " + elapsed(now - since)
    }
}

/// Where a card's plan stands, decided from what the Mac already sends: the
/// approved digest, the plan's current digest and the approval time. The
/// digest rule is the panel's `approveControl` verbatim, and `.changed` is
/// exactly the state the Mac's plan gate refuses a Start with. Pure — no view
/// state, no clock — so a repaint cannot change the answer.
private enum PlanApprovalStatus {
    case notYet
    case approved(Date?)
    case changed

    static let notYetWords = "not yet approved"
    static let changedWords = "plan changed since approval"
    static let approvedPrefix = "approved on "
    /// The button's whole height, label plus `AlarmOutline`'s two paddings.
    static let touchFloor: CGFloat = 44

    static func decide(approved: String, digest: String,
                       approvedAt: Double) -> PlanApprovalStatus {
        if approved.isEmpty { return .notYet }
        if approved == digest {
            // `0` is "never stamped", not the first second of 1970.
            return .approved(approvedAt > 0
                             ? Date(timeIntervalSince1970: approvedAt) : nil)
        }
        return .changed
    }

    var sentence: String {
        switch self {
        case .notYet:
            return PlanApprovalStatus.notYetWords
        case .changed:
            return PlanApprovalStatus.changedWords
        case .approved(let date):
            guard let date else { return "approved" }
            let formatter = DateFormatter()
            formatter.dateStyle = .medium
            formatter.timeStyle = .short
            return PlanApprovalStatus.approvedPrefix + formatter.string(from: date)
        }
    }

    var ink: Color {
        switch self {
        case .notYet: return Theme.dim
        case .approved: return Theme.phosphor
        case .changed: return Theme.amber
        }
    }

    var offersButton: Bool {
        if case .approved = self { return false }
        return true
    }

    var hint: String {
        switch self {
        case .notYet:
            return "Records this wording of the plan as the one that may be started."
        case .changed:
            return "The plan was edited after it was approved. "
                + "Approving records this wording as the one that may be started."
        case .approved:
            return ""
        }
    }
}

/// What the priority box may hold, judged as it is typed rather than at
/// Save: the store's `normalise_priority` rule — empty is "no opinion",
/// otherwise ASCII digits from 0 to 100 — restated so the sentence beside
/// the box and the daemon's refusal can never disagree. Copied byte-equal
/// into `ios/BobPhone/CardDetailView.swift` and pinned by
/// `host/tests/test_priority_input.py`.
enum PriorityInput {
    static let limit = 100
    static let words = "a whole number from 0 to 100, or empty"

    /// `nil` where the text is fine; otherwise one short sentence.
    static func problem(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return nil }
        guard trimmed.allSatisfy({ $0.isASCII && $0.isNumber }),
              let number = Int(trimmed) else { return words }
        return number > limit ? words : nil
    }
}
