import AppKit
import Combine
import SwiftUI

struct PanelView: View {
    @ObservedObject var client: DaemonClient
    /// Key presses, read by a monitor on the AppKit side. Defaulted so a preview
    /// or a test can build the view without one.
    @ObservedObject var keys: KeyRouter = KeyRouter()
    /// "Show me this session" from outside the panel — today, a tap on a
    /// notification banner. Defaulted for the same reason `keys` is.
    @ObservedObject var focus: FocusRouter = FocusRouter()
    /// The same ask, aimed at the board: bring this session's card into view.
    /// Only the reverse jump from VS Code sends one; defaulted like `focus`
    /// so a preview or a test can build the view without one. Passed straight
    /// through to `BoardView`, which is what applies it — the rail half is
    /// `focus`'s job and nothing here reads this.
    @ObservedObject var cardFocus: CardFocusRouter = CardFocusRouter()
    /// The board lives in this window, as the pane left of the fleet rail.
    /// Owned by the app delegate rather than here (`@ObservedObject`, not
    /// `@StateObject`): the card composer is its own window now, and
    /// `main.swift` has to reach the same instance to close it when the panel
    /// goes away. Constructed **exactly once** in the delegate — a default
    /// argument here would mint a new one per view rebuild and wipe the draft.
    @ObservedObject var board: BoardState

    /// Which view the panel is showing. A segmented head reusing the same frame
    /// and the same row furniture — a 50-row table does not justify a second
    /// window, and a second window is precisely what this project deleted.
    enum Tab: String, CaseIterable {
        /// First and default: what is waiting on you is the reason the panel
        /// gets opened, so it is what the panel opens on.
        case inbox = "Inbox"
        case agents = "Agents"
        /// Talk to Mission Control: the board beside its terminal column,
        /// the chips and composer in the rail (`CommRail.swift`).
        case comm = "Comm"
        /// The desk-only report over `history.db`. Back on 20 Sep 2026 after
        /// a same-day removal; the phone never had it and still does not.
        case history = "History"
        /// Every scout report Dark Army lists, newest first, and one opened
        /// as a document — the wide pane (`ScoutReportsPane`); the rail
        /// holds the search and the project rows (`ScoutReportsRail`).
        case reports = "Reports"
    }

    @State private var tab: Tab = .inbox
    /// The one-line shortcut legend under the brand bar, opened by the
    /// **keys** chip or `?` (`TriageLegend`, drawn from `TriageKeys`).
    @State private var keysLegendShown = false
    /// Reduce Motion, read once here and handed to every `Motion` call.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// History's own lenses. They do not refetch: the report is already
    /// in hand, and the fold reads it again.
    @State private var historyLens: LedgerLens = .spent
    @State private var historyPerson = ""
    @State private var historyDay = ""
    @State private var historyRun = ""
    /// The Reports tab's search line, project row and open report. The
    /// list itself is shared by the rail (projects, counts) and the pane.
    @State private var reportsSearch = ""
    @State private var reportsProject = ""
    @State private var openReportPath = ""
    @State private var reportsIndex = ScoutReportIndex()
    /// The text search's hits and whether one is in flight — hoisted so the
    /// rail's count and the pane's list read one merged list.
    @State private var reportsHits = ScoutReportIndex()
    @State private var reportsSearching = false
    @State private var reportsDetail = ""
    /// Bumped by the rail's Refresh; part of the pane's load key.
    @State private var reportsReload = 0
    /// The project tab the overview is showing. Keyed by the `ProjectTab`
    /// itself (a name) rather than by index, so a vanished
    /// neighbour cannot slide the highlight onto someone else.
    @State private var selectedTab: ProjectTab?
    /// The project whose full row list is open below the
    /// band. `nil` is the overview grid. Set synchronously with
    /// `keys.drilledIn` so Escape cannot hide the panel a beat before the
    /// flag lands.
    @State private var drilled: DrillTarget?
    /// Whether the project tabs + card grid are rolled down. Starts hidden
    /// whenever anyone needs you — that band is why the panel was opened, and
    /// the grid stole its height. Always shown when the band is empty, because
    /// then it is the only content. Selecting a waiter must not undo an
    /// open Projects shelf.
    @State private var showProjects = false
    @State private var expanded: String?
    /// Which rows are showing their agent's last words in full.
    ///
    /// Up here rather than as `@State` on the row, and that is a layout fact
    /// rather than a taste one: a `LazyVStack` with pinned section headers
    /// caches the height it measured for a row and does not re-measure it when
    /// state *inside* the row changes it. The chevron grew the text, the row
    /// kept its old height, and the extra lines painted straight over the
    /// section below. Toggling from here re-evaluates the stack's own body, so
    /// the row is measured again — which is why `expanded`, which has always
    /// lived here, never had the bug.
    @State private var fullText: Set<String> = []
    /// Which rows are on their way *down* the ladder.
    ///
    /// The rungs are closed → detail → message, and a row sitting at "detail,
    /// message folded" is two different states depending on how it got there:
    /// on the way up the next click opens the message, on the way down it
    /// closes the row. Without this the descent could not exist — folding the
    /// message would land on the rung whose click unfolds it again, and the row
    /// would never close. A row enters the set when its message is folded and
    /// leaves it whenever it is unfolded or the row is opened afresh, so the
    /// memo is only ever one click old.
    @State private var folding: Set<String> = []
    /// The list body's real content height, measured inside the scroll view.
    ///
    /// The row constants in `PanelMetrics` are one number each for a row that
    /// may carry a reply bar, a wrap-up strip, a card, a permission bar and a
    /// message of any length, so the estimate was wrong in both directions:
    /// an unfolded row scrolled inside a window with half a screen free under
    /// it, and folding it back left the window shorter than the row it was
    /// drawing. Measured here rather than guessed there — the content's width
    /// is fixed, so its height does not depend on the height the scroll view
    /// is given, which is what keeps this out of the measure-A-to-size-A loop
    /// the height path was written to avoid. Zero whenever the body is not a
    /// list; `PanelMetrics` falls back to the estimate then.
    @State private var listMeasured: CGFloat = 0
    /// Each rail band's measured content height, keyed by its section —
    /// `processBand`'s `listMeasured`, one per Active / Recently finished /
    /// Abandoned list.
    @State private var bandMeasured: [PanelSection: CGFloat] = [:]
    /// Which rows the dwell is still holding in the attention section — a
    /// **membership set, not a clock**. See `inAttention` for why leaving is
    /// delayed and arriving is not, and `stamps` for why the times are kept
    /// somewhere the view cannot see.
    @State private var attentionHold: Set<String> = []
    /// When each row was last seen needing a human.
    ///
    /// A reference box rather than `@State`, and that is the whole point:
    /// mutating it does not invalidate the view. It used to be the state itself,
    /// keyed by row and stamped with `Date()` — so **every** snapshot re-stamped
    /// every attention row with a new instant, the dictionary compared unequal,
    /// and `withAnimation { attentionHold = next }` relaid the entire list
    /// several times a second for as long as anything sat in *Needs you*. That
    /// is the same relayout-under-the-pointer that `api_server._broadcast`
    /// exists to throttle one layer down, reintroduced by the fix for it: a
    /// mouse-down and the mouse-up 100ms later must hit-test to the same view,
    /// and rows in that section did not survive the wait. What actually drives
    /// the drawing is which ids are held, which changes only when a row arrives
    /// or the dwell lets one go.
    private final class HoldStamps { var seen: [String: Date] = [:] }
    @State private var stamps = HoldStamps()
    /// Whether a sweep of `attentionHold` is already pending, so a burst of
    /// pushes schedules one rather than one each.
    @State private var sweepScheduled = false
    /// Which sections are rolled up. Seeded from `startsCollapsed` and then
    /// owned by the user. Held in memory rather than on disk deliberately: the
    /// panel process is launched once by the menu bar and driven over stdin, so
    /// it outlives every open and close — the state already survives everything
    /// except an app restart, and a preferences file for it would be machinery
    /// bought for that one case.
    /// Keyed by section rather than by category now that projects are the
    /// headings. A project that empties and comes back returns rolled down,
    /// which is the same amnesia a category section has always had.
    @State private var collapsed: Set<PanelSection> =
        Set(Category.allCases.filter(\.startsCollapsed).map(PanelSection.category))

    /// The row the keyboard is on. `nil` until someone presses a key — a panel
    /// that opens with a row already highlighted invites a press of Return that
    /// nobody aimed.
    @State private var selected: String?
    /// The row a focus request put the selection on, and the sequence number of
    /// the request that did it. The id is what stops `selectTopAttentionIfNeeded`
    /// taking the selection straight back off a grid row we were *asked* to show
    /// — the two land in the same update when a banner opens a hidden panel. The
    /// seq is what makes applying idempotent, so the two deliverers can both call
    /// `syncFocus` and only the first one acts.
    @State private var focusedRow: String?
    @State private var appliedFocus = 0
    /// A card was just revealed on the board (`showCard(card:)`), so the
    /// board must stay up until a person acts.
    /// `selectTopAttentionIfNeeded` honours it without consuming it: on a
    /// hidden panel `client.visible` flips before `focus.shown`, so the
    /// reveal runs *before* the open's aim, and
    /// the aim that follows — and the one the next snapshot carries
    /// (`aimPendingSinceShown`) — would otherwise draw the top waiter's
    /// detail over the card. Spent with the pending aim, and cleared by
    /// `deselect()`, a row click, a focus request landing (`show(_:)`) and
    /// the panel leaving the screen — every one of them a person acting.
    @State private var revealedForCard = false
    /// A deliberate open aimed the selection against a snapshot that may be
    /// frozen (`hide()` stops the SSE client). Set on `focus.shown`, spent by
    /// the next snapshot through `RailLayout.aimAfterSnapshot`; `deselect()`,
    /// a row click and a hidden panel cancel it, so a closed detail stays
    /// closed.
    @State private var aimPendingSinceShown = false
    /// The first-run checklist's per-opening state (`FirstRunChecklist`):
    /// decided on the first authoritative snapshot after the panel appears,
    /// advanced by every snapshot, reset when it appears again.
    @State private var onboarding = FirstRunChecklist.Opening()
    /// The remembered completion, read once from `panel-position.json` and
    /// written back the moment it becomes true.
    @State private var checklistCompleted = PanelPlacement.checklistCompleted()
    /// The checklist's Enrol press: in flight, and the daemon's last refusal.
    @State private var checklistEnrolling = false
    @State private var checklistEnrolError = ""
    @State private var filter = ""
    /// + TERMINAL's sheet, and the two fields it is filling in. View state
    /// only: an assistant started this way has no card, so there is nothing
    /// to bank and nothing to remember once the sheet closes.
    @State private var adhocSheetShown = false
    @State private var adhocRoot = ""
    @State private var adhocTool = ""
    /// The daemon's own refusal, held until the next press so the sheet can
    /// draw it verbatim.
    @State private var adhocRefusal = ""
    @State private var adhocWorking = false
    @FocusState private var filterFocused: Bool
    /// The strip's ReplyBar is a second claimant on Space. OR'd into
    /// `keys.editing` with the filter so a focused field never also unfolds.
    @State private var stripEditing = false
    /// Held so the key handler can bring a selection into view. The proxy only
    /// exists inside `ScrollViewReader`'s builder, and the handler does not.
    @State private var scroller: ScrollViewProxy?

    /// The row verbs and their arm-then-confirm gate, shared by mouse and
    /// keyboard. See `RowActions` for why this is not row state.
    @StateObject private var actions = RowActions()

    private var snapshot: Snapshot { client.snapshot }

