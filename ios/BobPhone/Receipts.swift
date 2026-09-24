import Foundation
import SwiftUI

/// What happened to every press, from *sent* to *accepted* to
/// *confirmed-by-what-you-can-see*.
///
/// The phone's two-state hold — a button that stops looking busy after twelve
/// seconds whether the Mac acted or not, and a press made out of reach that
/// is simply gone — is what this replaces. A receipt is written down **before**
/// the press goes, carries a one-time mark the Mac remembers for a while
/// (`command_receipts.py`), and closes only on evidence: the snapshot showing
/// what the press did. A press whose effect never turns up goes `stuck` and
/// **stays listed**, which is the whole point of that state.
///
/// The idempotency is the pair: the token makes a replay the same press
/// rather than a second one on the Mac's side, and `Outbox`'s own rule —
/// never resend what you can check — covers the window the Mac's memory-only
/// ledger forgets across a restart.

/// What a receipt watches for, to know its press actually happened.
///
/// The first four cases are **today's four settling predicates**, moved here
/// verbatim rather than restated, so the rules cannot fork: a row leaving the
/// live buckets, a row leaving `waiting` or standing on a different question,
/// a card leaving the board, and a pipeline dial reporting the asked-for
/// value.
enum ReceiptEffect: Codable, Equatable {
    /// The session should shortly stop being listed as live.
    case rowGone(sessionId: String)
    /// The session should shortly be listed **nowhere** — not live, not
    /// finished, not abandoned. The judge for a verb offered on a row that
    /// is *already* out of the live buckets (`delete_abandoned_agent`,
    /// whose subject is abandoned by definition): `.rowGone` would read
    /// "already happened" on the very snapshot that offers the button.
    case rowGoneAnywhere(sessionId: String)
    /// The row's question should shortly go, or change.
    case questionGone(sessionId: String, questionId: String)
    /// A reply typed at a waiting row: judged like `.questionGone` — the
    /// row leaving `waiting` or standing on a different question — **but
    /// it carries a payload** (the person's words), so it is never
    /// dropped unsent as goal-achieved (`evidenceBeforeSending`), and a
    /// row that stays waiting (blocked on a permission, a channel message
    /// not yet consumed) closes it `.done` after `ReceiptLedger.replyHold`
    /// rather than `.stuck` at the effect deadline: the 200 said the Mac
    /// delivered it, and the hold exists only for the duplicate window.
    case replyHold(sessionId: String, questionId: String)
    /// The card should shortly leave the board.
    case cardGone(cardId: String)
    /// The card should shortly be in this column.
    case cardColumn(cardId: String, column: String)
    /// The card's change number should shortly be at least this.
    case cardRevision(cardId: String, atLeast: Int)
    /// The card should shortly carry a session.
    case cardLinked(cardId: String)
    /// The board should shortly report this dial at this value.
    case preference(key: String, want: String)
    /// The store-wide Done set should shortly differ from this count+token.
    case doneScopeChanged(count: Int, token: String)
    /// The prompt should shortly stop being listed for that session.
    case permissionGone(sessionId: String, requestId: String)
    /// The card should shortly carry **this** plan approval. The digest
    /// travels with the effect because a card whose plan changed since an
    /// older approval already carries a non-empty `plan_approved`, and
    /// "any approval" would read as landed on the very snapshot that
    /// offers Approve. An empty digest (an older caller) falls back to
    /// "any approval".
    case planApproved(cardId: String, digest: String)
    /// The card should shortly name this assistant. A one-field toggle
    /// with no revision guard used to settle as `.none` on the Mac's 200,
    /// so the switcher went back to the *old* tile until the next board
    /// frame — from away, a minute later — and read as "nothing happened".
    case cardTool(cardId: String, tool: String)
    /// The card should shortly name this model (`""` is Default).
    case cardModel(cardId: String, model: String)
    /// The card should shortly show a refinement at work — a planning
    /// session, a plan, or the card out of Prep — for the same reason:
    /// Refine settling on the 200 alone put the button back to "Refine"
    /// before the frame said a planner had it.
    case cardRefining(cardId: String)
    /// Nothing on the board or the fleet says whether this landed
    /// (`register_push_token`, `prepare_card`). Settles on acceptance.
    case none
}

/// One press, written down before it goes.
struct Receipt: Identifiable, Codable, Equatable {
    /// **Is** the `command_token` the Mac dedupes on. A person's RETRY mints
    /// a fresh one, which is what makes recording a refusal safe.
    var id: String = ""
    var action: String = ""
    /// The `post` scope this press was made under — the session, else the
    /// card, else the bare verb. What the PENDING list groups by eye.
    var scope: String = ""
    /// Everything needed to send this press again, exactly as it went.
    var fields: [String: String] = [:]
    var state: State = .sent
    var createdAt: Double = 0
    var acceptedAt: Double = 0
    var attempts: Int = 0
    /// The Mac's own words about this press, never the phone's rewording,
    /// except for the two sentences below which say what only the phone can
    /// know.
    var lastError: String = ""
    /// A refusal's backoff, `OutboxEntry`'s two fields and its rule.
    var heldUntil: Double = 0
    var retryDelay: Double = 0
    var effect: ReceiptEffect = .none
    /// The pairing this press was minted under. A press that belongs to
    /// another pairing is never replayed against this Mac.
    var pairingToken: String = ""
    /// Face ID (or the passcode) passed at the moment of the press, from
    /// away. The sender hands it to `post`, so a queue that drains later
    /// never raises the sheet again for a press the person already
    /// authorised. Tolerant default false: an older record prompts.
    var authorised: Bool = false

