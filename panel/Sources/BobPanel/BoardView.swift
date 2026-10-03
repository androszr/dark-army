import AppKit
import SwiftUI

// MARK: - The view

/// What a card-focus request should do against the board on screen right now.
///
/// Pure, and separate from the applier, because the interesting half is the
/// *waiting*: a request routinely lands before the snapshot that would carry
/// its card. `wait` keeps it, and `drop` consumes it the first time a real
/// board answers without one — without that, a request outlives the gesture
/// that made it and yanks the board an hour later when a card happens to bind
/// to that session.
enum CardReveal {
    enum Outcome: Equatable {
        /// No board yet — hold the request, the snapshot has not answered.
        case wait
        /// A real board, and this session has no card. Consume, touch nothing.
        case drop
        /// Scroll to this card and glow it.
        case reveal(BoardCard)
    }

    static func outcome(session: String, board: Board) -> Outcome {
        guard board.available else { return .wait }
        guard let card = board.card(forSession: session) else { return .drop }
        return .reveal(card)
    }

    /// Reverse jump from the editor (`card: true`): land on the board when
    /// this session has a card. A banner tap never asks for the card, so it
    /// still opens the agent. Hold the focus request while the board has
    /// not answered yet — same wait `outcome` already uses.
    static func openBoard(wantsCard: Bool, outcome: Outcome) -> Bool {
        guard wantsCard else { return false }
        if case .reveal = outcome { return true }
        return false
    }

    static func holdFocus(wantsCard: Bool, outcome: Outcome) -> Bool {
        wantsCard && outcome == .wait
    }
}

/// A search phrase that names exactly one card: `#card` followed by the
/// leading hex digits of a card id.
///
/// Pure statics, `BoardSearchBar`'s testability shape. It is an ordinary
/// search — it rides `state.query`, the × CLEAR button and Escape clear it,
/// and typing over it makes it a text search again — so "the narrowing never
/// lifts itself" holds by construction: nothing but the user writes `query`
/// after the gesture that aimed the board.
enum CardToken {
    /// The one place the marker is spelled.
    static let marker = "#card"

    /// The hex prefix this query names, or nil where it is ordinary text.
    ///
    /// The whole query must be the marker, whitespace, and one word of hex
    /// digits — nothing before it and nothing after. A bare marker, a marker
    /// mid-sentence, extra words or a non-hex word are all ordinary searches.
    static func parse(_ query: String) -> String? {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = trimmed.split(whereSeparator: { $0.isWhitespace })
        guard parts.count == 2 else { return nil }
        guard parts[0].lowercased() == marker else { return nil }
        let token = parts[1].lowercased()
        guard !token.isEmpty,
              token.allSatisfy({ $0.isHexDigit }) else { return nil }
        return token
    }

    /// Prefix matching: an id is 32 hex characters and the phrase carries the
    /// first eight of them.
    static func matches(token: String, cardId: String) -> Bool {
        cardId.lowercased().hasPrefix(token)
    }

    /// The phrase that names this card, in the panel's existing short-id
    /// convention.
    static func query(for card: BoardCard) -> String {
        "\(marker) \(card.id.prefix(8))"
    }

    /// The chip drawn beside the field while a token is up. The title is the
    /// **first** card in snapshot order the token matches — a hand-typed
    /// one-character token can name several, which is search behaviour rather
    /// than an error.
    static func chipLabel(resolvedTitle: String?) -> String {
        let title = (resolvedTitle ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty ? "⌗ card not on the board" : "⌗ \(title)"
    }
}

struct BoardView: View {
    /// Reduce Motion, handed to every `Motion` call on the board.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Held, **not observed**: the verbs and the press-time reads go through
    /// it, but the board redraws on `feed` — the client republishes on every
    /// agents frame, and observing it re-ran the board and every card each
    /// time a tool changed anywhere on the fleet.
    let client: DaemonClient
    /// What the board draws from: the board, the fleet rows its cards name,
    /// the card faces' board-wide facts — each republished only on change.
    @ObservedObject var feed: BoardFeed
    /// The Done column's fetch state, which `doneEmptyLine` draws. Its own
    /// publisher, observed here because the client no longer is.
    @ObservedObject var doneArchive: DoneArchive
    @ObservedObject var state: BoardState
    /// "Bring this session's card into view" from outside the panel — the
    /// reverse jump from VS Code. Defaulted in `init` so every other call
    /// site, preview and test builds unchanged.
    @ObservedObject var cardFocus: CardFocusRouter
    /// START PROJECT on the Backlog heading. **Nil is how the press stays
    /// absent** — launching switched off — and it is drawn only while the
    /// project picker names exactly one project, so there is a folder to
    /// aim at. Called with that project's root.
    var onStartProject: ((String) -> Void)? = nil
    /// The `RUN n/m` picker on the In progress heading: `(root, rung)`, 0 for
    /// back to the machine default. Same single-project rule.
    var onSetLimit: ((String, Int) -> Void)? = nil
    /// The card-isolation switch on the In progress heading: `(root, on)`.
    /// Same single-project rule; drawn only for a git project.
    var onSetIsolation: ((String, Bool) -> Void)? = nil
    /// The sequence number of the last card-focus request applied. The router
    /// does not consume its request (three things deliver it), so the applied
    /// mark lives here — `PanelView.appliedFocus`' shape.
    @State private var appliedCardFocus: Int?
    /// The AppKit scroll view under `rows`, captured by `ScrollViewFinder`
    /// so the reveal can wait for the narrowed layout's frame rather than
    /// guess a delay, and move the clip to it in one animated step. Nothing
    /// else uses it.
    @State private var scroller: NSScrollView?
    /// The card the next reported frame should scroll to. Set by `reveal`,
    /// spent by the first `RevealFrameKey` value that names it, so the scroll
    /// happens once per reveal and never re-runs on every frame the scroll
    /// itself changes.
    @State private var pendingScrollCard: String?
    /// Bumped by every reveal and folded into the row stack's `.id`, so a
    /// reveal always lays the board out afresh. Without it a reveal over a
    /// search that was *already* up (both queries non-empty, so no fold
    /// changes and the `.id` stays put) kept the lazy stack's retained
    /// section heights and scroll offset from the old, longer result: the
    /// one revealed card sat outside the viewport, never laid out, never
    /// reported its frame, and the board read as empty. A second press on
    /// the same card reported no *new* frame either, so it did nothing.
    @State private var revealEpoch = 0
    @FocusState private var searchFieldFocused: Bool
    /// The drafts list. `@State` on the view is enough — the sheet is modal,
    /// and its own arms live inside it for `BoardCardSheet`'s stated reason:
    /// a shared `BoardState` slot would leave something behind it armed after
    /// it closed.
    @State private var showingDrafts = false
    /// This redraw's `BoardVisible`, for the row functions — see
    /// `BoardVisible.Slot`.
    private let visibleSlot = BoardVisible.Slot()

    /// The memberwise signature every call site already uses, spelled out so
    /// the two observed publishers come off the client. `cardFocus` is
    /// optional only because a default argument cannot construct a
    /// main-actor object; absent means a router of the view's own, as before.
    init(client: DaemonClient, state: BoardState,
         cardFocus: CardFocusRouter? = nil,
         onStartProject: ((String) -> Void)? = nil,
         onSetLimit: ((String, Int) -> Void)? = nil,
         onSetIsolation: ((String, Bool) -> Void)? = nil) {
        self.client = client
        _feed = ObservedObject(wrappedValue: client.boardFeed)
        _doneArchive = ObservedObject(wrappedValue: client.doneArchive)
        _state = ObservedObject(wrappedValue: state)
        _cardFocus = ObservedObject(wrappedValue: cardFocus ?? CardFocusRouter())
        self.onStartProject = onStartProject
        self.onSetLimit = onSetLimit
        self.onSetIsolation = onSetIsolation
    }