    /// A filter field over four rows is furniture: it costs a permanent band of
    /// the panel to save a scroll that is not happening. It appears once there
    /// are enough rows for finding one to be a task — and stays, of course,
    /// while it holds text or focus.
    private static let filterFloor = 8

    /// How long a row stays in the attention section after it stops needing
    /// anyone. Long enough to cover the gap between one turn ending and the
    /// next tool call — the flap that made rows unclickable — and short enough
    /// that a section called "Needs you" is still telling the truth by the time
    /// you have read it.
    private static let attentionDwell: TimeInterval = 5

    private var totalRows: Int {
        Category.allCases.reduce(0) { $0 + rows($1).count }
    }

    /// One bucket's rows, **less Mission Control's**: its session lives in
    /// Dark Army's own checkout and would otherwise be listed as work on that
    /// project; its home is the Comm tab, which reads `snapshot.agents`
    /// directly (`CommRules.isMissionRow`, the phone's `FleetView.allRows`
    /// rule). Every bucket the table draws goes through here.
    private func rows(_ category: Category) -> [SectionRow] {
        let mission = snapshot.mission.sessionId
        return category.rows(snapshot.agents)
            .filter {
                !CommRules.isMissionRow(sessionId: $0.sessionId, originBy: $0.originBy,
                                        missionSessionId: mission)
            }
            .map { SectionRow(agent: $0, category: category) }
    }

    /// Every live row, before the filter and before any grouping. One
    /// definition, because the dwell has to be stamped from exactly the list the
    /// sections are built from.
    private var liveRows: [SectionRow] {
        Category.live.flatMap { rows($0) }
    }

    private var showsFilter: Bool {
        !filter.isEmpty || filterFocused || totalRows >= Self.filterFloor
    }

    private var showsProjectFacts: Bool {
        projectFactsVisible(tab: tab, resolvedTab: resolvedTab)
    }

    /// `now` is a live wall clock, never the snapshot's stamp: the line says
    /// how long ago the project was last active, and the fleet section it
    /// reads is only re-sent when it has news — so on a quiet machine an age
    /// taken from the applied frame would stick at whatever it said when the
    /// last thing happened. `facts.lastActive` is an absolute, minute-rounded
    /// stamp, so ageing it against the panel's own clock is the same rule
    /// `ProcessRow`'s AGE column already follows.
    private func factsLine(for name: String, now: Double) -> ProjectFactsLine {
        let rows = gridByProject[name] ?? []
        return ProjectFactsLine.compose(
            branches: rows.map(\.agent.branch),
            running: rows.count,
            facts: snapshot.agents.projectFacts.first { $0.project == name },
            now: now)
    }

    /// Live rows after the filter, still including those that belong in the
    /// band. One definition so the band, the tabs and the filter results cannot
    /// disagree about who matches.
    private var matchingLive: [SectionRow] {
        filter.isEmpty
            ? liveRows
            : liveRows.filter {
                agentMatches($0.agent, filter: filter,
                             card: client.snapshot.board.card(forSession: $0.agent.sessionId))
            }
    }

    private var needsYouCards: [BoardCard] { snapshot.board.needsYouCards() }

    /// Everything waiting on a person, in one list. A computed property over
    /// the snapshot — the same shape `attentionRows` already is, and for the
    /// same reason: nothing here is stored, so nothing can go stale.
    private var inboxItems: [InboxItem] {
        Inbox.items(rows: attentionRows,
                    prompts: snapshot.permissions,
                    cards: snapshot.board.cards,
                    answered: actions.answered,
                    // The stamp of the frame the *fleet rows* came from, not
                    // the frame's own clock. A slim frame omits an unchanged
                    // fleet, so a row's `idleSeconds` is as old as the last
                    // frame that carried one; pairing it with a fresher clock
                    // would slide `waitingSince` forward a second per second
                    // and freeze the age `InboxView` draws from it.
                    now: snapshot.agentsStamp,
                    acks: snapshot.inbox.available ? snapshot.inbox.acks : [],
                    fleet: snapshot.agents)
    }

    private var inboxGroups: [InboxGroup] { Inbox.groups(inboxItems) }

    /// The face an inbox entry wears: the fleet's own row for the session it
    /// names, resolved through the same single source `ProcessRow` reads, so
    /// one session cannot wear two faces on two surfaces. A session the
    /// fleet has forgotten has no face — never a hashed stranger.
    private func inboxFace(for item: InboxItem)
        -> (character: String, state: Cast.State)? {
        guard !item.sessionId.isEmpty,
              let (agent, bucket) = snapshot.agents.row(session: item.sessionId)
        else { return nil }
        return (Cast.character(for: agent), Cast.state(forBucket: bucket))
    }

    /// Land the reader on the surface that can answer the entry.
    ///
    /// The inbox routes and does not answer: a session entry goes to the
    /// process table, where the permission bar and the question buttons
    /// already know the burst rules, and a card entry reveals the card on the
    /// board. A target that no longer resolves is a log line and nothing else.
    private func open(item: InboxItem) {
        switch item.target {
        case .session(let id):
            guard let row = focusableRow(id) else {
                Trace.log("inbox — gone \(id.prefix(8))")
                return
            }
            show(row)
        case .card(let id):
            guard let card = client.snapshot.board.cards.first(where: { $0.id == id }) else {
                Trace.log("inbox — card gone \(id.prefix(8))")
                return
            }
            showCard(card: card)
        }
    }

    /// Rows waiting on the human, extracted rather than copied: a row drawn
    /// twice is two claimants on one selection id. Membership is read off the
    /// same snapshot as everything else — answer the prompt, dismiss the card,
    /// unblock the session, and the row is back under its project on the next
    /// pass. Hard-blocked before merely-waiting, then cards, then the rest of
    /// `waiting`; ties break on start time so the band only reorders when what
    /// it is ranking on actually changes.
    private var attentionRows: [SectionRow] {
        liveRows.filter { inAttention($0) }
            .sorted { a, b in
                let (ra, rb) = (attentionRank(a), attentionRank(b))
                if ra != rb { return ra < rb }
                let (ka, kb) = (startKey(a), startKey(b))
                return ka != kb ? ka > kb : a.agent.sessionId < b.agent.sessionId
            }
    }

    /// Project names that still have a live session, including those extracted
    /// into the band — a tab whose every agent needs you must not vanish.
    /// Name order, `Other` last. Running-count rank used to live here and is
    /// what made the strip reshuffle at tool-call cadence; urgency now lives
    /// in the band and the tab's red dot.
    private var liveProjectNames: [String] {
        Array(Set(liveRows.map(\.agent.project))).sorted(by: projectNameOrder)
    }

    /// Every live row under its project, unfiltered, `byStart` inside each.
    /// Tab badges read this count, never a second computation — waiters are
    /// listed on their tab now rather than extracted, so they count here too,
    /// and the badge equals the live rows the tab's table draws. (Abandoned
    /// rows stay out of the badge, as before.)
    private var gridByProject: [String: [SectionRow]] {
        var byProject: [String: [SectionRow]] = [:]
        for row in liveRows {
            byProject[row.agent.project, default: []].append(row)
        }
        return byProject.mapValues { byStart($0) }
    }

    private var finishedRows: [SectionRow] { rows(.finished) }

    private var abandonedRows: [SectionRow] { rows(.abandoned) }

    /// The strip is the union of everything with something left to show: a
    /// live session, a run finished inside the retention window, or a stuck
    /// job. A tombstone with an empty project lands on the existing `Other`
    /// tab; a project whose sessions have all ended keeps its tab (count 0)
    /// until the last tombstone ages out.
    private var projectTabs: [ProjectTab] {
        projectTabUnion(live: liveRows.map(\.agent.project),
                        finished: finishedRows.map(\.agent.project),
                        abandoned: abandonedRows.map(\.agent.project))
    }

    /// Live rows only — the one count rule. The footer sections under a tab
    /// carry their own counts on their headings.
    private var tabCounts: [ProjectTab: Int] {
        projectTabCounts(liveNames: liveRows.map(\.agent.project),
                         tabs: projectTabs)
    }

    /// Tabs whose project currently has a row in the band — the red dot.
    /// Read from the same `attentionRows` slice the band draws, never rebuilt.
    private var tabFlags: Set<ProjectTab> {
        Set(attentionRows.map { ProjectTab.project($0.agent.project) })
            .union(needsYouCards.map { ProjectTab.project($0.project) })
    }

    private var resolvedTab: ProjectTab? {
        fallbackTab(from: selectedTab, in: projectTabs)
    }

    /// The project tabs + card grid. Hidden by default while the band has
    /// anyone in it; a filter overrides the fold the same way it overrides
    /// a collapsed Finished section — a match you cannot see is no match.
    private var projectsVisible: Bool {
        if attentionRows.isEmpty { return true }
        if !filter.isEmpty { return true }
        return showProjects
    }


    /// Filter results below the band: matching live rows that are not already
    /// drawn above, plus matching records. A match on another tab must not be
    /// invisible — the same rule that used to override collapse.
    private var filterLiveRows: [SectionRow] {
        let needIds = Set(attentionRows.map(\.id))
        return byStart(matchingLive.filter { !needIds.contains($0.id) })
    }

    private var filterRecordRows: [SectionRow] {
        (finishedRows + abandonedRows)
            .filter {
                agentMatches($0.agent, filter: filter,
                             card: client.snapshot.board.card(forSession: $0.agent.sessionId))
            }
    }

    /// The order rows are stacked in: arrival order — the oldest session at
    /// the top, each new one appended at the bottom, nothing above it moving.
    ///
    /// The only sortable fact about an agent that does not change while you look
    /// at it. Everything else the panel knows — idleness, category, cost, context
    /// — moves on its own every few seconds, and a list ordered by a moving
    /// number is one you cannot point at: the row you were reaching for is
    /// somewhere else by the time you get there. A start time is fixed for the
    /// life of the row, so the list only changes when a session actually starts
    /// or ends — and a start is an append that shifts no row above it.
    ///
    /// The nil sentinel is inverted along with the direction: an undated row
    /// takes the *largest* key here, so rows the daemon could not date still
    /// sort to the bottom — an unknown start is not seniority. (`startKey`
    /// keeps its descending-shaped sentinel for `attentionRows`' tie-break;
    /// reused ascending it would top the list with every undated row.) The
    /// session id breaks the remaining ties, because Swift's sort makes no
    /// stability promise and two rows swapping places every tick is the bug
    /// this method exists to fix.
    private func byStart(_ rows: [SectionRow]) -> [SectionRow] {
        let key = { (row: SectionRow) -> Double in
            row.agent.startedAt ?? .greatestFiniteMagnitude
        }
        return rows.sorted { a, b in
            let (ka, kb) = (key(a), key(b))
            return ka != kb ? ka < kb : a.agent.sessionId < b.agent.sessionId
        }
    }

    private func startKey(_ row: SectionRow) -> Double {
        row.agent.startedAt ?? -.greatestFiniteMagnitude
    }

    /// Whether a section is drawn rolled up. A filter overrides the user's
    /// collapse: it is a request to see the matches, and a match hidden inside a
    /// folded section is indistinguishable from no match at all.
    private func isCollapsed(_ section: PanelSection) -> Bool {
        filter.isEmpty && collapsed.contains(section)
    }

    /// Which surface a visible row is drawn on. The walk order is the draw
    /// order — band, then whatever the content region is showing — and
    /// left/right / up-by-column only apply in the grid.
    private enum RowRegion {
        case band, grid, list
    }

    private struct VisibleItem {
        let row: SectionRow
        let region: RowRegion
        var id: String { row.id }
        var agent: Agent { row.agent }
    }

