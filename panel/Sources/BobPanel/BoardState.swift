import AppKit
import SwiftUI

extension Foundation.Notification.Name {
    /// The footer's **Open Board** row. Posted on the local `NotificationCenter`
    /// like `.panelDidJump`, and for the same reason: the board window lives
    /// in *this* process, so the menu bar has nothing to do with it and no
    /// way to open it. A view has no handle on `AppDelegate` either.
    static let panelOpenBoard = Foundation.Notification.Name("bob.panelOpenBoard")
    /// The ⋯ button asking for the settings window (`SettingsWindow.swift`),
    /// posted for the same reason: the window lives in this process and a view
    /// has no handle on `AppDelegate`.
    static let panelOpenSettings = Foundation.Notification.Name("bob.panelOpenSettings")
    /// Jump — from a row, a card, or "Open in editor" — is the user sending
    /// their attention to the editor. The panel yields so it is not covering
    /// the answer.
    static let panelDidJump = Foundation.Notification.Name("bob.panelDidJump")
    /// Jump to a session Dark Army itself hosts. The object is the session id.
    /// The panel selects that row so the agent's full pane — the live
    /// terminal — is the thing on screen, and does **not** yield: there is
    /// no editor window to uncover.
    static let panelShowSession = Foundation.Notification.Name("bob.panelShowSession")
    /// A card detail asking the main panel to reveal an exact current card.
    static let panelShowCard = Foundation.Notification.Name("bob.panelShowCard")
    /// The Inbox's Open log press asking for the access-log window
    /// (`AccessLogWindow.swift`), `panelOpenSettings`'s copy and for its
    /// reason: the window is this process's and a view has no handle on
    /// `AppDelegate`.
    static let panelOpenAccessLog = Foundation.Notification.Name("bob.panelOpenAccessLog")
}

/// The exact store-wide Done scope a person confirmed. Count alone is not a
/// token: one card can leave while another enters and preserve it.
struct BulkClearDoneScope: Equatable {
    let count: Int
    let token: String

    init(board: Board) {
        count = board.doneCount
        token = board.doneClearToken
    }
}

/// Initial intent, two confirmations, the request, then the authoritative
/// snapshot. The HTTP answer does not remove cards from the view.
enum BulkClearDoneGate: Equatable {
    case idle
    case firstConfirmation(BulkClearDoneScope)
    case finalConfirmation(BulkClearDoneScope)
    case requesting(BulkClearDoneScope)
    case awaitingSnapshot(BulkClearDoneScope)
}

/// Everything the board holds that is not a card: which project is shown, which
/// card is open in the detail sheet, which Start is armed, which Delete is armed,
/// and which cards are waiting on a session to appear.
///
/// **The arm lives here and nowhere else.** Two copies of a gate is not a gate —
/// that is `RowActions`' lesson, learned when keyboard state kept its own copy
/// and would have armed an invisible one while the button still said "Stop".
@MainActor
final class BoardState: ObservableObject {
    /// These two seams are used by the async editor and tested with actual
    /// ragged responses. A late response gets no authority over another draft.
    @discardableResult
    func applyOutcomeReport(_ report: OutcomeReport, cardId: String,
                            expectedRevision: Int) -> Bool {
        guard editing == cardId,
              expectedRevision >= draft.outcomeRevision,
              !report.available || report.matches(cardId: cardId, revision: expectedRevision) else { return false }
        guard report.available else { return true }
        if let current = report.card {
            // First load fills the boxes. A later, newer report refills
            // them only while the boxes still hold exactly what was loaded
            // — a plan attaching mid-edit seeds the objective on the
            // daemon, and untouched boxes should show that rather than
            // hold the old empties until a Save is refused; a typed word
            // is never replaced.
            let untouched = draft.outcomeLoaded
                && draft.outcome == draft.outcomeAsLoaded
                && current.revision > draft.outcomeRevision
            if !draft.outcomeLoaded || untouched {
                draft.outcome = current.objective
                draft.outcomeAsLoaded = current.objective
                draft.outcomeRevision = current.revision
                draft.outcomeLoaded = true
            }
        }
        return true
    }

    @discardableResult
    func applyOutcomeDecision(_ reply: OutcomeActionReply, cardId: String,
                              expectedRevision: Int) -> Bool {
        guard editing == cardId, draft.outcomeRevision == expectedRevision else { return false }
        if reply.ok, let revision = reply.revision {
            draft.outcomeRevision = revision
        } else {
            refusals[cardId] = reply.detail
        }
        return true
    }

    /// The ticked projects. **Empty means every project**, exactly as the old
    /// single-choice `""` did. `""` is a real member here — the "Other" pile,
    /// cards in no nameable workspace — and `Set([""])` is a selection, which
    /// is what finally distinguishes "Other" from "All projects" (the old
    /// `Picker` tagged both `""` and selecting Other silently showed
    /// everything). Every filter goes through `showsProject(_:)` so that
    /// distinction cannot be re-lost one call site at a time.
    @Published var outcomeSaving = false
    /// Changing it **keeps** a row's select mode and its ticks: a person
    /// who ticks cards, looks at another project and comes back finds the
    /// batch where they left it. The batch button's count names every
    /// ticked card, seen or not. It disarms a Start that waits on its
    /// confirmation, because the person confirmed a board they no longer see.
    @Published var projectFilter: Set<String> {
        didSet { if projectFilter != oldValue { rowBatchArmed = false } }
    }
    /// The board rows a person has flipped away from their default fold —
    /// `BoardRowFold`'s `flipped`, never the folded set itself. Seeded from
    /// the habit file beside `projectFilter`, saved on every toggle.
    @Published var rowFlips: Set<String>

    init() {
        // Seed from the panel's own habit file. Deliberately *not* pruned
        // against live projects: the first snapshot arrives after launch, so
        // an early clean would erase every tick on every start. A stale tick
        // stays visible (and individually clearable) through the menu's
        // union rule in `projectFilterItems`.
        projectFilter = Set(PanelPlacement.boardProjects() ?? [])
        rowFlips = BoardRowFold.decode(PanelPlacement.boardRowFlips())
        // The DRAFTS button has to be right on the first frame, not after the
        // first autosave tick.
        drafts = CardDrafts.all()
    }

    deinit {
        autosaveTimer?.invalidate()
    }

    /// Whether the filter admits this project. Empty set admits everything.
    func showsProject(_ name: String) -> Bool {
        projectFilter.isEmpty || projectFilter.contains(name)
    }

    /// The one ticked project, or nil under zero or several ticks. The lane
    /// header and the card chip key off this — "the picker has just said the
    /// project" is only true with exactly one tick.
    var singleProject: String? {
        projectFilter.count == 1 ? projectFilter.first : nil
    }

    func toggleProjectFilter(_ name: String) {
        if projectFilter.contains(name) {
            projectFilter.remove(name)
        } else {
            projectFilter.insert(name)
        }
        PanelPlacement.saveBoardProjects(projectFilter.sorted())
    }

    /// Widen the filter so `name`'s cards show, keeping every other tick — a
    /// banner reveal must not erase a persisted selection. An empty set
    /// already shows everything and stays empty, because inserting into it
    /// would *narrow* the board to one project.
    func includeProjectInFilter(_ name: String) {
        guard !showsProject(name) else { return }
        projectFilter.insert(name)
        PanelPlacement.saveBoardProjects(projectFilter.sorted())
    }