    private var board: Board { feed.board }

    /// The visible cards of this redraw, computed once at the top of `body`.
    private var visible: BoardVisible { visibleSlot.value }

    /// Recompute this redraw's `BoardVisible` into the slot. `body` calls it
    /// first; a `BoardRowRedraw` calls it again before drawing its row, so
    /// a row redrawn on its own reads the current filter, never the pass
    /// of whichever body last ran.
    private func refreshVisible() {
        visibleSlot.refresh(board: board, state: state)
    }

    var body: some View {
        refreshVisible()
        return VStack(spacing: 0) {
            MacOutcomeProjects(client: client)
            if !board.available {
                unavailable
            } else if board.isStoreEmpty {
                empty
            } else {
                searchBar
                createRow
                if visible.anyVisible || board.doneCount > 0 {
                    rows
                } else {
                    noMatches
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .onChange(of: board) { _, new in
            state.reconcile(with: new)
            syncCardFocus()
        }
        // The three deliverers, `PanelView.syncFocus`'s shape: the request
        // landing, the next board, and the view appearing with one already
        // pending. All idempotent by sequence number.
        .onChange(of: cardFocus.request) { _, _ in syncCardFocus() }
        // The panel's own presses — the band's ⌗, the detail header's chip,
        // a banner — arrive here. Resolved against the *current* snapshot:
        // a card that left the board between the press and this frame is
        // not revealed and the board is left exactly as it was, so a search
        // cannot be narrowed to a card that is not there to scroll to.
        .onChange(of: state.revealRequest) { _, request in
            guard let request,
                  let card = board.cards.first(where: { $0.id == request.id })
            else { return }
            reveal(card)
        }
        .onAppear { syncCardFocus() }
        // The card screen is **not** a sheet any more: it is its own ordinary
        // window, driven off the same single `state.editing` slot by the
        // controller in the card-window file. Nothing to present here — and
        // the refusal that used to block an interactive dismiss while
        // `state.composerSaving` now lives in that controller's
        // `windowShouldClose`. Nothing in *this* file names that window: the
        // reveal above aims the board and opens nothing, and a grep for the
        // type here is the acceptance check that says so.
        // The plan gate's confirmation. On `BoardView`, deliberately not on
        // the card view: the board redraws on every frame and a card
        // vanishing mid-flap would dismiss its own dialog. `presenting:`
        // hands the held move to the buttons so the action never has to read
        // a slot the dismissal may already have cleared. A dialog is a sheet
        // on macOS, and `PanelExit.requested` already ends attached sheets
        // before quitting — nothing new to do there.
        .confirmationDialog(
            // Two rungs, two questions. A card whose *approved* plan has been
            // edited since is not an unplanned card, and a dialog that said so
            // would be asking somebody to agree to something that is not true.
            (state.pendingUnplanned?.planChanged ?? false)
                ? "Start with a changed plan?" : "Start without a plan?",
            isPresented: unplannedDialogShown,
            titleVisibility: .visible,
            presenting: state.pendingUnplanned
        ) { pending in
            Button(pending.planChanged
                   ? (pending.dispatches ? "Start with the changed plan"
                                         : "Move with the changed plan")
                   : (pending.dispatches ? "Start without a plan"
                                         : "Move without a plan")) {
                confirmUnplanned(pending)
            }
            .cursorAffordances(true)
            Button("Cancel", role: .cancel) { state.pendingUnplanned = nil }
                .cursorAffordances(true)
        } message: { pending in
            // What is being *skipped*, in words — the gesture cannot see the
            // gate, so the dialog is where the discard is said out loud.
            if pending.planChanged {
                Text("The plan file has been edited since somebody approved "
                     + "it, so what runs is not the wording that was read.")
            } else {
                Text(pending.dispatches
                     ? "No interview, no written plan, no acceptance checklist — "
                       + "the assistant starts from the card text alone."
                     : "No interview, no written plan, no acceptance checklist — "
                       + "the card moves with nothing behind it.")
            }
        }
    }

    // MARK: - The reverse jump's board half

    /// Apply an outstanding card-focus request, if there is one to apply.
    ///
    /// Idempotent by sequence number, so the three deliverers can all call it.
    /// It **aims** the board and nothing more: it never writes
    /// `state.editing`, never opens the card window, and never clears a tick
    /// the user put in the project filter by hand. A half-typed card left open
    /// elsewhere is untouched by construction — nothing here reads that slot.
    private func syncCardFocus() {
        guard let request = cardFocus.request,
              request.seq != appliedCardFocus else { return }
        switch CardReveal.outcome(session: request.sessionId, board: board) {
        case .wait:
            // The snapshot has not carried a board yet. Keep the request.
            return
        case .drop:
            // A real board, and this session has no card — a hand-started
            // session, which is the common case. Consume it and leave the
            // board exactly as it was.
            appliedCardFocus = request.seq
        case .reveal(let card):
            appliedCardFocus = request.seq
            reveal(card)
        }
    }

    /// The coordinate space the revealed card reports its frame in: the
    /// scroll view's viewport, so the frame is where the card is *on screen*
    /// and the document position is that plus the clip's current origin.
    static let scrollSpace = "boardScroll"

    /// Put one card in front of the reader: widen whatever hides it, narrow
    /// the search to it alone, scroll it into view and glow it briefly.
    private func reveal(_ card: BoardCard) {
        // Widen, never clear: the ticks persist to disk, and an automated
        // reveal must not erase a selection the user made by hand
        // (`PanelView.showCard`'s rule).
        state.includeProjectInFilter(card.project)
        // Narrow the board to this one card, the way a search does. This
        // overwrites whatever the user had typed — deliberate: the press is an
        // explicit aim at this card, and `PanelView.showCard` already behaves
        // this way. Only the user takes it back off (× CLEAR, Escape, typing
        // over it); no card move and no session ending lifts it.
        // The caret leaves the field too: a press is not typing, and a caret
        // left in it would make the next letters edit the token.
        searchFieldFocused = false
        state.query = CardToken.query(for: card)
        state.revealedCard = card.id
        // A fresh layout for the narrowed board (`revealEpoch`).
        revealEpoch &+= 1
        let id = card.id
        // The scroll itself waits for the card's frame: the narrowed board
        // has to be laid out first, and the revealed card reports its frame
        // (`RevealFrameKey`) once it is — no guessed delay.
        pendingScrollCard = id
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(2500))
            // Still this card: a second press before the fade must not have
            // its own glow cut short by the first one's timer.
            if state.revealedCard == id {
                Motion.animate(.easeOut(duration: 0.3), reduced: reduceMotion) {
                    state.revealedCard = nil
                }
            }
        }
    }

    /// Scroll the board so `frame` — the revealed card, in `scrollSpace` —
    /// sits at the centre of the viewport, clamped to the document. One
    /// axis: the board is a vertical scroll and nothing is ever off to the
    /// right. AppKit rather than `ScrollViewProxy` so the scroll can be
    /// keyed off the card's *reported* frame (see `scroller`).
    private func scrollRevealed(to frame: CGRect) {
        guard let scrollView = scroller, frame.width > 0 else { return }
        let clip = scrollView.contentView
        let viewport = clip.bounds.size
        let doc = scrollView.documentView?.bounds.size ?? viewport
        let current = clip.bounds.origin
        let target = NSPoint(
            x: current.x,
            y: (current.y + frame.midY - viewport.height / 2)
                .clamped(to: 0...max(0, doc.height - viewport.height)))
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            context.allowsImplicitAnimation = true
            clip.animator().setBoundsOrigin(target)
            scrollView.reflectScrolledClipView(clip)
        }
    }