    /// Every row on screen, in the order they are drawn — which is the order the
    /// arrow keys have to walk: the process table, in arrival order with
    /// waiters in place. History draws no rows, so the arrows walk nothing.
    private var visibleItems: [VisibleItem] {
        if tab == .history || tab == .comm || tab == .reports { return [] }
        return processRows.map { VisibleItem(row: $0, region: .list) }
    }

    /// The htop table: the current tab's rows in arrival order — oldest first,
    /// a new session appends at the bottom and nothing above it moves. Waiters
    /// are **marked in place** (the red left edge and the red `wait`), never
    /// hoisted: a hoist reorders the list at turn cadence, which is the jump
    /// this table exists to prevent. Cross-tab urgency is carried by the tab's
    /// red dot, the BrandBar count and the banner — a waiter is
    /// listed only where it lives. A filter is a request to see matches, so
    /// it replaces the tab slice.
    private var processRows: [SectionRow] {
        if !filter.isEmpty {
            return byStart(matchingLive) + filterRecordRows
        }
        guard let slices = tabSlices else { return byStart(liveRows) }
        return tabTableRows(
            live: slices.live,
            finished: slices.finished,
            abandoned: slices.abandoned,
            liveOpen: !isCollapsed(.active),
            finishedOpen: !isCollapsed(.category(.finished)),
            abandonedOpen: !isCollapsed(.category(.abandoned)))
    }

    /// The selected project tab's three slices, or nil when a filter or no
    /// tab means the flat list. One computation feeding both `processRows`
    /// and the grouped rendering in `processSplit`, so the rows drawn and the
    /// rows the keyboard walks can never disagree — a collapsed footer's rows
    /// are out of both.
    private var tabSlices:
        (live: [SectionRow], finished: [SectionRow], abandoned: [SectionRow])? {
        guard filter.isEmpty, case .project(let name) = resolvedTab
        else { return nil }
        return (byStart(liveRows.filter { $0.agent.project == name }),
                finishedRows.filter { $0.agent.project == name },
                abandonedRows.filter { $0.agent.project == name })
    }

    private var visibleRows: [Agent] { visibleItems.map(\.agent) }

    /// Columns the overview grid can fit. A 200pt floor lands on two at
    /// today's 520pt rail — it was three while the rail was 680, and the
    /// board's fourth column bought that cell back as board width. The
    /// floor is two above 340pt of content so a further width change
    /// cannot collapse to one.
    private var columnCount: Int {
        let content = PanelMetrics.width - 28
        let minCol: CGFloat = 200
        let spacing: CGFloat = 8
        return max(2, Int((content + spacing) / (minCol + spacing)))
    }

    var body: some View {
        watchSelection(watchOpening(scaffold))
    }

    /// The frame, without the watchers. Split from `body` so the modifier
    /// chain still type-checks.
    private var scaffold: some View {
        VStack(spacing: 0) {
            BrandBar(client: client, board: board,
                     attention: inboxItems.count,
                     keysShown: keysLegendShown,
                     onKeys: { keysLegendShown.toggle() })
            .onChange(of: board.searchFocused) { _, focused in
                keys.editing = focused || filterFocused || stripEditing
            }
            // The card face's assistant switcher holds the keyboard: the
            // monitor stands aside so the arrows reach the row.
            .onChange(of: board.switcherFocused) { _, holder in
                keys.controlFocused = holder != nil
            }
            if keysLegendShown {
                // The same list the key monitor reads, so every key named
                // here does what it says.
                Text(TriageLegend.line)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 5)
                    .background(Theme.bar)
                    .accessibilityLabel("Keyboard shortcuts: " + TriageLegend.line)
            }
            if snapshot.board.available {
                ProjectSwitchStrip(board: board, names: snapshot.board.projectNames)
            }
            HStack(spacing: 0) {
                // The wide pane is the board, or the selected agent drawn
                // large over it (`RailLayout.leftPane`); the rail keeps the
                // compact table either way. The board is **mounted
                // unconditionally** and merely covered while the detail is
                // up: unmounting it would reset its `@State` — the applied
                // card-focus mark, the drafts sheet, the scroll proxy — so
                // every later deselect would
                // re-run the last reverse jump's reveal and overwrite the
                // search. Covered means opacity 0, no hit-testing (no click,
                // no drop) and hidden from VoiceOver; the search caret is
                // dropped by the `selected` onChange. **Never `.disabled`**:
                // `isEnabled` propagates into every presentation the board
                // owns — the plan-gate dialog and the drafts sheet — and a
                // sheet up when a banner tap opens
                // a detail would stay modal with every button, Cancel
                // included, greyed out: a wedge only Quit recovers.
                widePane
                Rectangle()
                    .fill(Theme.rule)
                    .frame(width: 1)
                rail
                    .frame(width: PanelMetrics.width)
                    .frame(maxHeight: .infinity, alignment: .top)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
    }

    private func watchOpening<V: View>(_ base: V) -> some View {
        base
        // The dwell is re-stamped from every snapshot, including the first: a
        // panel opened onto a row that is already blocked must draw it in the
        // attention section, not five seconds later.
        .onAppear {
            refreshAttentionHold()
            syncKeyFlags()
            selectTopAttentionIfNeeded()
            syncFocus()
            DictationFocus.install()
            logDictationGate()
            advanceChecklist()
        }
        .onReceive(navigationRequests, perform: receiveNavigation)
        .onChange(of: snapshot.generatedAt) { _, _ in
            // A banner can name a session this panel has not been told about yet
            // — the tap and the next snapshot race, and the tap usually wins.
            // An unapplied request simply waits for the row to exist.
            syncFocus()
            // The open's aim ran against a frozen snapshot; this is the
            // first live one, so a waiter that appeared while the panel was
            // hidden is aimed at now — unless something is already selected
            // or a card reveal is holding the board. One-shot either way,
            // and the card hold has done its job once this is decided. The
            // hold is *not* cleared on a snapshot that spends no aim: the
            // `visible` didSet publishes a held frame synchronously, so this
            // handler can run in the same flush as — and, by declaration
            // order, before — the `focus.shown` aim the hold exists for.
            if aimPendingSinceShown {
                let aim = RailLayout.aimAfterSnapshot(
                    pending: aimPendingSinceShown, selected: selected,
                    revealedForCard: revealedForCard)
                aimPendingSinceShown = false
                revealedForCard = false
                if aim { selectTopAttentionIfNeeded() }
            }
            advanceChecklist()
            refreshAttentionHold()
            let needing = Set(liveRows.filter(needsHuman).map(\.agent.id))
            let prompted = Set(liveRows.compactMap { prompt(for: $0.agent) != nil ? $0.agent.id : nil })
            actions.pruneSending(needing, prompted: prompted)
            // A confirmed Stop clears when the fleet stops calling the row live
            // — it leaves the live buckets for `finished`, or the daemon marks
            // it dead. Retire clears the same way: the record it deletes is
            // gone from the snapshot entirely.
            let live = Set(liveRows.filter { $0.agent.alive != false }.map(\.agent.id))
            actions.pruneStopping(live)
            // And a confirmed Wrap up clears on the same evidence: `/clear` ends
            // the session id and starts a new one, so the row this was pressed
            // on stops being listed. Its own 12s backstop covers a clear that
            // never lands.
            actions.pruneWrapping(live)
        }
        .onChange(of: keys.event) { _, event in
            if let event { handle(event.intent) }
        }
        .onChange(of: filterFocused) { _, focused in
            // The monitor has to know whether letters are being typed or aimed.
            keys.editing = focused || stripEditing || board.searchFocused
        }
        // `client.visible` is the SSE gate and is written by the occlusion
        // callback too — a window covered and uncovered, a card window
        // closing over it. Aiming the selection off it would reopen a detail
        // the reader had just closed. The aim rides `focus.shown`, which
        // only the stdin `show`/`toggle` path bumps, and only when the panel
        // was not already on screen; the gate keeps its one job.
        // The chatter clocks read the same flag, from the environment, so no
        // typed line or caret under this window wakes while it is hidden.
        .environment(\.agentChatterRunning, client.visible)
        .onChange(of: client.visible) { _, visible in
            if visible {
                syncFocus()
            } else {
                // Off screen: a pending open aim and a card hold are both
                // about a panel somebody is looking at.
                aimPendingSinceShown = false
                revealedForCard = false
                // A switcher focused on a panel nobody can see must not
                // leave the key monitor standing aside. The focus itself is
                // released by `AppDelegate` (`releaseSwitcherFocus`), which
                // resigns the first responder so the row's own `onChange`
                // clears this; the by-hand clear is the floor underneath.
                board.switcherFocused = nil
                keys.controlFocused = false
                // Deliberately **not** the checklist's opening: this gate
                // flips when VS Code covers the panel, which is the
                // newcomer's own next step. The opening ends on
                // `focus.hidden`, the put-away path, below.
            }
        }
        // The panel was put away (Escape, ⌘W, the status-item toggle): the
        // checklist's opening is over. A completed checklist is drawn until
        // this and then never again; one still showing keeps its place and
        // resumes on the next opening (`Opening.reset` is sticky), so a
        // session arriving while the panel is away completes it rather
        // than skipping it.
        .onChange(of: focus.hidden) { _, _ in
            onboarding.reset()
            // The floor under `releaseSwitcherFocus`, as above.
            board.switcherFocused = nil
            keys.controlFocused = false
        }
        .onChange(of: focus.shown) { _, _ in
            // The snapshot this aims against may be the one frozen at the
            // last hide; the next snapshot gets one more go.
            aimPendingSinceShown = true
            selectTopAttentionIfNeeded()
            // After, never before: a panel opened *by* a banner is opening
            // to show one row, and the default selection must not be the
            // last word on which one.
            syncFocus()
        }
        .onChange(of: focus.request) { _, _ in syncFocus() }
        .onChange(of: drilled) { _, _ in syncKeyFlags() }
        .onChange(of: tab) { _, _ in syncKeyFlags() }
        .onChange(of: filter) { _, _ in syncKeyFlags() }
        .onChange(of: historyRun) { _, _ in syncKeyFlags() }
        .onChange(of: historyDay) { _, _ in syncKeyFlags() }
        .onChange(of: client.historyRange) { _, _ in
            historyDay = ""
            historyRun = ""
            syncKeyFlags()
        }
        .onChange(of: client.historyRoot) { _, _ in
            historyRun = ""
            syncKeyFlags()
        }
        .onChange(of: client.historyClaude) { _, _ in
            historyRun = ""
        }
        .onChange(of: client.historyGrok) { _, _ in
            historyRun = ""
        }
    }

    private func watchSelection<V: View>(_ base: V) -> some View {
        base
        // A selection is a pointer into a list the daemon rewrites every few
        // seconds. When the row it names goes — finished, evicted, filtered out
        // — the highlight has to go with it, or D and S would be aimed at
        // nothing while looking aimed at something.
        // Only a *pruned* selection re-aims: a detail the reader closed on
        // purpose must not reopen on the next snapshot that reshuffles the
        // list. Opening the panel still lands on whoever needs you.
        .onChange(of: visibleRows.map(\.id)) { _, ids in
            if RailLayout.reaimAfterListChange(selected: selected, listed: Set(ids)) {
                selected = nil
                selectTopAttentionIfNeeded()
            }
        }
        // There is deliberately no onChange re-aiming the selection when a row
        // leaves attention: the row keeps its slot in the table, so the
        // highlight stays on it — following the status was the highlight-jump.
        .onChange(of: selected) { _, now in
            if stripEditing { setStripEditing(false) }
            // A new selection is where Return and Space act: a control
            // focused before it gives up its claim (one cursor, never two).
            FocusedControls.shared.clear()
            // The focus hold is about one row, and it ends the moment the
            // selection is somewhere else — by key, by click, or by the row
            // going away.
            if now != focusedRow { focusedRow = nil }
            // The board stays mounted under the detail; a caret left in its
            // search field would keep eating letters behind an opaque pane.
            if now != nil, tab == .agents, board.searchFocused {
                board.searchFocused = false
            }
            // Escape reads `detailOpen` off the router, and the selection is
            // half of that answer.
            syncKeyFlags()
        }
        .onChange(of: projectTabs) { _, tabs in
            let next = fallbackTab(from: selectedTab, in: tabs)
            if next != selectedTab {
                if selectedTab != nil {
                    Trace.log("tab fallback \(next?.title ?? "—")")
                }
                selectedTab = next
            }
            if case .project(let name) = drilled,
               !tabs.contains(.project(name)) {
                popDrill()
            }
        }
        // A prompt hidden on press stays hidden until the daemon stops listing
        // it, and is then forgotten: ids are five letters and do come round
        // again, and one remembered forever would swallow a real prompt later.
        .onChange(of: snapshot.permissions.map(\.requestId)) { _, ids in
            actions.pruneAnswered(Set(ids))
        }
    }

    // MARK: - Keyboard

    private func handle(_ intent: TriageIntent) {
        switch intent {
        case .focusFilter:
            setProjectsVisible(true)
            filterFocused = true
        case .clearFilter:
            filter = ""
            filterFocused = false
            board.query = ""
            board.searchFocused = false
        case .endEditing:
            // Only the flags. The caret is released by the monitor resigning
            // first responder on the window — the reply field's `@FocusState`
            // lives inside `ReplyBar`, and reaching down into it from here is
            // exactly the two-copies-of-one-state mistake `RowActions` records.
            setStripEditing(false)
        case .up:
            move2D(dx: 0, dy: -1)
        case .down:
            move2D(dx: 0, dy: 1)
        case .left:
            move2D(dx: -1, dy: 0)
        case .right:
            move2D(dx: 1, dy: 0)
        case .nextTab:
            cycleRailTab(1)
        case .prevTab:
            cycleRailTab(-1)
        case .toggleKeys:
            keysLegendShown.toggle()
        case .back:
            popDrill()
        case .deselect:
            deselect()
        case .closeHistoryRun:
            historyRun = ""
            syncKeyFlags()
        case .closeHistoryDay:
            historyDay = ""
            syncKeyFlags()
        case .leaveHistory:
            leaveHistory()
        case .closeReport:
            openReportPath = ""
            syncKeyFlags()
        case .leaveReports:
            leaveReports()
        case .open:
            if let item = selectedItem {
                if item.region == .band {
                    // Space unfolds the strip message. There is no in-place
                    // tile expansion.
                    toggleFullText(item.agent)
                } else if item.region == .grid {
                    // Same gesture as a click on the card: drill, then open
                    // the row. `drillTarget` keeps it aimed at the *tab*.
                    if let target = drillTarget(for: resolvedTab) {
                        openCard(item.agent, target: target)
                    }
                } else {
                    open(item.agent, scroll: scroller)
                }
            }
        case .jump:
            if let agent = selectedAgent { actions.jump(agent, client: client) }
        case .dismiss:
            if let agent = selectedAgent { actions.dismiss(agent, client: client) }
        case .stop:
            if let agent = selectedAgent { actions.stop(agent, client: client) }
        case .retire:
            if let agent = selectedAgent { actions.retire(agent, client: client) }
        case .wrapUp:
            if let agent = selectedAgent { actions.wrapUp(agent, client: client) }
        }
    }

    private var selectedItem: VisibleItem? {
        guard let selected else { return nil }
        return visibleItems.first { $0.id == selected }
    }

    private var selectedAgent: Agent? { selectedItem?.agent }

    /// Walk the drawn order. The band is a single column (up/down only);
    /// the project grid is the two-column surface. A down step that would
    /// leave the band lands on the first project card. The ends do not wrap.
    private func move2D(dx: Int, dy: Int) {
        let items = visibleItems
        guard !items.isEmpty else { return }
        // Arming is aimed at the row it was pressed on. Moving off that row
        // cancels it rather than carrying a loaded Stop somewhere else.
        actions.disarm()
        // The row the keyboard is on, or — failing that — **the row you are
        // looking at**. Opening a row with the mouse and then pressing ↑ used
        // to jump the selection to the last row in the panel, several screens
        // down, which reads as the highlight vanishing: nothing on screen is
        // marked and the thing that moved is out of sight. An unfolded row is
        // as clear a statement of where you are as a selected one.
        let anchor = selected ?? expanded
        let index: Int
        if let anchor, let i = items.firstIndex(where: { $0.id == anchor }) {
            index = i
        } else {
            let forward = dy > 0 || (dy == 0 && dx > 0)
            selected = items[forward ? 0 : items.count - 1].id
            scrollToSelected()
            return
        }
        let current = items[index]
        let next: Int
        switch current.region {
        case .band, .list:
            if dx != 0 { return }
            next = min(max(index + dy, 0), items.count - 1)
        case .grid:
            let gridStart = items.firstIndex(where: { $0.region == .grid }) ?? index
            let gridEnd = items.lastIndex(where: { $0.region == .grid }) ?? index
            let cols = max(columnCount, 1)
            if dx != 0 {
                let candidate = index + dx
                if candidate < gridStart || candidate > gridEnd { return }
                next = candidate
            } else {
                let candidate = index + dy * cols
                if candidate < gridStart {
                    next = max(0, gridStart - 1)
                } else if candidate > gridEnd {
                    next = gridEnd
                } else {
                    next = candidate
                }
            }
        }
        selected = items[next].id
        scrollToSelected()
    }

    private func scrollToSelected() {
        guard let selected, visibleItems.contains(where: { $0.id == selected })
        else { return }
        Motion.animate(.snappy(duration: 0.12), reduced: reduceMotion) {
            scroller?.scrollTo(selected, anchor: .center)
        }
    }

    /// ⌃Tab / ⌃⇧Tab: the rail's tabs — Inbox, Agents, Comm, History,
    /// Reports — in order, wrapping, exactly as pressing the tab would. A
    /// plain Tab is AppKit's since 25 Sep 2026 and walks the drawn buttons;
    /// a project tab is one of those buttons.
    private func cycleRailTab(_ step: Int) {
        let all = Tab.allCases
        guard let i = all.firstIndex(of: tab) else { return }
        let count = all.count
        let next = all[(i + step % count + count) % count]
        actions.disarm()
        if next == .history || next == .reports { selected = nil }
        tab = next
    }

    /// Fold or unfold the project shelf. The disclosure chevron is the one
    /// caller allowed to also pop a drill-in or clear a filter — those are
    /// "I am leaving this surface" gestures. Expanding an attention row
    /// only writes `showProjects`, so a search or an open project survives.
    private func setProjectsVisible(_ visible: Bool) {
        if !visible {
            if drilled != nil { popDrill() }
            if !filter.isEmpty {
                filter = ""
                filterFocused = false
            }
        }
        foldProjects(visible)
    }

    private func foldProjects(_ visible: Bool) {
        guard showProjects != visible else {
            syncKeyFlags()
            return
        }
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            showProjects = visible
        }
        syncKeyFlags()
        Trace.log("projects \(visible ? "shown" : "hidden")")
    }