    func clearProjectFilter() {
        projectFilter = []
        PanelPlacement.saveBoardProjects([])
    }

    /// Whether this row is drawn folded right now. The rail's rule
    /// (`PanelView.isCollapsed`): **a search overrides a fold**, because a
    /// match hidden behind a heading is indistinguishable from no match.
    /// `reveal(_:)` writes the `#card` query, so `query` is non-empty during
    /// a reveal and the revealed card's row draws open with no
    /// reveal-specific fold code; clearing the search brings the folds back.
    func rowFolded(_ column: BoardColumn) -> Bool {
        BoardRowFold.drawnFolded(column.rawValue, flipped: rowFlips,
                                 searching: !query.isEmpty)
    }

    /// A press on a row's heading. Disarms, because moving on disarms;
    /// flips the row and saves the flipped set, in `rows` order, to the
    /// habit file — the same synchronous small merge write
    /// `toggleProjectFilter` makes.
    func toggleRowFold(_ column: BoardColumn) {
        disarm()
        rowBatchArmed = false
        // Folding keeps select mode and its ticks: the folded heading still
        // draws the batch control, so the count and CANCEL stay in reach.
        rowFlips = BoardRowFold.toggled(column.rawValue, flipped: rowFlips)
        PanelPlacement.saveBoardRowFlips(BoardRowFold.encode(rowFlips))
    }
    /// Filter over title, summary and prompt. Empty is no filter. Changing
    /// it keeps a row's select mode and disarms, `projectFilter`'s rule.
    @Published var query = "" {
        didSet { if query != oldValue { rowBatchArmed = false } }
    }
    /// Whether the board search field has the caret. PanelView folds this
    /// into `keys.editing` so letters typed here are not S/D/R on a row.
    @Published var searchFocused = false
    /// The id of the card whose face switcher holds keyboard focus, or `nil`
    /// where none does. PanelView folds it to `KeyRouter.controlFocused` so
    /// the key monitor lets ← / → / Space / Return / Escape reach the row
    /// instead of consuming them as triage. Not `searchFocused`: Escape on
    /// `editing` clears the board search, and a focused switcher must not.
    ///
    /// An identity, not a bool, on purpose: every card face reports through
    /// `noteSwitcherFocus`, and a snapshot that rebinds, reorders or recycles
    /// *another* card's tile fires that tile's disappearance — a bool would
    /// be cleared while the focused row still draws its ring, and the monitor
    /// would eat the arrows. The same shape settles focus moving straight
    /// from one face to another: two `onChange(of: focused)` land in one
    /// transaction, and the release from the old card cannot undo the claim
    /// of the new one.
    @Published var switcherFocused: String? = nil

    /// A card face's switcher took (`true`) or let go of (`false`) the
    /// keyboard. A claim always lands; a release lands only from the card
    /// that holds the slot.
    ///
    /// **Assigned only when the holder changes.** Every tile's switcher
    /// reports a release from `onDisappear`, and a lazy grid tears tiles down
    /// on every scroll step — a `@Published` assignment publishes even when
    /// it writes the value already held, and every tile, every row and the
    /// whole panel observe this object, so an unguarded `nil = nil` re-ran
    /// the board once per tile that scrolled out of view (measured: 42
    /// republishes in a 6,000pt scroll). That was the board's scroll stutter.
    func noteSwitcherFocus(card id: String, focused: Bool) {
        let next = Self.switcherFocus(after: switcherFocused, card: id,
                                      focused: focused)
        if next != switcherFocused { switcherFocused = next }
    }

    /// The pure rule behind `noteSwitcherFocus`, tabled in
    /// `ProviderSwitchTests`.
    nonisolated static func switcherFocus(after current: String?, card id: String,
                                          focused: Bool) -> String? {
        if focused { return id }
        return current == id ? nil : current
    }
    /// A press somewhere else in the panel asking the board to put one card
    /// in front of the reader — the pipeline band's ⌗, the detail header's
    /// `card ⌗` chip, a banner. Carries a sequence number, `CardFocusRouter`'s
    /// shape, so two presses on the same card both land and the applier can
    /// tell a new request from the one it already served. `BoardView` is the
    /// only reader and answers it with `reveal`: narrow, scroll, glow. It is
    /// a request to the board, never a filter by itself — the narrowing it
    /// causes is written by the applier, in one place, so a press cannot
    /// leave the board narrowed to a card it never scrolled to.
    @Published var revealRequest: CardRevealRequest?
    private var revealSeq = 0
    /// Open the Checks window on one check file — the card window's
    /// **Open in Checks**. Set once by the app delegate, which owns the window.
    var onOpenManualCheck: ((String) -> Void)?

    func requestReveal(_ cardId: String) {
        revealSeq += 1
        revealRequest = CardRevealRequest(id: cardId, seq: revealSeq)
    }

    /// The card a reverse jump from VS Code has just scrolled into view, if
    /// one is still glowing. Cleared on a timer by the applier — a highlight
    /// that never fades is a selection, and this board has none: it says
    /// "here it is", once, and then gets out of the way.
    @Published var revealedCard: String?
    /// The card open in the detail sheet, or `BoardState.newCard` for the
    /// composer. One surface for both: "show me everything about this card" and
    /// "let me change it" are the same screen, and splitting them meant a card
    /// whose whole prompt was three clipped lines had no place that showed it.
    @Published var editing: String?
    /// The one card whose Start is armed. Selecting anything else disarms it.
    @Published var armed: String?
    /// The one card whose **START HERE** is armed. A separate slot from
    /// `armed` on purpose: they are different places for the terminal to
    /// open, and one slot would let arming Start silently re-aim a pending
    /// HERE (or the other way around) and spawn in the editor after a
    /// press that asked for Dark Army's own terminal.
    @Published var armedHere: String?
    /// The one card whose **Delete** is armed. A separate slot from `armed` on
    /// purpose: they are different verbs with different consequences, and one
    /// slot would let arming Start silently re-aim a pending Delete. Deletion is
    /// the only irreversible thing on this board — there is no archive, the
    /// store says so — so it gets the same arm-then-confirm as Stop and Wrap up.
    @Published var deleteArmed: String?
    /// The one card whose **Done** is armed. A third slot for `deleteArmed`'s
    /// stated reason — one slot would let arming any of these silently re-aim
    /// the others — and it exists at all because moving a card into Done also
    /// types `/clear` into the live session bound to it. `/clear` has no undo,
    /// and every other `/clear` in this app is armed first (`WrapUpBar`, Stop,
    /// Retire, and Delete on this very card). Armed **only** where the press
    /// would really reach a session: see `BoardCardView.doneClears`.
    @Published var doneArmed: String?
    /// The one card whose **MERGE** is armed, and the one whose **Fix** is.
    /// Their own slots for `deleteArmed`'s stated reason: MERGE moves the
    /// project's main line and Fix starts an assistant, different acts, and
    /// one slot would let arming either silently re-aim the other or any of
    /// the above. Armed only on a Done card (`CardMerge.offered`); the
    /// daemon re-checks everything at the press.
    @Published var mergeArmed: String?
    @Published var fixArmed: String?
    /// The store-wide Done clear is deliberately three presses: intent, first
    /// confirmation, final confirmation. Count and exact membership ride
    /// together through every stage and are re-checked in SQLite.
    @Published var clearDoneGate = BulkClearDoneGate.idle
    /// A bulk refusal cannot live on a card: every Done card may be gone, so it
    /// has a line of its own in the column chrome.
    @Published var clearDoneRefusal = ""

