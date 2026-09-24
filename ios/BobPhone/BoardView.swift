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
    /// The Board's own project choice, `""` for `ALL` — Fleet's semantics
    /// (`@SceneStorage`, resolved through `FleetProjects.resolve`) under its
    /// own key, so narrowing one tab never narrows the other.
    @SceneStorage("board.project") private var project = ""

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
            .onDisappear { arm.disarm() }
        }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            noteClearDoneSnapshot()
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
        FleetProjects.resolve(selected: project, in: projects, heard: board.available)
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
    private func projectCards(in id: String) -> [BoardCard] {
        board.cards(in: id).filter { BoardProjects.matches(project: $0.project, active: activeProject) }
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
                    Text("[ \(item.title) ] \(count)")
                        .font(Theme.mono(14))
                        .tracking(0.96)
                        .foregroundStyle(Theme.phosphor)
                    Text("// \(item.caption)")
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
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
            waitingSection
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
            cardsStack(visible)
        }
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

    private func cardLink(_ card: BoardCard) -> some View {
        // A card on its way out stays where it is, dimmed and saying so,
        // and the way through to its detail screen is shut: the press
        // happened on that screen, `apply(result, pop: true)` popped back
        // here, and re-opening a card being destroyed is not a route.
        let leaving = client.cardLeaving(card.id)
        return DecryptButton(action: { sheets.show(.card(card)) }) {
            PhoneBoardCard(card: card,
                           notice: client.boardNotices[card.id] ?? "",
                           leaving: leaving,
                           live: liveStageNames(card))
        }
        .buttonStyle(.plain)
        .disabled(leaving)
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
            Text("> \(card.title.isEmpty ? "untitled" : card.title)")
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphorBright)
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
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
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
        .padding(10)
        .background(Rectangle().fill(Theme.card))
        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
        .contentShape(Rectangle())
        .opacity(leaving ? 0.45 : 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// The whole card as one sentence. Every part is a string this card
    /// already draws — `card.queueReason` and `card.dispatchError` **verbatim**,
    /// because the Mac composed them and a second phrasing is how two surfaces
    /// come to disagree.
    private var spoken: String {
        var parts: [String] = []
        if card.manualCheckDue { parts.append("manual check needed") }
        parts.append(card.title.isEmpty ? "untitled" : card.title)
        if !card.project.isEmpty { parts.append(card.project) }
        if !metaLine.isEmpty { parts.append(metaLine) }
        if !card.summary.isEmpty { parts.append(card.summary) }
        if card.queueState == "queued" {
            parts.append(card.queueReason.isEmpty ? "queued" : card.queueReason)
        }
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