    /// Read-only in the getter and clearing in the setter, `sheetTarget`'s
    /// shape: dismissing the dialog by any route lands in one place.
    private var unplannedDialogShown: Binding<Bool> {
        Binding(
            get: { state.pendingUnplanned != nil },
            set: { if !$0 { state.pendingUnplanned = nil } })
    }

    /// Replay the held move with the skip flag set — through the same verbs
    /// the unheld gesture would have used, resolved against the *current*
    /// snapshot: the card can have moved or died while the dialog was up.
    private func confirmUnplanned(_ pending: PendingMove) {
        state.pendingUnplanned = nil
        guard let card = board.cards.first(where: { $0.id == pending.cardId })
        else { return }
        if !pending.dispatches {
            // Launcher off: a plain move, confirmed. Reorder when the drop
            // named a slot, update otherwise.
            Task { @MainActor in
                let result: ActionResult
                if let beforeId = pending.beforeId {
                    result = await client.boardReorder(
                        card.id, column: pending.column, beforeId: beforeId,
                        skipPlanGate: true)
                } else {
                    result = await client.boardUpdate(
                        card.id, fields: ["column_name": pending.column],
                        skipPlanGate: true)
                }
                state.refusals[card.id] = result.ok ? "" : result.detail
                await client.refresh()
            }
            return
        }
        state.refusals[card.id] = ""
        state.starting.insert(card.id)
        Task { @MainActor in
            let result = await client.boardDispatch(
                card.id, skipPlanGate: true,
                ownTerminal: pending.ownTerminal)
            if !result.ok {
                state.starting.remove(card.id)
                state.refusals[card.id] = result.detail
                await client.refresh()
                return
            }
            if let beforeId = pending.beforeId {
                let placed = await client.boardReorder(
                    card.id, column: pending.column, beforeId: beforeId,
                    skipPlanGate: true)
                if !placed.ok { state.refusals[card.id] = placed.detail }
            }
            await client.refresh()
        }
    }