    var clearingDone: Bool {
        switch clearDoneGate {
        case .requesting, .awaitingSnapshot: return true
        default: return false
        }
    }
    /// Cards whose Start was confirmed and whose session has not appeared yet.
    /// Cleared by the **snapshot**, never by the HTTP 200 — a 200 only says the
    /// daemon accepted the verb, and the session takes seconds to arrive. That
    /// is `RowActions.stopping`'s reasoning, and it applies here for longer.
    @Published var starting: Set<String> = []
    /// Cards whose **Delete** was confirmed and whose removal has not reached a
    /// snapshot yet. `starting`'s sibling and cleared the same way — by the
    /// board, never by the HTTP 200, which only says the daemon took the verb.
    /// Until then the card fades, spins and stops taking presses, because the
    /// alternative is a second and a third press at a card that is already
    /// going.
    @Published var deleting: Set<String> = []
    /// Cards whose **Refine** was pressed and whose `refineState` has not
    /// reached a snapshot yet. `starting`'s sibling in every particular:
    /// optimistic about the spinner only, taken back on a refusal, otherwise
    /// reconciled against the snapshot's own `refineState`.
    @Published var refining: Set<String> = []
    /// The row whose select mode is on, or nil. **One row at a time, one
    /// selection** — never a set per verb: the row names the verb
    /// (`RowSelection`), so a second select mode would be a second press
    /// fighting the first for the same tick boxes.
    @Published var selectingRow: BoardColumn? = nil
    /// Rows the person opened past `BoardVisible.rowTileCap` with
    /// "show all". View state for this window only: a fresh panel starts
    /// every long row short again.
    @Published var expandedRows: Set<BoardColumn> = []
    /// The cards ticked in that row, by id. Pruned against every snapshot, so
    /// a card that stops being tickable — refined by somebody else, moved,
    /// deleted — falls out of the batch by itself.
    @Published var rowSelection: Set<String> = []
    /// The daemon's own words for a refused batch press, drawn verbatim under
    /// the row's batch button. `refusals`' rule: nothing else on screen moves
    /// when a launch does not happen.
    @Published var rowBatchRefusal = ""
    /// Whether the Backlog row's START n TOGETHER has been pressed once and
    /// waits for its confirmation. Its own slot rather than `armed`, which
    /// names one card; cleared by leaving select mode, a project change
    /// (which leaves it) and a fold.
    @Published var rowBatchArmed = false
    /// The ticked cards themselves, in tick order, kept beside `rowSelection`
    /// so `RowSelection.admits` can compare a candidate's folder with the
    /// first card ticked. Refreshed from each snapshot by `reconcile`.
    private(set) var rowSelectionCards: [BoardCard] = []
    /// The one unplanned drop into In progress being confirmed, or nil.
    /// Plain published state — no timers, no tracking areas; the
    /// `.confirmationDialog` on `BoardView` is driven by it.
    @Published var pendingUnplanned: PendingMove?
    /// The daemon's own words for a refusal, per card. Nothing else on screen
    /// moves when a launch does not happen, so a discarded `detail` reads as a
    /// press that did not register.
    @Published var refusals: [String: String] = [:]
    /// The destination the pointer is currently over during a drag, so it can
    /// say so before the release rather than after. View state and nothing
    /// else — a drop is resolved against the snapshot, never against this.
    @Published var dropTarget: BoardDropTarget?
    /// Kept when `isTargeted(false)` clears the highlight before the drop
    /// lands. Not published: it is a drop-time latch, not a draw.
    var lastDropTarget: BoardDropTarget?
    var dropHandled = false
    @Published var draft = BoardDraft()
    /// Every composer shares `newCard`, so a late Prepare result must not
    /// land in the next draft. Identity for an in-flight Task, not a draw.
    var prepareGeneration: UInt = 0
    /// Staging folder for the open composer's copies. Minted per open, and
    /// it stays the folder name for the card's life — nothing renames it
    /// on save, so there is no rename race with the daemon.
    var stagingId = ""
    /// Relative paths (`<stagingId>/<name>`) of files copied for the open
    /// composer. Published so the sheet's list redraws as copies land.
    @Published var stagedAttachments: [String] = []
    /// Outstanding `CardAttachments.stage` copies for *this* `stagingId`.
    /// Save is held while this is > 0; Prepare joins them before sending
    /// the list. Published so the Save button redraws when a copy starts
    /// or lands. Counted per composer: a late `endStaging` from a closed
    /// sheet must not decrement the next one's count.
    @Published var stagingInFlight = 0
    /// Composer create is on the wire. The sheet stays open so a refusal
    /// can put the daemon's sentence back on the form with the copies
    /// still listed; Escape/Close are ignored so they cannot discard a
    /// folder the request still names.
    @Published var composerSaving = false
    /// Waiters for `waitForStaging`. Resumed when the count hits 0, or
    /// when the composer is discarded so a join cannot hang on a sheet
    /// that no longer exists.
    private var stagingWaiters: [CheckedContinuation<Void, Never>] = []
    /// Every stored composer draft, newest first — read from
    /// `card-drafts.json` and nowhere else. **No second count**: the DRAFTS
    /// button reads this list, nothing re-derives draft-ness from a snapshot
    /// field or a join, and the daemon has never heard of any of it.
    @Published var drafts: [CardDraft] = []
    /// Banks the open composer every `CardDrafts.autosaveInterval`. `[weak
    /// self]` in the closure form: a `Timer` holding `self` strongly would
    /// keep `BoardState` alive for the life of the run loop.
    private var autosaveTimer: Timer?

    static let newCard = "__new__"

    func arm(_ id: String?) {
        armed = id
        armedHere = nil
        deleteArmed = nil
        doneArmed = nil
        mergeArmed = nil
        fixArmed = nil
        rowBatchArmed = false
        resetClearDoneGate()
    }

    func armHere(_ id: String?) {
        armedHere = id
        armed = nil
        deleteArmed = nil
        doneArmed = nil
        mergeArmed = nil
        fixArmed = nil
        rowBatchArmed = false
        resetClearDoneGate()
    }

    func armDelete(_ id: String?) {
        deleteArmed = id
        armed = nil
        armedHere = nil
        doneArmed = nil
        mergeArmed = nil
        fixArmed = nil
        resetClearDoneGate()
    }

    func armDone(_ id: String?) {
        doneArmed = id
        armed = nil
        armedHere = nil
        deleteArmed = nil
        mergeArmed = nil
        fixArmed = nil
        resetClearDoneGate()
    }

    /// Arm MERGE on one card; any other armed verb is dropped.
    func armMerge(_ id: String?) {
        mergeArmed = id
        armed = nil
        armedHere = nil
        deleteArmed = nil
        doneArmed = nil
        fixArmed = nil
        rowBatchArmed = false
        resetClearDoneGate()
    }

    /// Arm Fix on one card; any other armed verb is dropped.
    func armFix(_ id: String?) {
        fixArmed = id
        armed = nil
        armedHere = nil
        deleteArmed = nil
        doneArmed = nil
        mergeArmed = nil
        rowBatchArmed = false
        resetClearDoneGate()
    }