    /// Escape reads these, not the view tree. Keep them in lockstep with
    /// what is actually on screen.
    private func syncKeyFlags() {
        keys.filterActive = !filter.isEmpty
        keys.drilledIn = tab == .agents && drilled != nil && projectsVisible
        keys.detailOpen = tab == .agents && selected != nil
        keys.historyRun = tab == .history && !historyRun.isEmpty
        keys.historyDay = tab == .history && !historyDay.isEmpty
        keys.historyOpen = tab == .history
        keys.reportOpen = tab == .reports && !openReportPath.isEmpty
        keys.reportsOpen = tab == .reports
    }

    /// Leave History the way the Board control does: the board comes back,
    /// and nothing is remembered about a run or a day. An Agents selection
    /// from before is already gone, so this is the board and not a detail.
    private func leaveHistory() {
        historyRun = ""
        historyDay = ""
        historyPerson = ""
        selected = nil
        tab = .inbox
        syncKeyFlags()
    }

    /// Leave the Reports tab the way History is left: the board comes back
    /// and the open report closes. The search and the list are kept, so a
    /// return finds them where they were.
    private func leaveReports() {
        openReportPath = ""
        reportsHits = ScoutReportIndex()
        reportsSearching = false
        selected = nil
        tab = .inbox
        syncKeyFlags()
    }

    /// The key that reloads the report list: the tab shown, the board's set
    /// of attached reports (a scout attaching one moves it) and Refresh.
    private var reportsLoadKey: String {
        let paths = Set(client.snapshot.board.cards.map(\.reportPath)
            .filter { !$0.isEmpty })
        return "\(reportsReload)\u{1}" + paths.sorted().joined(separator: "\u{1}")
    }

