import SwiftUI

/// Board, four stacked rows in one scroll. New-card pushes the composer; the
/// card screen is where Start, Refine, move and delete live.
///
/// The paged `TabView` and its chip strip this replaced showed one column at
/// a time and hid the other three behind a swipe. The rows are the Mac's
/// board on the phone — Prep, Backlog, In progress, Done, top to bottom, each
/// folding to its heading line — and which rows start folded is the Mac's
/// own rule (`BoardRowFold`, copied byte-equal at the bottom of this file).
/// A person's folds are held in `@AppStorage` rather than view state, which
/// is what makes them survive a relaunch — as `@State` they reset on every
/// rebuild.
struct BoardView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    @ObservedObject var outbox: OutboxStore
    /// The rows whose fold differs from the default, comma-joined
    /// (`BoardRowFold.encode(joined:)`); "" is the defaults. `RecentlyView`'s
    /// `fleet.recently.collapsed` is the precedent for a remembered fold.
    @AppStorage("board.rowFlips") private var rowFlipsJoined: String = ""
    @StateObject private var arm = Arm()
    @State private var armedClear: ClearDoneScope?
    @State private var pendingClear: ClearDoneScope?
    @State private var clearDoneRefusal = ""
    @State private var query = ""
    @FocusState private var searchFocused: Bool
    /// Which row is in select mode, `nil` for none — one row at a time, the
    /// Mac's `selectingRow` shape: `"prep"` (batch Refine) or `"backlog"`
    /// (batch Start). Each row's SELECT is drawn only while this is nil.
    @State private var selectingRow: String? = nil
    /// The ticked card ids, in tick order (the first names the project the
    /// rest must share); the press sends them in board order.
    @State private var selection: [String] = []
    /// The batch press's refusal in the Mac's own words (or the phone's own
    /// two: the queue's duplicate and a declined Face ID), drawn under the
    /// batch button. The ticks stay, so one can be fixed and pressed again.
    @State private var batchRefusal = ""
    /// The receipt whose refusal was last drawn, so a redraw reads a note
    /// once (`CardDetailView.noteArrived`'s pattern).
    @State private var batchNoteSeen = ""
    /// The Backlog row's synchronous batch Start is out. A synchronous press
    /// has no receipt, so this — not the queue mark — is its in-flight state.
    @State private var batchSending = false
    /// The Mac's report of the last batch Start, verbatim, drawn under the
    /// Backlog heading outside select mode until the next SELECT or the
    /// Board goes away.
    @State private var startReport = ""
    /// The assistant just picked for every ticked card, held on the tile
    /// until the board names it on all of them (or a write is refused).
    @State private var pendingBatchTool: String? = nil
    /// The Board's own project choice, `""` for `ALL` — Fleet's semantics
    /// (`@SceneStorage`, resolved through `FleetProjects.resolve`) under its
    /// own key, so narrowing one tab never narrows the other.
    @SceneStorage("board.project") private var project = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The card whose swipe buttons are showing, `nil` for none: one card is
    /// open at a time (`docs/phone-contract.md`, *A Prep or Backlog card is
    /// swiped on the Board tab*).
    @State private var revealed: String?
    /// The card whose dots menu is up, and the card whose "Are you sure?" is.
    @State private var moreFor: BoardCard?
    @State private var deleting: BoardCard?
    /// The receipt whose note a swiped card last drew, so a redraw reads a
    /// note once (`batchNoteSeen`'s pattern).
    @State private var swipeNoteSeen = ""
    /// A swiped press the phone's own queue turned down (the duplicate, a
    /// declined Face ID), in its words, drawn under that card.
    @State private var swipeRefusal: [String: String] = [:]

    private var board: Board { client.snapshot.board }
    private var rowFlips: Set<String> { BoardRowFold.decode(joined: rowFlipsJoined) }
    private var searching: Bool { BoardSearch.isSearching(query) }

    /// The captions are copied **verbatim** from the panel's
    /// `BoardColumn.caption`, and `test_phone_theme_drift.py` pins them. The
    /// phone cannot drop a card or press Reviewed, and they still say so:
    /// this screen is a window onto the Mac's board, and a phone-specific
    /// rewording is a second string to keep in step for no gain.
    private static let columns: [(id: String, title: String, caption: String)] = [
        ("prep", "PREP",
         "just written — refine it into a plan before anything starts"),
        ("backlog", "BACKLOG",
         "planned, nobody working on it yet"),
        ("in_progress", "IN PROGRESS",
         "drop here to start the assistant"),
        ("done", "DONE",
         "finished — an assistant's close waits at the top until you press Reviewed"),
    ]

    var body: some View {
        Group {
            if board.available {
                columnsBody
            } else {
                unavailable
            }
        }
        .background(Theme.bg)
    }

    /// Stated, never inferred from an empty card list: a board that is not
    /// there and a board with nothing on it are different facts.
    private var unavailable: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if client.refreshing {
                    AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                     spoken: "Checking with the Mac")
                        .id("refresh")
                }
                PromptLine(path: "~/board")
                CommentLine(text: "the Mac's board is not available")
                // The queue is the one part of the board that is genuinely
                // here, so it shows even when the Mac's own is not.
                waitingSection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .tint(Theme.phosphor)
        .refreshable { await client.refreshNow() }
    }

    /// Cards written on this phone that the Mac has not taken yet. Absent at
    /// zero — a heading over nothing is a queue that looks stuck.
    @ViewBuilder
    private var waitingSection: some View {
        if !outbox.entries.isEmpty {
            BoardProjectHeader(title: "WAITING FOR THE MAC (\(outbox.entries.count))")
            ForEach(outbox.entries) { entry in
                OutboxRow(entry: entry, armed: arm.deleteCard == entry.id,
                          sending: outbox.syncing,
                          onRetry: {
                    outbox.retryNow(entry.id)
                    // Swept immediately rather than on the next poll; the
                    // snapshot on hand is at most one poll old, which is
                    // what the landed check reads anyway.
                    Task { await outbox.sync(using: client) }
                }, onRemove: {
                    // The id is load-bearing here and nowhere else in the
                    // app: this is the one list that draws many rows over
                    // one `Arm`. Without it, arming REMOVE on one card
                    // confirmed the first press on any other.
                    if arm.confirm(.deleteCard, id: entry.id) {
                        outbox.remove(entry.id)
                    } else {
                        arm.arm(.deleteCard, id: entry.id)
                    }
                })
            }
        }
    }

    private var columnsBody: some View {
        VStack(spacing: 0) {
            if board.outcomesSupported {
                PhoneOutcomeProjects(client: client)
            }
            pipelineLink
            // Fleet's rule, and for Fleet's reason: `ALL` stays pressable
            // while a choice is up, even one with no names listed.
            if !projects.isEmpty || !activeProject.isEmpty {
                projectStrip
            }
            searchField
            if searching && !anyVisible {
                CommentLine(text: BoardSearch.noMatchLine(query: query))
            } else if !activeProject.isEmpty && !anyVisible {
                // Said in words: a filter that hides every card is not an
                // empty board.
                CommentLine(text: BoardProjects.boardLine(project: activeProject))
            }
            // One vertical scroll, every row in it. `ScrollView` +
            // `LazyVStack` as before; a folded row is its heading alone, and
            // the heading is the press that opens it.
            ScrollView {
                LazyVStack(spacing: 8) {
                    if client.refreshing {
                        AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                         spoken: "Checking with the Mac")
                            .id("refresh")
                    }
                    ForEach(Self.columns, id: \.id) { item in
                        rowHeading(for: item)
                        if !BoardRowFold.drawnFolded(item.id, flipped: rowFlips,
                                                     searching: searching) {
                            rowBody(for: item.id)
                        } else if selectingRow == item.id {
                            // The ticks survive a fold, so the selecting
                            // row's count and CANCEL stay under its heading.
                            if item.id == "prep" { prepBatchControl }
                            if item.id == "backlog" { backlogBatchControl }
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
            .tint(Theme.phosphor)
            .refreshable { await client.refreshNow() }
            // Dragging the rows puts the keyboard away too, the way the
            // composer's scroll does.
            .scrollDismissesKeyboard(.interactively)
            // Leaving the Board keeps the ticks: a person who goes to
            // another tab and comes back finds the batch where they left it.
            // Only a waiting confirmation goes.
            .onDisappear {
                arm.disarm()
                revealed = nil
                startReport = ""
            }
        }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            noteClearDoneSnapshot()
            pruneSelection()
            if let open = revealed,
               !PhoneCardSwipe.rows.contains(where: { row in
                   board.cards(in: row).contains { $0.id == open }
               }) {
                revealed = nil
            }
        }
        // The search and the project filter keep the ticks — the batch
        // button's count names every ticked card, drawn or not — and disarm
        // a waiting press, confirmed over a board no longer on screen.
        .onChange(of: query) { _, _ in disarmBatch() }
        .onChange(of: project) { _, _ in disarmBatch() }
        // A swiped-open card shuts when the list under it changes.
        .onChange(of: query) { _, _ in revealed = nil }
        .onChange(of: project) { _, _ in revealed = nil }
        // A card in select mode does not swipe, and entering it shuts one.
        .onChange(of: selectingRow) { _, _ in revealed = nil }
        .confirmationDialog(moreFor.map { "More for \($0.title)" } ?? "More",
                            isPresented: Binding(get: { moreFor != nil },
                                                 set: { if !$0 { moreFor = nil } }),
                            titleVisibility: .visible,
                            presenting: moreFor) { card in
            DecryptButton(PhoneCardSwipe.deleteRow, role: .destructive) {
                // The second dialog opens on the next turn, after this one
                // has dismissed, so iOS does not drop the presentation.
                Task { @MainActor in deleting = card }
            }
            DecryptButton("Cancel", role: .cancel) {}
        }
        .confirmationDialog(PhoneCardSwipe.deleteTitle,
                            isPresented: Binding(get: { deleting != nil },
                                                 set: { if !$0 { deleting = nil } }),
                            titleVisibility: .visible,
                            presenting: deleting) { card in
            DecryptButton(Verbs.delete.label, role: .destructive) { sendSwipeDelete(card) }
            DecryptButton("Cancel", role: .cancel) {}
        } message: { _ in
            Text(PhoneCardSwipe.deleteMessage)
        }
        // A changed set of ticks is a different press: the arm was keyed on
        // the old list, so it goes rather than firing on a set nobody read.
        .onChange(of: selection) { _, _ in disarmBatch() }
        // The batch press left the queue. Landed with nothing said: select
        // mode is done. Refused: the note below keeps the ticks.
        .onChange(of: client.queueMark(for: PhoneRowSelection.scope)) { _, mark in
            // Only the Prep row's own press ends the Prep row's select mode:
            // a Refine receipt leaving the queue never ends a Backlog one.
            guard selectingRow == "prep" else { return }
            if mark == nil && client.queueNote(for: PhoneRowSelection.scope) == nil {
                leaveSelectMode()
            }
        }
        .onChange(of: client.queueNote(for: PhoneRowSelection.scope)) { _, queued in
            batchNoteArrived(queued)
        }
        .onAppear {
            batchNoteArrived(client.queueNote(for: PhoneRowSelection.scope))
        }
        .onChange(of: arm.clearDone) { _, id in
            if id == nil { armedClear = nil }
        }
        // Fleet's write-back: forget a project that has gone. The offline
        // keep is `heard`, answered by whether the Mac published a board.
        .onChange(of: projects) { _, names in
            let next = FleetProjects.resolve(selected: project, in: names,
                                             heard: board.available)
            if next != project { project = next }
        }
    }

    // MARK: - Project filter

    /// The strip Fleet draws (`PhoneProjectStrip`), fed the board's facts:
    /// open cards (Prep, Backlog, In progress) per project, as Fleet counts
    /// live agents rather than finished ones.
    private var projectStrip: some View {
        PhoneProjectStrip(names: projects, active: activeProject,
                          total: openProjects.count,
                          counts: BoardProjects.counts(openProjects),
                          noun: (one: "open card", many: "open cards")) { project = $0 }
    }

    /// Every card the Mac listed, all four rows, before any filter.
    private var allCards: [BoardCard] {
        Self.columns.flatMap { board.cards(in: $0.id) }
    }

    private var openProjects: [String] {
        BoardProjects.openColumns.flatMap { board.cards(in: $0).map(\.project) }
    }

    /// The cards' own project names plus the projects the Mac says are open.
    private var projects: [String] {
        BoardProjects.names(cards: allCards.map(\.project),
                            open: board.projects.map(\.name))
    }

    /// The stored choice resolved against the names on screen, on read as
    /// well as on write-back — Fleet's `activeProject`, for the same reason.
    private var activeProject: String {
        // `""` is `ALL` whatever the names are (`resolve`'s own first
        // line), so the walk over every card that builds them is skipped.
        guard !project.isEmpty else { return project }
        return FleetProjects.resolve(selected: project, in: projects, heard: board.available)
    }

    /// Daemon tally when the Mac sent one; listed-card count only for an
    /// older daemon that sends no map. A non-empty map that omits a column
    /// is 0, never a recount of the preview — Done's chip may honestly
    /// outnumber the cards on the page.
    ///
    /// Nothing is subtracted for a card whose delete is in flight. The
    /// subtraction existed because such a card was hidden from the page; it
    /// is now drawn, greyed, so taking it off the chip would be the count
    /// and the list disagreeing rather than agreeing.
    ///
    /// While a search is up the heading chip counts `visibleCards(in:)` —
    /// the list actually drawn — instead of this tally. Clearing the field
    /// returns here.
    private func count(for id: String) -> Int {
        board.counts.isEmpty
            ? board.cards(in: id).count
            : (board.counts[id] ?? 0)
    }

    /// The project filter first, then the search.
    ///
    /// `activeProject` is read **once**, before the filter, and that hoist
    /// is load-bearing: it is a computed property over every card on the
    /// board (`projects` → `allCards`), so reading it inside the closure
    /// made one call O(cards²). The body calls this a dozen times, and the
    /// body re-runs on every publish of the client — about half a second of
    /// main thread per poll on a 180-card board (measured in a debug build
    /// on the live board: 457 ms before, 3 ms after), which is what froze
    /// the Board tab's scroll. Pinned by `test_phone_board_project_filter.py`.
    private func projectCards(in id: String) -> [BoardCard] {
        let activeProject = activeProject
        return board.cards(in: id).filter { BoardProjects.matches(project: $0.project, active: activeProject) }
    }

    private func visibleCards(in id: String) -> [BoardCard] {
        projectCards(in: id).filter { BoardSearch.matches(query: query, title: $0.title, summary: $0.summary, project: $0.project) }
    }

    private var anyVisible: Bool {
        Self.columns.contains { !visibleCards(in: $0.id).isEmpty }
    }

    /// The way through to the per-project pipeline. Drawn only where the Mac
    /// has said it will honour something on that screen — absent, never a
    /// link to a page of inert controls. No new tab: `PhoneTab`,
    /// `PhoneRouter` and the widget's deep links are untouched, so this is a
    /// push inside the Board tab's own navigation stack.
    @ViewBuilder
    private var pipelineLink: some View {
        if board.queueWritable || board.preferencesWritable {
            NavigationLink {
                PipelineView(client: client)
            } label: {
                HStack(spacing: 6) {
                    Text("PIPELINE")
                        .font(Theme.mono(12, weight: .semibold))
                        .tracking(0.8)
                    Text("›").font(Theme.mono(12))
                        .accessibilityHidden(true)
                }
                .foregroundStyle(Theme.dim)
                .frame(maxWidth: .infinity, minHeight: 36, alignment: .leading)
                .padding(.horizontal, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }

    /// Pinned above the rows. The tab root hides the navigation bar, so a
    /// system search field drawn into that bar would be invisible.
    private var searchField: some View {
        HStack(spacing: 8) {
            Text(">")
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphor)
            TextField("", text: $query,
                      prompt: Text(BoardSearch.placeholder).foregroundStyle(Theme.faint))
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphorBright)
                .textFieldStyle(.plain)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                // The keyboard's Search key is the way out: the query
                // stays, the keyboard goes. Without this the key did
                // nothing and an empty search had no exit at all.
                .onSubmit { searchFocused = false }
                .focused($searchFocused)
                .accessibilityLabel("Search the board")
                .accessibilityHint("Narrows every column to cards whose title, summary or project contain the words")
            // One control with two words: DONE while the keyboard is up
            // over an empty query (puts it away), CLEAR once there is a
            // query (empties it and puts the keyboard away).
            if searchFocused || !query.isEmpty {
                DecryptButton(query.isEmpty ? "× DONE" : "× CLEAR") {
                    query = ""
                    searchFocused = false
                }
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                    .accessibilityLabel(query.isEmpty ? "Hide the keyboard"
                                                      : "Clear the search")
            }
        }
        .frame(maxWidth: .infinity, minHeight: 36, alignment: .leading)
        .padding(.horizontal, 12)
        .fieldWell(focused: searchFocused, onTap: { searchFocused = true })
        .padding(.horizontal, 12)
    }

    /// A row's heading, and the press that folds it. The count is the same
    /// folded or open — a fold hides cards, never the number of them. The
    /// glyph is `RecentlyView`'s; the label is the old per-page heading's.
    /// No cap on the caption: it wraps.
    private func rowHeading(for item: (id: String, title: String, caption: String)) -> some View {
        let folded = BoardRowFold.drawnFolded(item.id, flipped: rowFlips, searching: searching)
        // A search or a project filter counts what is drawn: the daemon's
        // tally spans every project.
        let count = searching || !activeProject.isEmpty
            ? visibleCards(in: item.id).count : count(for: item.id)
        return DecryptButton {
            rowFlipsJoined = BoardRowFold.encode(joined: BoardRowFold.toggled(item.id, flipped: rowFlips))
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(folded ? "▸" : "▾")
                    .font(Theme.mono(14, weight: .semibold))
                    .foregroundStyle(Theme.faint)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(item.title)  \(count)")
                        .font(Theme.prose(18, weight: .semibold))
                        .foregroundStyle(Theme.text)
                    Text(item.caption)
                        .font(Theme.prose(14))
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.top, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // On the `Button`, so `.combine`: `.ignore` would drop the press.
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.title), \(count) cards, \(folded ? "folded" : "open")")
        .accessibilityHint(folded ? "Shows the cards" : "Hides the cards")
        .accessibilityAddTraits(.isHeader)
    }

    /// What an open row draws under its heading — the old page body minus
    /// its own `ScrollView`, since the four rows share one now.
    @ViewBuilder
    private func rowBody(for id: String) -> some View {
        // Every card the Mac listed, including one whose delete is in
        // flight — that one is drawn greyed and inert by `cardLink`, not
        // filtered away. `Board.cards(in:)` stays raw: the outbox's landed
        // check reads the same list.
        let cards = board.cards(in: id)
        let visible = visibleCards(in: id)
        if id == "prep" {
            prepBatchControl
            waitingSection
        }
        if id == "backlog" {
            backlogBatchControl
        }
        if id == "done" {
            clearDoneChrome
        }
        if cards.isEmpty {
            if id == "done" && board.doneCount > 0 {
                doneEmptyLine(count: board.doneCount)
            } else {
                CommentLine(text: "nothing here")
            }
        } else if projectCards(in: id).isEmpty {
            CommentLine(text: BoardProjects.rowLine(project: activeProject))
        } else if visible.isEmpty {
            CommentLine(text: "no cards match")
        } else {
            cardsStack(tickedFirst(visible, in: id))
        }
    }

    /// The selecting row draws its ticked cards first, in the row's own
    /// order among themselves, so a batch being gathered sits together at
    /// the top of Prep or Backlog. Every other row is untouched.
    private func tickedFirst(_ cards: [BoardCard], in id: String) -> [BoardCard] {
        guard selectingRow == id, !selection.isEmpty else { return cards }
        let ticked = Set(selection)
        return cards.filter { ticked.contains($0.id) }
            + cards.filter { !ticked.contains($0.id) }
    }

    /// What the finished row says when it draws no cards.
    ///
    /// The count is store-wide, so a blank row under a number must never be
    /// left unexplained. Until the finished column has been fetched it is
    /// *incomplete*, not empty, and it says so; a fetch that failed says that
    /// and offers to ask again. Only once it is in hand is the old sentence
    /// the honest one.
    @ViewBuilder
    private func doneEmptyLine(count: Int) -> some View {
        switch client.doneArchiveState {
        case .loading:
            Text("Reading the \(count) finished items…")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .failed:
            VStack(alignment: .leading, spacing: 6) {
                Text("Could not read the finished items.")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                DecryptButton("Retry") { client.retryDoneArchive() }
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        case .ready:
            Text("\(count) Done items exist across all projects, "
                 + "but none are on this page.")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Group under project headings only when the column spans more than one
    /// named project. 0–1 projects keep the flat snapshot order.
    @ViewBuilder
    private func cardsStack(_ cards: [BoardCard]) -> some View {
        let names = Set(cards.map(\.project).filter { !$0.isEmpty })
        let ungrouped = cards.filter { $0.project.isEmpty }
        if names.count > 1 {
            ForEach(names.sorted(), id: \.self) { name in
                BoardProjectHeader(title: name)
                ForEach(cards.filter { $0.project == name }) { card in
                    cardLink(card)
                }
            }
            if !ungrouped.isEmpty {
                BoardProjectHeader(title: "—")
                ForEach(ungrouped) { card in
                    cardLink(card)
                }
            }
        } else {
            ForEach(cards) { card in
                cardLink(card)
            }
        }
    }

    @ViewBuilder
    private func cardLink(_ card: BoardCard) -> some View {
        if selectingRow == "prep" && card.column == "prep" {
            // Select mode: the tile is a tick box, and the press toggles it
            // rather than opening the card.
            tickableCard(card)
        } else if selectingRow == "backlog" && card.column == "backlog" {
            // The Backlog row's select mode: the same tick box, judged by
            // the Start rule.
            tickableCard(card)
        } else {
            // A card on its way out stays where it is, dimmed and saying so,
            // and the way through to its detail screen is shut: the press
            // happened on that screen, `apply(result, pop: true)` popped back
            // here, and re-opening a card being destroyed is not a route.
            let leaving = client.cardLeaving(card.id)
            let swipes = PhoneCardSwipe.rows.contains(card.column) && !leaving
            let primary = swipePrimary(for: card)
            let tile = DecryptButton(action: {
                // A slid-aside card closes instead of opening.
                if revealed == card.id {
                    revealed = nil
                } else {
                    swipeRefusal[card.id] = nil
                    sheets.show(.card(card))
                }
            }) {
                PhoneBoardCard(card: card,
                               notice: client.boardNotices[card.id] ?? "",
                               leaving: leaving,
                               live: liveStageNames(card))
                    .equatable()
            }
            .buttonStyle(.plain)
            .disabled(leaving)
            if swipes {
                VStack(alignment: .leading, spacing: 4) {
                    SwipeRevealRow(revealed: revealed == card.id, onReveal: { open in
                        // Only a real change disarms: a drag that ends where
                        // it began leaves a waiting "Really start?" alone.
                        if open, revealed != card.id {
                            revealed = card.id
                            arm.disarm()
                        } else if !open, revealed == card.id {
                            revealed = nil
                            arm.disarm()
                        }
                    }, actions: {
                        swipeButtons(for: card, primary: primary)
                    }, content: {
                        tile
                            .accessibilityActions {
                                if let primary {
                                    DecryptButton(swipeLabel(primary, for: card)) {
                                        pressSwipe(primary, card)
                                    }
                                }
                                DecryptButton(PhoneCardSwipe.deleteRow) { deleting = card }
                            }
                    })
                    swipeNote(for: card)
                }
                .onAppear { swipeNoteArrived(client.queueNote(for: card.id), for: card) }
                .onChange(of: client.queueNote(for: card.id)) { _, queued in
                    swipeNoteArrived(queued, for: card)
                }
                // The press is on its way: the phone's own refusal of an
                // earlier one is stale.
                .onChange(of: client.queueMark(for: card.id)) { _, mark in
                    if mark != nil { swipeRefusal[card.id] = nil }
                }
            } else {
                tile
            }
        }
    }

    // MARK: - Swipe on Prep and Backlog cards

    /// The verb the first swipe button wears: the card screen's own two
    /// reaches (`PhoneCardDetailView.canRefine` / `canStart`) through the
    /// one rule, `PhoneCardSwipe.primary`.
    private func swipePrimary(for card: BoardCard) -> PhoneCardSwipe.Primary? {
        PhoneCardSwipe.primary(
            column: card.column,
            canRefine: PhoneRowSelection.tickable("prep", card: card,
                                                  dispatchEnabled: board.dispatchEnabled),
            canStart: PhoneCardSwipe.canStart(card: card,
                                              dispatchEnabled: board.dispatchEnabled))
    }

    private func swipeLabel(_ primary: PhoneCardSwipe.Primary, for card: BoardCard) -> String {
        switch primary {
        case .start:
            if client.queuedAction(for: card.id) == PhoneActions.boardDispatch,
               let mark = client.queueMark(for: card.id) {
                return mark
            }
            return PhoneCardSwipe.startLabel(armed: arm.start == card.id,
                                             unplanned: card.planPath.isEmpty && !card.isScout)
        case .refine:
            if client.queuedAction(for: card.id) == PhoneActions.boardRefine,
               let mark = client.queueMark(for: card.id) {
                return mark
            }
            return PhoneCardSwipe.refineLabel(armed: arm.refine == card.id)
        }
    }

    private func pressSwipe(_ primary: PhoneCardSwipe.Primary, _ card: BoardCard) {
        switch primary {
        case .start: pressSwipeStart(card)
        case .refine: pressSwipeRefine(card)
        }
    }

    /// The buttons under a slid-aside card: the one next action, and the
    /// dots, whose menu holds the quiet verbs (Delete card today).
    @ViewBuilder
    private func swipeButtons(for card: BoardCard,
                              primary: PhoneCardSwipe.Primary?) -> some View {
        HStack(spacing: 6) {
            if let primary {
                DecryptButton(swipeLabel(primary, for: card)) {
                    pressSwipe(primary, card)
                }
                .buttonStyle(AlarmOutline(size: 11))
                .frame(maxWidth: .infinity)
            }
            DecryptButton(action: { moreFor = card }) {
                Text(PhoneCardSwipe.moreLabel)
                    .font(Theme.mono(16, weight: .medium))
                    .foregroundStyle(.white)
                    .frame(minWidth: 44, minHeight: 44)
                    .background(SwipeInk.more)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(PhoneCardSwipe.moreSpoken)
        }
    }

    /// The Mac's own words about a swiped press, or the phone's own refusal,
    /// under the card in orange.
    @ViewBuilder
    private func swipeNote(for card: BoardCard) -> some View {
        let words = client.queueNote(for: card.id)?.text ?? swipeRefusal[card.id] ?? ""
        if client.queuedAction(for: card.id) == PhoneActions.boardDelete,
           let mark = client.queueMark(for: card.id) {
            Text("Deleting this card · \(mark)")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else if !words.isEmpty {
            Text(words)
                .font(Theme.mono(11))
                .foregroundStyle(.orange)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Start, the card screen's `pressStart` / `dispatchStart` line for line:
    /// the first press arms, the second sends. START HERE and the plain
    /// move stay on the card screen.
    private func pressSwipeStart(_ card: BoardCard) {
        let skip = card.planPath.isEmpty && !card.isScout
        if arm.confirm(.start, id: card.id) {
            var fields = ["card_id": card.id]
            if skip { fields["skip_plan_gate"] = "true" }
            swipeRefusal[card.id] = nil
            let sent = fields
            Task {
                let result = await client.enqueue(action: PhoneActions.boardDispatch,
                                                  fields: sent, scope: card.id)
                if !result.ok { swipeRefusal[card.id] = result.detail }
            }
        } else {
            arm.arm(.start, id: card.id)
        }
    }

    private func pressSwipeRefine(_ card: BoardCard) {
        if arm.confirm(.refine, id: card.id) {
            swipeRefusal[card.id] = nil
            Task {
                let result = await client.enqueue(action: PhoneActions.boardRefine,
                                                  fields: ["card_id": card.id], scope: card.id)
                if !result.ok { swipeRefusal[card.id] = result.detail }
            }
        } else {
            arm.arm(.refine, id: card.id)
        }
    }

    /// The card screen's confirmed Delete without its `Arm`: the dialog is
    /// the confirmation here, and there is no screen to leave — the tile
    /// dims through `client.cardLeaving`.
    private func sendSwipeDelete(_ card: BoardCard) {
        revealed = nil
        swipeRefusal[card.id] = nil
        Task {
            let result = await client.enqueue(action: PhoneActions.boardDelete,
                                              fields: ["card_id": card.id], scope: card.id)
            if !result.ok { swipeRefusal[card.id] = result.detail }
        }
    }

    /// The Mac's note about a swiped press, drawn and therefore read, once
    /// per receipt. A plan-gate refusal is read too: it is drawn here, and
    /// the card screen's `noteArrived` arms its confirmation off the note
    /// whether or not it was read, so leaving it unread only left the
    /// receipt stuck on the QUEUE list.
    private func swipeNoteArrived(_ queued: QueueNote?, for card: BoardCard) {
        guard let queued, queued.id != swipeNoteSeen else { return }
        swipeNoteSeen = queued.id
        swipeRefusal[card.id] = nil
        client.readQueueNote(for: card.id)
    }

    // MARK: - Select mode (batch Refine on Prep, batch Start on Backlog)

    /// The batch press's queue mark — `QUEUED`, `SENDING…`, `SENT` — or nil.
    private var batchMark: String? {
        client.queueMark(for: PhoneRowSelection.scope)
    }

    /// The row whose ticks `selection` holds — read everywhere a literal
    /// row would be, so a Backlog tick is never judged by the Prep rule.
    private var selectingColumn: String { selectingRow ?? "prep" }

    /// Whether the selecting row's press is out: the Prep row's queue mark,
    /// or the Backlog row's synchronous send.
    private var pressOut: Bool {
        selectingRow == "backlog" ? batchSending : batchMark != nil
    }

    /// The ticked cards, in tick order, off the live board.
    private var selectedCards: [BoardCard] {
        let row = board.cards(in: selectingColumn)
        return selection.compactMap { id in row.first { $0.id == id } }
    }

    /// The ticked ids in board order: what the press sends and what the
    /// arm is keyed on, computed the same way on both presses.
    private var batchIds: [String] {
        PhoneRowSelection.orderedIds(selected: selection,
                                     in: board.cards(in: selectingColumn))
    }

    private func tick(for card: BoardCard) -> PhoneRowSelection.Tick {
        if selection.contains(card.id) { return .ticked }
        return PhoneRowSelection.admits(selectingColumn, card: card, given: selectedCards,
                                        dispatchEnabled: board.dispatchEnabled)
            ? .open : .barred
    }

    private func tickableCard(_ card: BoardCard) -> some View {
        let tick = tick(for: card)
        let leaving = client.cardLeaving(card.id)
        return DecryptButton(action: { toggle(card) }) {
            PhoneBoardCard(card: card,
                           notice: client.boardNotices[card.id] ?? "",
                           leaving: leaving,
                           live: liveStageNames(card),
                           tick: tick)
                .equatable()
        }
        .buttonStyle(.plain)
        // A barred card does not tick; nor does anything while the press
        // is out, so the set that went is the set on screen.
        .disabled(tick == .barred || leaving || pressOut)
        .accessibilityLabel((selectingRow == "backlog" ? "Select for batch start: "
                                                       : "Select for batch refine: ")
                            + (card.title.isEmpty ? "untitled" : card.title))
        .accessibilityValue(PhoneBoardCard.tickWord(tick))
    }

    private func toggle(_ card: BoardCard) {
        if let index = selection.firstIndex(of: card.id) {
            selection.remove(at: index)
        } else if tick(for: card) == .open {
            selection.append(card.id)
        }
    }

    /// Under the Prep heading: SELECT where the Mac takes the batch verb
    /// and at least two of the drawn cards could be refined — **absent**,
    /// never inert — or, in select mode, the batch button, CANCEL and the
    /// note. SELECT is absent too while a batch press is out: a select mode
    /// entered then would be a greyed QUEUED button over no ticks.
    @ViewBuilder
    private var prepBatchControl: some View {
        if selectingRow != "prep" {
            if board.refineBatchSupported && batchMark == nil && selectingRow == nil
                && PhoneRowSelection.offersSelect("prep", cards: visibleCards(in: "prep"),
                                                  dispatchEnabled: board.dispatchEnabled) {
                DecryptButton("SELECT") {
                    arm.disarm()
                    selection = []
                    batchRefusal = ""
                    selectingRow = "prep"
                }
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
                .frame(minWidth: 44, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
                .buttonStyle(.plain)
                .accessibilityLabel("Select Prep cards to refine together")
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 12) {
                    batchButton
                    // Enabled while the press is out: CANCEL leaves select
                    // mode and never cancels the queued press, which stays on
                    // the QUEUE list with its mark and RETRY — a press queued
                    // offline must not lock the row until reconnect.
                    DecryptButton("CANCEL") { leaveSelectMode() }
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                        .accessibilityLabel("Leave select mode")
                }
                batchAssistantRow
                if !batchRefusal.isEmpty {
                    Text(batchRefusal)
                        .font(Theme.mono(11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// In select mode, once a card is ticked: one assistant for every ticked
    /// card, the card screen's own tile row. A batch runs in one session, so
    /// the Mac refuses a set naming different assistants; one tap here
    /// writes the pick to each ticked card that names another one — the
    /// card screen's `boardUpdate` with `tool` alone, one per card.
    @ViewBuilder
    private var batchAssistantRow: some View {
        let cards = selectedCards
        if !cards.isEmpty && !board.tools.isEmpty {
            let shared = PhoneRowSelection.sharedTool(cards)
            let waiting = pendingBatchTool != nil && shared != pendingBatchTool
            VStack(alignment: .leading, spacing: 6) {
                Text(shared.isEmpty && !waiting
                     ? "assistant for all — the ticked cards differ"
                     : (waiting ? "assistant for all — sending…" : "assistant for all"))
                    .font(Theme.mono(11))
                    .foregroundStyle(shared.isEmpty && !waiting ? .orange : Theme.faint)
                    .accessibilityHidden(true)
                PhoneProviderSwitch(tools: board.tools,
                                    installed: board.installed,
                                    selected: waiting ? (pendingBatchTool ?? "") : shared,
                                    sending: waiting,
                                    pick: setBatchTool)
                    .disabled(pressOut)
            }
            .onChange(of: shared) { _, now in
                if now == pendingBatchTool { pendingBatchTool = nil }
            }
        }
    }

    /// Writes `tool` to every ticked card naming another assistant. Each is
    /// queued under its own card's scope, exactly as the card screen's pick,
    /// so its receipt settles when that card names the tool. The first
    /// refusal is drawn under the button and lets the tiles go.
    private func setBatchTool(_ tool: String) {
        let ids = PhoneRowSelection.retoolIds(selectedCards, to: tool)
        guard !ids.isEmpty else { return }
        arm.disarm()
        batchRefusal = ""
        pendingBatchTool = tool
        Task { @MainActor in
            for id in ids {
                let result = await client.enqueue(action: PhoneActions.boardUpdate,
                                                  fields: ["card_id": id, "tool": tool],
                                                  scope: id)
                if !result.ok {
                    pendingBatchTool = nil
                    batchRefusal = result.detail
                    return
                }
            }
        }
    }

    private var batchButton: some View {
        let count = selection.count
        let armed = arm.refineBatch != nil
        let words = batchMark
            ?? (armed ? PhoneRowSelection.armedVerb("prep", count: count)
                      : PhoneRowSelection.verb("prep", count: count))
        return DecryptButton(words) { pressBatch() }
            .font(Theme.mono(12, weight: .semibold))
            .foregroundStyle(armed ? Theme.alarm : Theme.phosphorBright)
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
            .buttonStyle(.plain)
            .disabled(count < PhoneRowSelection.minimum || batchMark != nil)
            .accessibilityLabel(words)
    }

    /// Armed on the first press, sent on the second. The arm's id is the
    /// joined list in board order, so a tick changed in between fails the
    /// confirm and re-arms rather than firing on a set nobody read.
    private func pressBatch() {
        let ids = batchIds
        let key = ids.joined(separator: ",")
        if arm.confirm(.refineBatch, id: key) {
            Task { await sendBatch(ids) }
        } else {
            batchRefusal = ""
            arm.arm(.refineBatch, id: key)
        }
    }

    /// Written into the queue under one scope and returned at once; the
    /// board shows every ticked card being planned when it lands. A refused
    /// enqueue (the duplicate, a declined Face ID) never entered the queue,
    /// so its words go under the button and the ticks stay.
    private func sendBatch(_ ids: [String]) async {
        arm.disarm()
        batchRefusal = ""
        let result = await client.enqueue(action: PhoneActions.boardRefineBatch,
                                          fields: ["card_ids": ids.joined(separator: ",")],
                                          scope: PhoneRowSelection.scope)
        if !result.ok { batchRefusal = result.detail }
    }

    /// The Mac's refusal of the batch press, drawn and therefore read, once
    /// per receipt. Only while the row is selecting: a note nobody can see
    /// is not read, and stays STUCK on the QUEUE list with its RETRY.
    private func batchNoteArrived(_ queued: QueueNote?) {
        guard selectingRow == "prep", let queued, queued.id != batchNoteSeen else {
            return
        }
        batchNoteSeen = queued.id
        batchRefusal = queued.text
        client.readQueueNote(for: PhoneRowSelection.scope)
    }

    /// Drop ticks the new board no longer allows — never while a press is
    /// out, whose members stop being tickable the moment the frame shows
    /// them refining. Judged against the whole row, not what is drawn: a
    /// search or a project filter hides a ticked card, it does not untick it.
    private func pruneSelection() {
        if selectingRow == "backlog" {
            guard !batchSending else { return }
            let pruned = PhoneRowSelection.prune("backlog", selected: selection,
                                                 in: board.cards(in: "backlog"),
                                                 dispatchEnabled: board.dispatchEnabled)
            if pruned != selection { selection = pruned }
            return
        }
        guard selectingRow == "prep", batchMark == nil else { return }
        let pruned = PhoneRowSelection.prune("prep", selected: selection,
                                             in: board.cards(in: "prep"),
                                             dispatchEnabled: board.dispatchEnabled)
        if pruned != selection { selection = pruned }
    }

    /// A changed set of ticks, search or project is a different press: the
    /// arm was keyed on the old one, so it goes rather than firing on a set
    /// nobody read.
    private func disarmBatch() {
        if arm.refineBatch != nil || arm.startBatch != nil { arm.disarm() }
    }

    private func leaveSelectMode() {
        disarmBatch()
        selectingRow = nil
        selection = []
        batchRefusal = ""
        pendingBatchTool = nil
    }

    // MARK: - Batch Start on Backlog

    /// Under the Backlog heading: the Mac's report of the last batch Start;
    /// SELECT where the Mac takes the batch verb, no row is selecting and at
    /// least two of the drawn cards could be started — **absent**, never
    /// inert — or, in select mode, the batch button, CANCEL and the refusal.
    @ViewBuilder
    private var backlogBatchControl: some View {
        if selectingRow != "backlog" {
            if !startReport.isEmpty {
                Text(startReport)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(startReport)
            }
            if selectingRow == nil && board.startBatchSupported && !batchSending
                && PhoneRowSelection.offersSelect("backlog", cards: visibleCards(in: "backlog"),
                                                  dispatchEnabled: board.dispatchEnabled) {
                DecryptButton("SELECT") {
                    arm.disarm()
                    selection = []
                    batchRefusal = ""
                    startReport = ""
                    selectingRow = "backlog"
                }
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
                .frame(minWidth: 44, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
                .buttonStyle(.plain)
                .accessibilityLabel("Select planned cards to start together")
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 12) {
                    startBatchButton
                    // Disabled while the press is out: a synchronous press
                    // has no queue row to fall back on, and it returns
                    // within the client's own timeout.
                    DecryptButton("CANCEL") { leaveSelectMode() }
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                        .disabled(batchSending)
                        .accessibilityLabel("Leave select mode")
                }
                batchAssistantRow
                if !batchRefusal.isEmpty {
                    Text(batchRefusal)
                        .font(Theme.mono(11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var startBatchButton: some View {
        let count = selection.count
        let armed = arm.startBatch != nil
        let words = batchSending ? "STARTING…"
            : (armed ? PhoneRowSelection.armedVerb("backlog", count: count)
                     : PhoneRowSelection.verb("backlog", count: count))
        return DecryptButton(words) { pressStartBatch() }
            .font(Theme.mono(12, weight: .semibold))
            .foregroundStyle(armed ? Theme.alarm : Theme.phosphorBright)
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
            .buttonStyle(.plain)
            .disabled(count < PhoneRowSelection.minimum || batchSending)
            .accessibilityLabel(words)
    }

    /// Armed on the first press, sent on the second, keyed on the joined
    /// list in board order — `pressBatch()`'s rule on its own slot.
    private func pressStartBatch() {
        let ids = batchIds
        let key = ids.joined(separator: ",")
        if arm.confirm(.startBatch, id: key) {
            Task { @MainActor in await sendStartBatch(ids) }
        } else {
            batchRefusal = ""
            arm.arm(.startBatch, id: key)
        }
    }

    /// Sent synchronously and never banked: a batch Start opens a terminal,
    /// so it happens at the person's press or not at all — an unreachable
    /// Mac is a refusal in words and nothing fires later on its own. The
    /// reply's `detail` is the Mac's report and is drawn whether or not the
    /// press succeeded, START PROJECT's rule: under the heading on success,
    /// under the button with the ticks kept on a refusal. `post` refetches
    /// the board itself, which is where the batch lines come from.
    private func sendStartBatch(_ ids: [String]) async {
        arm.disarm()
        batchRefusal = ""
        batchSending = true
        let result = await client.post(action: PhoneActions.boardStartBatch,
                                       fields: ["card_ids": ids.joined(separator: ",")],
                                       scope: PhoneRowSelection.startScope)
        batchSending = false
        if result.ok {
            startReport = result.detail
            selection = []
            selectingRow = nil
        } else {
            batchRefusal = result.detail
        }
    }

    /// The stages running under this card's session right now — the panel's
    /// `liveStageNames`, resolved through the fleet's own `row(session:)` so
    /// the two surfaces cannot disagree about which row a card is bound to.
    /// Empty for a card whose session the fleet has forgotten, which is
    /// correct: nothing is active, and every stage it reached stays done.
    private func liveStageNames(_ card: BoardCard) -> Set<String> {
        let sid = !card.sessionId.isEmpty
            ? card.sessionId
            : (card.isRefining ? card.refineSessionId : "")
        guard let (agent, _) = client.snapshot.agents.row(session: sid) else {
            return []
        }
        return Set(agent.subagentRows.map(\.label).filter { !$0.isEmpty })
    }

    /// Store-wide Done sweep. Drawn only on the Done page, and absent rather
    /// than inert against a Mac that does not publish `clear_done_writable`.
    @ViewBuilder
    private var clearDoneChrome: some View {
        let shown = board.available && board.clearDoneWritable
            && board.hasSafeDoneClearToken && board.doneCount > 0
        if shown {
            VStack(alignment: .leading, spacing: 8) {
                if pendingClear != nil {
                    HStack(spacing: 7) {
                        AgentChatterView(.caret, wait: .clearingDone,
                                         seed: "clear-done",
                                         spoken: "Clearing all done items")
                            .id("clear-done")
                        Text("Clearing all Done items…")
                            .font(Theme.mono(11, weight: .medium))
                            .foregroundStyle(Theme.faint)
                    }
                } else if arm.clearDone == "clear-done", let armed = armedClear {
                    Text("This includes all \(armed.count) Done items across every "
                         + "project, including ones not on this page. There is no "
                         + "undo. Outcome evidence stays in history.")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHidden(true)
                    DecryptButton {
                        confirmClearDone()
                    } label: {
                        Text("Really clear all \(armed.count) now?")
                            .font(Theme.mono(13, weight: .semibold))
                            .foregroundStyle(Theme.alarm)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .frame(minWidth: 44, minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(
                        "This includes all \(armed.count) Done items across every "
                        + "project, including ones not on this page. There is no "
                        + "undo. Outcome evidence stays in history. Really clear "
                        + "all \(armed.count) now?")
                } else {
                    DecryptButton {
                        armClearDone()
                    } label: {
                        Text("Clear all done items")
                            .font(Theme.mono(13, weight: .semibold))
                            .foregroundStyle(Theme.phosphor)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .frame(minWidth: 44, minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(clearDoneRefusal.isEmpty
                        ? "Clear all done items"
                        : "Clear all done items. \(clearDoneRefusal)")
                }
                if !clearDoneRefusal.isEmpty {
                    Text(clearDoneRefusal)
                        .font(Theme.mono(11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHidden(true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .allowsHitTesting(pendingClear == nil)
        }
    }

    private func armClearDone() {
        clearDoneRefusal = ""
        armedClear = ClearDoneScope(count: board.doneCount,
                                    token: board.doneClearToken)
        arm.arm(.clearDone, id: "clear-done")
    }

    private func confirmClearDone() {
        guard let expected = armedClear else { return }
        guard arm.confirm(.clearDone, id: "clear-done") else { return }
        armedClear = nil
        let live = ClearDoneScope(count: board.doneCount,
                                  token: board.doneClearToken)
        if live != expected {
            clearDoneRefusal = "Done changed while you were confirming. "
                + "Nothing was cleared; confirm again against the new set."
            return
        }
        clearDoneRefusal = ""
        pendingClear = expected
        Task { @MainActor in
            let result = await client.post(
                action: PhoneActions.boardClearDone,
                fields: [
                    "expected_count": String(expected.count),
                    "expected_done_token": expected.token,
                ],
                scope: "clear-done")
            if result.ok {
                if board.doneCount != expected.count
                    || board.doneClearToken != expected.token {
                    pendingClear = nil
                }
                return
            }
            pendingClear = nil
            clearDoneRefusal = (result.detail == PhoneActions.olderMac
                                || result.detail == "not found")
                ? PhoneActions.olderMac : result.detail
        }
    }

    private func noteClearDoneSnapshot() {
        if let sent = pendingClear {
            if board.doneCount != sent.count
                || board.doneClearToken != sent.token {
                pendingClear = nil
            }
            return
        }
        if arm.clearDone == "clear-done", let expected = armedClear {
            let live = ClearDoneScope(count: board.doneCount,
                                      token: board.doneClearToken)
            if live != expected {
                arm.disarm()
                armedClear = nil
                clearDoneRefusal = "Done changed while you were confirming. "
                    + "Nothing was cleared; confirm again against the new set."
            }
        }
    }
}

/// Count and membership token stashed at arm, re-checked at confirm.
private struct ClearDoneScope: Equatable {
    var count: Int
    var token: String
}

/// PhoneSectionHeader's type, minus the 12pt horizontal padding that file
/// adds for Fleet's zero-inset list rows. The board page already pads the
/// stack; repeating it here would inset the heading past the cards.
private struct BoardProjectHeader: View {
    let title: String

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Text(title)
            .font(Theme.mono(11, weight: .semibold))
            .tracking(dynamicTypeSize.isAccessibilitySize ? 0 : 0.8)
            .foregroundStyle(Theme.faint)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 14)
            .padding(.bottom, 4)
            // A landmark the rotor can jump between, rather than one more
            // line to swipe past.
            .accessibilityAddTraits(.isHeader)
    }
}

/// The Mac's card, at phone measure. It carries no verb of its own; its one
/// disabled state names a verb pressed elsewhere — a Delete confirmed on the
/// card's own screen, still out or still settling.
struct PhoneBoardCard: View {
    let card: BoardCard
    /// The phone's own line about this card — today, only the settling
    /// backstop's. Empty on every card that has nothing to admit.
    var notice = ""
    /// A confirmed Delete for this card is out or settling. Drawn dimmed and
    /// with the word, never dimmed alone: colour-alone says nothing.
    var leaving = false
    /// The stages running under this card's session right now. Empty is a
    /// legitimate answer everywhere — the fleet has forgotten the row, or
    /// nothing is running — and reads as "every stage it reached is done".
    var live: Set<String> = []
    /// The select-mode box, `nil` outside select mode — every other
    /// constructor is unchanged and draws no box.
    var tick: PhoneRowSelection.Tick? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if card.manualCheckDue {
                // Above the title, because it is the first thing about this
                // card that is true. Amber rather than the orange a refusal
                // wears: orange means Dark Army refused something, this means your
                // turn. The title is never rewritten.
                Text("MANUAL CHECK NEEDED")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .tracking(0.8)
            }
            // Every line on this tile runs on as far as it needs: these
            // are `VStack` children in a full-width cell, so they wrap
            // without a `fixedSize`, and the column scrolls further.
            if let tick {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(Self.tickGlyph(tick))
                        .font(Theme.mono(13, weight: .semibold))
                        .foregroundStyle(Self.tickColour(tick))
                        .accessibilityHidden(true)
                    titleLine
                }
            } else {
                titleLine
            }
            if card.isScout {
                Text("SCOUT")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
            }
            if !card.area.isEmpty {
                Text(Areas.name(card.area))
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
            }
            if !card.project.isEmpty {
                Text(card.project)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            if !metaLine.isEmpty {
                // The panel's `liveState` cursor, in the row: a card being
                // started or refined blinks beside its state word. Drawn
                // from the daemon's own `linkState` / `refineState`, never
                // derived. The 9pt caret carries the chatter's tap-to-skip,
                // so a tap exactly on it finishes nothing visible.
                HStack(spacing: 5) {
                    if card.linkState == "dispatching" {
                        AgentChatterView(.caret, wait: .starting, seed: card.id,
                                         spoken: "Starting this card")
                            .id(card.id)
                    } else if card.isRefining {
                        AgentChatterView(.caret, wait: .refining, seed: card.id,
                                         spoken: "Refining this card")
                            .id(card.id)
                    }
                    Text(metaLine)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                }
            }
            if !card.summary.isEmpty {
                Text(card.summary)
                    .font(Theme.prose(14))
                    .foregroundStyle(Theme.muted)
            }
            // The crew, same view and same style as the Mac's card face.
            // Decoration inside a row that is already one accessibility
            // element; the sentence rides `spoken` instead.
            CrewBand(workflow: Specialists.parse(card.workflow),
                     trail: Specialists.parse(card.agentTrail),
                     live: live,
                     crew: card.crew,
                     lead: card.area,
                     promised: card.leadFace,
                     style: .strip)
                .accessibilityHidden(true)
            if card.queueState == "queued" {
                // Verbatim, or the bare word if the Mac sent none. Never a
                // phone-built sentence — that is how two surfaces disagree.
                Text(card.queueReason.isEmpty ? "queued" : card.queueReason)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            // Its place in a batch — the Mac's line, faint while it waits.
            if !card.batchLine.isEmpty {
                Text(card.batchLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(card.isBatchWaiting ? Theme.faint : Theme.phosphor)
            }
            // What this card waits on and what it unblocks — the Mac's two
            // sentences, verbatim, each only where the Mac sent one.
            if !card.dependencyLine.isEmpty {
                Text(card.dependencyLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            if !card.dependentsLine.isEmpty {
                Text(card.dependentsLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            if !card.dispatchError.isEmpty {
                Text(card.dispatchError)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
            }
            if leaving {
                // The notice slot. Only one of the two is ever set: the
                // backstop writes `notice` when it gives up, which is the
                // same moment the settling hold — and so `leaving` — ends.
                // The typed line above the word: the card is
                // `.disabled(leaving)` for the whole window, so the
                // chatter's tap-to-skip never steals a link press here.
                AgentChatterView(.line, wait: .deleting, seed: card.id,
                                 spoken: "Deleting this card")
                    .id(card.id)
                Text(PhoneClient.cardLeavingLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            } else if !notice.isEmpty {
                Text(notice)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(SignalTokens.Spacing.md)
        .background(RoundedRectangle(cornerRadius: Theme.cardRadius).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius)
            .strokeBorder(Theme.line, lineWidth: 1))
        .contentShape(Rectangle())
        .opacity(leaving ? 0.45 : 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    private var titleLine: some View {
        Text(card.title.isEmpty ? "untitled" : card.title)
            .font(Theme.prose(17, weight: .semibold))
            .foregroundStyle(Theme.text)
    }

    static func tickColour(_ tick: PhoneRowSelection.Tick) -> Color {
        switch tick {
        case .ticked: return Theme.phosphorBright
        case .open: return Theme.phosphor
        case .barred: return Theme.faint
        }
    }

    /// The drawn box: `[x]` ticked, `[ ]` open, `[-]` barred — the shape
    /// differs, never the colour alone (`tickColour` is the second cue).
    static func tickGlyph(_ tick: PhoneRowSelection.Tick) -> String {
        switch tick {
        case .ticked: return "[x]"
        case .open: return "[ ]"
        case .barred: return "[-]"
        }
    }

    /// The box in words — the spoken half of the drawn `[x]` / `[ ]` / `[-]`.
    static func tickWord(_ tick: PhoneRowSelection.Tick) -> String {
        switch tick {
        case .ticked: return "ticked"
        case .open: return "not ticked"
        case .barred: return "cannot be ticked"
        }
    }

    /// The whole card as one sentence. Every part is a string this card
    /// already draws — `card.queueReason` and `card.dispatchError` **verbatim**,
    /// because the Mac composed them and a second phrasing is how two surfaces
    /// come to disagree.
    private var spoken: String {
        var parts: [String] = []
        if card.manualCheckDue { parts.append("manual check needed") }
        parts.append(card.title.isEmpty ? "untitled" : card.title)
        if let tick { parts.append(Self.tickWord(tick)) }
        if !card.project.isEmpty { parts.append(card.project) }
        if !metaLine.isEmpty { parts.append(metaLine) }
        if !card.summary.isEmpty { parts.append(card.summary) }
        if card.queueState == "queued" {
            parts.append(card.queueReason.isEmpty ? "queued" : card.queueReason)
        }
        if !card.batchLine.isEmpty { parts.append(card.batchLine) }
        if !card.dependencyLine.isEmpty { parts.append(card.dependencyLine) }
        if !card.dependentsLine.isEmpty { parts.append(card.dependentsLine) }
        if !card.dispatchError.isEmpty { parts.append(card.dispatchError) }
        // Composed once, in the shared file, so the Mac's tooltip and this
        // sentence cannot come to say different things.
        parts.append(CrewBand.caption(workflow: Specialists.parse(card.workflow),
                                      trail: Specialists.parse(card.agentTrail),
                                      live: live, crew: card.crew, lead: card.area,
                                      promised: card.leadFace))
        if leaving {
            parts.append(PhoneClient.cardLeavingLine)
        } else if !notice.isEmpty {
            parts.append(notice)
        }
        return parts.filter { !$0.isEmpty }.joined(separator: ", ")
    }

    private var metaLine: String {
        var parts: [String] = []
        // The importance number first: it is what the column is ordered by,
        // so it is the first thing to read. `""` is "nobody has scored this"
        // and adds nothing. `spoken` already appends `metaLine`, so VoiceOver
        // gets the number with no second edit and the rule that a spoken row
        // is composed only from fields it draws still holds by construction.
        if !card.priority.isEmpty { parts.append("P \(card.priority)") }
        if !card.tool.isEmpty { parts.append(card.tool) }
        if !card.planPath.isEmpty { parts.append("planned") }
        if !card.linkState.isEmpty { parts.append(card.linkState) }
        // A refinement claims no `linkState` — the card stays in Prep with
        // its agent under `refineState` — so without this word the row
        // says nothing about a session at work on it. In `metaLine`
        // rather than beside it, so `spoken` says it too.
        if card.isRefining { parts.append("refining") }
        return parts.joined(separator: " · ")
    }
}


/// A tile is its inputs and nothing else — no state, no environment, no
/// clock — so two tiles with equal inputs draw the same, and `.equatable()`
/// at both call sites lets SwiftUI skip the body of every row whose card did
/// not move. The client publishes several times a poll (the phase line, the
/// link note, `lastHeard`, the agents' churn), and each publish re-runs
/// `BoardView.body`; without this every visible tile rebuilt its crew band,
/// its two `Specialists.parse` calls and its spoken sentence each time.
extension PhoneBoardCard: Equatable {}

/// One queued card and its two verbs. Two presses to remove, so a stray tap
/// cannot throw away words nobody else has a copy of; one press to RETRY a
/// refused entry now — a person overriding the refusal's backoff window.
private struct OutboxRow: View {
    let entry: OutboxEntry
    let armed: Bool
    /// The store's own sweep flag, not a view-local one: `sync` returns at
    /// its guard when a sweep is already running, so a flag raised around
    /// the call would clear in the same frame and say nothing.
    let sending: Bool
    let onRetry: () -> Void
    let onRemove: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("> \(entry.title.isEmpty ? "untitled" : entry.title)")
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphorBright)
            if !entry.project.isEmpty {
                Text(entry.project)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            if !entry.summary.isEmpty {
                Text(entry.summary)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
            HStack(spacing: 8) {
                // The Mac's own words when it refused, never a phone-built
                // sentence — that is how two surfaces come to disagree.
                Text(entry.lastError.isEmpty
                     ? OutboxStore.waitingLine : entry.lastError)
                    .font(Theme.mono(11))
                    .foregroundStyle(entry.lastError.isEmpty ? Theme.faint : .orange)
                Spacer(minLength: 0)
                // 11pt text is a ~13pt-tall target. These two sit eight
                // points apart and one of them destroys a card, so both
                // carry `BrandBar`'s 44pt hit area rather than their own
                // glyph height — the ink stays small, the target does not.
                if !entry.lastError.isEmpty {
                    DecryptButton(sending ? "SENDING…" : "RETRY") { onRetry() }
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.phosphorBright)
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                        .disabled(sending)
                        .accessibilityLabel(sending
                                            ? "Sending \(rowTitle) now"
                                            : "Send \(rowTitle) again now")
                }
                DecryptButton(armed ? "REMOVE?" : "REMOVE") { onRemove() }
                    .font(Theme.mono(11))
                    .foregroundStyle(armed ? Theme.alarm : Theme.faint)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                    // Throwing the card away mid-send is the conflicting
                    // sibling: the sweep is already carrying these words.
                    .disabled(sending)
                    // The armed step says so out loud: an armed control that
                    // sounds identical to an unarmed one is a trap.
                    .accessibilityLabel(armed ? "Throw \(rowTitle) away?"
                                              : "Throw \(rowTitle) away")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Rectangle().fill(Theme.card))
        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
        .contentShape(Rectangle())
    }

    private var rowTitle: String {
        entry.title.isEmpty ? "untitled" : entry.title
    }
}

// MARK: - BoardProjects

/// The Board tab's project filter as pure rules. The resolution step is
/// Fleet's own `FleetProjects`; this adds only what is the board's.
enum BoardProjects {
    /// The rows whose cards a chip counts — every row but Done, as a Fleet
    /// chip counts live agents and not finished ones.
    static let openColumns = ["prep", "backlog", "in_progress"]

    /// Chip names: the cards' projects and the Mac's open projects, through
    /// `FleetProjects.names` (unique, non-empty, sorted).
    static func names(cards: [String], open: [String]) -> [String] {
        FleetProjects.names(cards + open)
    }

    /// `""` is `ALL` and matches every card, an unnamed one included.
    static func matches(project: String, active: String) -> Bool {
        active.isEmpty || project == active
    }

    static func counts(_ projects: [String]) -> [String: Int] {
        projects.reduce(into: [:]) { $0[$1, default: 0] += 1 }
    }

    /// A row that has cards, none of them the chosen project's.
    static func rowLine(project: String) -> String {
        "no \(project) cards in this row"
    }

    /// Every row hidden by the filter.
    static func boardLine(project: String) -> String {
        "no \(project) cards on the board · ALL shows every project"
    }
}

// MARK: - BoardRowFold

/// Which rows are folded, as one pure rule — a **byte-equal copy** of the
/// panel's `panel/Sources/BobPanel/BoardRowFold.swift` from the `enum` line
/// down (`host/tests/test_board_rows.py` pins the two equal; edit both
/// copies together). Foundation only; the rows are the daemon's column ids
/// as plain strings.
enum BoardRowFold {
    /// The four rows, top to bottom. The column ids the daemon speaks.
    static let rows = ["prep", "backlog", "in_progress", "done"]

    /// Done starts folded; the three working rows start open.
    static func defaultFolded(_ id: String) -> Bool {
        id == "done"
    }

    /// Folded right now, given the rows a person has flipped away from the
    /// default. XOR: flipping a row that starts folded opens it.
    static func folded(_ id: String, flipped: Set<String>) -> Bool {
        defaultFolded(id) != flipped.contains(id)
    }

    /// What is drawn: a search overrides every fold, so a matching card is
    /// never hidden behind a heading.
    static func drawnFolded(_ id: String, flipped: Set<String>, searching: Bool) -> Bool {
        !searching && folded(id, flipped: flipped)
    }

    /// The set after a press on this row's heading — symmetric difference
    /// on `id`. An id that names no row is a no-op, so a stray value can
    /// never be written into the habit file.
    static func toggled(_ id: String, flipped: Set<String>) -> Set<String> {
        guard rows.contains(id) else { return flipped }
        var out = flipped
        if out.contains(id) {
            out.remove(id)
        } else {
            out.insert(id)
        }
        return out
    }

    /// A stored array back to the flipped set. Members of `rows` only —
    /// anything else is dropped, and nil (never saved) is the defaults.
    static func decode(_ raw: [String]?) -> Set<String> {
        Set((raw ?? []).filter { rows.contains($0) })
    }

    /// The flipped set as an array in `rows` order, so the stored value is
    /// stable across runs. Empty for the defaults.
    static func encode(_ flipped: Set<String>) -> [String] {
        rows.filter { flipped.contains($0) }
    }

    /// The comma-joined spelling the phone's `@AppStorage` string holds.
    static func decode(joined: String) -> Set<String> {
        decode(joined.split(separator: ",").map(String.init))
    }

    static func encode(joined flipped: Set<String>) -> String {
        encode(flipped).joined(separator: ",")
    }
}

/// A card that slides left to show the buttons under it. The slide is a
/// horizontal `DragGesture` attached beside the tile's own button
/// (`.simultaneousGesture`, never `.gesture`), so the tap and the scroll
/// view's vertical pan both survive; the arithmetic is
/// `PhoneCardSwipe`'s, pinned by `host/tests/test_phone_card_swipe.py`.
private struct SwipeRevealRow<Actions: View, Content: View>: View {
    let revealed: Bool
    let onReveal: (Bool) -> Void
    @ViewBuilder let actions: () -> Actions
    @ViewBuilder let content: () -> Content
    /// Reset by SwiftUI when the gesture ends *or is cancelled* (the scroll
    /// view taking the touch), so the tile never stays part-slid.
    @GestureState private var drag: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack(alignment: .trailing) {
            actions()
                .frame(width: PhoneCardSwipe.actionsWidth)
                .accessibilityHidden(!revealed)
                .allowsHitTesting(revealed)
            content()
                .background(Theme.bg)
                .offset(x: PhoneCardSwipe.offset(forTranslation: drag, revealed: revealed))
                .simultaneousGesture(DragGesture(minimumDistance: PhoneCardSwipe.minimumDrag)
                    .updating($drag) { value, state, _ in
                        guard PhoneCardSwipe.dominantHorizontal(
                            dx: value.translation.width,
                            dy: value.translation.height) else { return }
                        state = value.translation.width
                    }
                    .onEnded { value in
                        guard PhoneCardSwipe.dominantHorizontal(
                            dx: value.translation.width,
                            dy: value.translation.height) else { return }
                        onReveal(PhoneCardSwipe.revealAfter(
                            translation: value.translation.width, revealed: revealed))
                    })
        }
        .animation(Motion.animation(.snappy, reduced: reduceMotion), value: drag)
        .animation(Motion.animation(.snappy, reduced: reduceMotion), value: revealed)
    }
}