    /// Anything that is not pressing the armed button counts as moving on, and
    /// moving on disarms — both gates, because a click elsewhere is not evidence
    /// for either of them.
    func disarm() {
        armed = nil
        armedHere = nil
        deleteArmed = nil
        doneArmed = nil
        mergeArmed = nil
        fixArmed = nil
        resetClearDoneGate()
    }

    // MARK: - The card verbs, one copy

    /// Start, Refine and the column move, here rather than on the tile so
    /// the board tile and the card window press the same code
    /// (`plans/2026-09-20-simplify-card-details.md`). `client` is passed
    /// in, `openEditor(_:client:)`'s reason: this is view state, and a held
    /// client would leave every call site wondering which is in charge of a
    /// fetch. `BoardView`'s drag route keeps its own copies on purpose.
    ///
    /// Optimistic only about the *spinner*, never about the outcome: it is
    /// taken back the moment the daemon refuses, and otherwise held until
    /// the snapshot stops calling the card `dispatching`.
    func startCard(_ card: BoardCard, client: DaemonClient,
                   skipPlanGate: Bool = false, ownTerminal: Bool = false) {
        refusals[card.id] = ""
        starting.insert(card.id)
        Task { @MainActor in
            let result = await client.boardDispatch(
                card.id, skipPlanGate: skipPlanGate,
                ownTerminal: ownTerminal)
            if !result.ok {
                starting.remove(card.id)
                if result.isPlanConfirmable && !skipPlanGate {
                    // The daemon gated what `startsUnplanned` could not see:
                    // `planPath` is set but the *file* is gone (a branch
                    // switch does that to plans/ routinely), so the armed
                    // label read "Really start?" and this press carried no
                    // flag. Fall into the same confirmation the unplanned
                    // drop uses — `BoardView`'s dialog, driven by this slot —
                    // so the gate stays a confirmation rather than a wall.
                    // From the card window that dialog rises in the main
                    // window behind it: the rare branch (the armed label
                    // carries the flag for the ordinary unplanned case), and
                    // a second dialog would be a second copy of the gate.
                    // `!skipPlanGate` keeps a flagged press that still came
                    // back gated from looping; `ownTerminal` rides through
                    // so START HERE does not fall back to the editor.
                    pendingUnplanned = PendingMove(
                        cardId: card.id,
                        column: BoardColumn.inProgress.rawValue,
                        beforeId: nil, dispatches: true,
                        planChanged: result.isPlanChangedRefusal,
                        ownTerminal: ownTerminal)
                } else {
                    refusals[card.id] = result.detail
                }
            }
            await client.refresh()
        }
    }

    /// `startCard`'s optimism, `startCard`'s take-back: the spinner is
    /// reconciled against the snapshot's own `refineState`.
    func refineCard(_ card: BoardCard, client: DaemonClient) {
        refusals[card.id] = ""
        refining.insert(card.id)
        Task { @MainActor in
            let result = await client.boardRefine(card.id)
            if !result.ok {
                refining.remove(card.id)
                refusals[card.id] = result.detail
            }
            await client.refresh()
        }
    }

    // MARK: - A row's select mode

    /// Turn `column`'s select mode on, empty. Entering another row's mode
    /// drops whatever the first row had ticked.
    func enterRowSelection(_ column: BoardColumn) {
        selectingRow = column
        rowSelection = []
        rowSelectionCards = []
        rowBatchRefusal = ""
    }

    /// Tick or untick one card. Untick always; tick only where
    /// `RowSelection.admits` holds for the row being selected.
    func toggleRowSelection(_ card: BoardCard, chrome: BoardChrome) {
        guard let row = selectingRow else { return }
        rowBatchRefusal = ""
        // A changed selection is not the one "Really start N?" confirmed.
        rowBatchArmed = false
        if rowSelection.contains(card.id) {
            rowSelection.remove(card.id)
            rowSelectionCards.removeAll { $0.id == card.id }
            return
        }
        guard RowSelection.admits(row, card: card, given: rowSelectionCards,
                                  chrome: chrome) else { return }
        rowSelection.insert(card.id)
        rowSelectionCards.append(card)
    }

    /// Leave select mode: the row, the ticks and the refusal line all go.
    func exitRowSelection() {
        selectingRow = nil
        rowSelection = []
        rowSelectionCards = []
        rowBatchRefusal = ""
        rowBatchArmed = false
    }

    /// START n TOGETHER, confirmed: one `board_start_batch` naming every
    /// ticked card, in board order. `refineSelected`'s optimism — every
    /// ticked card shows as starting at once, taken back the moment the
    /// daemon refuses. The daemon's report is drawn whether or not the press
    /// landed, START PROJECT's rule: under the batch button while selecting
    /// (a refusal keeps the ticks so the person can fix one and press
    /// again), and in START PROJECT's slot for the project once a landed
    /// press has left select mode.
    func startSelected(board: Board, client: DaemonClient) {
        let picked = board.cards.filter { rowSelection.contains($0.id) }
        let ids = picked.map(\.id)
        rowBatchArmed = false
        guard ids.count >= RowSelection.minimum else { return }
        rowBatchRefusal = ""
        for id in ids { refusals[id] = "" }
        starting.formUnion(ids)
        let root = picked.first?.root ?? ""
        Task { @MainActor in
            let result = await client.boardStartBatch(ids)
            if result.ok {
                exitRowSelection()
                if !root.isEmpty { refusals[root] = result.detail }
            } else {
                starting.subtract(ids)
                rowBatchRefusal = result.detail
            }
            await client.refresh()
        }
    }

    /// The Backlog row's batch button: the first press arms, the second
    /// fires. `startButton`'s arm-then-confirm, for the same reason.
    func pressStartSelected(board: Board, client: DaemonClient) {
        if rowBatchArmed {
            startSelected(board: board, client: client)
        } else {
            disarm()
            rowBatchArmed = true
        }
    }

    /// REFINE n TOGETHER: one `board_refine_batch` naming every ticked card,
    /// in board order. `refineCard`'s optimism exactly — every ticked card
    /// spins at once, taken back the moment the daemon refuses (with its
    /// words under the button), and otherwise held until the snapshot
    /// carries each card's own `refineState`. A landed press leaves select
    /// mode; a refused one keeps the ticks so the person can fix one and
    /// press again.
    func refineSelected(board: Board, client: DaemonClient) {
        let ids = board.cards.filter { rowSelection.contains($0.id) }.map(\.id)
        guard ids.count >= RowSelection.minimum else { return }
        rowBatchRefusal = ""
        for id in ids { refusals[id] = "" }
        refining.formUnion(ids)
        Task { @MainActor in
            let result = await client.boardRefineBatch(ids)
            if !result.ok {
                refining.subtract(ids)
                rowBatchRefusal = result.detail
            } else {
                exitRowSelection()
            }
            await client.refresh()
        }
    }

    /// **The rule, because we have now been bitten by it twice: every route
    /// back into a startable column clears the card's link.** `board_update`
    /// only ever writes the fields it is given, so a card moved into Backlog
    /// by a plain update keeps the `session_id` of the run that already
    /// happened — and `dispatch.guard` then refuses it forever with "this
    /// card is already being worked on". Back and Reopen had the identical
    /// bug, so the fix is here in the one function every column button goes
    /// through rather than at each call site. `BoardView.move(_:to:)` is the
    /// drag-and-drop half and follows the same rule off the same flag.
    ///
    /// `done` and `in_progress` stay a plain update — the link is exactly
    /// what they want to keep.
    func moveCard(_ card: BoardCard, to column: BoardColumn, client: DaemonClient) {
        Task { @MainActor in
            let fields = ["column_name": column.rawValue]
            let result = column.clearsSessionLink
                ? await client.boardReset(card.id, fields: fields)
                : await client.boardUpdate(card.id, fields: fields)
            refusals[card.id] = result.ok ? "" : result.detail
            await client.refresh()
        }
    }