    enum State: String, Codable {
        /// Written down at the press, never yet attempted. The queue's own
        /// state: the sender takes it in creation order.
        case queued
        /// A transmit is in flight right now. Skipped by the sender; a
        /// relaunch reads it back as `.sent` (an interrupted transmit may
        /// have landed with its reply lost, which is what a token replay
        /// is for).
        case sending
        /// Written down; the Mac has not answered.
        case sent
        /// The Mac answered 200. It has not been seen to have happened.
        case accepted
        /// Something the phone can see says it happened.
        case done
        /// The Mac answered, in words, and the person **read those words on
        /// the screen they pressed from**. Terminal — it is never re-sent —
        /// but deliberately *not listed*: `PENDING (n)` exists so a press
        /// nobody has heard about is visible, and filling it with every
        /// plan-gate refusal and every older-Mac 404 the person has already
        /// been shown inline is how the one row that matters gets lost.
        case refused
        /// Nobody has been told. The Mac accepted it and nothing ever
        /// showed, or a resend nobody was watching was refused, or the
        /// phone gave up. **Stays listed** — this is the state the whole
        /// section exists for, and the only one the machine never drops.
        case stuck
    }

    /// The two states nobody needs to see. Aged and capped; `.stuck` is not.
    static let finishedStates: Set<State> = [.done, .refused]

    /// The states a press is still in play in: nothing the Mac has finally
    /// answered. What `nextSendable`, `duplicate` and `mark` reason over.
    static let unresolved: Set<State> = [.queued, .sending, .sent]

    enum CodingKeys: String, CodingKey {
        case id, action, scope, fields, state, createdAt, acceptedAt
        case attempts, lastError, heldUntil, retryDelay, effect, pairingToken
        case authorised
    }

    init(id: String, action: String, scope: String, fields: [String: String],
         effect: ReceiptEffect, pairingToken: String) {
        self.id = id
        self.action = action
        self.scope = scope
        self.fields = fields
        self.effect = effect
        self.pairingToken = pairingToken
        self.createdAt = Date().timeIntervalSince1970
    }

    /// Tolerant, `OutboxEntry`'s rule: an unknown key is ignored, a
    /// wrong-shaped field reads as its default, and only a missing id makes
    /// the record absent — without a token there is nothing to dedupe on.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        guard !id.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .id, in: c, debugDescription: "a receipt with no token")
        }
        action = c.value(.action, "")
        scope = c.value(.scope, "")
        fields = c.value(.fields, [:])
        state = State(rawValue: c.value(.state, "sent")) ?? .sent
        createdAt = c.value(.createdAt, 0)
        acceptedAt = c.value(.acceptedAt, 0)
        attempts = c.value(.attempts, 0)
        lastError = c.value(.lastError, "")
        heldUntil = c.value(.heldUntil, 0)
        retryDelay = c.value(.retryDelay, 0)
        effect = (try? c.decode(ReceiptEffect.self, forKey: .effect)) ?? .none
        pairingToken = c.value(.pairingToken, "")
        authorised = c.value(.authorised, false)
    }

    /// Still inside a refusal's backoff window.
    var held: Bool { heldUntil > Date().timeIntervalSince1970 }

    /// The press in plain words. One table, `InboxKind.nextAction`'s shape:
    /// a wire verb (`board_update`, `answer_questions`) names neither the
    /// card nor the agent it was aimed at, and this section exists to be
    /// read by somebody deciding RETRY or DISCARD. A verb with no entry
    /// falls back to its own name rather than to silence — an unknown press
    /// still has to be listable.
    var wording: String {
        switch action {
        case PhoneActions.boardCreate: return "Write a card"
        case PhoneActions.boardUpdate: return "Save a card"
        case PhoneActions.boardReset: return "Unlink a card"
        case PhoneActions.boardDelete: return "Delete a card"
        case PhoneActions.boardClearDone: return "Clear all done items"
        case PhoneActions.boardDispatch: return "Start a card"
        case PhoneActions.boardRefine: return "Refine a card"
        case PhoneActions.boardApprovePlan: return "Approve a plan"
        case PhoneActions.boardUnqueue: return "Take a card out of the line"
        case PhoneActions.boardQueueMove: return "Move a card up the line"
        case PhoneActions.setBoardAutostart: return "Change the autostart dial"
        case PhoneActions.setBoardParallelRoot: return "Change how many run at once"
        case PhoneActions.reply: return "Send a message"
        case PhoneActions.permissionVerdict: return "Answer a permission prompt"
        case PhoneActions.dismiss: return "Dismiss a card"
        case PhoneActions.closeTerminal: return "Close a terminal"
        case PhoneActions.lowPriority: return "Switch to low priority"
        case PhoneActions.hideSession: return "Hide a session"
        case PhoneActions.stopSession: return "Stop an agent"
        case PhoneActions.deleteAgent: return "Retire an agent"
        case PhoneActions.answerQuestion, PhoneActions.answerQuestions:
            return "Answer a question"
        case PhoneActions.prepareCard: return "Prepare a card"
        case PhoneActions.inboxAck: return "Acknowledge a Needs you item"
        case PhoneActions.boardManualClear: return "Mark a check done"
        case PhoneActions.boardReview: return "Mark a result reviewed"
        case PhoneActions.boardPromote: return "Promote a report to a build card"
        default: return action
        }
    }

    /// What it was aimed at, for the same reason: the verb alone does not
    /// say *which* card or *which* agent. `scope` is the id `post` resolved
    /// — already on the record and, until now, drawn nowhere.
    var subject: String {
        let id = fields["card_id"] ?? fields["session_id"] ?? scope
        guard !id.isEmpty, !id.hasPrefix("verb:") else { return "" }
        return String(id.prefix(8))
    }

    /// What the PENDING list says about this press, in one line. The Mac's
    /// own words win wherever it has spoken, `queuedLine(autostart:)`'s rule.
    var line: String {
        if !lastError.isEmpty { return lastError }
        switch state {
        case .queued: return ReceiptLedger.queuedLine
        case .sending: return ReceiptLedger.sendingLine
        case .sent: return ReceiptLedger.sentLine
        case .accepted: return ReceiptLedger.acceptedLine
        case .stuck: return ReceiptLedger.stuckLine
        case .done, .refused: return ""
        }
    }

    /// The one word a row wears for this press — the QUEUE list's tag and
    /// the mark an acting control swaps its label for. Pure. `REFUSED` is
    /// a `.refused` record, or a `.stuck` one carrying the Mac's own words;
    /// a `.stuck` record wearing one of the phone's own sentences
    /// (`ReceiptLedger.phoneAuthored`) is `STUCK`, because nobody refused
    /// it — the phone stopped trying, or the pairing changed.
    var statusWord: String {
        switch state {
        case .queued: return "QUEUED"
        case .sending: return "SENDING…"
        case .sent: return "SENT"
        case .accepted: return "LANDING"
        case .done: return "DONE"
        case .refused: return "REFUSED"
        case .stuck:
            return ReceiptLedger.phoneAuthored.contains(lastError)
                ? "STUCK" : "REFUSED"
        }
    }

    /// The mark a control wears, as a screen reader says it: `QUEUED` is
    /// "Queued", `SENDING…` is "Sending", `SENT` is "Sent". A control whose
    /// drawn word is QUEUED must not be *spoken* as "Sending" — the two are
    /// different facts about the press. Pure; any other word is spoken as
    /// it is drawn.
    static func spoken(mark: String) -> String {
        switch mark {
        case "QUEUED": return "Queued"
        case "SENDING…": return "Sending"
        case "SENT": return "Sent"
        default: return mark
        }
    }
}