    /// The board, covered by History or a detail and narrowed on Comm.
    /// Split out of `body` so that expression still type-checks.
    private var widePane: some View {
        ZStack {
            BoardView(client: client, state: board,
                      cardFocus: cardFocus,
                      onStartProject: startProjectPress,
                      onSetLimit: { root, rung in
                          Panel.send(action: "set_board_parallel_root",
                                     value: ["root": root, "limit": rung])
                      },
                      onSetIsolation: { root, on in
                          Panel.send(action: "set_board_isolation_root",
                                     value: ["root": root, "enabled": on])
                      })
                // Compared by identity, so a client frame that rebuilds this
                // view does not re-run the board (`BoardView`'s `==`).
                .equatable()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                // On Comm the board keeps its structural place and
                // merely narrows: padding, never an `HStack` move,
                // or its `@State` resets.
                .padding(.trailing, tab == .comm ? PanelMetrics.commTerminalWidth : 0)
                .opacity(boardCovered ? 0 : 1)
                .allowsHitTesting(!boardCovered)
                .accessibilityHidden(boardCovered)
                .cursorAffordances(!boardCovered)
                // Out of the focus chain too, and claiming nothing: Return
                // and Space must never press a verb on a covered board.
                .environment(\.keyboardClaimsSuppressed, boardCovered)
            if tab == .comm {
                MissionTerminalColumn(
                    client: client,
                    onInputFocus: { setStripEditing($0) },
                    onTerminalFocus: { keys.terminalFocused = $0 })
                .frame(width: PanelMetrics.commTerminalWidth)
                .frame(maxWidth: .infinity, maxHeight: .infinity,
                       alignment: .trailing)
            }
            if case .history = workspacePane {
                LedgerPane(client: client, lens: $historyLens,
                           person: $historyPerson, day: $historyDay,
                           run: $historyRun)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            if case .reports = workspacePane {
                ScoutReportsPane(client: client, index: $reportsIndex,
                                 detail: $reportsDetail,
                                 search: $reportsSearch,
                                 project: $reportsProject,
                                 openPath: $openReportPath,
                                 hits: $reportsHits,
                                 searching: $reportsSearching,
                                 loadKey: reportsLoadKey)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    // Here rather than on `watchOpening`'s chain, which is
                    // at the type checker's limit: Escape's report rung
                    // follows the open report.
                    .onChange(of: openReportPath) { _, _ in syncKeyFlags() }
            }
            if let row = detailRow {
                let bound = client.snapshot.board
                    .card(forSession: row.agent.sessionId)
                AgentDetailPane(
                    agent: row.agent,
                    category: row.category,
                    card: card(for: row.agent),
                    prompt: prompt(for: row.agent),
                    actions: actions,
                    client: client,
                    onJump: { actions.jump(row.agent, client: client) },
                    onShowCard: bound == nil ? nil
                        : { showCard(session: row.agent.sessionId) },
                    boardCard: bound,
                    onBack: { deselect() },
                    collaborationHelper: collaborationHelper,
                    onCollaborationSession: { provider, sid, helper in
                        showCollaboration(provider: provider, session: sid, helper: helper)
                    },
                    onCollaborationCard: { showCard(card: $0) },
                    onReplyFocus: setStripEditing,
                    onInputFocus: setStripEditing,
                    onTerminalFocus: { keys.terminalFocused = $0 })
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    /// The one place the wide pane is decided. History covers the board
    /// the way a selected agent's detail does; Comm only narrows it.
    private var workspacePane: RailLayout.LeftPane {
        RailLayout.leftPane(tab: tab, selected: selected)
    }

    private var boardCovered: Bool {
        switch workspacePane {
        case .detail, .history: return true
        case .reports: return true
        case .board, .mission: return false
        }
    }

    /// The row the wide pane draws large, or `nil` for the board: the
    /// `workspacePane` answer resolved against the rows actually
    /// listed, so a selection the snapshot no longer carries falls back to
    /// the board rather than an empty pane.
    private var detailRow: SectionRow? {
        guard case .detail(let id) = workspacePane else { return nil }
        return processRows.first { $0.id == id }
    }

    /// Close the selected agent's detail and bring the board back — the one
    /// thing a second click on the row, Escape and `‹ Board` all do.
    private func deselect() {
        actions.disarm()
        selected = nil
        expanded = nil
        focusedRow = nil
        // A person closed the detail: nothing may reopen it by itself, and
        // a card hold set *before* this press has been acted on.
        aimPendingSinceShown = false
        revealedForCard = false
    }

    private func selectTab(_ tab: ProjectTab) {
        // Against what is *drawn*, not what is stored: `selectedTab` starts
        // nil while `resolvedTab` falls back to the first tab, so a first
        // click on the already-highlighted tab would otherwise pass this
        // test and throw away the detail the person is reading.
        if resolvedTab != tab {
            Trace.log("tab \(tab.title)")
            // A press on a project is not a prune. Without this, the
            // `visibleRows` change the tab swap causes looks to
            // `reaimAfterListChange` like the read row vanishing, and
            // `selectTopAttentionIfNeeded()` would open a stranger's detail
            // over the board. Asked for a project, land on that project.
            //
            // The banner's own route (`show(_ row:)`) assigns `selectedTab`
            // directly and must never come through here: it sets `selected`
            // immediately after, and this would eat the row it asked for.
            deselect()
        }
        selectedTab = tab
        if drilled != nil { popDrill() }
    }

    private func drill(_ target: DrillTarget, selecting id: String? = nil) {
        switch target {
        case .project(let name):
            Trace.log("drill \(name.isEmpty ? "Other" : name)")
        }
        if let id { selected = id }
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            drilled = target
        }
        // Same block as the write — a one-event lag would make Escape hide
        // the panel instead of popping the drill-in.
        syncKeyFlags()
    }

    /// Which list a grid card belongs to — the *tab* the card is drawn on.
    private func drillTarget(for tab: ProjectTab?) -> DrillTarget? {
        switch tab {
        case .project(let name): return .project(name)
        case nil: return nil
        }
    }

    /// A click on a grid card is the whole gesture, not half of one: it drills
    /// into the list the card belongs to and opens that row. The card used to
    /// only take the selection — it lit a border and did nothing else, so
    /// reaching the row a card stands for cost a click on *Open project* and
    /// then a second one on the row, with the card's own click a no-op in
    /// between. The jump chip is untouched by this: it is a `Button` inside
    /// the card, so AppKit gives it the click and the card's tap never fires,
    /// and *Open project* still drills without opening anything.
    private func openCard(_ agent: Agent, target: DrillTarget) {
        actions.disarm()
        Trace.log("card open \(agent.nickname)")
        drill(target, selecting: agent.id)
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            expanded = agent.id
            // Same fresh-ascent reset as `open`: the row must not arrive in
            // the drill-in believing it is on its way down.
            folding.remove(agent.id)
            fullText.remove(agent.id)
        }
        // The drill-in is a different subtree and brings its own proxy, so the
        // scroll is booked for after the swap rather than taken against the
        // grid that is on its way out.
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(220))
            guard expanded == agent.id else { return }
            Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
                scroller?.scrollTo(agent.id, anchor: .bottom)
            }
        }
    }

    private func popDrill() {
        guard drilled != nil else { return }
        Trace.log("drill pop")
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            drilled = nil
        }
        syncKeyFlags()
    }

    private var filterField: some View {
        HStack(spacing: 6) {
            Image(systemName: "line.3.horizontal.decrease")
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
            TextField("Filter by name, project, branch…", text: $filter)
                .textFieldStyle(.plain)
                .font(.system(size: 11.5))
                .focused($filterFocused)
                // Return belongs to the selected row, not to the field: the
                // whole point of typing here is to reach a row and act on it.
                .onSubmit { if let agent = selectedAgent { actions.jump(agent, client: client) } }
            if !filter.isEmpty {
                Button {
                    filter = ""
                    filterFocused = false
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("Clear the filter")
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6))
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
    }

    private var noMatches: some View {
        VStack(spacing: 4) {
            Text("No agent matches “\(filter)”")
                .font(.callout).foregroundStyle(.secondary)
            Text("\(totalRows) hidden")
                .font(.caption).foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 26)
    }


    private func logDictationGate() {
        let s = client.context.settings
        DictationFocus.logGate(
            whisper: s.macwhisperInstalled,
            trusted: s.accessibilityTrusted,
            label: s.dictationShortcutLabel)
    }

    /// First attention row **the current surface actually draws**, when none
    /// of those is already selected. On open this aims the highlight at what
    /// needs a human; it must never select a row `visibleItems` does not hold,
    /// because the `visibleRows` onChange nils exactly such a selection and
    /// the two would ping-pong. Raw `attentionRows` spans every tab and is
    /// deliberately not consulted here.
    private func selectTopAttentionIfNeeded() {
        // A card was just revealed on the board, and the board is what the
        // reader asked for: the top waiter's detail must not cover it. Read,
        // not consumed — the same hold has to survive to the snapshot aim
        // (`RailLayout.aimAfterSnapshot`), which is where it is spent.
        if revealedForCard { return }
        let drawn = visibleItems.filter { inAttention($0.row) }
        guard !drawn.isEmpty else { return }
        if let selected, drawn.contains(where: { $0.id == selected }) {
            return
        }
        // A row we were *asked* to show keeps the selection even when it does
        // not need anyone. Without this, opening a hidden panel on a running
        // agent handed the highlight straight back to the top waiter — the
        // answer to "take me to this one" would be a different one.
        if let selected, selected == focusedRow { return }
        selected = drawn.first?.id
    }

    // MARK: - Focus

    /// Apply an outstanding focus request, if there is one and the row exists.
    ///
    /// Idempotent by sequence number, so the three deliverers — the request
    /// landing, the panel becoming visible, the next snapshot — can all call it.
    /// A request naming a session the snapshot does not carry is *kept*: the tap
    /// on a banner routinely beats the snapshot that would have listed the row.
    private func syncFocus() {
        guard let request = focus.request, request.seq != appliedFocus else { return }
        guard client.visible else { return }
        // The editor's Dark Army button sends `focus` *and* `cardFocus`.
        // Opening the agent here used to cover the board, so the card
        // reveal landed underneath `‹ Board` and looked like a miss.
        let wantsCard = cardFocus.request?.sessionId == request.sessionId
        let outcome = CardReveal.outcome(session: request.sessionId,
                                         board: client.snapshot.board)
        if CardReveal.holdFocus(wantsCard: wantsCard, outcome: outcome) {
            return
        }
        if CardReveal.openBoard(wantsCard: wantsCard, outcome: outcome),
           case .reveal(let card) = outcome {
            appliedFocus = request.seq
            showCard(card: card)
            return
        }
        guard let row = focusableRow(request.sessionId) else { return }
        appliedFocus = request.seq
        show(row)
    }

    /// Every row a focus request could name — live, finished or abandoned.
    private func focusableRow(_ sessionId: String) -> SectionRow? {
        (liveRows + finishedRows + abandonedRows)
            .first { $0.agent.sessionId == sessionId }
    }

    /// Put one row in front of the reader, opened, wherever it happens to live.
    ///
    /// One destination: the process table. Every row — waiter, tombstone or
    /// stuck job — lives on its own project's tab now, so the old band branch
    /// is gone with the hoist (it aimed `bandScroller`, a proxy the undrawn
    /// band never set, so focusing a waiter scrolled nothing). The row's
    /// project tab is selected — a finished or abandoned row's footer is
    /// unrolled in the same breath, because a selection aimed into a
    /// rolled-up footer is a highlight on a row that is not drawn — the row
    /// is selected and expanded, its message is unfolded (the difference
    /// between landing on the row and landing on what it said) and the table
    /// scrolls to it.
    @State private var collaborationHelper = ""
    private func showCollaboration(provider: String, session sid: String, helper: String) {
                                let fleet = client.snapshot.agents
                                let groups: [(Category, [Agent])] = [(.running, fleet.running),
                                    (.waiting, fleet.waiting), (.sleeping, fleet.sleeping),
                                    (.finished, fleet.finished), (.abandoned, fleet.abandoned)]
                                let targets = groups.flatMap { category, agents in
                                    agents.filter { $0.sessionId == sid && $0.provider == provider }
                                        .map { SectionRow(agent: $0, category: category) }
                                }
                                if targets.count == 1 {
                                    show(targets[0])
                                    collaborationHelper = helper
                                }
    }


    private func show(_ row: SectionRow) {
        collaborationHelper = ""
        let agent = row.agent
        Trace.log("focus \(agent.nickname)")
        actions.disarm()
        // A request for a row outranks a card hold. The reverse jump
        // never comes through here: `syncFocus` calls `showCard` when
        // the editor asked for the card, so the board stays in front.
        revealedForCard = false
        tab = .agents
        // A filter is a view of the fleet, and the row asked for may not be in
        // it. Clearing it is the same call the disclosure makes when it folds.
        if !filter.isEmpty {
            filter = ""
            filterFocused = false
        }
        focusedRow = agent.id
        foldProjects(true)
        selectedTab = .project(agent.project)
        selected = agent.id
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            if row.category == .finished || row.category == .abandoned {
                collapsed.remove(.category(row.category))
            } else {
                collapsed.remove(.active)
            }
            expanded = agent.id
            // The ladder's fresh-ascent reset is right for a click and wrong
            // here, where the whole point is to arrive already reading.
            folding.remove(agent.id)
            _ = fullText.insert(agent.id)
        }
        // The tab swap rebuilds the table subtree and brings its own proxy,
        // so the scroll is booked for after the swap — the delayed shape
        // `openCard` uses — rather than taken against the list on its way out.
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(220))
            guard selected == agent.id else { return }
            Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
                scroller?.scrollTo(agent.id, anchor: .center)
            }
        }
        syncKeyFlags()
    }


    private func setStripEditing(_ focused: Bool) {
        stripEditing = focused
        keys.editing = focused || filterFocused || board.searchFocused
    }

    /// Which rung a click on a row's body climbs.
    ///
    /// Up: open the row, then unfold its message. Down, one rung per click in
    /// the order they were opened: fold the message, then close the row. The
    /// middle rung — open, message folded — reads the same going up and coming
    /// down, so the direction comes from `folding`, which is what the previous
    /// click left behind.
    ///
    /// **It lives here rather than in the row** because every term in it is
    /// `PanelView` state, and a row is a value type SwiftUI may hand a click to
    /// while holding a copy built several snapshots ago. Read from the row, the
    /// ladder decided from that copy; read from here, it decides from the state
    /// the panel is actually in. That is the difference between a second click
    /// unfolding the text and a second click shutting the row it never saw open.
    private func tap(_ agent: Agent,
                     showsMessage: Bool,
                     hasHiddenText: Bool,
                     scroll: ScrollViewProxy) {
        let isOpen = expanded == agent.id
        let isFull = fullText.contains(agent.id)
        Trace.log("ladder \(agent.nickname) open=\(isOpen) msg=\(showsMessage)"
                  + " full=\(isFull) cut=\(hasHiddenText)"
                  + " folding=\(folding.contains(agent.id))")
        if isOpen, showsMessage, isFull {
            toggleFullText(agent)
        } else if isOpen, showsMessage, hasHiddenText, !folding.contains(agent.id) {
            toggleFullText(agent)
        } else {
            open(agent, scroll: scroll)
        }
    }

    /// Expand a row — and bring what it just grew into view.
    ///
    /// The detail is roughly a row and a half tall and opens *below* the row, so
    /// on a short panel it used to unfold past the bottom edge: the row you
    /// clicked stayed put, nothing visibly happened, and the panel read as
    /// "the bottom rows are not clickable". Anchored `.bottom` because it is
    /// the newly revealed foot that has to land on screen, not the row; the
    /// scroll is a no-op when the detail already fits. Growing the window to
    /// the new ideal height is what actually makes room; this only scrolls
    /// if the list is already at its cap.
    ///
    /// Twice, deliberately. The first call runs against the layout *before* the
    /// detail exists, so it can only scroll to where the row is now; the second
    /// runs after the expansion animation has given the row its full height.
    private func open(_ agent: Agent, scroll: ScrollViewProxy?) {
        let opening = expanded != agent.id
        Trace.log("open \(agent.nickname) → \(opening ? "expanded" : "closed")")
        // A click is also an aim: the keyboard and the mouse have to agree about
        // which row is current, or ↓ after a click would resume from wherever
        // the keyboard was last, which is somewhere the user is not looking.
        selected = agent.id
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            expanded = opening ? agent.id : nil
            // An open is the foot of a fresh ascent, so it clears any memo of
            // the last descent — otherwise a row closed and reopened would
            // arrive already believing it was on its way down.
            folding.remove(agent.id)
            if opening { scroll?.scrollTo(agent.id, anchor: .bottom) }
            // Folding the row folds everything it grew. The row is a ladder now
            // — detail, then message, then closed — and a close that left the
            // message unfolded would make the next click on the same row skip a
            // rung and land somewhere the last click did not leave it.
            else { fullText.remove(agent.id) }
        }
        guard opening else { return }
        Task {
            try? await Task.sleep(for: .milliseconds(200))
            guard expanded == agent.id else { return }
            Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
                scroll?.scrollTo(agent.id, anchor: .bottom)
            }
        }
    }

    /// Unfold or refold one row's last words. Independent of `expanded`: the
    /// detail and the message are two different questions about the same row.
    ///
    /// It also records which way the row is now travelling, whichever control
    /// called it — the chevron and the row-sized target have to leave the ladder
    /// in the same state, or hitting the chevron to fold the message would arm
    /// the *up* rung for the click that follows.
    private func toggleFullText(_ agent: Agent) {
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            if fullText.contains(agent.id) {
                fullText.remove(agent.id)
                folding.insert(agent.id)
            } else {
                fullText.insert(agent.id)
                folding.remove(agent.id)
            }
        }
    }

    /// Roll a section up or down. Collapsing the section an expanded row lives in
    /// closes that row too — otherwise its detail would be the only thing left of
    /// a section that is supposed to be hidden, and reopening the section would
    /// find it still unfolded.
    private func toggle(_ section: PanelSection, rows: [SectionRow]) {
        // Belt to the header's braces: a non-collapsible header draws no
        // chevron and sends no toggle, but a second entry point (a future
        // keyboard binding, say) must not be able to fold it either.
        guard section.isCollapsible else { return }
        Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) {
            if collapsed.contains(section) {
                collapsed.remove(section)
            } else {
                collapsed.insert(section)
                if let open = expanded, rows.contains(where: { $0.id == open }) {
                    expanded = nil
                }
                if let sel = selected, rows.contains(where: { $0.id == sel }) {
                    selected = nil
                }
            }
        }
    }

    private var allEmpty: Bool {
        Category.allCases.allSatisfy { rows($0).isEmpty }
    }

    /// A card belongs to the row of the session that raised it — the message is
    /// the reason that session is in the section it is in, so repeating it in a
    /// separate list would say the same thing twice.
    private func card(for agent: Agent) -> Notification? {
        snapshot.notifications.first { $0.sessionId == agent.sessionId }
    }


    /// The tool call this agent is blocked on, if Dark Army's channel relayed one.
    /// At most one is drawn: a session can only be stopped at one prompt at a
    /// time, and stacking two on a row would say otherwise.
    private func prompt(for agent: Agent) -> PermissionPrompt? {
        guard !agent.sessionId.isEmpty else { return nil }
        return snapshot.permissions.first {
            $0.sessionId == agent.sessionId && !actions.answered.contains($0.requestId)
        }
    }

    /// Whether a row belongs in the pinned attention section: blocked in the
    /// `waiting` bucket, holding a notification card, or stopped on a
    /// permission prompt. The three reads that define it are the same ones the
    /// row itself renders from, so the section and the row's own furniture
    /// cannot disagree — and because `prompt(for:)` already excludes answered
    /// prompts, clicking Allow drops the row back to its project on the very
    /// next recompute, before the daemon has even confirmed.
    private func needsHuman(_ row: SectionRow) -> Bool {
        row.category == .waiting
            || card(for: row.agent) != nil
            || prompt(for: row.agent) != nil
    }

    /// Where the row is *drawn*, which is `needsHuman` plus a short dwell.
    ///
    /// `needsHuman` is a pure function of the snapshot and it flaps, because the
    /// two things it reads flap: every `Stop`/`Notification` hook raises the
    /// stock wait card and the next tool event dismisses it again, so a session
    /// that ends a turn and resumes — an orchestrator with live subagents, a row
    /// at idle 0s — crosses between `waiting` and `running` at turn cadence. The
    /// attention section and the project sections are two different `ForEach`
    /// subtrees, so each crossing *destroys the row's view here and builds a new
    /// one there*, and a click whose mouse-down and mouse-up straddle a push is
    /// silently cancelled: measured, three full pushes inside 34ms, and rows
    /// that could not be opened at all until the flap stopped.
    ///
    /// So the row leaves late and arrives on time. The dwell is only ever a
    /// delay on *leaving*: nothing that needs a human is held back for a
    /// moment, which is the one property this section exists to guarantee. What
    /// it costs is a row that says "needs you" for a few seconds after it went
    /// back to work — and that was already the picture, since a flapping row
    /// spent half its time drawn there anyway.
    private func inAttention(_ row: SectionRow) -> Bool {
        needsHuman(row) || attentionHold.contains(row.agent.id)
    }

    /// Order inside the attention section. See the call site for the argument.
    private func attentionRank(_ row: SectionRow) -> Int {
        if prompt(for: row.agent) != nil { return 0 }
        if card(for: row.agent) != nil { return 1 }
        if row.category == .waiting { return 2 }
        // Held by the dwell alone: it asked for nothing on this snapshot, so it
        // sorts under everything that did, and is the first to go.
        return 3
    }

    /// Re-stamp the dwell from the snapshot that has just arrived.
    ///
    /// Driven by the push rather than by a timer: a repeating timer would
    /// re-render the whole list every second, which is the cost this fix exists
    /// to reduce. The one-shot below is the backstop for the case where the
    /// pushes stop while a row is still held — otherwise it would sit in the
    /// attention section until the daemon next said anything at all.
    private func refreshAttentionHold() {
        let now = Date()
        let rows = liveRows
        let liveIds = Set(rows.map(\.agent.id))
        var seen = stamps.seen.filter { liveIds.contains($0.key) }
        for row in rows where needsHuman(row) {
            seen[row.agent.id] = now
        }
        for id in actions.skipDwell {
            seen.removeValue(forKey: id)
        }
        for (id, at) in seen where now.timeIntervalSince(at) >= Self.attentionDwell {
            seen.removeValue(forKey: id)
        }
        stamps.seen = seen
        let next = Set(seen.keys)
        if next != attentionHold {
            Motion.animate(.snappy(duration: 0.18), reduced: reduceMotion) { attentionHold = next }
        }
        // Outside the equality check, and that is the whole point of the
        // backstop. Armed only on ticks where the held set *changed*, it was
        // never armed on the one case it exists for: a row arrives in the hold,
        // the next snapshot re-stamps it without changing the set, and then the
        // pushes stop (daemon restart, panel hidden, stream dropped). With
        // nothing scheduled and nothing arriving, the row sat in "Needs you"
        // indefinitely — the failure this section is supposed to prevent.
        scheduleAttentionSweep()
    }

    /// One pending sweep at a time, fired just past the dwell. Cheap, and it
    /// only ever runs while something is actually being held.
    private func scheduleAttentionSweep() {
        guard !attentionHold.isEmpty, !sweepScheduled else { return }
        sweepScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.attentionDwell + 0.1) {
            sweepScheduled = false
            refreshAttentionHold()
        }
    }

    // MARK: - First-run checklist

    /// Feed the current snapshot to the opening's state machine and persist
    /// completion the moment it is decided. Every caller is on the main
    /// actor; the write is one merge into `panel-position.json`.
    private func advanceChecklist() {
        if onboarding.advance(snapshot: snapshot, completed: checklistCompleted,
                              ownTerminal: client.context.settings.boardOwnTerminal),
           !checklistCompleted {
            checklistCompleted = true
            PanelPlacement.saveChecklistCompleted(true)
        }
    }

    /// The checklist, above the tab content, absent when the opening decided
    /// against it.
    @ViewBuilder
    private var checklistBand: some View {
        if onboarding.placement != .none {
            let settings = client.context.settings
            FirstRunChecklistView(
                progress: FirstRunChecklist.progress(
                    root: onboarding.selectedRoot, enrollment: snapshot.enrollment,
                    ownTerminal: settings.boardOwnTerminal),
                completed: onboarding.placement == .completed,
                folders: FirstRunChecklist.candidates(enrollment: snapshot.enrollment),
                selectedRoot: onboarding.selectedRoot,
                launchLine: FirstRunChecklist.launchLine(settings.launch),
                notificationsDenied: FirstRunChecklist.notificationsDenied(
                    settings.notificationStatus),
                enrolling: checklistEnrolling,
                enrolError: checklistEnrolError,
                onChooseRoot: { root in
                    onboarding.choose(root: root)
                    advanceChecklist()
                },
                onEnrol: enrolFromChecklist,
                onNotificationSettings: {
                    Panel.send(action: "open_notification_settings")
                },
                onFirstCard: openFirstCard)
        }
    }

    /// "Setup complete"'s `firstCardAction`: the board's own new-card path
    /// (`BoardState.openComposer`, what `+ NEW CARD` calls). Filed under the
    /// folder the checklist followed — not the busiest live project, which
    /// on a developer's Mac is Dark Army's own checkout — but only where the
    /// board offers that folder (`FirstRunChecklist.firstCardFiling`);
    /// otherwise blank. The composer is its own window, so it opens from any
    /// tab; the detail is closed so the board stands behind it. Nothing is
    /// sent to the daemon by the press itself.
    private func openFirstCard() {
        deselect()
        let filing = FirstRunChecklist.firstCardFiling(
            selectedRoot: onboarding.selectedRoot,
            boardProjects: snapshot.board.projects)
        board.openComposer(defaultProject: filing.project, defaultRoot: filing.root,
                           offeredTools: snapshot.board.tools)
    }

    /// Step 1's button: the same chooser the settings window uses, with the
    /// answer shown here. A cancel is inert; a refusal is drawn in the
    /// daemon's words; a success ticks nothing itself — the next snapshot
    /// lists the folder, and that is the tick.
    private func enrolFromChecklist() {
        guard !checklistEnrolling else { return }
        checklistEnrolError = ""
        checklistEnrolling = true
        SettingsActions.enrolChosenFolder(client: client) { result in
            checklistEnrolling = false
            guard let result else { return }
            checklistEnrolError = result.ok ? "" : result.detail
            if result.ok {
                Task { await client.refresh() }
            }
        }
    }

    // MARK: - Regions


    private var projectTabStrip: some View {
        ProjectTabStrip(
            tabs: projectTabs,
            selected: resolvedTab,
            counts: tabCounts,
            flagged: tabFlags,
            enabled: filter.isEmpty,
            onSelect: selectTab)
    }

    /// The fleet, as a right rail. BrandBar and the board sit outside this
    /// stack so a second identity row does not appear above the list.
    ///
    /// Token chips stay in `header` (`SidebarUsageBand`) — the htop table
    /// does not replace them.
    private var rail: some View {
        VStack(spacing: 0) {
            header
            VStack(spacing: 0) {
                tabs
                if tab == .agents { projectTabStrip }
                if tab == .agents, offersAdhocTerminal {
                    AdhocTerminalButton {
                        adhocRefusal = ""
                        adhocSheetShown = true
                    }
                }
                if showsProjectFacts, case .project(let name) = resolvedTab {
                    // Half a minute is the resolution of the figure it draws
                    // (`lastActive` is rounded to the minute on the way in),
                    // and it keeps counting while the fleet section is quiet.
                    TimelineView(.periodic(from: .now, by: 30)) { context in
                        ProjectFactsBar(
                            line: factsLine(
                                for: name,
                                now: context.date.timeIntervalSince1970))
                    }
                }
                if tab == .agents, showsFilter { filterField }
                Divider()
            }

            // The first-run checklist, above whatever the tab shows — so the
            // default Inbox opening carries it and nothing below is hidden
            // or unmounted by it.
            checklistBand

            if tab == .agents { enrolmentBanner }

            if tab == .inbox {
                InboxView(client: client, board: board,
                          groups: inboxGroups, onOpen: open(item:),
                          faceFor: inboxFace(for:))
                    .frame(maxHeight: .infinity)
            } else if tab == .comm {
                CommRail(client: client, onEditing: setStripEditing)
                    .frame(maxHeight: .infinity)
            } else if tab == .history {
                HistoryView(client: client, lens: $historyLens,
                            person: $historyPerson, day: $historyDay,
                            run: $historyRun, onBoard: leaveHistory)
                    .frame(maxHeight: .infinity)
            } else if tab == .reports {
                ScoutReportsRail(index: reportsIndex,
                                 hits: reportsHits,
                                 searching: reportsSearching,
                                 search: $reportsSearch,
                                 project: $reportsProject,
                                 onEditing: setStripEditing,
                                 onRefresh: { reportsReload += 1 },
                                 onBoard: leaveReports)
                    .frame(maxHeight: .infinity)
            } else if allEmpty {
                empty
            } else if processRows.isEmpty, !filter.isEmpty {
                noMatches
            } else {
                processSplit
            }
        }
        .background(Theme.bg)
        // A sheet on the panel window, `DraftsSheet`'s pattern. The rail
        // is deliberately **not** `.disabled` while it is up: `isEnabled`
        // propagates into the sheet and would grey out its own Cancel.
        .sheet(isPresented: $adhocSheetShown) {
            AdhocTerminalSheet(
                projects: snapshot.enrollment.enrolled,
                tools: snapshot.board.tools,
                installed: snapshot.board.installed,
                root: $adhocRoot, tool: $adhocTool,
                refusal: adhocRefusal, working: adhocWorking,
                start: spawnAdhocTerminal)
        }
    }

    /// Whether + TERMINAL is drawn. One read of `AdhocSpawn.shouldOffer`, so
    /// the view decides nothing the enum does not.
    private var offersAdhocTerminal: Bool {
        AdhocSpawn.shouldOffer(
            dispatchEnabled: snapshot.board.dispatchEnabled,
            enrollmentAvailable: snapshot.enrollment.available,
            enrolled: snapshot.enrollment.enrolled,
            tools: snapshot.board.tools)
    }

    /// Ask the daemon for a terminal with no card behind it. The sheet stays
    /// up on a refusal, holding the daemon's own sentence; a success closes
    /// it and the session arrives in the fleet like any other.
    private func spawnAdhocTerminal() {
        guard AdhocSpawn.canStart(root: adhocRoot, tool: adhocTool),
              !adhocWorking else { return }
        adhocWorking = true
        adhocRefusal = ""
        Task { @MainActor in
            let result = await client.spawnTerminal(root: adhocRoot,
                                                    tool: adhocTool)
            adhocWorking = false
            if result.ok {
                adhocSheetShown = false
                await client.refresh()
            } else {
                adhocRefusal = result.detail
            }
        }
    }

    private var navigationRequests: AnyPublisher<Foundation.Notification, Never> {
        NotificationCenter.default.publisher(for: .panelShowSession)
            .merge(with: NotificationCenter.default.publisher(for: .panelShowCard))
            .eraseToAnyPublisher()
    }

    private func receiveNavigation(_ note: Foundation.Notification) {
        if note.name == .panelShowCard {
            guard let card = note.object as? BoardCard else { return }
            showCard(card: card)
        } else {
            guard let sid = note.object as? String else { return }
            if let provider = note.userInfo?["provider"] as? String {
                showCollaboration(provider: provider, session: sid,
                                  helper: note.userInfo?["helper"] as? String ?? "")
            } else if let row = focusableRow(sid) {
                show(row)
            }
        }
    }

    /// Bring the board up with this card as the visible result.
    ///
    /// Re-resolved by id against the current snapshot rather than captured at
    /// draw time: a card can be deleted or finish between the frame that drew
    /// the control and the press. A miss is a logged no-op, never a stale
    /// reveal. The drop-is-dispatch re-check, applied to a reveal.
    ///
    /// The phrase is an ordinary search, not a hidden filter mode — the field
    /// stays editable and its × clears it — so the board's search focus flag is
    /// deliberately untouched: the press aims the board, it does not take the
    /// keyboard. It is `CardToken`'s id phrase rather than the card's title:
    /// a title matches siblings that share words and an untitled card matches
    /// nothing at all, where the id names this card and only this card — the
    /// same phrase the editor's own jump writes.
    private func showCard(card: BoardCard) {
        let current = client.snapshot.board.cards.filter { $0.id == card.id && $0.root == card.root }
        guard current.count == 1, let live = current.first else {
            Trace.log("show card — gone \(card.id.prefix(8))")
            return
        }
        // The board does the narrowing, the scroll and the glow in one
        // place (`BoardView.reveal`): widening the project filter, writing
        // the `#card` query and scrolling the card into view. Writing the
        // query from here and leaving the scroll to nobody is what put a
        // reader on a board narrowed to one card that sat two columns to
        // the right of the pane's edge, reading as empty.
        board.requestReveal(live.id)
        // The card lives on the board, and the board is behind the selected
        // agent's detail while a row is selected: close it so the reveal has
        // a board to land on.
        if selected != nil {
            deselect()
            Trace.log("show card — detail closed")
        }
        // Hold the board through the open's aim: `.panelOpenBoard` on an
        // occluded panel bumps `focus.shown`, whose aim would otherwise put
        // the top waiter's detail straight over this card.
        revealedForCard = true
        // `show(nil, nil)` while the panel is already visible fronts it
        // and does not re-place — a card reveal must not yank a window
        // that is sitting on another display onto main.
        NotificationCenter.default.post(name: .panelOpenBoard, object: nil)
        Trace.log("show card \(live.id)")
    }

    /// The detail header's `card ⌗` chip's path: resolve the session, then the card.
    private func showCard(session: String) {
        guard let card = client.snapshot.board.card(forSession: session) else {
            Trace.log("show card — none for \(session.prefix(8))")
            return
        }
        showCard(card: card)
    }

    /// Start every startable card in one project, and keep the daemon's own
    /// report against that folder. **Nil is how the press stays absent** —
    /// launching switched off or a daemon without the verb — because a
    /// button present and inert is a promise this app cannot keep.
    private var startProjectPress: ((String) -> Void)? {
        guard client.snapshot.board.dispatchEnabled else { return nil }
        return { root in
            Task { @MainActor in
                let result = await client.boardStartProject(root: root)
                // Whether or not it succeeded: the report *is* the point of
                // the press, and it is the daemon's own words, drawn
                // verbatim.
                board.refusals[root] = result.detail
                await client.refresh()
            }
        }
    }

    private var processSplit: some View {
        VStack(spacing: 0) {
            if let slices = tabSlices {
                // Three matching lists: Active, Recently finished and
                // Abandoned, siblings of the same height and the same fold.
                // The rows drawn are exactly `processRows` (both read
                // `tabSlices` / `tabTableRows`), so the keyboard walk and
                // the selection prune agree with the screen.
                ScrollViewReader { scroll in
                    VStack(spacing: 0) {
                        processBand(.active, rows: slices.live)
                        processBand(.category(.finished), rows: slices.finished)
                        processBand(.category(.abandoned), rows: slices.abandoned)
                    }
                    .onAppear { scroller = scroll }
                }
            } else {
                ProcessTableHeader()
                ScrollViewReader { scroll in
                    ScrollView {
                        VStack(spacing: 0) {
                            ForEach(processRows) { row in processRow(row) }
                        }
                        .background(
                            GeometryReader { geo in
                                Color.clear.preference(key: ListHeightKey.self,
                                                       value: geo.size.height)
                            })
                    }
                    .onAppear { scroller = scroll }
                }
                .frame(maxHeight: tableHeight)
                .onPreferenceChange(ListHeightKey.self) { height in
                    listMeasured = height
                }
            }
            // The selected row's detail is no longer stacked here: it is
            // drawn by `AgentDetailPane` in the board's place. The spacer
            // keeps the table top-aligned.
            Spacer(minLength: 0)
        }
        .frame(maxHeight: .infinity, alignment: .top)
    }

    /// One process-row list on the rail: Active, Recently finished or
    /// Abandoned. Absent — not empty — when the project has none of that
    /// kind; a rolled-up heading still carries its count. A 7.5-item
    /// viewport, so the three sections occupy the same amount of list.
    @ViewBuilder
    private func processBand(_ section: PanelSection,
                             rows: [SectionRow]) -> some View {
        if !rows.isEmpty {
            let folded = isCollapsed(section)
            SectionHeader(
                title: section.title,
                count: rows.count,
                collapsed: folded,
                collapsible: true,
                toggle: { toggle(section, rows: rows) })
            if !folded {
                ProcessTableHeader()
                // Measured, not counted: a row is 26pt drawn (16pt chip slot
                // over 5pt either side) against a 24pt estimate, and seven
                // counted rows cut the seventh mid-line with scrolling
                // switched off. The same `ListHeightKey` seam `tableHeight`
                // uses; the count-based estimate only draws the first frame.
                let measured = bandMeasured[section] ?? 0
                let height = processViewportHeight(count: rows.count,
                                                   measured: measured)
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(rows) { row in processRow(row) }
                    }
                    .background(
                        GeometryReader { geo in
                            Color.clear.preference(key: ListHeightKey.self,
                                                   value: geo.size.height)
                        })
                }
                .frame(height: height)
                .scrollIndicators(.visible)
                .scrollDisabled(processBandFits(measured: measured,
                                                viewport: height))
                .onPreferenceChange(ListHeightKey.self) { value in
                    bandMeasured[section] = value
                }
            }
        }
    }

    private func processRow(_ row: SectionRow) -> some View {
        let bound = client.snapshot.board.card(forSession: row.agent.sessionId)
        return ProcessRow(
            agent: row.agent,
            category: row.category,
            selected: selected == row.id,
            attention: inAttention(row),
            ticking: client.visible,
            onSelect: {
                actions.disarm()
                // A second click on the selected row unselects it, which
                // brings the board back (`RailLayout.selectionAfterTap`).
                selected = RailLayout.selectionAfterTap(selected: selected,
                                                        tapped: row.id)
                expanded = selected
                // A click is the person choosing: no pending open aim and
                // no card hold may override it.
                aimPendingSinceShown = false
                revealedForCard = false
            },
            // The row's own `⌗` and `↗`: the detail header's two chips,
            // reachable without opening the detail — `⌗` keeps the board
            // in view, which is the point of pressing it from the list.
            onShowCard: bound == nil ? nil
                : { showCard(session: row.agent.sessionId) },
            onJump: row.agent.isJumpable
                ? { actions.jump(row.agent, client: client) } : nil,
            // The detail header's own title line, so the row names what
            // clicking it opens.
            cardLine: AgentDetailHeader.cardLine(card: bound,
                                                 sessionId: row.agent.sessionId))
        .id(row.id)
    }

    /// The table is exactly as tall as its rows; what is left under it is a
    /// spacer now that the selected row's detail is drawn in the board's
    /// place (`AgentDetailPane`) rather than stacked here.
    ///
    /// Both were flexible before this — a scroll view of rows and a scroll
    /// view of the selected row's last words — so SwiftUI split the rail's
    /// leftover height between them: the column headings floated a hundred
    /// points down the rail with nothing above them, a band of empty air sat
    /// between the last row and the pane, and the pane itself was capped at
    /// 280pt while the window had half a screen going spare. Capping the table
    /// at its own content is what makes the split unambiguous.
    ///
    /// Measured rather than counted (`ListHeightKey`, the same key the old
    /// list body used) because a row's height is the row's business. It is not
    /// the measure-A-to-size-A loop the height path forbids: the rows are
    /// fixed-width and single-line, so their height does not depend on the
    /// height this frame hands them, and the count-based estimate stays as the
    /// value before the first measurement lands.
    private var tableHeight: CGFloat {
        listMeasured > 0
            ? listMeasured
            : CGFloat(processRows.count) * PanelMetrics.processRow
    }

    private var header: some View {
        // Signals stand apart from each other and from the tallies. Stacked
        // flush they read as one striped block, and two facts about two
        // different budgets are not one fact.
        VStack(alignment: .leading, spacing: 0) {
            tallies
            SidebarUsageBand(client: client)
            // Fleet-level signals: facts about the *account*, not about any one
            // row. The daemon has always sent these and the panel decoded them
            // into nothing. "84% of the 5h budget with three agents running" is
            // exactly the thing you cannot learn by reading rows one at a time,
            // which is why it is computed once and belongs above them.
            VStack(alignment: .leading, spacing: 6) {
                ForEach(snapshot.signals, id: \.self) { signal in
                    FleetSignal(signal: signal)
                }
                // Absence has to be told apart from emptiness: with the
                // reconciler down, background agents are simply missing from
                // every bucket and the panel would otherwise look merely quiet.
                if snapshot.reconcilerAvailable == false {
                    FleetSignal(signal: Signal(
                        rule: "reconciler",
                        severity: "warn",
                        text: snapshot.reconcilerError.isEmpty
                            ? "Background agents unavailable — `claude agents` is not answering."
                            : "Background agents unavailable — \(snapshot.reconcilerError)",
                        action: "",
                        provider: "claude"))
                }
            }
            // Only when something is actually said: with no signals the tallies'
            // own bottom padding (or the band's hairline) is already the gap
            // above the tabs.
            .padding(.top, hasSignals ? 6 : 0)
            .padding(.bottom, hasSignals ? 8 : 0)
        }
    }

    private var hasSignals: Bool {
        !snapshot.signals.isEmpty || snapshot.reconcilerAvailable == false
    }

    private var tabs: some View {
        // Read once: the badge and the top bar draw the same number — every
        // entry, since nothing FYI is listed any more.
        let blocking = inboxItems.count
        return HStack(spacing: 0) {
            ForEach(Tab.allCases, id: \.self) { item in
                Button {
                    if item == .history || item == .reports { selected = nil }
                    tab = item
                } label: {
                    HStack(spacing: 4) {
                        Text(item.rawValue.uppercased())
                            .font(Theme.mono(10, weight: tab == item ? .semibold : .regular))
                            .tracking(0.8)
                            .foregroundStyle(tab == item ? Theme.phosphor : Theme.faint)
                        // The same number the top bar draws, from the same
                        // function: two readouts of one count, never two rules.
                        if item == .inbox, blocking > 0 {
                            Text("\(blocking)")
                                .font(Theme.mono(10, weight: .semibold))
                                .foregroundStyle(.red)
                        }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
                    .background(tab == item ? Theme.well : Color.clear)
                }
                .buttonStyle(.plain)
                .clickable()
                .reportsKeyboardFocus()
            }
        }
        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 8)
    }

    private var tallies: some View {
        HStack(spacing: 14) {
            // This band used to be the panel's drag handle. The window fills
            // the screen now, so there is nowhere to drag it to — see
            // `Placement.swift`. The patch is gone and the row is plain
            // readout again.
            HStack(spacing: 14) {
                Tally(label: "working", value: snapshot.counts.working, tint: Theme.phosphor,
                      // The subagent total annotates the working count rather than
                      // standing beside it as a fourth figure — it counts something
                      // no row is, and the strip already treats it as a footnote.
                      footnote: snapshot.counts.subagents)
                Tally(label: "idle", value: snapshot.counts.idle, tint: Theme.faint)
                Tally(label: "need you", value: snapshot.counts.attention, tint: .red)
                Spacer()
            }
            .contentShape(Rectangle())
            // Offline and the budget chips live here, beside the tallies.
            // The brand bar's subtitle also says offline; the chips are the
            // account reading, not a restatement of that word.
            if !client.connected {
                Label("offline", systemImage: "bolt.horizontal.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                // One chip per account, each wearing its own mark. A
                // single max() across both providers used to report a
                // number that was true of neither — the same trap
                // `evaluate_global` avoids by grouping — and "5h" is
                // Claude's window, not Grok's.
                ForEach(budgets) { BudgetChipView(chip: $0) }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    /// The worst budget reading each account is showing, one entry per provider
    /// that reported one. Claude first, then Grok — alphabetical is stable, and
    /// a chip that jumps sides when a Grok session starts is a chip that has to
    /// be re-read every time.
    private var budgets: [BudgetChip] {
        var worst: [String: Double] = [:]
        var cycle: [String: String] = [:]
        for agent in snapshot.agents.running {
            let provider = agent.provider.isEmpty ? "claude" : agent.provider
            // The cycle is taken from whichever row carries one, not from the
            // row that happens to hold the worst reading: the same account's
            // sessions report the same window, and only some of them name it.
            if cycle[provider] == nil,
               let named = agent.metrics.budgetCycle, !named.isEmpty {
                cycle[provider] = named
            }
            guard let pct = agent.metrics.fiveHourPct else { continue }
            worst[provider] = max(worst[provider] ?? pct, pct)
        }
        return worst.keys.sorted().map { provider in
            let pct = worst[provider] ?? 0
            let window = provider == "claude" ? "5h" : (cycle[provider] ?? "")
            return BudgetChip(
                provider: provider,
                label: window.isEmpty ? "\(Int(pct))%"
                                      : "\(window) \(Int(pct))%",
                tint: pct >= 90 ? .red : pct >= 75 ? .orange : .secondary)
        }
    }

    /// The middle of the rail with no rows in it, which is two different
    /// states wearing one face until the daemon says which. "Nothing running"
    /// is the ordinary quiet machine; a machine with **nothing enrolled** has
    /// to say so and offer the way in, or it reads as broken.
    ///
    /// Keyed on `enrollment.isEmpty`, which is `available && enrolled.isEmpty`
    /// — a daemon too old to carry the section leaves `available` false and
    /// gets the old sentence, never the prompt.
    @ViewBuilder
    private var empty: some View {
        if snapshot.enrollment.isEmpty, onboarding.placement == .none {
            enrolmentPrompt
        } else {
            VStack(spacing: 6) {
                AvatarView(character: "overwatch", state: .sleep, size: 56)
                overwatchCaption
                Text("Nothing running")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 34)
        }
    }

    /// The quiet screens' face is a large portrait, so it carries its name
    /// and its one line (`CastQuotes`) under it, above the screen's own words.
    private var overwatchCaption: some View {
        VStack(spacing: 2) {
            Text(CrewBand.display("overwatch"))
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphor)
            Text(CastQuotes.line(for: "overwatch"))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// No project is enrolled yet. Says so in words, names any folder Dark Army has
    /// just turned away, and offers to enrol it — the settings window's
    /// **Projects ▸ Enrol a folder…** is the route for a project with no
    /// window open. Drawn only when the first-run checklist is not, which
    /// already says the same thing with the route on it.
    private var enrolmentPrompt: some View {
        VStack(spacing: 10) {
            AvatarView(character: "overwatch", state: .sleep, size: 48)
            overwatchCaption
            Text("No project is enrolled")
                .font(.callout).foregroundStyle(Theme.phosphorBright)
            Text("Dark Army watches only the projects you enrol. Enrolling one puts "
                 + "a private key file inside it; everything else stays quiet.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(snapshot.enrollment.pending.prefix(4)) { folder in
                EnrolButton(label: folder.label, root: folder.root,
                            client: client)
            }
            if snapshot.enrollment.pending.isEmpty {
                Text("Open a project in VS Code, or use Settings (⌘,) → "
                     + "Projects → Enrol a folder…")
                    .font(.caption2).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 14)
        .padding(.vertical, 26)
    }

    /// One line above the rail when Dark Army has turned a folder away while other
    /// rows are showing. Deliberately one line and deliberately not a card:
    /// the fleet is the news here, and this is a footnote about what is *not*
    /// in it.
    @ViewBuilder
    private var enrolmentBanner: some View {
        let pending = snapshot.enrollment.pending
        if snapshot.enrollment.available, !pending.isEmpty,
           !snapshot.enrollment.enrolled.isEmpty {
            HStack(spacing: 6) {
                Image(systemName: "eye.slash")
                    .font(.system(size: 9))
                Text(pending.count == 1
                     ? "\(pending[0].label) is not enrolled — Dark Army is ignoring it"
                     : "\(pending.count) folders are not enrolled — Dark Army is "
                       + "ignoring them")
                    .font(.caption2)
                    .lineLimit(1)
                Spacer(minLength: 0)
                if pending.count == 1 {
                    EnrolButton(label: "Enrol", root: pending[0].root,
                                client: client, compact: true)
                }
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            Divider()
        }
    }
}