    func armClearDone(scope: BulkClearDoneScope) {
        disarm()
        clearDoneGate = .firstConfirmation(scope)
        clearDoneRefusal = ""
    }

    func advanceClearDone(currentScope: BulkClearDoneScope) {
        guard case .firstConfirmation(let expected) = clearDoneGate,
              expected == currentScope else {
            noteDoneScopeChanged(currentScope)
            return
        }
        clearDoneGate = .finalConfirmation(expected)
    }

    func beginClearDone(currentScope: BulkClearDoneScope)
        -> BulkClearDoneScope? {
        guard case .finalConfirmation(let expected) = clearDoneGate,
              expected == currentScope else {
            noteDoneScopeChanged(currentScope)
            return nil
        }
        clearDoneGate = .requesting(expected)
        clearDoneRefusal = ""
        return expected
    }

    func finishClearDone(_ result: ActionResult, currentBoard: Board) {
        guard case .requesting(let expected) = clearDoneGate else { return }
        guard result.ok else {
            resetClearDoneGate()
            clearDoneRefusal = result.detail
            return
        }
        // A push can race ahead of HTTP 200. Enter the snapshot phase first,
        // then inspect the newest held board so that raced frame completes it.
        clearDoneGate = .awaitingSnapshot(expected)
        clearDoneRefusal = ""
        reconcileClearDone(with: currentBoard)
    }

    func cancelClearDone() {
        resetClearDoneGate()
        clearDoneRefusal = ""
    }

    func noteDoneScopeChanged(_ currentScope: BulkClearDoneScope) {
        switch clearDoneGate {
        case .firstConfirmation(let expected),
             .finalConfirmation(let expected):
            guard expected != currentScope else { return }
            resetClearDoneGate()
            clearDoneRefusal = "Done changed while you were confirming. "
                + "Nothing was cleared; confirm again against the new set."
        default:
            break
        }
    }

    private func resetClearDoneGate() {
        clearDoneGate = .idle
    }

    private func reconcileClearDone(with board: Board) {
        let current = BulkClearDoneScope(board: board)
        switch clearDoneGate {
        case .firstConfirmation, .finalConfirmation:
            noteDoneScopeChanged(current)
        case .awaitingSnapshot(let expected):
            if current != expected {
                resetClearDoneGate()
                clearDoneRefusal = ""
            }
        case .idle, .requesting:
            break
        }
    }