/// The Mac's words about one scope's last press, with the receipt they
/// were written on — so a screen can act on a refusal once per press
/// rather than on every redraw.
struct QueueNote: Equatable {
    let id: String
    let action: String
    let text: String
    let refused: Bool

    /// Whether the press behind this note was an answer or a reply — the
    /// verbs the answer box sends (`ReceiptLedger.heldWhileLanding`). A
    /// session's note is one per scope, and the agent screen holds two
    /// readers of it: the answer box takes an answer's refusal, the screen
    /// takes every other verb's, so one refusal is never drawn twice.
    var isAnswer: Bool { ReceiptLedger.heldWhileLanding.contains(action) }
}

@MainActor
final class ReceiptLedger: ObservableObject {
    /// Newest last, so the PENDING list reverses it and `sync` replays in
    /// the order the presses were made.
    @Published private(set) var receipts: [Receipt] = []

    /// How long an accepted press has to show its effect before it is called
    /// stuck. Well over the 12 s the controls stop dimming at — that figure
    /// decides whether a *button* is busy, this one decides whether the
    /// press **worked**, and conflating the two is what made a press that
    /// never happened read as finished.
    static let effectDeadline: TimeInterval = 45
    /// How many presses are remembered at all. A press older than this
    /// nobody has looked at is not a press anybody is waiting on.
    static let cap = 60
    static let retention: TimeInterval = 24 * 3600
    /// How many times one press may be re-sent before the phone stops on its
    /// own and says so. A record that could be replayed for ever is a press
    /// somebody was told was refused hours ago arriving the moment the Mac
    /// comes back — and, away, an unprompted Face ID sheet per attempt.
    static let maxAttempts = 5
    /// How long a replay can still be recognised on the Mac —
    /// `command_receipts.RECEIPT_SECONDS`, restated here because that is the
    /// number this side has to reason about. Past it, and across any daemon
    /// restart, a replay is not a replay: it **executes again**. So a press
    /// whose effect nothing can check (`.none` — `reply`,
    /// `answer_question(s)`) is given up rather than re-sent once the window
    /// has closed. A person can still press RETRY, which is a deliberate
    /// second press and says so.
    static let replayWindow: TimeInterval = 900
    /// How long an accepted **reply** stays held on a row that keeps
    /// waiting on the same question. About the away poll cadence: long
    /// enough that a double-tap on SEND is a duplicate rather than a second
    /// keystroke burst, short enough that a row blocked on something else
    /// (a permission, a channel message not yet consumed) does not turn a
    /// delivered message into a STUCK row saying nothing changed.
    static let replyHold: TimeInterval = 10

    /// The Mac's refusals that mean "already done": `inbox_ack.py`'s
    /// `INBOX_ACK_MISSING_REFUSAL` and `INBOX_ACK_STALE_REFUSAL`, byte for
    /// byte. An acknowledgement of an item that has since been cleared —
    /// by Dismiss all, by the Mac, by the row moving on — asks for what
    /// is already true; drawing it REFUSED with RETRY made a queue of red
    /// rows nobody could act on.
    nonisolated static let goalMetRefusals: Set<String> = [
        "that item is not waiting on you",
        "that item has changed or is already gone",
    ]
    nonisolated static func goalAlreadyMet(_ detail: String) -> Bool {
        goalMetRefusals.contains(detail)
    }

    nonisolated static let queuedLine = "queued — will go in turn"
    nonisolated static let sendingLine = "sending…"
    nonisolated static let sentLine = "sent — waiting for the Mac to answer"
    nonisolated static let acceptedLine = "the Mac accepted it"
    nonisolated static let stuckLine =
        "The Mac accepted it, but nothing on the board has changed."
    /// The ceiling's own sentence: tried and tried and never answered.
    /// Phone-authored, and one of the two written on a record the Mac never
    /// refused in words.
    nonisolated static let gaveUpLine =
        "Tried several times and never got an answer. RETRY to try again."
    /// The **other** one, and it is a different fact about a different
    /// record: a press whose replay window closed, which is routinely a
    /// press tried exactly once before the app was closed. Saying "tried
    /// several times" of it would be a count that never happened — and this
    /// wording is the more useful thing to tell somebody anyway, because it
    /// says what RETRY will actually do.
    nonisolated static let tooOldLine =
        "Too long ago to send safely — RETRY to send it as a new press."
    /// The one sentence a pairing change writes. A press made against
    /// another Mac must never be replayed against this one.
    nonisolated static let otherPairingLine = "this was sent to a different pairing"
    /// The away gate's own sentence: Face ID or the passcode was declined
    /// at the sheet. A first press that declines leaves no record; a
    /// **queued or replayed** press that declines — a press queued at home
    /// that fell back to the relay, or a `.sent` press re-taken outside
    /// the grace — is closed `.stuck` carrying this line, so it stays a row
    /// with RETRY rather than vanishing with no mark, no note and no row.
    nonisolated static let faceNeededLine =
        "Face ID or your passcode is needed to act from away."
    /// The sentences the phone writes on a `.stuck` record itself. Every
    /// other `lastError` is the Mac's, which is what `statusWord` reads as
    /// a refusal.
    nonisolated static let phoneAuthored: Set<String> = [
        stuckLine, gaveUpLine, tooOldLine, otherPairingLine, faceNeededLine,
    ]
    /// The verbs whose press stays held while it lands: an answer or a
    /// reply the Mac accepted is typed into a dialog that the snapshot
    /// still lists for a poll cadence, so `.accepted` counts as a
    /// duplicate for these too — a second copy under a fresh token would
    /// be a second keystroke burst, and `reply` has no daemon-side guard.
    nonisolated static let heldWhileLanding: Set<String> = [
        PhoneActions.answerQuestion, PhoneActions.answerQuestions,
        PhoneActions.reply,
    ]
    /// How long a refusal the person read stays on the QUEUE list, so the
    /// order of a chain — approved, then refused — can still be read back.
    static let refusedShown: TimeInterval = 600