    private var unavailable: some View {
        VStack(spacing: 6) {
            Text("The board is not open.")
                .font(Theme.prose(15, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            Text("Dark Army could not open board.db. The rest of Dark Army is unaffected; "
                 + "see the log for why.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .background(Theme.well)
        .overlay { ScanlineOverlay() }
    }

    private var empty: some View {
        VStack(spacing: 8) {
            Text("Nothing queued yet.")
                .font(Theme.prose(15, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            // The absence is explained rather than left mysterious: Codex and
            // Grok have no channel, so they cannot author cards and there is no
            // second route.
            Text("Write a card here, or ask a session running Dark Army's channel to "
                 + "add one. Nothing starts until you drag a card into "
                 + "In progress yourself.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
            Button {
                state.openComposer(defaultProject: defaultProject,
                                   defaultRoot: defaultRoot,
                                   offeredTools: board.tools)
            } label: {
                Text("+ NEW CARD")
            }
            .buttonStyle(AlarmOutline())
            .clickable()
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .background(Theme.well)
        .overlay { ScanlineOverlay() }
    }

    /// The title the `#card` phrase currently names, or nil where the query
    /// is ordinary text. An empty string means the phrase parses but names no
    /// card on this board — the chip says so in words.
    ///
    /// Computed from the query each frame; no stored state. Resolution is the
    /// **first** match in snapshot order, which is deterministic.
    private var pinnedCardTitle: String? {
        guard let token = CardToken.parse(state.query) else { return nil }
        return board.cards.first { CardToken.matches(token: token, cardId: $0.id) }?.title ?? ""
    }

    /// Always on once there is a card to find. A board you cannot search
    /// is a pile, and this one is already past that.
    private var searchBar: some View {
        let chrome = BoardSearchBar.chrome(queryEmpty: state.query.isEmpty)
        return HStack(spacing: 8) {
            Text(">")
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphor)
            TextField("grep title, summary…", text: $state.query)
                .textFieldStyle(.plain)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphorBright)
                .focused($searchFieldFocused)
                .frame(maxWidth: BoardSearchBar.fieldMaxWidth, alignment: .leading)
            if let title = pinnedCardTitle {
                // Non-interactive: it says *why* the board looks narrow. The
                // clear affordance is the × beside it, unchanged.
                Text(CardToken.chipLabel(resolvedTitle: title.isEmpty ? nil : title))
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .lineLimit(1)
                    .padding(.horizontal, AlarmOutline.hPad)
                    .padding(.vertical, AlarmOutline.vPad)
                    .overlay {
                        RoundedRectangle(cornerRadius: 3)
                            .stroke(Theme.phosphor.opacity(0.5),
                                    lineWidth: AlarmOutline.stroke)
                    }
            }
            if chrome.showClear {
                Button {
                    state.query = ""
                    searchFieldFocused = false
                } label: {
                    Text(BoardSearchBar.clearLabel)
                }
                .buttonStyle(AlarmOutline(color: Theme.phosphor, size: 10))
                .clickable()
            }
            Spacer(minLength: 16)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Theme.bar)
        .onChange(of: searchFieldFocused) { _, focused in
            state.searchFocused = focused
        }
        .onChange(of: state.searchFocused) { _, focused in
            if !focused { searchFieldFocused = false }
        }
    }

    /// Hairline lives here so the chrome block has exactly one rule under
    /// it, not two a band apart.
    private var createRow: some View {
        HStack(spacing: 8) {
            Button {
                state.openComposer(defaultProject: defaultProject,
                                   defaultRoot: defaultRoot,
                                   offeredTools: board.tools)
            } label: {
                Text("+ NEW CARD")
            }
            .buttonStyle(AlarmOutline())
            .clickable()
            // Absent rather than inert at zero, the house rule: a button that
            // can do nothing is a row the ⋯ menu's sweep took four out for.
            if !state.drafts.isEmpty {
                Button {
                    showingDrafts = true
                } label: {
                    Text("DRAFTS (\(state.drafts.count))")
                }
                .buttonStyle(AlarmOutline())
                .clickable()
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Theme.bar)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        // A sheet on the panel window, not a second `NSWindow`. `CardWindow` became a window so a half-typed card
        // could be *minimised and left*; a drafts list is pick-one-and-dismiss
        // with nothing to leave open, and a window would cost a controller, a
        // key-monitor guard and a widened SSE occlusion gate for a surface
        // that gains nothing from any of them.
        .sheet(isPresented: $showingDrafts) {
            DraftsSheet(client: client, state: state)
                .cursorAffordances(true)
        }
    }

    private var noMatches: some View {
        VStack(spacing: 6) {
            Text("no cards match")
                .font(Theme.prose(15, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            Text(CardToken.parse(state.query) != nil
                 ? "that card is no longer on the board."
                 : "“\(state.query)” is not in a title or a summary.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .background(Theme.well)
        .overlay { ScanlineOverlay() }
    }

    private var doneCount: Int { board.doneCount }

    private var doneScope: BulkClearDoneScope {
        BulkClearDoneScope(board: board)
    }

    /// One vertical scroll over the four rows — Prep, Backlog, In progress,
    /// Done, top to bottom — each a `Section` whose pinned header is its
    /// heading, so a heading stays above the row it names while the row
    /// scrolls under it. **One axis.** The four side-by-side 600pt columns
    /// this replaced were wider than the pane on most windows and forced a
    /// two-axis scroll in which a revealed card could sit off the right edge
    /// looking like it was not there; a row's cards wrap as tiles instead
    /// (`rowTiles`), so nothing is ever to the right of the pane. A folded
    /// row draws its heading alone; a search (and a reveal is a search)
    /// draws every row open — `BoardState.rowFolded` states the rule.
    private var rows: some View {
        ScrollView(.vertical) {
            LazyVStack(alignment: .leading, spacing: 0,
                       pinnedViews: [.sectionHeaders]) {
                ForEach(BoardColumn.allCases) { column in
                    // Each piece in a `BoardRowRedraw`: the lazy stack keeps
                    // a row it already built and does not rebuild it when
                    // this body re-runs, so without its own subscription a
                    // project tick or an armed Clear Done reached the row
                    // only when a fold changed the stack's `.id` (23 Sep 2026).
                    Section {
                        BoardRowRedraw(state: state, feed: feed,
                                       archive: doneArchive, refresh: refreshVisible) {
                            if !state.rowFolded(column) {
                                rowTiles(column)
                            }
                        }
                    } header: {
                        BoardRowRedraw(state: state, feed: feed,
                                       archive: doneArchive, refresh: refreshVisible) {
                            rowHeading(column)
                        }
                    }
                }
            }
            // A scrolled lazy stack retains offscreen section heights when
            // their bodies disappear. Recreate its layout on effective fold
            // changes (including search overrides), keeping the scroll view
            // itself and the board's state alive. A reveal forces the same
            // rebuild (`revealEpoch`), whatever the search held before it.
            .id(RowStackIdentity(folds: BoardColumn.allCases.map { state.rowFolded($0) },
                                 revealEpoch: revealEpoch))
            .background(ScrollViewFinder { scroller = $0 })
        }
        .coordinateSpace(name: Self.scrollSpace)
        .onPreferenceChange(RevealFrameKey.self) { frames in
            // One shot per reveal: the frame changes on every scroll step,
            // and re-aiming on each would restart the animation for ever.
            guard let id = pendingScrollCard, let frame = frames[id] else {
                return
            }
            pendingScrollCard = nil
            scrollRevealed(to: frame)
        }
        .background(Theme.well)
        .overlay { ScanlineOverlay() }
        .overlay { VignetteOverlay() }
    }

    /// What the Done heading says when the row under it draws nothing.
    ///
    /// The count this line names is store-wide, so a blank row beneath it is
    /// the one thing this must never leave unexplained. Until the archive has
    /// landed the row is *incomplete*, not empty, and it says so; a fetch
    /// that failed says that and offers to ask again. Only once the archive is
    /// in hand is the old sentence the honest one — a search that matched
    /// nothing.
    @ViewBuilder
    private var doneEmptyLine: some View {
        switch doneArchive.state {
        case .loading:
            Text("Reading the \(doneCount) finished items…")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        case .failed:
            VStack(alignment: .leading, spacing: 2) {
                Text("Could not read the finished items.")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Retry") { client.retryDoneArchive() }
                    .buttonStyle(.plain)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .clickable()
            }
        case .ready:
            Text("\(doneCount) Done items exist across all projects, "
                 + "but none are in this search or recent preview.")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// One row's heading: the chevron, the `[ TITLE ] count` line and its
    /// caption, pinned above the row. The whole strip is a press that folds
    /// the row, and a drop target that moves a card into the row — **the
    /// folded heading still accepts a drop**, resolved to the same move the
    /// tiles' `.column` is, so a card can be filed into a folded pile
    /// without opening it. Its slot value is its own (`.heading`), never the
    /// tiles' `.column`: two views writing one value into the shared hover
    /// slot is how a late leave from one wiped the tint of the other
    /// (`BoardDropTarget`). Opaque background, because the tiles scroll
    /// under it. The Done heading keeps the bulk-clear control and the
    /// empty line while the row is open — row chrome, not card content —
    /// and drops both when folded: the reader folded it away.
    private func rowHeading(_ column: BoardColumn) -> some View {
        let folded = state.rowFolded(column)
        // `BoardVisible.headingCount`: the row's visible cards, and the
        // store-wide Done count only while nothing narrows the board.
        let counted = visible.headingCount(
            column, storeDone: doneCount,
            archiveReady: doneArchive.state == .ready)
        let displayedCount = counted.map(String.init) ?? "…"
        // The heading and the tiles are one row to the eye, so either
        // being the target lights both.
        let targeted = state.dropTarget?.row == column
        return VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 6) {
                // Rotated rather than swapped for a second glyph, so the
                // chevron animates between the two states — `SectionHeader`'s.
                // Hidden from VoiceOver: the label below says "folded" or
                // "open" in words, and a spoken "chevron down" beside it
                // would be the state said twice, once as a picture.
                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .semibold))
                    .rotationEffect(.degrees(folded ? -90 : 0))
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
                Text("\(column.title)  \(displayedCount)")
                    .font(Theme.prose(11, weight: .semibold))
                    .foregroundStyle(Theme.text)
                    .accessibilityHidden(true)
                Spacer()
            }
            // The caption every row carries. It is the answer to the
            // question this board could not answer before — what is the
            // difference between these two piles — and it is on the board
            // rather than in a tooltip because the person asking it is
            // looking at the board.
            Text(column.caption)
                .font(Theme.prose(10))
                .foregroundStyle(Theme.muted)
            if !folded { projectControl(column) }
            // A selecting row keeps its batch control when folded: the
            // ticks survive the fold, so the count and CANCEL must too.
            if !folded || state.selectingRow == column {
                rowBatchControl(column)
            }
            if column == .done && !folded && doneCount > 0
                && board.hasSafeDoneClearToken {
                clearDoneControl(scope: doneScope)
            }
            // Also when a narrowed heading reads "…" over a failed fetch:
            // the heading waits on the finished list, so the Retry that
            // fetches it must be on screen even with some Done cards shown.
            if column == .done && !folded && doneCount > 0
                && (visible.cards(.done).isEmpty
                    || (visible.narrowed && doneArchive.state == .failed)) {
                doneEmptyLine
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 6)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        // Tint first, bar second: the inner background is drawn in front,
        // and an opaque bar in front of the tint would hide the hover.
        .background(targeted ? Theme.phosphor.opacity(0.10) : Color.clear)
        .background(Theme.bar)
        .overlay(alignment: .top) {
            if targeted {
                Rectangle().fill(Theme.phosphor).frame(height: 2)
            }
        }
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        // The whole strip is the hit area, not just the text.
        .contentShape(Rectangle())
        .onTapGesture { state.toggleRowFold(column) }
        .clickable()
        // The strip is one element to VoiceOver — a button that says the
        // pile, its count and whether it is folded, and folds on activate —
        // the phone heading's own label, so the two surfaces read alike.
        .accessibilityElement(children: .contain)
        .accessibilityLabel(
            "\(column.title), \(displayedCount) cards, "
            + (folded ? "folded" : "open"))
        .accessibilityHint(folded ? "Shows the cards" : "Hides the cards")
        .accessibilityAddTraits([.isHeader, .isButton])
        .accessibilityAction { state.toggleRowFold(column) }
        .dropDestination(for: String.self) { ids, _ in
            handleDrop(ids, fallback: .heading(column))
            return true
        } isTargeted: { hovering in
            state.noteHover(.heading(column), hovering: hovering)
        }
    }

    /// The row's select mode — SELECT, then REFINE n TOGETHER / CANCEL — for
    /// the rows `RowSelection` gives a verb (Prep, here). Drawn while that
    /// row is selecting, and otherwise only where Dark Army may start
    /// sessions and the row shows at least two cards that could be ticked:
    /// absent, never inert. Another row's select mode hides this one's
    /// SELECT, so one selection is ever on screen.
    @ViewBuilder
    private func rowBatchControl(_ column: BoardColumn) -> some View {
        let chrome = feed.chrome
        let selecting = state.selectingRow == column
        if RowSelection.verb(column, count: 0).isEmpty {
            EmptyView()
        } else if selecting || (chrome.dispatchEnabled
                                && RowSelection.offersSelect(
                                    column, cards: visible.cards(column),
                                    chrome: chrome)) {
            // Another row's SELECT stays drawn while one row selects: taking
            // it away shortened that heading and moved every card below it
            // (28 Sep 2026). Pressing it moves select mode to that row, which
            // `enterRowSelection` already does cleanly.
            RowBatchControl(
                column: column,
                count: state.rowSelection.count,
                selecting: selecting,
                refusal: state.rowBatchRefusal,
                onSelect: { state.enterRowSelection(column) },
                onCancel: { state.exitRowSelection() },
                onFire: {
                    // Backlog arms first and fires on the second press — a
                    // batch Start opens a terminal, the single START's
                    // argument; Prep's Refine fires at once, as its tile's
                    // Refine does.
                    if column == .backlog {
                        state.pressStartSelected(board: board, client: client)
                    } else {
                        state.refineSelected(board: board, client: client)
                    }
                },
                armed: column == .backlog && state.rowBatchArmed)
            .padding(.top, 4)
        }
    }

    /// The per-project control a heading carries — START PROJECT under
    /// Backlog, the `RUN n/m` capacity picker under In progress — drawn
    /// only where the picker has named **one** project and its cards name a
    /// folder. Both used to live on the rail's pipeline band; the cards it
    /// drew are these rows, so the two controls moved onto them.
    @ViewBuilder
    private func projectControl(_ column: BoardColumn) -> some View {
        if let project = state.singleProject {
            switch column {
            case .backlog:
                let ready = board.backlog(in: project)
                let root = BoardProjectControls.projectRoot(cards: ready)
                if let onStartProject, !root.isEmpty {
                    StartProjectButton(report: state.refusals[root] ?? "") {
                        onStartProject(root)
                    }
                    .padding(.top, 4)
                }
            case .inProgress:
                let running = board.running(in: project)
                let queued = board.queued(in: project)
                let cards = running + queued + board.backlog(in: project)
                let root = BoardProjectControls.projectRoot(cards: cards)
                let showsRun = onSetLimit != nil && !root.isEmpty
                    && BoardProjectControls.showsRunHeading(running: running.count,
                                                            queued: queued.count)
                // The isolation switch is independent of the RUN heading: a
                // project with nothing running still decides where its next
                // Start works. Its state is the daemon's, off the cards.
                let isoCards = board.cards.filter { $0.project == project }
                let isoRoot = BoardProjectControls.projectRoot(cards: isoCards)
                let isolation = BoardProjectControls.isolationState(cards: isoCards)
                let showsIsolation = onSetIsolation != nil && !isoRoot.isEmpty
                    && !isolation.isEmpty
                if showsRun || showsIsolation {
                    HStack(spacing: 8) {
                        if showsRun, let onSetLimit {
                            RunLimitPicker(
                                count: running.count,
                                limit: BoardProjectControls.resolvedLimit(
                                    cards: cards, fallback: board.parallelLimit),
                                defaultLimit: board.parallelLimit,
                                overridden: board.parallelOverrides[root] != nil,
                                onSetLimit: { onSetLimit(root, $0) })
                        }
                        if showsIsolation, let onSetIsolation {
                            IsolationSwitch(on: isolation == "on") {
                                onSetIsolation(isoRoot, $0)
                            }
                        }
                    }
                    .padding(.top, 2)
                }
            case .prep, .done:
                EmptyView()
            }
        }
    }

    /// One row's cards, as tiles that wrap to the pane: an adaptive grid
    /// between `PanelMetrics.boardTileMin` and `PanelMetrics.boardColumn`
    /// wide, as many per line as fit, one per line on a narrow window. No
    /// `GeometryReader` — the grid measures nothing, it is offered the
    /// pane's width and reflows. **No `ScrollView` of its own** — the
    /// scroll is `rows`', or four scroll views would let the rows scroll
    /// independently, and a card is read against its neighbours.
    ///
    /// A row is natural height and its whole area is a drop target by
    /// construction: the grid, the trailing gap and the padding all sit
    /// inside the one `.contentShape`, so a release anywhere in the row —
    /// beside the last tile on a half-filled line included — lands on
    /// `.column`.
    private func rowTiles(_ column: BoardColumn) -> some View {
        let all = visible.cards(column)
        let (cards, held) = BoardVisible.shown(
            all, column: column, expanded: state.expandedRows.contains(column),
            searching: !state.query.isEmpty)
        // With cards held back, the row's trailing gap drops before the first
        // of them — where the drop visibly lands — never after the last card
        // of the whole column, which is off the screen.
        let tailBefore = held > 0 ? all[cards.count].id : ""
        let targeted = state.dropTarget?.row == column
        return VStack(alignment: .leading, spacing: 0) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: PanelMetrics.boardTileMin,
                                                   maximum: PanelMetrics.boardColumn),
                                         spacing: 12, alignment: .top)],
                      alignment: .leading, spacing: 8) {
                // The facts ride in the data, never only in the closure:
                // the grid skips content whose data is unchanged
                // (`BoardTileItem`).
                ForEach(cards.map { card in
                    BoardTileItem(card: card,
                                  facts: BoardTileFacts(card: card, state: state,
                                                        chrome: feed.chrome),
                                  chrome: feed.chrome)
                }) { item in
                    let card = item.card
                    HStack(alignment: .top, spacing: 0) {
                        gapView(column, beforeId: card.id, axis: .vertical)
                        BoardCardView(client: client, state: state,
                                      facts: item.facts,
                                      card: card,
                                      chrome: item.chrome,
                                      agent: agent(for: card),
                                      authorAgent: authorAgent(for: card),
                                      refinerAgent: refinerAgent(for: card))
                            .equatable()
                            // The payload is the card id, not the card: a
                            // drop is resolved against the *current*
                            // snapshot on the way in, so a card that moved
                            // or was deleted while the pointer was down
                            // cannot be acted on as the copy that was
                            // picked up.
                            // The revealed card reports where it is, so the
                            // reveal can scroll it into view once the
                            // narrowed board has laid it out. Only that
                            // card: every other card reports nothing. This
                            // is the board's one `GeometryReader`, and it
                            // measures the card, never the window.
                            .background {
                                if state.revealedCard == card.id {
                                    GeometryReader { geo in
                                        Color.clear.preference(
                                            key: RevealFrameKey.self,
                                            value: [card.id: geo.frame(
                                                in: .named(Self.scrollSpace))])
                                    }
                                }
                            }
                            .draggable(card.id)
                            .dropDestination(for: String.self) { ids, _ in
                                handleDrop(ids, fallback: .card(card.id))
                                return true
                            } isTargeted: { hovering in
                                state.noteHover(.card(card.id), hovering: hovering)
                            }
                    }
                }
            }
            gapView(column, beforeId: tailBefore, axis: .horizontal)
            if held > 0 {
                Button {
                    state.expandedRows.insert(column)
                } label: {
                    Text("show all \(all.count) — \(held) more")
                        .font(Theme.mono(10, weight: .semibold))
                        .tracking(0.8)
                        .foregroundStyle(Theme.phosphor)
                        .padding(.vertical, 6)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("Show all \(all.count) cards in \(column.title)")
            }
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 6)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(targeted ? Theme.phosphor.opacity(0.10) : Color.clear)
        // A click on the empty part of a row is "not pressing anything", and
        // moving on disarms.
        .contentShape(Rectangle())
        .onTapGesture { state.disarm() }
        .dropDestination(for: String.self) { ids, _ in
            handleDrop(ids, fallback: .column(column))
            return true
        } isTargeted: { hovering in
            // A view may only *clear* the slot while the slot still holds
            // its own value. `dropTarget` is one shared slot and every row
            // and every heading writes it, so dragging from one view to the
            // next fires an enter on the new one and a leave on the old one
            // with no ordering promise between them — and the leave commonly
            // lands second. Written as `hovering ? target : nil` that stray
            // leave wiped the highlight the new view had just set, so the
            // tint and the accent bar went dark with the pointer sitting
            // over a perfectly valid target, which is the moment the board
            // is being asked where the card will land. The drop itself was
            // never affected; only the answer to that question was. The
            // test only works while every view writes a *distinct* value,
            // which is why this row's heading writes `.heading(column)` and
            // not this `.column(column)`: with both writing the same value,
            // a late leave from the heading was indistinguishable from a
            // leave from these tiles and cleared their tint.
            state.noteHover(.column(column), hovering: hovering)
        }
    }

    @ViewBuilder
    private func clearDoneControl(scope: BulkClearDoneScope) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            if state.clearingDone {
                HStack(spacing: 7) {
                    AgentChatterView(.caret, wait: .clearingDone, seed: "clear-done",
                                     spoken: "Clearing all done items")
                        .id("clear-done")
                    Text("Clearing all Done items…")
                        .font(Theme.mono(11, weight: .medium))
                        .foregroundStyle(Theme.faint)
                }
            } else {
                switch state.clearDoneGate {
                case .idle:
                    Button {
                        state.armClearDone(scope: scope)
                    } label: {
                        Text("Clear all done items")
                    }
                    .buttonStyle(AlarmOutline(color: Theme.danger))
                    .clickable()

                case .firstConfirmation:
                    Text("This includes all \(scope.count) Done items across "
                         + "projects, outside this search, and outside the "
                         + "recent-card window.")
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Button("Confirm clear all") {
                            state.advanceClearDone(currentScope: scope)
                        }
                        .buttonStyle(AlarmOutline(color: Theme.danger))
                        .clickable()
                        Button("Keep done items") { state.cancelClearDone() }
                            .buttonStyle(.plain)
                            .foregroundStyle(Theme.faint)
                            .clickable()
                    }

                case .finalConfirmation:
                    Text("Final confirmation: permanently clear all "
                         + "\(scope.count) Done items across projects, outside "
                         + "this search, and outside the recent-card window. "
                         + "There is no undo. Outcome evidence and measurements remain in history.")
                        .font(Theme.mono(10, weight: .medium))
                        .foregroundStyle(Theme.alarm)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Button("Clear all \(scope.count) now") {
                            clearAllDone(currentScope: scope)
                        }
                        .buttonStyle(AlarmOutline(color: Theme.danger))
                        .clickable()
                        Button("Keep done items") { state.cancelClearDone() }
                            .buttonStyle(.plain)
                            .foregroundStyle(Theme.faint)
                            .clickable()
                    }
                case .requesting, .awaitingSnapshot:
                    EmptyView()
                }
            }
            if !state.clearDoneRefusal.isEmpty {
                Text(state.clearDoneRefusal)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.alarm)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        .allowsHitTesting(!state.clearingDone)
    }

    private func clearAllDone(currentScope: BulkClearDoneScope) {
        guard let expected = state.beginClearDone(currentScope: currentScope)
        else { return }
        Task { @MainActor in
            let result = await client.boardClearDone(
                expectedCount: expected.count, doneToken: expected.token)
            state.finishClearDone(result, currentBoard: client.snapshot.board)
            await client.refresh()
            state.reconcile(with: client.snapshot.board)
        }
    }

    /// The drop target for order inside a row; hovering it draws the accent
    /// line, not the whole row. Two shapes: `.vertical` is the 8pt-wide,
    /// full-height strip before a tile — "insert before this card", which
    /// in a wrapping grid is reading order, so the same strip covers a move
    /// onto the previous line (a drop on the first tile's strip on a later
    /// line lands after the last tile of the line above; the accent is
    /// drawn at the left of the line, not the right of the previous one).
    /// `.horizontal` is the 8pt-high strip under the grid — "append". Both
    /// fall back to `.gap(column, beforeId:)`.
    private func gapView(_ column: BoardColumn, beforeId: String,
                         axis: Axis) -> some View {
        let active: Bool = {
            if case .gap(let col, let bid) = state.dropTarget {
                return col == column && bid == beforeId
            }
            return false
        }()
        return Color.clear
            .frame(maxWidth: axis == .horizontal ? .infinity : 8,
                   maxHeight: axis == .vertical ? .infinity : 8)
            .overlay(alignment: .center) {
                if active {
                    Rectangle().fill(Theme.phosphor)
                        .frame(width: axis == .vertical ? 2 : nil,
                               height: axis == .horizontal ? 2 : nil)
                }
            }
            .contentShape(Rectangle())
            .dropDestination(for: String.self) { ids, _ in
                handleDrop(ids, fallback: .gap(column, beforeId: beforeId))
                return true
            } isTargeted: { hovering in
                state.noteHover(.gap(column, beforeId: beforeId),
                                hovering: hovering)
            }
    }

    /// First destination to fire wins, keyed off the last hover rather than
    /// off which view SwiftUI delivered the release to. Nested
    /// `.dropDestination` is not a platform promise.
    private func handleDrop(_ ids: [String], fallback: BoardDropTarget) {
        guard let target = state.takeDrop(fallback: fallback) else { return }
        guard let id = ids.first,
              let card = board.cards.first(where: { $0.id == id })
        else { return }
        switch target {
        case .card(let targetId):
            // A release over another card lands *before* it, in its row:
            // the same placement the gap beside it would give, so a drop
            // that misses the gap by a few points still does what the
            // hand meant. (It used to make the dragged card wait on the
            // target; cards no longer wait on cards.)
            guard let target = board.cards.first(where: { $0.id == targetId }),
                  let column = BoardColumn(rawValue: target.column)
            else { return }
            place(card, into: column, beforeId: targetId)
        case .gap(let column, let beforeId):
            place(card, into: column, beforeId: beforeId)
        case .column(let column), .heading(let column):
            drop(card, into: column)
        }
    }

    /// Same-column gap is order only, never a dispatch. Cross-column into In
    /// progress keeps today's three non-dispatch branches and the dispatch
    /// branch, then places at the slot. Always `board_reorder` — Backlog
    /// unlink is the daemon's job so this path never sends a raw float.
    ///
    /// The plan gate holds an *unplanned* drop (no plan, no session, not
    /// dispatching) as a `PendingMove` carrying the slot, instead of sending
    /// anything — the confirmation dialog replays it with the skip flag. A
    /// card with a session (or a run in flight) is tracking, not admitting
    /// unplanned work, and moves as before; the daemon applies the same
    /// exemption.
    private func place(_ card: BoardCard, into column: BoardColumn,
                       beforeId: String) {
        state.disarm()
        if card.column == column.rawValue {
            Task { @MainActor in
                let result = await client.boardReorder(
                    card.id, column: column.rawValue, beforeId: beforeId)
                state.refusals[card.id] = result.ok ? "" : result.detail
                await client.refresh()
            }
            return
        }
        if column == .inProgress {
            let hasRun = !card.sessionId.isEmpty || card.isDispatching
            if hasRun {
                Task { @MainActor in
                    let result = await client.boardReorder(
                        card.id, column: column.rawValue, beforeId: beforeId)
                    state.refusals[card.id] = result.ok ? "" : result.detail
                    await client.refresh()
                }
                return
            }
            if card.isRefining {
                // `canStart`'s `!card.isRefining`, mirrored on the drag path
                // — and `dispatch.guard`'s own words, so the drag and the
                // press refuse identically. Without this the drag confirmed
                // an unplanned start beside a live planner, and the planner's
                // eventual attach was refused because its card had left Prep.
                state.refusals[card.id] =
                    "this card is being refined — wait for the planner"
                return
            }
            if board.dispatchEnabled && card.tool.isEmpty {
                state.refusals[card.id] =
                    "Choose an assistant on the card first — dropping it here starts one."
                return
            }
            if card.planPath.isEmpty && !card.isScout {
                state.pendingUnplanned = PendingMove(
                    cardId: card.id, column: column.rawValue,
                    beforeId: beforeId, dispatches: board.dispatchEnabled)
                return
            }
            if !board.dispatchEnabled {
                Task { @MainActor in
                    let result = await client.boardReorder(
                        card.id, column: column.rawValue, beforeId: beforeId)
                    if !result.ok && result.isPlanConfirmable {
                        // The daemon gated what `planPath` alone could not
                        // see — the plan *file* is gone — so fall into the
                        // same confirmation the empty-path branch above
                        // raises: the refusal's own words offer it, and a
                        // route that cannot produce the flag is a wall.
                        state.pendingUnplanned = PendingMove(
                            cardId: card.id, column: column.rawValue,
                            beforeId: beforeId, dispatches: false,
                            planChanged: result.isPlanChangedRefusal)
                    } else {
                        state.refusals[card.id] = result.ok ? "" : result.detail
                    }
                    await client.refresh()
                }
                return
            }
            state.refusals[card.id] = ""
            state.starting.insert(card.id)
            Task { @MainActor in
                let result = await client.boardDispatch(card.id)
                if !result.ok {
                    state.starting.remove(card.id)
                    if result.isPlanConfirmable {
                        // Same fallback as the launcher-off branch, on the
                        // dispatch route.
                        state.pendingUnplanned = PendingMove(
                            cardId: card.id, column: column.rawValue,
                            beforeId: beforeId, dispatches: true,
                            planChanged: result.isPlanChangedRefusal)
                    } else {
                        state.refusals[card.id] = result.detail
                    }
                    await client.refresh()
                    return
                }
                let placed = await client.boardReorder(
                    card.id, column: column.rawValue, beforeId: beforeId)
                if !placed.ok { state.refusals[card.id] = placed.detail }
                await client.refresh()
            }
            return
        }
        Task { @MainActor in
            let result = await client.boardReorder(
                card.id, column: column.rawValue, beforeId: beforeId)
            state.refusals[card.id] = result.ok ? "" : result.detail
            await client.refresh()
        }
    }

    /// A card released over a column. **This is the board's one dispatch
    /// gesture**, and the whole reason `ready` could go away.
    ///
    /// A drag is picked up, carried across the window and released; it is not
    /// something a slipped click does, which is why it stands in for the
    /// arm-then-confirm that Start still uses (a single click is a cheaper
    /// gesture and keeps the cheaper gate). Dropping into **In progress** is
    /// therefore the confirmation: it starts the assistant the card names, and
    /// the daemon — not this view — moves the column, so a refused spawn leaves
    /// the card exactly where it was.
    ///
    /// Three things are *not* a dispatch and each is handled differently rather
    /// than swept into one branch:
    ///
    /// * **No assistant named.** The drop is refused in words and the card does
    ///   not move. Moving it would put a card in In progress that nothing is
    ///   progressing, which is the board asserting a run that never happened.
    /// * **Dark Army's launcher is switched off.** The card moves, plainly: the person
    ///   is tracking work they started themselves, and the preference removes
    ///   Dark Army's ability to launch, not their ability to use a board.
    /// * **The card already has a session** (live, or a run that ended). Also a
    ///   plain move — starting a second assistant on a card that has one is
    ///   exactly what `dispatch.guard` refuses, and it is right to.
    ///
    /// And one thing that is a dispatch *pending a word*: an **unplanned**
    /// card (no `planPath`, no session) is held behind the confirmation
    /// dialog rather than sent — see `PendingMove`. This is not the
    /// drag-does-not-arm rule broken: that rule says a drag is deliberate *as
    /// a gesture*; the dialog is about **skipping a gate** the gesture cannot
    /// see. Dispatching a planned card does exactly what the column caption
    /// promises, while starting an unplanned one silently discards a whole
    /// stage of the pipeline, and the dialog is where that discard is said
    /// out loud.
    private func drop(_ card: BoardCard, into column: BoardColumn) {
        state.disarm()
        // A release over the row the card is already in is an **append**:
        // in a wrapping grid the empty space a same-row release lands on —
        // the tail of a half-filled line, the spacing between tiles, the
        // space under a short tile — is `.column`, not a gap, and a release
        // the board answered with nothing sprang the card back to where it
        // was as if the drag had been refused. `place` sends it through
        // the reorder verb with the append slot, the same thing the strip
        // under the grid sends; a card already last is a no-op there.
        guard card.column != column.rawValue else {
            place(card, into: column, beforeId: "")
            return
        }
        guard column == .inProgress else {
            move(card, to: column)
            return
        }
        let hasRun = !card.sessionId.isEmpty || card.isDispatching
        if hasRun {
            move(card, to: column)
            return
        }
        if card.isRefining {
            // `canStart`'s `!card.isRefining`, mirrored on the drag path —
            // `place` states the argument, and `dispatch.guard` refuses in
            // the same words so the daemon is never the only thing saying so.
            state.refusals[card.id] =
                "this card is being refined — wait for the planner"
            return
        }
        if board.dispatchEnabled && card.tool.isEmpty {
            state.refusals[card.id] =
                "Choose an assistant on the card first — dropping it here starts one."
            return
        }
        if card.planPath.isEmpty && !card.isScout {
            // The plan gate: held, not sent. See `PendingMove` — the daemon
            // has not been asked, so the card visibly stays put until the
            // dialog answers. A scout has no plan to skip.
            state.pendingUnplanned = PendingMove(
                cardId: card.id, column: column.rawValue,
                beforeId: nil, dispatches: board.dispatchEnabled)
            return
        }
        if !board.dispatchEnabled {
            move(card, to: column)
            return
        }
        start(card)
    }

    private func move(_ card: BoardCard, to column: BoardColumn) {
        Task { @MainActor in
            let fields = ["column_name": column.rawValue]
            let result = column.clearsSessionLink
                ? await client.boardReset(card.id, fields: fields)
                : await client.boardUpdate(card.id, fields: fields)
            if !result.ok && result.isPlanConfirmable
                && column == .inProgress {
                // The plan-gate fallback (`place` states it): the daemon
                // gated a move `planPath` said was planned — the file is
                // gone — so the confirmation must still be reachable here,
                // which is `drop`'s launcher-off route into In progress.
                state.pendingUnplanned = PendingMove(
                    cardId: card.id, column: column.rawValue,
                    beforeId: nil, dispatches: false,
                    planChanged: result.isPlanChangedRefusal)
            } else {
                state.refusals[card.id] = result.ok ? "" : result.detail
            }
            await client.refresh()
        }
    }

    /// Shared with the card's own Start button so there is one launch path.
    /// Optimistic only about the *spinner*, never about the outcome.
    private func start(_ card: BoardCard) {
        state.refusals[card.id] = ""
        state.starting.insert(card.id)
        Task { @MainActor in
            let result = await client.boardDispatch(card.id)
            if !result.ok {
                state.starting.remove(card.id)
                if result.isPlanConfirmable {
                    // The plan-gate fallback (`place` states it): this send
                    // carried no flag, so the daemon's refusal is answered
                    // with the confirmation that can carry one.
                    state.pendingUnplanned = PendingMove(
                        cardId: card.id,
                        column: BoardColumn.inProgress.rawValue,
                        beforeId: nil, dispatches: true,
                        planChanged: result.isPlanChangedRefusal)
                } else {
                    state.refusals[card.id] = result.detail
                }
            }
            await client.refresh()
        }
    }

    /// The column's order — `BoardVisible.cardOrder`, where the one pass
    /// that uses it lives. Kept here so its callers read unchanged.
    static func cardOrder(_ a: BoardCard, _ b: BoardCard,
                          column: BoardColumn) -> Bool {
        BoardVisible.cardOrder(a, b, column: column)
    }

    /// The session doing this card, looked up in the agents snapshot the panel
    /// already decodes. **The board renders the fleet's answer and never derives
    /// its own** — that is what keeps it from disagreeing with the menu-bar strip
    /// about what an assistant is doing.
    private func agent(for card: BoardCard) -> FleetRow? {
        guard !card.sessionId.isEmpty else { return nil }
        return agent(session: card.sessionId)
    }

    /// The session that *wrote* this card, if it is still in the fleet.
    ///
    /// Deliberately the author and not `sessionId`: the card names its author
    /// on its face ("added by …") and Jump on that line acts on that session.
    /// `isAgentAuthored` is the guard that makes a hand-typed card — whose
    /// `author` is the literal string `user` — resolve to nothing.
    private func authorAgent(for card: BoardCard) -> FleetRow? {
        guard card.isAgentAuthored else { return nil }
        return agent(session: card.author)
    }

    /// The session *refining* this card, if the refinement is live and the
    /// fleet still lists it. `nil` during `dispatching` on purpose — the
    /// refiner id is empty until the bind, and there is nothing to jump to
    /// yet, which matches the "waiting for the session to appear" wording
    /// already on the card.
    private func refinerAgent(for card: BoardCard) -> FleetRow? {
        guard card.refineState == "live", !card.refineSessionId.isEmpty
        else { return nil }
        return agent(session: card.refineSessionId)
    }

    /// One dictionary lookup in the feed's card-named rows, which are
    /// `Agents.row(session:)`'s answers for exactly those ids.
    private func agent(session: String) -> FleetRow? {
        guard !session.isEmpty else { return nil }
        return feed.rows[session]
    }

    private var defaultProject: String {
        state.filingProject(board: board, agents: client.snapshot.agents)
    }

    private var defaultRoot: String {
        state.filingRoot(board: board, agents: client.snapshot.agents)
    }
}

/// The parent route: `PanelView` rebuilds this view on every client frame and
/// hands it two fresh closures each time, which SwiftUI cannot compare, so
/// without this every fleet frame re-ran the board's body from above even
/// with the client unobserved. `PanelView.widePane` applies `.equatable()`.
///
/// Identity of the three objects, plus whether each closure is present: the
/// two closures capture only the client and the board state (both compared
/// here by identity), so identity plus presence is the whole of their
/// meaning. Presence must be compared — `onStartProject` is nil exactly
/// while launching is switched off, and an identity-only test would keep
/// START PROJECT drawn from a stale closure until something else moved.
extension BoardView: Equatable {
    nonisolated static func == (lhs: BoardView, rhs: BoardView) -> Bool {
        MainActor.assumeIsolated {
            lhs.client === rhs.client
                && lhs.state === rhs.state
                && lhs.cardFocus === rhs.cardFocus
                && (lhs.onStartProject == nil) == (rhs.onStartProject == nil)
                && (lhs.onSetLimit == nil) == (rhs.onSetLimit == nil)
                && (lhs.onSetIsolation == nil) == (rhs.onSetIsolation == nil)
        }
    }
}

/// What the board's row stack is keyed on: the drawn folds, and the reveal
/// count, so either one changing lays the rows out from scratch.
private struct RowStackIdentity: Hashable {
    let folds: [Bool]
    let revealEpoch: Int
}

/// The revealed card's frame in `BoardView.scrollSpace`, keyed by card id.
/// One entry at most: only the revealed card reports.
private struct RevealFrameKey: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect],
                       nextValue: () -> [String: CGRect]) {
        value.merge(nextValue()) { _, new in new }
    }
}

/// Hands back the `NSScrollView` a SwiftUI `ScrollView` is built on, found
/// by walking up from a view placed in its content. The reveal takes the
/// AppKit object so it can scroll to the revealed card's *reported* frame —
/// after the narrowed board has laid it out — rather than ask a proxy to
/// find an id that may not be instantiated yet inside a lazy stack.
private struct ScrollViewFinder: NSViewRepresentable {
    let found: (NSScrollView) -> Void