    /// Title, summary, prompt — the three things a person searching the
    /// board is looking at. Case and diacritic insensitive.
    ///
    /// One phrase is not text at all: `#card <hex>` names exactly one card by
    /// id (`CardToken`). It **skips the text scan entirely**, so a card whose
    /// title happens to carry the literal phrase is not a match.
    func matches(_ card: BoardCard) -> Bool {
        if let token = CardToken.parse(query) {
            return CardToken.matches(token: token, cardId: card.id)
        }
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return true }
        if card.title.localizedStandardContains(needle) { return true }
        if card.summary.localizedStandardContains(needle) { return true }
        if card.prompt.localizedStandardContains(needle) { return true }
        return false
    }

    func noteHover(_ target: BoardDropTarget, hovering: Bool) {
        if hovering {
            // A column stays targeted while the pointer is over a card or a
            // gap inside it, and that re-enter must not demote the inner
            // destination — otherwise a drop on a card becomes a column move.
            // Demote only while the *inner highlight is still up*. Once
            // `dropTarget` is nil the inner view has been left, and a column
            // enter is a real retarget (header / empty In progress), not a
            // leftover. Reading the latch here would freeze the last card
            // forever and turn "drop here to start" into a wait-on mark.
            // The heading is not in this test: it is the row's own sibling
            // view, pinned above the tiles and overlapping none of them, so
            // an enter there is always a retarget — and its own slot value
            // (`BoardDropTarget.heading`) is what keeps a late leave from it
            // out of the tiles' tint.
            if case .column = target {
                switch dropTarget {
                case .card, .gap: return
                default: break
                }
                // A leftover column enter after the drop was already consumed
                // must not start a second session. A new drag begins on a
                // card or a gap, not on the empty column.
                if dropHandled && lastDropTarget == nil { return }
            } else if lastDropTarget == nil {
                dropHandled = false
            }
            dropTarget = target
            lastDropTarget = target
        } else if dropTarget == target {
            // Highlight only. The latch has to survive until takeDrop —
            // SwiftUI clears hover before the drop lands, and nested
            // destinations are not inner-wins.
            dropTarget = nil
        }
    }

    func takeDrop(fallback: BoardDropTarget) -> BoardDropTarget? {
        if dropHandled { return nil }
        dropHandled = true
        let target = lastDropTarget ?? dropTarget ?? fallback
        lastDropTarget = nil
        dropTarget = nil
        return target
    }

    /// Open an existing card.
    ///
    /// `client` is passed in rather than held: `BoardState` is view state, and
    /// owning a reference to the network client would make every call site
    /// wonder which of the two is in charge of a fetch.
    /// `client` is **not** optional, and that is the
    /// point: a card whose prompt or summary arrived truncated cannot be
    /// saved until the real text is fetched, so a call site with no way to
    /// fetch it would open an editor whose Save is disabled forever with
    /// nothing on screen saying why. It used to default to nil for the
    /// composer's benefit; the composer has its own verb now, and this one
    /// cannot be called without the means to finish what it starts.
    func openEditor(_ card: BoardCard, client: DaemonClient) {
        // An in-flight create still names the current folder. Opening
        // another card would skip keep on a successful create, then
        // discard the copies the new card now owns.
        if composerSaving { return }
        // A composer displaced by an existing card used to leave its staging
        // to die at the next close. It banks instead — the fork lives inside
        // `bankComposerDraft()` so no route can pick wrong.
        bankComposerDraft()
        stopDraftAutosave()
        disarm()
        prepareGeneration += 1
        draft = BoardDraft(card)
        editing = card.id
        if card.promptTruncated || card.summaryTruncated {
            fetchFullCard(card.id, client: client)
        }
    }

    /// Open the composer — the same sheet with no card behind it. Needs no
    /// client: there is nothing to fetch, because there is nothing there yet.
    func openComposer(defaultProject: String, defaultRoot: String,
                      offeredTools: [String]) {
        // An in-flight create still names the current folder. Minting a
        // new one would discard copies the daemon is about to record.
        if composerSaving { return }
        disarm()
        prepareGeneration += 1
        // A contentful composer displaced by a fresh New card banks rather
        // than burns; an empty one still discards its folder.
        bankComposerDraft()
        stagingId = CardAttachments.mintStagingId()
        stagedAttachments = []
        refusals[BoardState.newCard] = ""
        draft = BoardDraft()
        draft.project = defaultProject
        draft.root = defaultRoot
        // Pre-select Claude only where the daemon says it can start it. The
        // field's own default stays "" so `resumeDraft` and `init(_ card:)`
        // never inherit a choice nobody made. `model` stays "" from `init()`.
        draft.tool = BoardDraft.defaultTool(offered: offeredTools)
        editing = BoardState.newCard
        startDraftAutosave()
    }

    /// Project a new card is filed under: the single tick if there is one;
    /// under several ticks, the old ladder (busiest live project, else first
    /// known name) restricted to the ticked set — a card filed under an
    /// unticked project would vanish from the board being looked at — with
    /// the first ticked name (sorted) as the floor; with nothing ticked,
    /// today's behaviour verbatim.
    func filingProject(board: Board, agents: Agents) -> String {
        if let single = singleProject { return single }
        if !projectFilter.isEmpty {
            if let live = agents.running.first(
                where: { showsProject($0.project) })?.project {
                return live
            }
            if let known = board.projectNames.first(
                where: { showsProject($0) }) {
                return known
            }
            return projectFilter.sorted().first ?? ""
        }
        if let last = PanelPlacement.composerRoot(),
           let hit = board.projects.first(where: { $0.root == last }) {
            return hit.name
        }
        return agents.running.first?.project
            ?? board.projectNames.first ?? ""
    }

    /// Folder Start would launch into, taken from the daemon's window list
    /// so the composer cannot offer a root the guard would refuse.
    func filingRoot(board: Board, agents: Agents) -> String {
        if projectFilter.isEmpty,
           let last = PanelPlacement.composerRoot(),
           board.projects.contains(where: { $0.root == last }) {
            return last
        }
        let want = filingProject(board: board, agents: agents)
        // Project `""` — the Other pile, a reachable filing answer now that
        // `Set([""])` is a real selection — has no root by definition.
        // Falling through to an arbitrary project's root seeded the composer
        // with project "" beside some named project's root, and the picker
        // (which reads the *root*) displayed a project a save would never
        // send. Unanswered is honest; mismatched is not.
        guard !want.isEmpty else { return "" }
        if let hit = board.projects.first(where: { $0.name == want }) {
            return hit.root
        }
        return board.cards.first(where: { $0.project == want && !$0.root.isEmpty })?
            .root ?? board.projects.first?.root ?? ""
    }

    func closeEditor() {
        // An in-flight create still names this folder. Closing would
        // discard copies the daemon is about to record, and would hide
        // the sheet that has to show a refusal.
        if composerSaving { return }
        // The card window's Start and Done arm the same slots the tile
        // reads (`armed`, `doneArmed`), so an arm left standing here would
        // outlive Escape / ⌘W / Close and leave the tile asking "Really
        // start?" for a press nobody can see any more.
        disarm()
        prepareGeneration += 1
        // Before `editing` is cleared: banking asks whether the composer is
        // the thing being closed. Six routes land here — Close, Escape, ⌘W,
        // the red button, `hide()`, `CardWindowController.close()` — and the
        // bank-or-discard fork lives inside `bankComposerDraft()` so not one
        // of them can pick wrong.
        bankComposerDraft()
        stopDraftAutosave()
        editing = nil
    }

    /// Throw away copies the composer made if the card was never saved.
    /// Save clears `stagingId` first so this is a no-op on success. The
    /// folder is discarded even when the staged list is still empty — a
    /// copy in flight has not listed its paths yet.
    func discardStagedAttachments() {
        let id = stagingId
        releaseStagingToDraft()
        guard !id.isEmpty else { return }
        Task.detached { CardAttachments.discard(id) }
    }

    /// `discardStagedAttachments`' bookkeeping **minus the folder removal**:
    /// the copies now belong to a draft row that names them, so deleting the
    /// folder here would throw away exactly the work being kept.
    func releaseStagingToDraft() {
        stagedAttachments = []
        stagingId = ""
        stagingInFlight = 0
        let waiters = stagingWaiters
        stagingWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    // MARK: - Drafts

    func reloadDrafts() {
        drafts = CardDrafts.all()
    }

    /// The record the open composer would be banked as, or nil when there is
    /// no composer, no identity, a create on the wire, or nothing worth
    /// keeping. **One predicate, three callers** — autosave, banking, and the
    /// tests — so the tick and the close can never disagree about what a
    /// draft is.
    private func composerDraftRecord() -> CardDraft? {
        guard editing == BoardState.newCard, !stagingId.isEmpty,
              !composerSaving,
              CardDrafts.worthKeeping(draft: draft, staged: stagedAttachments)
        else { return nil }
        return CardDraft(id: stagingId,
                         project: draft.project,
                         root: draft.root,
                         title: draft.title,
                         summary: draft.summary,
                         prompt: draft.prompt,
                         workflow: draft.workflow,
                         idea: draft.idea,
                         tool: draft.tool,
                         kind: draft.kind,
                         model: draft.model,
                         effort: draft.effort,
                         attachments: stagedAttachments,
                         updatedAt: Date().timeIntervalSince1970,
                         priority: draft.priority,
                         area: draft.area,
                         beneficiary: draft.outcome.beneficiary,
                         intendedBenefit: draft.outcome.intendedBenefit,
                         successCriterion: draft.outcome.successCriterion,
                         expanded: draft.expanded,
                         blockedBy: draft.blockedBy)
    }

    /// The autosave tick. Writes the row and leaves the folder where it is —
    /// it never clears or discards anything, because deletion is only ever a
    /// person's verb or a successful create.
    func autosaveDraftIfNeeded() {
        guard let record = composerDraftRecord() else { return }
        CardDrafts.upsert(record)
        reloadDrafts()
    }

    /// Keep the open composer's work, or discard it — the fork the whole
    /// feature turns on, in one place. Contentful: write the row, then
    /// release the staging *without* removing the folder, because the row now
    /// names it. Otherwise fall through to today's behaviour exactly.
    func bankComposerDraft() {
        guard let record = composerDraftRecord() else {
            discardStagedAttachments()
            return
        }
        CardDrafts.upsert(record)
        releaseStagingToDraft()
        reloadDrafts()
    }

    /// Put a stored draft back on screen. Deliberately **no fresh
    /// `mintStagingId()`**: resuming reuses the draft's own folder, so
    /// autosave keeps overwriting one row instead of breeding a row per
    /// reopen, and the staged copies are already in place. A file deleted on
    /// disk since the draft was written drops off the list rather than riding
    /// as a phantom the save would record.
    func resumeDraft(_ d: CardDraft) {
        guard !composerSaving else { return }
        bankComposerDraft()
        disarm()
        prepareGeneration += 1
        stagingId = d.id
        stagedAttachments = d.attachments.filter {
            CardAttachments.resolve($0) != nil
        }
        var next = BoardDraft()
        next.title = d.title
        next.summary = d.summary
        next.prompt = d.prompt
        next.project = d.project
        next.root = d.root
        next.tool = d.tool
        next.kind = d.kind
        next.model = d.model
        next.effort = d.effort
        next.workflow = d.workflow
        next.idea = d.idea
        next.outcome.beneficiary = d.beneficiary
        next.outcome.intendedBenefit = d.intendedBenefit
        next.outcome.successCriterion = d.successCriterion
        next.priority = d.priority
        next.area = d.area
        next.blockedBy = d.blockedBy
        next.expanded = d.expanded
        draft = next
        refusals[BoardState.newCard] = ""
        editing = BoardState.newCard
        startDraftAutosave()
        reloadDrafts()
    }

    private func startDraftAutosave() {
        stopDraftAutosave()
        autosaveTimer = Timer.scheduledTimer(
            withTimeInterval: CardDrafts.autosaveInterval,
            repeats: true) { [weak self] _ in
            Task { @MainActor in self?.autosaveDraftIfNeeded() }
        }
    }

    private func stopDraftAutosave() {
        autosaveTimer?.invalidate()
        autosaveTimer = nil
    }

    /// Keep the copies — the card now owns them. Call after create
    /// succeeds, before `closeEditor`. Does not discard the folder.
    /// In-flight copies must have finished first (`waitForStaging`),
    /// or `keepIfOwned` would see a cleared stagingId and unlink them.
    func keepStagedAttachments() {
        stagedAttachments = []
        stagingId = ""
    }

    /// A drop or Attach started copying into `id`. No-op if this is no
    /// longer the open composer's folder — the copy is then a leftover
    /// `keepIfOwned` will unlink.
    func beginStaging(for id: String) {
        guard stagingId == id, !id.isEmpty else { return }
        stagingInFlight += 1
    }

    /// One copy finished (accepted or refused). Ignored when `id` is not
    /// the current composer, so a late hop cannot hold or release the
    /// next New card's Save.
    func endStaging(for id: String) {
        guard stagingId == id else { return }
        guard stagingInFlight > 0 else { return }
        stagingInFlight -= 1
        guard stagingInFlight == 0 else { return }
        let waiters = stagingWaiters
        stagingWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    /// Join every in-flight copy for this composer. Returns immediately
    /// when none are outstanding. Discarding the composer resumes this
    /// too, so a closed sheet cannot leave Prepare parked.
    func waitForStaging() async {
        await withCheckedContinuation { continuation in
            if stagingInFlight == 0 {
                continuation.resume()
            } else {
                stagingWaiters.append(continuation)
            }
        }
    }

    var composerExpanded: Bool {
        ComposerPhase.expanded(flag: draft.expanded, fields: [
            draft.title, draft.summary, draft.prompt, draft.workflow,
            draft.outcome.beneficiary, draft.outcome.intendedBenefit,
            draft.outcome.successCriterion, draft.priority, draft.model,
            draft.effort, draft.area, draft.blockedBy])
    }
    func revealPhaseTwo() { draft.expanded = true }

    /// Why Save is held, in the words the sheet draws beside the button, or
    /// `nil` when nothing holds it. The single source: `saveHeld` is this
    /// function's boolean view, so a term added to one is a term added to
    /// both and the sentence can never disagree with the gate.
    ///
    /// The order is a decision, not the accident of an `||` chain.
    /// `composerSaving` leads because the press already happened and that is
    /// the most current fact (it cannot co-occur with a blank title — a save
    /// only starts from a titled draft). The human-actionable missing-title
    /// term comes next, so it wins over any transient term beside it: an
    /// empty title and a file still copying genuinely co-occur when something
    /// is dropped onto an unnamed composer, and naming the copy there tells
    /// the person to wait for something that will not unhold the button. The
    /// four transient terms follow, in the order they resolve themselves.
    func saveHoldReason(preparing: Bool) -> String? {
        if outcomeSaving { return "Saving the outcome…" }
        if composerSaving { return "Adding the card…" }
        if editing == BoardState.newCard, !composerExpanded {
            return ComposerPhase.holdReason
        }
        if draft.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Name it first"
        }
        if draft.promptTruncated { return "Waiting for the full instructions" }
        if draft.summaryTruncated { return "Waiting for the full description" }
        if preparing { return "Dark Army is still writing the instructions" }
        if stagingInFlight > 0 { return "Still copying files" }
        return nil
    }

    /// Save is held while the title is empty, the prompt or summary is a
    /// truncated preview, Prepare is writing, a dropped file is still copying,
    /// or a composer create is on the wire. `preparing` stays a view flag
    /// because a finished Prepare must not unhold a truncated prompt.
    /// The terms themselves live in `saveHoldReason`; this is the boolean
    /// view of it.
    func saveHeld(preparing: Bool) -> Bool {
        saveHoldReason(preparing: preparing) != nil
    }

    /// Composer create finished. Success forgets the staging id (the
    /// card now owns the folder) and closes; a refusal keeps the
    /// composer open with the same folder and list, and writes the
    /// daemon's sentence where the sheet draws it.
    ///
    /// `stillComposer` is false when the sheet moved off `__new__`
    /// while the request was in flight. Success still keeps the
    /// folder — the card owns it — but must not close whatever is
    /// open now, and a refusal must not paint the new sheet.
    func finishComposerCreate(ok: Bool, detail: String,
                              stillComposer: Bool = true) {
        composerSaving = false
        if ok {
            // **Before** `keepStagedAttachments()` clears `stagingId`, or the
            // row survives its own card. The row only — the card owns the
            // folder now. And because the id is then empty, the `closeEditor`
            // below finds no composer identity and banks nothing: the
            // ordering is self-enforcing rather than commented into place.
            CardDrafts.remove(stagingId)
            stopDraftAutosave()
            keepStagedAttachments()
            reloadDrafts()
            if !draft.root.isEmpty {
                PanelPlacement.saveComposerRoot(draft.root)
            }
            if stillComposer {
                closeEditor()
            }
        } else if stillComposer {
            refusals[BoardState.newCard] = detail
        }
    }

    /// Drop one staged copy from the composer. No-op while create is
    /// on the wire: the request already snapshotted the list.
    func unstageAttachment(_ rel: String) {
        guard !composerSaving else { return }
        stagedAttachments.removeAll { $0 == rel }
        if let path = CardAttachments.resolve(rel) {
            try? FileManager.default.removeItem(atPath: path)
        }
    }

    /// Fill the draft with the card's real instructions and description.
    ///
    /// Writes only a field whose truncated flag is still set: that editor is
    /// disabled, but its sibling is live, and overwriting both would drop
    /// keystrokes made while the GET was in flight. Guarded on the editor
    /// still being open *and still on the same card*: a sheet closed and
    /// reopened on a different card while this was in flight would otherwise
    /// have one card's prompt dropped into another's draft, and the person
    /// would save it without ever seeing the swap. A failed fetch leaves the
    /// truncated flags set, which leaves Save disabled — refusing to write
    /// is the right way to fail here.
    private func fetchFullCard(_ cardId: String, client: DaemonClient) {
        Task { @MainActor in
            guard let full = await client.boardCard(cardId) else { return }
            guard editing == cardId else { return }
            applyFetchedCard(full)
        }
    }

    /// Apply a fetched full row onto the draft. Internal so the sibling-
    /// overwrite rule can be pinned without a live daemon.
    func applyFetchedCard(_ full: BoardCard) {
        // The full read is a later copy of the same card, so the number the
        // Save guards against moves with the text it seeded.
        draft.revision = full.revision
        if draft.promptTruncated {
            draft.prompt = full.prompt
            draft.promptTruncated = false
        }
        if draft.summaryTruncated {
            draft.summary = full.summary
            draft.summaryTruncated = false
        }
    }

    /// Drop a card out of `starting` once the fleet agrees it is live — or once
    /// the daemon has given up on it and sent the card back to Backlog.
    func reconcile(with board: Board) {
        reconcileClearDone(with: board)
        if !starting.isEmpty {
            let stillWaiting = Set(board.cards.filter { $0.isDispatching }.map(\.id))
            starting.formIntersection(stillWaiting)
        }
        // A card the board no longer lists is gone: that is the snapshot
        // agreeing, and the spinner has nothing left to wait for. A delete the
        // daemon refused clears at the call site instead, with its reason.
        if !deleting.isEmpty {
            deleting.formIntersection(Set(board.cards.map(\.id)))
        }
        // `starting`'s rule for the refinement: held until the snapshot
        // itself carries a refine state (the daemon publishes the board
        // before answering the HTTP call, so the frame this reconciles
        // against already has it), or until the card is gone.
        if !refining.isEmpty {
            let stillRefining = Set(board.cards.filter(\.isRefining).map(\.id))
            refining.formIntersection(stillRefining)
        }
        // A ticked card that can no longer be ticked leaves the batch, and
        // the kept copies are refreshed so the folder rule reads today's.
        if let row = selectingRow, !rowSelection.isEmpty {
            let chrome = BoardChrome(board)
            let still = board.cards.filter {
                rowSelection.contains($0.id)
                    && RowSelection.tickable(row, card: $0, chrome: chrome)
            }
            let kept = Set(still.map(\.id))
            if kept != rowSelection { rowSelection = kept }
            rowSelectionCards = rowSelectionCards.compactMap { old in
                still.first { $0.id == old.id }
            }
        }
    }
}