    private let fm = FileManager.default

    private var support: URL? {
        try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask,
                    appropriateFor: nil, create: true)
    }

    private var storeURL: URL? { support?.appendingPathComponent("receipts.json") }

    // --- persistence ----------------------------------------------------

    func load() {
        guard let url = storeURL, let data = try? Data(contentsOf: url) else { return }
        let tolerated = (try? JSONDecoder().decode([Tolerated].self, from: data)) ?? []
        let floor = Date().timeIntervalSince1970 - Self.retention
        receipts = tolerated.compactMap(\.receipt)
            .filter { $0.createdAt >= floor
                || !Receipt.finishedStates.contains($0.state) }
            .map { receipt in
                // An interrupted transmit may have landed with its reply
                // lost; `.sent` plus the token replay is exactly the state
                // for that, and it is the same fall-through an older
                // build takes for a state it does not know.
                var out = receipt
                if out.state == .sending { out.state = .sent }
                return out
            }
            .sorted { $0.createdAt < $1.createdAt }
    }

    private struct Tolerated: Decodable {
        let receipt: Receipt?
        init(from decoder: Decoder) throws {
            receipt = try? Receipt(from: decoder)
        }
    }

    private func persist() {
        guard let url = storeURL else { return }
        guard let data = try? JSONEncoder().encode(receipts) else { return }
        try? data.write(to: url, options: [.atomic, .completeFileProtection])
    }

    /// Everything goes: an explicit un-pair, beside
    /// `answerDrafts.prune(keeping: [])`.
    func forget() {
        receipts = []
        if let url = storeURL { try? fm.removeItem(at: url) }
    }

    // --- the three states -----------------------------------------------

    /// Written down **before** the request goes.
    @discardableResult
    func open(action: String, scope: String, fields: [String: String],
              effect: ReceiptEffect, pairingToken: String) -> Receipt {
        var receipt = Receipt(id: UUID().uuidString, action: action,
                              scope: scope, fields: fields, effect: effect,
                              pairingToken: pairingToken)
        receipt.attempts = 1
        receipts.append(receipt)
        trim()
        persist()
        return receipt
    }

    /// Written down at the press and **not** sent: the queue's entry. The
    /// same record `open` writes, in `.queued` with no attempt spent; the
    /// sender (`PhoneClient.flushReceipts`) takes it in turn.
    @discardableResult
    func enqueue(action: String, scope: String, fields: [String: String],
                 effect: ReceiptEffect, pairingToken: String,
                 authorised: Bool) -> Receipt {
        var receipt = Receipt(id: UUID().uuidString, action: action,
                              scope: scope, fields: fields, effect: effect,
                              pairingToken: pairingToken)
        receipt.state = .queued
        receipt.attempts = 0
        receipt.authorised = authorised
        receipts.append(receipt)
        trim()
        persist()
        return receipt
    }

    /// The sender is taking this one now: `.queued` or `.sent` → `.sending`,
    /// and the attempt is counted here, so the ceiling and the record cannot
    /// disagree about how many there were (`noteAttempt`'s rule).
    func markSending(_ id: String) {
        note(id) {
            guard $0.state == .queued || $0.state == .sent else { return }
            $0.state = .sending
            $0.attempts += 1
        }
    }

    /// The Mac answered 200. Not "it happened" — that is `settled(against:)`.
    ///
    /// `revision` is the card's change number the reply carried
    /// (`board_update` answers it). A save judged by `.cardRevision` is
    /// thereby told what the store holds *now*: a save the store found
    /// nothing to change in — the field already held the words, a press
    /// that had landed and was replayed — answers a number below the
    /// expected step, and waiting for that step made a press that had in
    /// fact landed go STUCK, three times over on one card (21 Sep 2026).
    /// The goal — "the store holds my save" — is met at the number the
    /// Mac names, so the effect is lowered to it and the next frame
    /// closes the record.
    func accept(_ id: String, revision: Int? = nil) {
        note(id) {
            if case .cardRevision(let cardId, let atLeast) = $0.effect,
               let revision, revision < atLeast {
                $0.effect = .cardRevision(cardId: cardId, atLeast: revision)
            }
            $0.state = $0.effect == .none ? .done : .accepted
            $0.acceptedAt = Date().timeIntervalSince1970
            $0.lastError = ""
            $0.heldUntil = 0
            $0.retryDelay = 0
        }
    }

    /// The Mac answered in words, and that is final about this press: the
    /// record closes carrying what it said, and is never re-sent (a
    /// person's RETRY mints a fresh mark rather than replaying this one).
    ///
    /// `surfaced` is the whole of what decides whether PENDING lists it.
    /// A **first press** was made from a screen that draws the refusal
    /// inline — the plan gate on the card, the lease sentence on the
    /// control — so the person has read it and a second copy in PENDING is
    /// noise that buries the row that matters. A **resend** had no screen
    /// behind it: nobody saw that answer, so it stays listed.
    func refuse(_ id: String, detail: String, surfaced: Bool = false) {
        note(id) {
            // A refusal that says the press's goal is already met — the
            // item it would acknowledge is no longer waiting — is not a
            // press to retry: the record closes done, with nothing on it.
            if Self.goalAlreadyMet(detail) {
                $0.state = .done
                $0.lastError = ""
                $0.heldUntil = 0
                $0.retryDelay = 0
                return
            }
            $0.state = surfaced ? .refused : .stuck
            $0.lastError = detail
            $0.retryDelay = $0.retryDelay > 0
                ? min($0.retryDelay * 2, OutboxStore.backoffCap)
                : OutboxStore.backoffStart
            $0.heldUntil = Date().timeIntervalSince1970 + $0.retryDelay
        }
    }

    /// A transport failure leaves the receipt in `sent` — the press may have
    /// landed with its reply lost, and only a token replay can tell. Nothing
    /// is painted on the record: `OutboxStore.transportSentences`' rule.
    func holdOff(_ id: String) {
        note(id) {
            // A transmit that ended without a final answer goes back to
            // `sent`, which is the one state the sender re-takes.
            if $0.state == .sending { $0.state = .sent }
            $0.retryDelay = $0.retryDelay > 0
                ? min($0.retryDelay * 2, OutboxStore.backoffCap)
                : OutboxStore.backoffStart
            $0.heldUntil = Date().timeIntervalSince1970 + $0.retryDelay
        }
    }

    func remove(_ id: String) {
        receipts.removeAll { $0.id == id }
        persist()
    }

    /// The person has read the Mac's words about this press, on the screen
    /// it was pressed from: a queued press's refusal closes `.stuck` (the
    /// sender has no screen behind it), and drawing it *is* the surfacing
    /// a first press had at the return. It becomes `.refused` — REFUSED on
    /// the QUEUE list for `refusedShown`, aged and capped like any finished
    /// business — rather than a row for ever. A `.stuck` record wearing one
    /// of the phone's own sentences is left alone: nobody refused it, and
    /// RETRY there is what the row exists to offer.
    func read(_ id: String) {
        // Decided before `note` runs: every screen that draws the note
        // calls this on appearance and on change, and a read that changes
        // nothing must neither publish nor rewrite `receipts.json`.
        guard let record = receipts.first(where: { $0.id == id }),
              record.state == .stuck, !record.lastError.isEmpty,
              !Self.phoneAuthored.contains(record.lastError) else { return }
        note(id) { $0.state = .refused }
    }

    /// The transport is back: a poll just went through. Every `.sent`
    /// press's backoff was written by a transport failure (`holdOff`), so
    /// it ends here, and the next sweep re-takes them at poll cadence —
    /// under the same attempt ceiling — instead of a fresh `.queued` press
    /// on the scope waiting up to `OutboxStore.backoffCap` behind a
    /// sibling whose transport trouble is already over.
    func releaseHolds() {
        var changed = false
        for index in receipts.indices
        where receipts[index].state == .sent && receipts[index].heldUntil > 0 {
            receipts[index].heldUntil = 0
            changed = true
        }
        if changed { persist() }
    }

    /// One more try spent. Counted here rather than at the call site so the
    /// ceiling and the record cannot disagree about how many there were.
    func noteAttempt(_ id: String) {
        note(id) { $0.attempts += 1 }
    }

    /// Stop trying, and say which of the two reasons it was. Still listed,
    /// still retryable by hand — `stuck` is the state that does not
    /// disappear. The sentence is the caller's because the two routes are
    /// different facts: `gaveUpLine` counts attempts, `tooOldLine` names a
    /// window, and a record tried once must never wear the first.
    func gaveUp(_ id: String, why: String = ReceiptLedger.gaveUpLine) {
        note(id) {
            $0.state = .stuck
            $0.lastError = why
        }
    }

    /// A person's RETRY: a **fresh token**, the backoff reset, the record
    /// back to `sent`. A fresh token is what stops the Mac replaying the old
    /// refusal at us.
    func retry(_ id: String) {
        guard let index = receipts.firstIndex(where: { $0.id == id }) else { return }
        // A press still going out keeps its token: re-minting it mid-transmit
        // would make the in-flight answer a no-op on the old id and send the
        // press again under the new one. A queued press has not gone yet.
        guard receipts[index].state != .sending,
              receipts[index].state != .queued else { return }
        var again = receipts[index]
        again.id = UUID().uuidString
        again.state = .sent
        again.lastError = ""
        again.heldUntil = 0
        again.retryDelay = 0
        again.acceptedAt = 0
        // The ceiling is per press, and a person's RETRY *is* a new press.
        again.attempts = 0
        // And a new press is asked for the face again. The check that
        // passed at the original tap covered *that* press; carrying it
        // onto a re-send made hours later would let RETRY skip the away
        // gate for good. The relay rung asks at the sheet, as a first
        // press is asked.
        again.authorised = false
        receipts[index] = again
        persist()
    }

    private func note(_ id: String, _ change: (inout Receipt) -> Void) {
        guard let index = receipts.firstIndex(where: { $0.id == id }) else { return }
        change(&receipts[index])
        persist()
    }

    /// Bound the store, in-session as well as across a relaunch.
    ///
    /// `.done` and `.refused` are both finished business and both age out;
    /// `.stuck` never does, because it is exactly the press nobody has been
    /// told about and the machine dropping it is the disappearance this
    /// state exists to prevent. Called on every write, so a long session
    /// bounds itself rather than waiting for the next `load()`.
    private func trim() {
        let floor = Date().timeIntervalSince1970 - Self.retention
        receipts.removeAll {
            Receipt.finishedStates.contains($0.state) && $0.createdAt < floor
        }
        while receipts.count > Self.cap,
              let index = receipts.firstIndex(
                  where: { Receipt.finishedStates.contains($0.state) }) {
            receipts.remove(at: index)
        }
    }

    // --- the effect test ------------------------------------------------

    /// Close every accepted receipt whose effect the snapshot now shows, and
    /// call `stuck` the ones past `effectDeadline`. Driven at poll cadence
    /// from the existing `pruneSettling…` call sites.
    func settled(against snapshot: Snapshot) {
        guard !receipts.isEmpty else { return }
        let now = Date().timeIntervalSince1970
        var changed = false
        for index in receipts.indices {
            guard receipts[index].state == .accepted else { continue }
            if Self.landed(receipts[index].effect, in: snapshot)
                || Self.holdExpired(receipts[index], now: now) {
                receipts[index].state = .done
                changed = true
            } else if receipts[index].acceptedAt > 0,
                      now - receipts[index].acceptedAt > Self.effectDeadline {
                receipts[index].state = .stuck
                receipts[index].lastError = Self.stuckLine
                changed = true
            }
        }
        if changed {
            trim()
            persist()
        }
    }

    /// A reply's hold has run its course: the Mac accepted it
    /// `replyHold` ago and the row still stands on the same question. Only
    /// `.replyHold` ever expires this way — every other effect either
    /// shows or goes `.stuck` at `effectDeadline`. Pure.
    static func holdExpired(_ receipt: Receipt, now: Double) -> Bool {
        guard case .replyHold = receipt.effect else { return false }
        return receipt.acceptedAt > 0 && now - receipt.acceptedAt >= replyHold
    }

    /// Whether an effect is evidence that **this** press landed, rather
    /// than merely that the goal it named has since been reached.
    ///
    /// The distinction only matters *before* sending — `settled(against:)`
    /// runs on a receipt the Mac already answered 200, so there the press
    /// demonstrably landed and every effect is fair evidence. Before
    /// sending, the question is different: may this press be dropped
    /// unsent because somebody can see it already happened?
    ///
    /// `.cardGone`, `.rowGone`, `.cardColumn`, `.cardLinked` and
    /// `.preference` each describe a **goal now achieved** — the card is
    /// gone, the row is gone, the column is right — and re-sending would
    /// only ask for what is already true. `.cardRevision` does not: it
    /// carries a *payload* the predicate does not describe. A save that
    /// transport-failed still holds the person's typed words, and any other
    /// write on that card — the agent's `dark_army_close_card`, a save on the Mac
    /// — moves the number past `atLeast`. Dropping the receipt then would
    /// throw those words away with no line in PENDING, where re-sending
    /// gets a 409 that keeps them *and* shows what moved underneath.
    /// `.doneScopeChanged` is the same shape — any other Done
    /// arrival/departure moves the token, and dropping the receipt would
    /// forget a sweep that never ran; re-sending 409s with the Mac's words.
    /// `.replyHold` is the third: the person's typed words are the
    /// payload, and a row that left `waiting` before the sender took the
    /// press (the plan's own chain — an answer, then a follow-up message)
    /// is no reason to throw them away with no row and no note.
    static func evidenceBeforeSending(_ effect: ReceiptEffect,
                                      in snapshot: Snapshot) -> Bool {
        switch effect {
        case .cardRevision, .doneScopeChanged, .replyHold, .none:
            return false
        default:
            return landed(effect, in: snapshot)
        }
    }

    /// Whether one effect can be seen in a snapshot. Pure arithmetic on
    /// purpose — this is the seam `OfflineCacheTests` drives.
    static func landed(_ effect: ReceiptEffect, in snapshot: Snapshot) -> Bool {
        switch effect {
        case .none:
            return true
        case .rowGone(let sessionId):
            let a = snapshot.agents
            let live = Set((a.waiting + a.running + a.sleeping).map(\.sessionId))
            return !live.contains(sessionId)
        case .rowGoneAnywhere(let sessionId):
            let a = snapshot.agents
            let listed = a.waiting + a.running + a.sleeping + a.finished + a.abandoned
            return !listed.contains { $0.sessionId == sessionId }
        case .questionGone(let sessionId, let questionId),
             .replyHold(let sessionId, let questionId):
            var current: [String: String] = [:]
            for agent in snapshot.agents.waiting {
                current[agent.sessionId] = agent.question.id
            }
            return current[sessionId] != questionId
        case .cardGone(let cardId):
            return !snapshot.board.cards.contains { $0.id == cardId }
        case .cardColumn(let cardId, let column):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return card.column == column
        case .cardRevision(let cardId, let atLeast):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return card.revision >= atLeast
        case .cardLinked(let cardId):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return !card.sessionId.isEmpty || card.linkState == "dispatching"
        case .preference(let key, let want):
            let board = snapshot.board
            if key == PhoneClient.autostartSettlingKey {
                return (board.autostartEnabled ? "on" : "off") == want
            }
            if key.hasPrefix("parallel:") {
                let root = String(key.dropFirst("parallel:".count))
                return String(board.parallelOverrides[root] ?? 0) == want
            }
            return false
        case .doneScopeChanged(let count, let token):
            return snapshot.board.doneCount != count
                || snapshot.board.doneClearToken != token
        case .permissionGone(let sessionId, let requestId):
            return !snapshot.permissions.contains {
                $0.sessionId == sessionId && $0.requestId == requestId
            }
        case .planApproved(let cardId, let digest):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return digest.isEmpty
                ? !card.planApproved.isEmpty
                : card.planApproved == digest
        case .cardTool(let cardId, let tool):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return card.tool == tool
        case .cardModel(let cardId, let model):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return card.model == model
        case .cardRefining(let cardId):
            guard let card = snapshot.board.cards.first(where: { $0.id == cardId })
            else { return false }
            return !card.refineState.isEmpty
                || !card.refineSessionId.isEmpty
                || !card.planPath.isEmpty
                || card.column != "prep"
        }
    }

    /// Which effect a verb's press should be judged by. One table, so a verb
    /// added later has one place to say what proves it happened.
    static func effect(for action: String, fields: [String: String],
                       scope: String) -> ReceiptEffect {
        let session = fields["session_id"] ?? scope
        let card = fields["card_id"] ?? ""
        switch action {
        case PhoneActions.stopSession, PhoneActions.closeTerminal,
             PhoneActions.hideSession:
            return session.isEmpty ? .none : .rowGone(sessionId: session)
        case PhoneActions.deleteAgent:
            // Offered on an abandoned row only, which is out of the live
            // buckets by definition: judged by the row leaving every list.
            return session.isEmpty ? .none : .rowGoneAnywhere(sessionId: session)
        case PhoneActions.answerQuestion, PhoneActions.answerQuestions:
            // The answer hold: the Mac's 200 proves nothing about the row
            // (`answer_question` touches nothing on success), so the press
            // is judged by the question leaving the row, and the mark stays
            // SENT until the snapshot agrees.
            let question = fields["question_id"] ?? ""
            guard !session.isEmpty, !question.isEmpty else { return .none }
            return .questionGone(sessionId: session, questionId: question)
        case PhoneActions.reply:
            // A reply on a waiting row takes the same hold, decided by the
            // client against the snapshot (`PhoneClient.enqueue`); here,
            // with no row to read, it is unverifiable.
            return .none
        case PhoneActions.permissionVerdict:
            let request = fields["request_id"] ?? ""
            guard !session.isEmpty, !request.isEmpty else { return .none }
            return .permissionGone(sessionId: session, requestId: request)
        case PhoneActions.boardApprovePlan:
            return card.isEmpty ? .none
                : .planApproved(cardId: card, digest: fields["plan_digest"] ?? "")
        case PhoneActions.boardDelete:
            return card.isEmpty ? .none : .cardGone(cardId: card)
        case PhoneActions.boardDispatch:
            return card.isEmpty ? .none : .cardLinked(cardId: card)
        case PhoneActions.boardRefine:
            return card.isEmpty ? .none : .cardRefining(cardId: card)
        case PhoneActions.boardUpdate:
            guard !card.isEmpty else { return .none }
            if let column = fields["column_name"], !column.isEmpty {
                return .cardColumn(cardId: card, column: column)
            }
            // The two one-field toggles on the card screen: judged by the
            // card naming the value asked for, so the control wears the
            // mark until the frame agrees rather than snapping back to
            // the old value on the 200.
            if let tool = fields["tool"], !tool.isEmpty {
                return .cardTool(cardId: card, tool: tool)
            }
            if let model = fields["model"] {
                return .cardModel(cardId: card, model: model)
            }
            // A field save: the Mac's own change number is the proof, and
            // the guard already told the phone which one to expect.
            guard let expected = Int(fields["expected_revision"] ?? "")
            else { return .none }
            return .cardRevision(cardId: card, atLeast: expected + 1)
        case PhoneActions.setBoardAutostart:
            return .preference(key: PhoneClient.autostartSettlingKey,
                               want: fields["enabled"] ?? "")
        case PhoneActions.setBoardParallelRoot:
            let root = fields["root"] ?? ""
            return .preference(key: PhoneClient.parallelSettlingKey(root),
                               want: fields["limit"] ?? "")
        case PhoneActions.boardClearDone:
            guard let count = Int(fields["expected_count"] ?? "") else {
                return .none
            }
            let token = fields["expected_done_token"] ?? ""
            if token.isEmpty { return .none }
            return .doneScopeChanged(count: count, token: token)
        default:
            return .none
        }
    }

    /// Every press nobody has been told about, newest first — what PENDING
    /// draws. A refusal the person read on the screen they pressed from is
    /// finished business, not a pending press.
    var outstanding: [Receipt] {
        receipts.filter { !Receipt.finishedStates.contains($0.state) }.reversed()
    }

    /// What the QUEUE list draws, **oldest first** so the order can be read:
    /// every press still in play or stuck, plus a refusal the person read
    /// on the screen they pressed from while it is younger than
    /// `refusedShown` — so "approved, then the start was refused" reads as
    /// the chain it was.
    var queueRows: [Receipt] {
        Self.queueRows(in: receipts, now: Date().timeIntervalSince1970)
    }

    /// `queueRows`' arithmetic, on the array so the harness can drive it.
    static func queueRows(in receipts: [Receipt], now: Double) -> [Receipt] {
        let floor = now - refusedShown
        return receipts
            .filter {
                !Receipt.finishedStates.contains($0.state)
                    || ($0.state == .refused && $0.createdAt >= floor)
            }
            .sorted { $0.createdAt < $1.createdAt }
    }

    // --- the queue's arithmetic, pure ------------------------------------

    /// The next press the sender should take, or nil: the oldest eligible
    /// receipt whose scope has nothing older still in play.
    ///
    /// Walked in `createdAt` order. A receipt minted under another pairing
    /// is skipped; one in `.sending` is skipped (it is out right now); a
    /// `.sent` one is eligible only past its backoff and under the ceiling;
    /// a `.queued` one always. **A scope waits for its own earlier press**:
    /// a candidate whose scope has an older receipt in `unresolved` is not
    /// returned and the walk moves on to the next scope, which is what
    /// puts "approve, then start" on one card through in that order while
    /// two cards' presses may interleave — the mailbox serialises them
    /// anyway.
    ///
    /// `holding` is the scopes the sender must leave alone this sweep — a
    /// subject whose synchronous press (`post`'s own per-scope lock) is
    /// still out. Such a scope waits like one with a press in flight, so
    /// the sweep neither spends an attempt on a transmit that cannot start
    /// nor doubles its backoff for it.
    static func nextSendable(in receipts: [Receipt], pairingToken: String,
                             now: Double, holding: Set<String> = []) -> Receipt? {
        let ordered = receipts.sorted { $0.createdAt < $1.createdAt }
        var busyScopes = holding
        for receipt in ordered {
            guard receipt.pairingToken.isEmpty
                || receipt.pairingToken == pairingToken else { continue }
            guard Receipt.unresolved.contains(receipt.state) else { continue }
            // Whatever this one is, an earlier unresolved press on the
            // scope makes every later one on it wait.
            if busyScopes.contains(receipt.scope) { continue }
            busyScopes.insert(receipt.scope)
            switch receipt.state {
            case .queued:
                return receipt
            case .sent:
                if receipt.heldUntil <= now
                    && receipt.attempts < maxAttempts {
                    return receipt
                }
            default:
                break
            }
        }
        return nil
    }

    /// Whether the same press — same scope **and** same action — is already
    /// in play. Wider than "queued and not yet sent" on purpose: the bug the
    /// old per-scope lock existed for was a double-tap on a confirmed
    /// button, and a second copy sent after the first returned would
    /// execute twice under a fresh token.
    ///
    /// For the answer verbs (`heldWhileLanding`) an `.accepted` copy counts
    /// too: the Mac said yes, the question is still listed for a poll
    /// cadence, and a second copy would be typed again.
    ///
    /// **The payload counts.** A true double-tap carries identical
    /// `fields`; a genuinely different press on the same subject and verb
    /// — a reply with other words, a verdict on a second prompt, a
    /// `board_update` of the tool and then the model, ▲ then ▼, the
    /// autostart dial on then off — queues behind the first. The two answer
    /// verbs are keyed on `question_id` alone: a second answer to the same
    /// dialog is the same keystroke burst whichever option it picks.
    static func duplicate(action: String, scope: String,
                          fields: [String: String],
                          in receipts: [Receipt]) -> Bool {
        let held = heldWhileLanding.contains(action)
        let byQuestion = action == PhoneActions.answerQuestion
            || action == PhoneActions.answerQuestions
        return receipts.contains {
            $0.scope == scope && $0.action == action
                && (Receipt.unresolved.contains($0.state)
                    || (held && $0.state == .accepted))
                && (byQuestion
                    ? $0.fields["question_id"] == fields["question_id"]
                    : $0.fields == fields)
        }
    }

    /// The press a scope's controls are wearing right now: the newest
    /// receipt for the scope that is unresolved or accepted. `.accepted`
    /// counts — the Mac said yes, the effect has not shown yet — and is
    /// drawn as `SENT` by `mark`.
    static func marked(for scope: String, in receipts: [Receipt]) -> Receipt? {
        receipts
            .filter { $0.scope == scope
                && (Receipt.unresolved.contains($0.state) || $0.state == .accepted) }
            .max { $0.createdAt < $1.createdAt }
    }

    /// The word a scope's pressed control wears — `QUEUED`, `SENDING…` or
    /// `SENT` — or nil when nothing of that scope is in play.
    static func mark(for scope: String, in receipts: [Receipt]) -> String? {
        guard let receipt = marked(for: scope, in: receipts) else { return nil }
        switch receipt.state {
        case .queued: return "QUEUED"
        case .sending: return "SENDING…"
        case .sent, .accepted: return "SENT"
        default: return nil
        }
    }

    /// The Mac's own words about a scope's last press, drawn under the
    /// row: the newest receipt for the scope carrying a `lastError`, and
    /// only while it is **the newest receipt for the scope in any state** —
    /// a fresh press queued behind a refusal is the person moving on, and a
    /// later press the Mac accepted or that landed has superseded it, so
    /// the note goes rather than resurfacing once that press closes.
    ///
    /// Two things are never the note. A record older than `refusedShown`:
    /// every screen re-reads the note on appearance, and a Start refused
    /// hours ago ("no plan yet") would otherwise re-arm "Start unplanned?"
    /// on a card that has a plan by now — the same window the QUEUE list
    /// keeps a read refusal for. And a `.stuck` record wearing one of the
    /// phone's own `phoneAuthored` sentences: nobody refused it, the QUEUE
    /// list is where it lives with its RETRY, and an answer that stalled
    /// is not the box's note for ever.
    static func note(for scope: String, in receipts: [Receipt],
                     now: Double) -> QueueNote? {
        let scoped = receipts.filter { $0.scope == scope }
        guard let refused = scoped
            .filter({ !$0.lastError.isEmpty })
            .max(by: { $0.createdAt < $1.createdAt })
        else { return nil }
        let superseded = scoped.contains {
            $0.createdAt >= refused.createdAt && $0.id != refused.id
        }
        guard !superseded,
              now - refused.createdAt <= refusedShown,
              !(refused.state == .stuck
                  && phoneAuthored.contains(refused.lastError))
        else { return nil }
        return QueueNote(id: refused.id, action: refused.action,
                         text: refused.lastError, refused: true)
    }

    /// Presses this pairing may still act on. A record minted under another
    /// pairing's token is moved straight to `stuck` and never re-sent.
    func rebase(to pairingToken: String) {
        var changed = false
        for index in receipts.indices
        where !receipts[index].pairingToken.isEmpty
            && receipts[index].pairingToken != pairingToken {
            if receipts[index].state != .stuck {
                receipts[index].state = .stuck
                receipts[index].lastError = Self.otherPairingLine
                changed = true
            }
        }
        if changed { persist() }
    }

    /// Presses still in `sent` that may be re-sent now: the same token, so
    /// the Mac's ledger answers with the original result rather than doing
    /// it twice. `accepted` and `stuck` receipts are **never** re-sent — the
    /// Mac already answered one and gave up on the other — and a press past
    /// `maxAttempts` is not either.
    func resendable(pairingToken: String) -> [Receipt] {
        let now = Date().timeIntervalSince1970
        return receipts.filter {
            $0.state == .sent && $0.heldUntil <= now
                && $0.attempts < Self.maxAttempts
                && ($0.pairingToken.isEmpty || $0.pairingToken == pairingToken)
        }
    }

    /// Presses at the ceiling that have not yet been told so.
    ///
    /// The pairing test is `resendable`'s, and it is here for the same
    /// reason rather than because it currently changes an answer: `rebase`
    /// has already moved a foreign-pairing record out of `.sent` by the time
    /// this runs, so today the two agree — but a parameter that looks like a
    /// guard and is not one is how the next caller gets it wrong.
    func exhausted(pairingToken: String) -> [Receipt] {
        receipts.filter {
            $0.state == .sent && $0.attempts >= Self.maxAttempts
                && ($0.pairingToken.isEmpty || $0.pairingToken == pairingToken)
        }
    }
}