    func makeNSView(context: Context) -> Probe {
        let probe = Probe()
        probe.found = found
        return probe
    }

    func updateNSView(_ nsView: Probe, context: Context) {}

    final class Probe: NSView {
        var found: ((NSScrollView) -> Void)?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let scrollView = enclosingScrollView else { return }
            DispatchQueue.main.async { [found] in found?(scrollView) }
        }
    }
}

private extension CGFloat {
    func clamped(to range: ClosedRange<CGFloat>) -> CGFloat {
        Swift.min(Swift.max(self, range.lowerBound), range.upperBound)
    }
}

/// One board row's heading or tiles, redrawn whenever the board's own
/// state, the feed or the finished-column archive publishes.
///
/// `BoardView.rows` is a `LazyVStack` of pinned sections, and a lazy stack
/// keeps the row views it already built: a `BoardView` body that re-ran on a
/// project tick produced new counts that no heading ever drew, and an armed
/// Clear Done that no heading ever showed. Observing the three sources here
/// makes each row re-run its own content on any change to them.
struct BoardRowRedraw<Content: View>: View {
    @ObservedObject var state: BoardState
    @ObservedObject var feed: BoardFeed
    @ObservedObject var archive: DoneArchive
    let refresh: () -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        refresh()
        return content()
    }
}