/// The fields of the card editor, kept apart from `BoardCard` because a draft is
/// text somebody is still typing and a card is what the daemon holds.
struct BoardDraft {
    var outcome = OutcomeObjective()
    var outcomeRevision = 0
    /// The card's change number as it was when this draft was opened — the
    /// value Save guards against. `outcomeRevision`'s ring one step out: a
    /// number the store owns, carried on the draft so a Save is judged
    /// against the copy the person actually read.
    var revision = 0
    var outcomeLoaded = false
    /// The objective exactly as the last load put it in the boxes, so a
    /// newer report can tell "untouched" from "edited" without a flag per
    /// keystroke.
    var outcomeAsLoaded = OutcomeObjective()
    var confirmOutcomeScopeChange = false

    /// What a Save says about the objective, and it is nothing unless the
    /// objective was actually loaded into this draft. `outcomeLoaded` is
    /// set only by `OutcomeEditor`'s fetch, and that view mounts only once
    /// the "Benefit and success" fold is opened — so on a card saved with
    /// the fold closed the draft still holds `OutcomeObjective()`'s empty
    /// defaults, which are not the card's objective and must not be sent
    /// as it. The daemon runs its objective guard only when one of these
    /// keys is named (`_objective_guard`), so an empty answer here is a
    /// save of the card's own fields and nothing else.
    func objectiveFields() -> [String: String] {
        guard outcomeLoaded else { return [:] }
        var fields = outcome.fields
        fields["expected_outcome_revision"] = String(outcomeRevision)
        fields["confirm_outcome_scope_change"] = String(confirmOutcomeScopeChange)
        return fields
    }
    /// The assistant a fresh composer opens with.
    static let preferredTool = "claude"

    /// The composer's starting assistant, gated on the daemon's own offered
    /// list (`Board.tools`): with dispatch off, or from an older daemon, that
    /// list is empty and pre-selecting a name it lacks would put the word
    /// `Picker`'s selection outside its rows — a form claiming an assistant
    /// Dark Army cannot start. `""` there means "Nobody yet", exactly as before.
    static func defaultTool(offered: [String]) -> String {
        offered.contains(preferredTool) ? preferredTool : ""
    }

    var title = ""
    var summary = ""
    var prompt = ""
    var project = ""
    var root = ""
    var tool = ""
    /// The card's kind. `""` is a build card; `"scout"` is an investigation.
    /// Chosen at creation and editable in Prep. Not counted by `worthKeeping`
    /// — like `tool`, a pick alone is seed state.
    var kind = ""
    /// Which model the assistant runs on, `""` meaning Default. Reset to `""`
    /// whenever `tool` changes (see `BoardCardSheet`), the view-side mirror of
    /// the store's own clear-on-retool — so a model from the wrong provider
    /// can never be left behind on a draft.
    var model = ""
    /// The reasoning effort, `""` meaning Default. Reset to `""` whenever the
    /// tool changes or the model's list no longer offers it (see
    /// `BoardCardSheet`) — the draft-side mirror of the store's own clears.
    var effort = ""
    /// How important this card is, `"0"`..`"100"`, or `""` for "no opinion".
    /// A string rather than an `Int?` all the way to the wire: the store's
    /// column is TEXT, and `_board_fields` turns a JSON integer `0` into
    /// `""` — so a number box that sent `0` would silently clear the score.
    /// Carried in a banked composer draft like the other typed fields: the
    /// composer draws the box, so a person can type the number before the
    /// card exists, and closing the form must not throw it away.
    var priority = ""
    var area = ""
    var column = BoardColumn.prep.rawValue
    /// The specialists this card expects, as the person typed them — one per
    /// line or comma-separated, normalised by the store.
    var workflow = ""
    /// The whole thought, in one box, that Prepare writes the other four
    /// fields from. **Composer scratch**: `init(_ card:)` deliberately does
    /// not set it, because a card has no idea field — it is never saved, and
    /// lives only here and in `card-drafts.json`.
    var idea = ""
    /// Whether the composer has opened its second half. False on a fresh
    /// composer; true in `init(_ card:)` so a saved card's editor is never
    /// gated.
    var expanded = false
    /// "Start the work by itself the moment the plan lands", ticked before the
    /// card exists. Rides `boardCreate`; on a saved card the toggle is
    /// immediate and this is not consulted. Deliberately **not** carried in a
    /// banked draft — `refine`'s stated rule, for the same reason: a banked
    /// draft carries no intent about starting anything.
    var startWhenPlanned = false
    /// Newline-separated card ids this draft waits on. The composer sends it
    /// with the create (`boardCreate(blockedBy:)`), Prepare fills it only
    /// while it is empty (`DependencySuggestion`), and a project-picker move
    /// clears it — the choices were that project's. Saving an existing card
    /// does not send it: the card window's WAITS ON section writes it.
    var blockedBy = ""
    /// Newline-joined relative attachment paths, filled from the card on
    /// an existing-card open. The composer writes `stagedAttachments`
    /// instead; this is the read-only half.
    var attachments = ""
    /// True while `prompt` here is only the preview the snapshot carried.
    ///
    /// The editor must not save in this state: the whole card's instructions
    /// would be replaced by their first 400 characters, silently, with no undo
    /// — the single most destructive thing in this change, which is why the
    /// flag is stated by the daemon rather than guessed from a length here.
    var promptTruncated = false
    /// Same contract as `promptTruncated` for the plain-words description.
    var summaryTruncated = false

    init() {}

    init(_ card: BoardCard) {
        outcomeRevision = card.outcomeRevision
        revision = card.revision
        title = card.title
        summary = card.summary
        prompt = card.prompt
        project = card.project
        root = card.root
        tool = card.tool
        kind = card.kind
        model = card.model
        effort = card.effort
        priority = card.priority
        area = card.area
        blockedBy = card.blockedBy
        column = card.column
        workflow = card.workflow.joined(separator: "\n")
        attachments = card.attachments.joined(separator: "\n")
        promptTruncated = card.promptTruncated
        summaryTruncated = card.summaryTruncated
        expanded = true
    }
}

/// One ask to show a card on the board. Equatable so `.onChange` can watch
/// it; the sequence number is what makes a repeat press a change.
struct CardRevealRequest: Equatable {
    let id: String
    let seq: Int
}
