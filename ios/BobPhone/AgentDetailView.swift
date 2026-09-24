import SwiftUI
import UIKit

/// Which of the agent detail's two tabs is on show, and the pure rules that
/// decide whether there are tabs at all.
///
/// A session on a terminal Dark Army hosts has two things worth reading — the
/// live screen and everything the pane showed before hosted terminals arrived
/// — and one pane to draw them in. These are the tabs. **Details is the
/// default on every open**: there is no persistence here and no per-session
/// memory, only `defaultTab`.
///
/// `terminalAttached` is the whole of the stream's lifetime, because not
/// drawing `TerminalPane` *is* the disconnect: its `.onDisappear` hands the
/// pty's width back to the phone and its `dismantleNSView` cancels the
/// socket. Nothing on the daemon changes.
enum DetailTab: String, CaseIterable {
    case details = "Details"
    case terminal = "Terminal"

    /// What a fresh open lands on, always.
    static let defaultTab: DetailTab = .details

    /// Only a hosted row has anywhere to switch to; a row running in an
    /// editor draws exactly today's layout, with no tab bar at all.
    static func showsTabBar(hosted: Bool) -> Bool { hosted }

    /// The tab actually drawn. A row that *stops* being hosted mid-view
    /// cannot leave a dead Terminal tab selected: without a terminal there
    /// is only one pane, and it is Details.
    static func pane(hosted: Bool, tab: DetailTab) -> DetailTab {
        hosted ? tab : .details
    }

    /// Whether the live screen is on show — and so whether the stream is
    /// connected and the Mac is holding that terminal's width.
    static func terminalAttached(hosted: Bool, tab: DetailTab) -> Bool {
        hosted && tab == .terminal
    }
}

/// What the detail says about a terminal the Mac is **not** drawing.
///
/// A row without a hosted terminal is, almost always, a session in an
/// editor window — but not when Dark Army itself opened its terminal
/// (`originBy == "adhoc"`, the + TERMINAL press, or `"mission"`, Mission
/// Control's standing terminal): that terminal was never
/// in an editor, and if the daemon no longer holds it the session inside
/// died with it. Saying "find it in the editor yourself" about it sent a
/// person looking for a tab that does not exist. The daemon retires such a
/// row on its own (`_retire_hostless_sessions`); this is the wording for the
/// frames in between, and it is one pure table both clients read.
enum TerminalWhereabouts: Equatable {
    /// The session runs in an editor window the Mac may or may not raise.
    case editor
    /// Dark Army opened this terminal and no longer holds it.
    case gone
    /// The session's tab is gone. The turn may still be working.
    case lost

    static func of(originBy: String, hosted: Bool, tabGone: Bool = false) -> TerminalWhereabouts {
        if tabGone { return .lost }
        if hosted { return .editor }
        if originBy == "mission" { return .gone }
        return originBy == "adhoc" ? .gone : .editor
    }

    /// The short label beside `# terminal`.
    var label: String {
        switch self {
        case .editor: return "runs in the editor"
        case .gone: return "terminal gone"
        case .lost: return "tab gone"
        }
    }

    /// The dim line under the state, where the Mac cannot raise anything.
    var hint: String {
        switch self {
        case .editor:
            return "Dark Army cannot raise this terminal — find it in the editor yourself."
        case .gone:
            return "Dark Army opened this terminal and no longer holds it — the session inside it has ended."
        case .lost:
            return "The tab is gone, so there is nowhere to open and nowhere to type; the session can still be working."
        }
    }
}

/// What one agent last said, in a sheet opened from its row.
///
/// The sheet leads with the question and controls, leaving the list behind it.
/// A hosted terminal opens full screen because its keyboard needs the height;
/// Done returns to these details and releases the terminal watch.
///
/// This is the panel's `StdoutPane` at phone measure — its header line and its
/// body ladder. The agent's last message is drawn as a *document* through the
/// phone's own `MarkdownText`. Reply, Close, Stop, permission, Dismiss and
/// abandoned Delete sit under that, absent where the Mac says this row cannot.
///
/// A row on a terminal Dark Army hosts gets two tabs under its identity
/// line, `DetailTab`: **Details** — the ladder above — and **Terminal**, the
/// live screen (`PhoneTerminalPane`, `TerminalPane.swift`) filling everything
/// under a compact strip of what still needs a press, which the phone types
/// straight into. Details is where every open lands, and the phone asks for
/// the terminal only while the Terminal tab is showing.
/// `@MainActor` for the same reason as `PhoneCardDetailView`: a press's
/// mark is `@State`, and a task started from a method must write it on
/// the main actor for the redraw to follow.
@MainActor
struct AgentDetailView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @Environment(\.phoneSheetEntry) private var sheetEntry
    @Environment(\.decryptFeedback) private var decryptFeedback
    let seed: Agent
    let category: Category
    @ObservedObject var client: PhoneClient
    var targetRequestId: String? = nil
    @Environment(\.dismiss) private var dismiss
    @StateObject private var arm = Arm()
    @State private var note = ""
    @State private var hideAccepted = false
    /// A confirmed Delete is in the queue. The screen stays up while the
    /// press is in play — a refusal from the Mac's re-check has to land
    /// somewhere — and pops only once the row is gone from the snapshot,
    /// or the mark leaves with no words against it.
    @State private var deleteQueued = false
    /// Which permission behavior ("allow"/"deny") fired the verdict that is
    /// out — the two buttons share one verb, so the in-flight record alone
    /// cannot say which of them should read SENDING….
    @State private var sendingBehavior: String?
    /// The verb whose confirmed press is the one settling. `settlingHere`
    /// is shared by Stop, Close and Delete, so a cursor that read it alone
    /// would say "closing the terminal" after a confirmed Stop.
    @State private var settledAs: String?
    @ObservedObject private var dictation = DictationEngine.shared
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    /// Which tab a hosted row is showing. Not persisted and not remembered
    /// per session: `DetailTab.defaultTab` is where every open lands.
    @State private var hostedTab: DetailTab = DetailTab.defaultTab
    /// Which screen the phone's own container is showing. Not persisted.
    @State private var screen: AgentScreen = AgentScreen.defaultScreen
    /// Whether the strip above the live terminal is unfolded. Starts folded
    /// so the screen is the terminal; see `terminalScreen`.
    @State private var stripOpen = false

    private var agent: Agent {
        let a = client.snapshot.agents
        return (a.waiting + a.running + a.sleeping + a.finished + a.abandoned)
            .first { $0.sessionId == seed.sessionId } ?? seed
    }

    private var prompts: [PermissionPrompt] {
        client.snapshot.permissions.filter { $0.sessionId == agent.sessionId && (targetRequestId == nil || $0.requestId == targetRequestId) }
    }

    private var cards: [Notification] {
        client.snapshot.notifications.filter { $0.sessionId == agent.sessionId }
    }

    /// The verb of the press in play for this agent — queued, on its way,
    /// or landed and waiting for the snapshot — or nil. Feeds the
    /// per-button label swaps alone: a press is queued, not awaited, so no
    /// control on this screen dims on it.
    private var outAction: String? {
        client.queuedAction(for: agent.sessionId)
    }

    /// The word the pressed control wears — QUEUED, SENDING…, SENT.
    private var mark: String {
        client.queueMark(for: agent.sessionId) ?? "SENDING…"
    }

    /// The same word as a screen reader says it — "Queued", "Sending",
    /// "Sent" — never "Sending" for a control drawn QUEUED.
    private var spokenMark: String { Receipt.spoken(mark: mark) }

    /// A confirmed Stop / Close / Delete the Mac accepted, held until the
    /// snapshot stops listing the agent — so a Mac that takes a few seconds
    /// to comply never reads as a dropped press.
    private var settlingHere: Bool {
        client.settling.contains(agent.sessionId)
    }

    /// The row is leaving — a confirmed Stop / Close / Delete the Mac
    /// accepted, or a Hide it accepted — and the screen dims with it. A
    /// press merely in play is **not** busy: a press is queued, not
    /// awaited, and a second identical one is turned away by the queue.
    private var busy: Bool { settlingHere || hideAccepted }

    private var rowPresent: Bool {
        let a = client.snapshot.agents
        return (a.waiting + a.running + a.sleeping + a.finished + a.abandoned)
            .contains { $0.sessionId == seed.sessionId }
    }

    private func leaveHiddenDetail() {
        if hideAccepted && !rowPresent { dismiss() }
        // The press is queued, not awaited: the recovery waits until the
        // hide has left the queue (landed, or refused — the note then
        // carries the Mac's words) and its settling hold has ended.
        if hideAccepted && rowPresent && !settlingHere
            && outAction != PhoneActions.hideSession {
            hideAccepted = false
            // The Mac's own refusal, where it gave one, is the note; the
            // generic sentence is for a hide it accepted and never applied.
            if client.queueNote(for: agent.sessionId) == nil {
                note = "The Mac still lists this thread; it may have changed since you hid it."
            }
        }
    }

    /// The session's note is one per scope and this screen holds two
    /// readers of it: the answer box it contains takes an answer's or a
    /// reply's refusal (`QueueNote.isAnswer`), this screen every other
    /// verb's — a refused Stop under the actions, never under the answer
    /// buttons as well. Reading is drawing: only the reader that draws it
    /// reads it, so the box's refusal is not aged off by this screen.
    private func takeNote(_ queued: QueueNote?) {
        guard let queued, !queued.isAnswer else { return }
        note = queued.text
        client.readQueueNote(for: agent.sessionId)
    }

    /// A queued Delete's exit: the row gone from the snapshot pops the
    /// screen; the mark leaving with nothing said pops it too (the press
    /// landed and the row is on its way); the mark leaving **with** the
    /// Mac's words keeps the screen, where those words are drawn.
    private func leaveDeletedDetail() {
        guard deleteQueued else { return }
        if !rowPresent { dismiss(); return }
        if outAction == nil {
            if client.queueNote(for: agent.sessionId) == nil {
                dismiss()
            } else {
                deleteQueued = false
            }
        }
    }

    /// Whether the lead still is tucked away: set by a scroll in either
    /// pane, cleared by a tap on the name.
    @State private var photoTucked = false

    var body: some View {
        VStack(spacing: 0) {
            // The lead: a smaller still on the left, and beside it who this
            // is, the card and where it came from. A scroll in either pane
            // tucks the still away and the text takes the whole width; a
            // tap on the name brings it back.
            HStack(alignment: .top, spacing: 12) {
                if !photoTucked {
                    PixelMark(character: Cast.character(for: agent),
                              state: Cast.state(for: agent, category: category),
                              size: Self.leadPhotoSize)
                        .transition(.opacity)
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 6) {
                    identity
                    cardLead
                    originLead
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 4)
            // The verbs sit above the tabs, not inside one: Acknowledge,
            // Stop, Allow and the rest are one reach away on Conversation
            // and Details alike. Bounded like the terminal's strip, so a
            // long permission detail cannot push the tabs off the sheet.
            if hasVerbs {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        verbs
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: dynamicTypeSize.isAccessibilitySize
                       ? Self.hostedStripMaxHeightLarge : Self.hostedStripMaxHeight)
                .fixedSize(horizontal: false, vertical: true)
            }
            PhoneAgentScreenBar(screen: $screen, hosted: hostedTerminal,
                                needsPress: TerminalStrip.needsPress(
                                    prompts: prompts.count,
                                    questions: agent.questionList.count))
                .padding(.horizontal, 14)
                .padding(.bottom, 8)
            switch AgentScreen.pane(hosted: hostedTerminal, screen: screen) {
            case .conversation:
                ConversationScreen(
                    agent: agent, stopped: stopped, client: client,
                    retainedReply: sheetEntry?.replyDraft(for: agent.sessionId))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .modifier(TucksPhotoOnScroll(tucked: $photoTucked))
            case .details, .terminal:
                detailsScreen
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .modifier(TucksPhotoOnScroll(tucked: $photoTucked))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())

        .decryptSurface("AgentDetailView")
        .fullScreenCover(isPresented: terminalCover) {
            terminalScreen
                .environment(\.decryptActive, true)
                .onAppear { sheets.terminalPresented = true; syncWatch() }
                .onDisappear {
                    sheets.terminalPresented = false
                    client.watchTerminal(nil)
                }
        }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            leaveHiddenDetail()
            leaveDeletedDetail()
        }
        .onChange(of: settlingHere) { _, settling in
            if !settling { leaveHiddenDetail() }
        }
        .onChange(of: outAction) { _, _ in
            leaveDeletedDetail()
        }
        .onChange(of: client.queueNote(for: agent.sessionId)) { _, queued in
            // The Mac's own words about the last press from this screen,
            // arriving after the press was queued. Drawn here is read:
            // the record ages off the QUEUE list as finished business.
            takeNote(queued)
        }
        .onAppear {
            syncWatch()
            screen = AgentScreen.defaultScreen(
                supported: client.snapshot.board.conversationSupported)
            hostedTab = AgentScreen.detailTab(screen)
            // A refusal that landed while this screen was not up is still
            // the Mac's last word about this agent: read it on arrival.
            takeNote(client.queueNote(for: agent.sessionId))
        }
        .onChange(of: client.snapshot.board.conversationSupported) { _, supported in
            screen = AgentScreen.defaultScreen(supported: supported)
        }
        .onChange(of: screen) { _, next in
            hostedTab = AgentScreen.detailTab(next)
        }
        .onChange(of: agent.ownTerminal) { _, _ in
            syncWatch()
        }
        .onChange(of: hostedTab) { _, _ in
            syncWatch()
        }
        .onDisappear {
            arm.disarm()
            dictation.stop()
            // A full-screen cover can hide its presenting view. Its own
            // disappearance releases the watch; opening it must not do so.
            if !terminalCover.wrappedValue { client.watchTerminal(nil) }
        }
    }

    /// The row runs on a terminal Dark Army hosts and the Mac is new enough
    /// to serve its screen: the live terminal is the document.
    private var hostedTerminal: Bool {
        agent.ownTerminal && client.snapshot.board.terminalSupported
    }

    /// Ask for the terminal only while the Terminal tab is actually showing.
    /// The stream follows the tab: on Details the phone stops asking, and the
    /// Mac may take the pty's width back.
    private func syncWatch() {
        if DetailTab.terminalAttached(hosted: hostedTerminal, tab: hostedTab) {
            client.watchTerminal(agent.sessionId)
        } else {
            client.watchTerminal(nil)
        }
    }

    /// Who this is, beside the lead still: name, provider, quote. A tap
    /// shows or tucks the still (`photoTucked`). Not the live terminal —
    /// that cover keeps `hostedIdentity` so the emulator keeps the screen.
    private var identity: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(nickname)
                    .font(Theme.mono(15, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                // Which assistant this is — mark and name together, as the
                // Mac's detail header spells it.
                PhoneProviderMark(provider: agent.provider, size: 11)
                    .accessibilityHidden(true)
                Text(PhoneProviderMark.label(for: agent.provider))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            // Secondary and always one line: a size smaller than the
            // provider label, shrinking to fit beside the still (the
            // longest line is 52 characters, ~0.75 on a 375pt phone) and
            // truncating only below that floor.
            if !quote.isEmpty {
                Text(quote)
                    .font(Theme.mono(Self.quoteSize))
                    .foregroundStyle(Theme.dim)
                    .lineLimit(1)
                    .minimumScaleFactor(Self.quoteMinScale)
                    .truncationMode(.tail)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeOut(duration: 0.2)) { photoTucked.toggle() }
        }
        .accessibilityAddTraits(.isButton)
        .accessibilityHint(photoTucked ? "Shows the portrait" : "Hides the portrait")
    }

    /// The lead still beside the text: smaller than a sheet still so the
    /// name, the card and the origin read beside it rather than under it.
    static let leadPhotoSize: CGFloat = 96

    /// The quote under the name: secondary, one line, shrinking to fit.
    static let quoteSize: CGFloat = 10
    static let quoteMinScale: CGFloat = 0.7

    /// Compact face above the live terminal. Stays 40: a sheet-sized still
    /// here would take the screen the emulator is for.
    private var hostedIdentity: some View {
        HStack(spacing: 10) {
            PixelMark(character: Cast.character(for: agent),
                      state: Cast.state(for: agent, category: category),
                      size: 40)
            VStack(alignment: .leading, spacing: 2) {
                Text(nickname)
                    .font(Theme.mono(15, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 5) {
                    PhoneProviderMark(provider: agent.provider, size: 11)
                        .accessibilityHidden(true)
                    Text(PhoneProviderMark.label(for: agent.provider))
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                }
            }
        }
    }

    private var terminalCover: Binding<Bool> {
        Binding(get: {
            PhoneSheet.presentsTerminalFullScreen(.agent)
                && DetailTab.terminalAttached(hosted: hostedTerminal, tab: hostedTab)
        }, set: { presented in
            if !presented { dismissTerminal() }
        })
    }

    /// Resign the emulator before the cover comes down. Dismissing a
    /// full-screen cover that still owns the keyboard, from inside a detent
    /// sheet, is the crash — Done and a swipe both come through here.
    private func dismissTerminal() {
        decryptFeedback?.cancel()
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder),
                                        to: nil, from: nil, for: nil)
        if hostedTab != .details { hostedTab = .details }
        if screen != .details { screen = .details }
    }

    /// The live screen, with what still needs a press **folded above it**.
    /// The strip — the question, the permission buttons, the card verbs —
    /// is one line until a tap or an arriving ask opens it, because with the
    /// emulator's keyboard up the old always-open strip left the terminal a
    /// sliver under the header: the thing the tab is for was the one thing
    /// not on it. Opened, it is still bounded, and the pane takes the rest.
    private var terminalScreen: some View {
        VStack(alignment: .leading, spacing: 0) {
            hostedIdentity
                .padding(.horizontal, 14)
                .padding(.top, 8)
            HStack(spacing: 8) {
                DecryptButton(action: { dismissTerminal() }) {
                    Text("Done")
                        .font(Theme.mono(13))
                        .frame(minHeight: 44)
                }
                .accessibilityLabel("Done with terminal, return to details")
                Spacer(minLength: 8)
                DecryptButton(action: { stripOpen.toggle() }) {
                    Text(TerminalStrip.foldLabel(open: stripOpen,
                                                 prompts: prompts.count,
                                                 questions: agent.questionList.count))
                        .font(Theme.mono(13))
                        .foregroundStyle(TerminalStrip.needsPress(prompts: prompts.count,
                                                                  questions: agent.questionList.count)
                                         ? Theme.phosphorBright : Theme.dim)
                        .frame(minHeight: 44)
                }
                .accessibilityLabel(stripOpen ? "Hide the controls" : "Show the controls")
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 14)
            if stripOpen {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        questionLead
                        verbs
                        actions
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: dynamicTypeSize.isAccessibilitySize
                       ? Self.hostedStripMaxHeightLarge : Self.hostedStripMaxHeight)
                .fixedSize(horizontal: false, vertical: true)
            }
            Rectangle().fill(Theme.hair).frame(height: 1)
            PhoneTerminalPane(agent: agent, client: client)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.bg)
        .foregroundStyle(Theme.phosphor)
        .overlay(ScanlineOverlay())
        // An ask that arrives while the screen is up opens the strip once;
        // a person may fold it again and the fold holds until the next ask.
        .onChange(of: TerminalStrip.needsPress(prompts: prompts.count,
                                               questions: agent.questionList.count)) { _, needs in
            if needs { stripOpen = true }
        }
    }

    private var detailsScreen: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                questionLead
                actions
                rest
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .clipped()
        }
    }

    @ViewBuilder private var rest: some View {
        CollaborationView(client: client, focus: .session(provider: agent.provider, id: agent.sessionId))
        Rectangle().fill(Theme.hair).frame(height: 1)
        header
        facts
        if !agent.name.isEmpty {
            Text("cmd       \(agent.name)")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
        elsewhereTerminal
        body(for: agent)
    }

    /// The most of the screen the strip above the terminal may take, and
    /// the larger allowance at an accessibility text size.
    static let hostedStripMaxHeight: CGFloat = 200
    static let hostedStripMaxHeightLarge: CGFloat = 320

    private var nickname: String {
        agent.nickname.isEmpty ? String(agent.sessionId.prefix(8)) : agent.nickname
    }

    /// The character's one line under the name, drawn under the two large
    /// portraits only (`identity`, `conversationLead`) and never beside the
    /// 40pt `hostedIdentity`. Empty for a nickname off the roster.
    private var quote: String {
        CastQuotes.line(forNickname: agent.nickname)
    }

    /// The board card this session is on, through the board's own two-rung
    /// ladder — never a stale refinement link.
    private var boardCard: BoardCard? {
        client.snapshot.board.card(forSession: agent.sessionId)
    }

    /// The card's title as the screen's lead line. A card *bound* to this
    /// session is being worked; one only *refining* under this session is
    /// being planned, and says so — plain words, so a reader can tell the
    /// two apart without knowing what a refinement is. Empty for no card.
    static func cardLine(card: BoardCard?, sessionId: String) -> String {
        guard let card else { return "" }
        let title = card.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return "" }
        if card.sessionId != sessionId, card.isRefining,
           card.refineSessionId == sessionId {
            return "Planning: " + title
        }
        return title
    }

    private var cardLine: String {
        Self.cardLine(card: boardCard, sessionId: agent.sessionId)
    }

    /// The card, named, above the facts — and a tap that opens it, the same
    /// sheet every other list on the phone opens. On no card — including a
    /// planning session whose card has moved on to Backlog, which the
    /// lookup rightly stops claiming — the session's own name leads
    /// instead, as plain words and no link, so the header still says what
    /// the agent was on (22 Sep 2026: a finished plan left it blank).
    ///
    /// The row *says* it is a link (21 Sep 2026): a title alone read as a
    /// heading, and nobody tapped it. The chip names the card's column
    /// and the verb; the sheet it opens stacks over this one, so Back
    /// returns here and the next agent is one swipe away.
    @ViewBuilder private var cardLead: some View {
        if let card = boardCard, !cardLine.isEmpty {
            DecryptButton(action: { sheets.show(.card(card)) }) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(cardLine)
                        .font(Theme.mono(15, weight: .medium))
                        .foregroundStyle(Theme.phosphorBright)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 4) {
                        Text(Self.cardChip(column: card.column))
                            .font(Theme.mono(10))
                            .tracking(0.8)
                        Image(systemName: "chevron.right")
                            .font(Theme.mono(10, weight: .semibold))
                            .accessibilityHidden(true)
                    }
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .overlay(RoundedRectangle(cornerRadius: 3)
                        .stroke(Theme.phosphor, lineWidth: 1))
                    // Under the title: beside the still there is no room
                    // for the title and the chip on one line.
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open card, \(cardLine)")
            .accessibilityHint("Shows the card over this screen; Back returns here")
        } else if !sessionLine.isEmpty {
            Text(sessionLine)
                .font(Theme.mono(15, weight: .medium))
                .foregroundStyle(Theme.phosphorBright)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// What the session is on when the board lookup claims no card: the
    /// fleet row's second line, rung for rung (`ProcessTable.command`) —
    /// the daemon's `card_title` first, else the session's own name. The
    /// daemon publishes `card_title` only beside its unnamed-session
    /// placeholder, and it keeps a planning session's card after the plan
    /// moved it to Backlog, which is exactly when the lookup lets go
    /// (23 Sep 2026: the sheet read the placeholder under a titled row).
    private var sessionLine: String {
        let title = agent.cardTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        if !title.isEmpty { return title }
        return agent.name.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The chip's words: the verb and the column the card stands in,
    /// upper-cased the way the phone's chips are — `OPEN CARD · BACKLOG`.
    /// A column the phone does not know is left off rather than guessed.
    static func cardChip(column: String) -> String {
        let names = ["prep": "PREP", "backlog": "BACKLOG",
                     "in_progress": "IN PROGRESS", "done": "DONE"]
        guard let name = names[column] else { return "OPEN CARD" }
        return "OPEN CARD · " + name
    }

    /// Where this session came from, in the daemon's own words and never
    /// re-composed here — the Mac's panel draws the same string. Absent on a
    /// session the person started themselves. Wraps rather than clipping:
    /// the phone contract forbids clipping prose to a line count.
    @ViewBuilder private var originLead: some View {
        if !agent.originLine.isEmpty {
            Text(agent.originLine)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
        if !agent.areaLine.isEmpty {
            Text(agent.areaLine).font(Theme.mono(11)).foregroundStyle(Theme.dim)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            // An `HStack` cannot rewrap across its children, so each word-
            // bearing cell wraps in place rather than ending in dots. The
            // `·` separators are one character and need nothing.
            Text("# stdout").foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            Text("·").foregroundStyle(Theme.faint)
            Text(nickname).foregroundStyle(Theme.phosphorBright)
                .fixedSize(horizontal: false, vertical: true)
            Text("·").foregroundStyle(Theme.faint)
            Text(category.rawValue)
                .foregroundStyle(category == .waiting ? Color.red : Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(Theme.mono(11))
        // Five cells and two bare `·` separators read as seven fragments;
        // this is one line about one agent, and a landmark to jump to.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(cardLine.isEmpty
                            ? "\(nickname), \(category.rawValue)"
                            : "\(nickname), \(category.rawValue), on card \(cardLine)")
        .accessibilityAddTraits(.isHeader)
    }

    private var facts: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(factLines, id: \.self) { line in
                Text(line)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var factLines: [String] {
        var lines: [String] = []
        lines.append("project   \(agent.project.isEmpty ? "—" : agent.project)")
        if !agent.branch.isEmpty {
            lines.append("branch    \(agent.branch)")
        }
        lines.append("provider  \(agent.provider.isEmpty ? "—" : agent.provider)")
        if !agent.stats.model.isEmpty {
            lines.append("model     \(agent.stats.model)")
        }
        lines.append("elapsed   \(Self.elapsed(agent.stats.durationSeconds))")
        if let cost = agent.metrics.costUsd {
            lines.append(String(format: "cost      $%.2f", cost))
        }
        if let ctx = agent.metrics.ctxUsedPct {
            lines.append(Self.contextLine(ctx: ctx, trend: agent.trend))
        }
        if !agent.currentTool.isEmpty {
            lines.append("tool      \(agent.currentTool)")
        }
        let isLive = category == .waiting || category == .running || category == .sleeping
        if isLive, agent.subagents > 0 {
            let names = agent.subagentRows.map(\.qualified).filter { !$0.isEmpty }
            if names.isEmpty {
                lines.append("helpers   \(agent.subagentSummary)")
            } else {
                lines.append("helpers   \(agent.subagents) · " + names.joined(separator: ", "))
            }
        }
        if category == .waiting {
            lines.append("waiting for you for \(Self.elapsed(agent.idleSeconds))")
        }
        return lines
    }

    /// Where this session's terminal is, when the Mac does not host it.
    ///
    /// A session running in a VS Code window has no screen to stream, so
    /// this says so and says what the session is doing right now instead.
    /// There is no jump: raising an editor window is a thing the Mac does,
    /// and the phone is not at the Mac.
    @ViewBuilder
    private var elsewhereTerminal: some View {
        if !agent.ownTerminal {
            let sentence = PhoneAgentFacts.happening(agent: agent, category: category)
            let whereabouts = TerminalWhereabouts.of(originBy: agent.originBy,
                                                     hosted: agent.ownTerminal,
                                                     tabGone: agent.tabGone)
            let footnote = whereabouts == .gone || whereabouts == .lost
                ? whereabouts.hint : "Its screen is on the Mac, not here."
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text("# terminal").foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("·").foregroundStyle(Theme.faint)
                    Text(whereabouts.label).foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .font(Theme.mono(11))
                Text(sentence)
                    .font(Theme.mono(13))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                Text(footnote)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Terminal \(whereabouts.label). \(sentence). \(footnote)")
        }
    }

    @ViewBuilder
    private var questionLead: some View {
        // Every question of the dialog, not just the first — a dialog can
        // ask up to four at once, and the ones not shown are the ones left
        // standing in the terminal.
        let question = agent.questionList
            .map { $0.text.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: "\n\n")
        if !question.isEmpty {
            Text(question)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphorBright)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func body(for agent: Agent) -> some View {
        let summary = agent.lastSummary.trimmingCharacters(in: .whitespacesAndNewlines)
        let text = agent.lastText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !summary.isEmpty {
            Text(summary)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.dim)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            workReport(for: agent, latest: summary)
        } else if !text.isEmpty {
            MarkdownText(source: text, base: 13, mono: true)
                .equatable()
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            workReport(for: agent, latest: text)
        } else {
            workReport(for: agent, latest: "")
            if agent.lastReport.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty {
                Text("— no output —")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// What the last finished piece of work actually was, under the words that
    /// came after it. The Mac's `StdoutPane.reportToShow` rule, said once here:
    /// nothing where there is no report, and never a second copy of a message
    /// that *is* the report.
    @ViewBuilder
    private func workReport(for agent: Agent, latest: String) -> some View {
        let report = agent.lastReport.trimmingCharacters(
            in: .whitespacesAndNewlines)
        if !report.isEmpty, !latest.contains("## Work done") {
            VStack(alignment: .leading, spacing: 6) {
                Text("# work done")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                MarkdownText(source: report, base: 13, mono: true)
                    .equatable()
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The reply, on the Details tab and in the terminal's strip. The
    /// Conversation tab draws its own `AnswerBox` under the thread; every
    /// other verb is `verbs`, drawn above the tabs.
    @ViewBuilder
    private var actions: some View {
        AnswerBox(agent: agent, stopped: stopped, client: client,
                  terminalWins: terminalInputHere,
                  retainedReply: sheetEntry?.replyDraft(for: agent.sessionId))
    }

    /// Whether `verbs` draws anything, so an empty strip adds no gap above
    /// the tabs. The same gates `verbs` reads, in its order.
    private var hasVerbs: Bool {
        !note.isEmpty || !cards.isEmpty || !prompts.isEmpty
            || agent.canLowPriority || agent.canClose || agent.canHide
            || agent.canStop || category == .abandoned
    }

    /// Every press on this agent except the reply: the Mac's note, Dismiss,
    /// Allow / Deny, Low priority, Acknowledge & close, Hide, Stop, Delete.
    @ViewBuilder
    private var verbs: some View {
        if !note.isEmpty {
            Text(note)
                .font(Theme.mono(12))
                .foregroundStyle(.orange)
        }
        ForEach(cards) { card in
            if !card.message.isEmpty {
                Text(card.message)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
            DecryptButton(outAction == PhoneActions.dismiss ? mark : "Dismiss") {
                Task { await send(PhoneActions.dismiss, ["session_id": agent.sessionId]) }
            }
            .buttonStyle(AlarmOutline())
            .disabled(busy)
            // Mid-press the words on it are "SENDING…" alone.
            .accessibilityLabel(outAction == PhoneActions.dismiss
                                ? spokenMark : "Dismiss this card")
        }
        ForEach(prompts) { prompt in
            VStack(alignment: .leading, spacing: 6) {
                Text("wants to run \(prompt.toolName.isEmpty ? "a tool" : prompt.toolName)")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphorBright)
                if !prompt.detail.isEmpty {
                    Text(prompt.detail)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                }
                if prompt.answerable {
                HStack(spacing: 8) {
                    DecryptButton(permissionLabel("allow", plain: "Allow",
                                           armed: "Really allow?")) {
                        pressPermission("allow", requestId: prompt.requestId)
                    }
                    .buttonStyle(AlarmOutline())
                    .disabled(busy)
                    DecryptButton(permissionLabel("deny", plain: "Deny",
                                           armed: "Really deny?")) {
                        pressPermission("deny", requestId: prompt.requestId)
                    }
                    .buttonStyle(AlarmOutline(color: Theme.alarm))
                    .disabled(busy)
                }
                } else {
                    Text(prompt.answerNote)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                }
            }
        }
        if agent.canLowPriority {
            lowPriorityBox
        }
        if agent.canClose {
            closeBox
        }
        if agent.canHide {
            VStack(alignment: .leading, spacing: 6) {
                DecryptButton(outAction == PhoneActions.hideSession || hideAccepted
                       ? "HIDING…" : "Hide until this thread changes") {
                    Task {
                        if await send(PhoneActions.hideSession, ["session_id": agent.sessionId]) {
                            hideAccepted = true
                            leaveHiddenDetail()
                        }
                    }
                }
                .buttonStyle(AlarmOutline())
                .disabled(busy)
                .accessibilityLabel(
                    outAction == PhoneActions.hideSession || hideAccepted
                    ? "Hiding" : "Hide \(nickname) until this thread changes")
                Text("Hides this row on the Mac and phone until Codex writes to the thread again.")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        if agent.canStop {
            DecryptButton(stopLabel) {
                press(.stop, action: PhoneActions.stopSession,
                      fields: ["session_id": agent.sessionId])
            }
            .buttonStyle(AlarmOutline(color: Theme.alarm))
            .disabled(busy)
        }
        if category == .abandoned {
            DecryptButton(deleteLabel) {
                press(.deleteAgent, action: PhoneActions.deleteAgent,
                      fields: ["session_id": agent.sessionId], pop: true)
            }
            .buttonStyle(AlarmOutline(color: Theme.alarm))
            .disabled(busy)
        }
    }

    // MARK: - In-flight labels

    /// Stop and Close also read SENDING… through `settling`, so the held
    /// state runs press → accepted → gone-from-the-list without a snap-back
    /// to the plain label while the Mac complies.
    private var stopLabel: String {
        if outAction == PhoneActions.stopSession || settlingHere {
            return outAction == PhoneActions.stopSession ? mark : "SENDING…"
        }
        return arm.stop != nil ? "Really stop?" : "Stop"
    }

    private var deleteLabel: String {
        if outAction == PhoneActions.deleteAgent { return mark }
        return arm.deleteAgent != nil ? "Really delete?" : "Delete"
    }

    private var lowPriorityLabel: String {
        if outAction == PhoneActions.lowPriority { return mark }
        return arm.lowPriority != nil
            ? "Really switch to low priority?"
            : "Low priority"
    }

    private var closeLabel: String {
        if outAction == PhoneActions.closeTerminal || settlingHere {
            return outAction == PhoneActions.closeTerminal ? mark : "SENDING…"
        }
        return arm.close != nil
            ? "Really close the terminal?"
            : "Acknowledge & close terminal"
    }

    private func permissionLabel(_ behavior: String, plain: String,
                                 armed: String) -> String {
        if outAction == PhoneActions.permissionVerdict,
           sendingBehavior == behavior {
            return mark
        }
        return arm.permission == behavior ? armed : plain
    }

    /// End of a turn: waiting or sleeping in the *current* snapshot, not
    /// the seed category the sheet was opened with.
    private var stopped: Bool {
        let a = client.snapshot.agents
        let id = agent.sessionId
        return a.waiting.contains { $0.sessionId == id }
            || a.sleeping.contains { $0.sessionId == id }
    }

    /// The one case where the terminal is the box: the row owns a Dark
    /// Army-hosted terminal the phone may type into and the Mac is new
    /// enough to stream it. `AnswerBox`'s free-text reply yields to the
    /// emulator's own keyboard; its buttons stay.
    private var terminalInputHere: Bool {
        agent.ownTerminal && agent.canTerminalInput
            && client.snapshot.board.terminalStreamSupported
            && hostedTab == .terminal
    }

    /// Offered only where the Mac says `can_low_priority`: a rate-limited
    /// Claude session Dark Army can type into. `/low-priority` is a toggle, so the
    /// armed line says what a second run would do.
    private var lowPriorityBox: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(arm.lowPriority != nil
                 ? "Types the low-priority command into this terminal. Running it twice switches it back off."
                 : "This session hit its usage limit. Carry on in low priority?")
                .font(Theme.mono(11))
                .foregroundStyle(arm.lowPriority != nil ? Theme.alarm : Theme.dim)
            DecryptButton(lowPriorityLabel) {
                press(.lowPriority, action: PhoneActions.lowPriority,
                      fields: ["session_id": agent.sessionId])
            }
            .buttonStyle(AlarmOutline(color: arm.lowPriority != nil ? Theme.alarm : Theme.phosphor))
            .disabled(busy)
        }
    }

    private var closeBox: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(arm.close != nil
                 ? "Closes the tab and ends this agent. There is no undo."
                 : "Done reading? Close this terminal tab and end the session.")
                .font(Theme.mono(11))
                .foregroundStyle(arm.close != nil ? Theme.alarm : Theme.dim)
            HStack(spacing: 8) {
                // The panel's `WrapUpBar` cursor: beside the button from the
                // confirmed press until the row leaves the list — the same
                // window `closeLabel` reads SENDING… for.
                if outAction == PhoneActions.closeTerminal
                    || (settlingHere && settledAs == PhoneActions.closeTerminal) {
                    AgentChatterView(.caret, wait: .closing, seed: agent.sessionId,
                                     spoken: "Closing the terminal")
                        .id(agent.sessionId)
                }
                DecryptButton(closeLabel) {
                    // The extra field is the assertion that a human pressed
                    // this button, and it is what separates this press from
                    // `close-out.sh`'s identical action: only a person's press
                    // finishes the session's board card.
                    press(.close, action: PhoneActions.closeTerminal,
                          fields: ["session_id": agent.sessionId,
                                   "by_person": "1"])
                }
                .buttonStyle(AlarmOutline(color: arm.close != nil ? Theme.alarm : Theme.phosphor))
                .disabled(busy)
            }
        }
    }

    private func press(_ slot: Arm.Slot, action: String,
                       fields: [String: String], pop: Bool = false) {
        if arm.confirm(slot) {
            settledAs = action
            Task {
                let ok = await send(action, fields)
                // Written down is not landed: the screen leaves when the
                // row does (`leaveDeletedDetail`), so a refusal from the
                // Mac's re-check is drawn here rather than on a screen
                // that has already gone.
                if ok && pop { deleteQueued = true }
            }
        } else {
            arm.arm(slot, id: agent.sessionId)
        }
    }

    private func pressPermission(_ behavior: String, requestId: String) {
        if arm.permission == behavior {
            guard arm.confirm(.permission) else { return }
            Task {
                // Held until the verdict leaves the queue, so the pressed
                // button — not both — wears the mark for as long as the
                // press is in play.
                sendingBehavior = behavior
                if !(await send(PhoneActions.permissionVerdict, [
                    "request_id": requestId, "behavior": behavior,
                ])) {
                    sendingBehavior = nil
                }
            }
        } else {
            arm.arm(.permission, id: behavior)
        }
    }

    /// The scope is what queues a press from this screen behind the same
    /// agent's earlier ones and lets the row wear its mark — a permission
    /// verdict's fields carry only `request_id`, so without it that press
    /// would key on the bare verb. `enqueue` returns at once: `true` means
    /// the press is written down and will go in turn; the Mac's own later
    /// words arrive through `queueNote(for:)`.
    @discardableResult
    private func send(_ action: String, _ fields: [String: String]) async -> Bool {
        let result = await client.enqueue(action: action, fields: fields,
                                          scope: agent.sessionId)
        if result.ok {
            note = ""
            return true
        }
        if !result.detail.isEmpty { note = result.detail }
        return false
    }

    nonisolated static func elapsed(_ seconds: Double) -> String {
        let total = max(Int(seconds), 0)
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        if hours > 0 { return "\(hours)h \(minutes)m" }
        if minutes > 0 { return "\(minutes)m" }
        return "\(total)s"
    }

    /// `signals.py`'s own sentence — the percentage, then the estimated time
    /// to full, then the signed rate per minute — at the fact list's measure.
    /// No rate means the bare percentage; nothing here ever prints a rate the
    /// Mac did not send.
    static func contextLine(ctx: Double, trend: Trend) -> String {
        var line = "ctx       \(Int(ctx.rounded()))%"
        guard let pace = trend.pace, let rate = trend.ctxPctPerMin else { return line }
        // The runway rides only a rising line: the Mac publishes one for any
        // positive slope, and a time-to-full beside "(steady)" contradicts itself.
        if pace == .rising, let runway = trend.ctxRunwaySeconds {
            line += " — full in ~\(elapsed(runway))"
        }
        switch pace {
        case .steady: line += " (steady)"
        case .rising, .falling: line += String(format: " (%+.1f%%/min)", rate)
        }
        return line
    }
}

/// The labels beneath every row's identity: **Conversation**, **Details**,
/// and **Terminal** when Dark Army hosts the terminal.
///
/// `FleetView`'s chip idiom. A **file-scope** struct on purpose, not a
/// member of `AgentDetailView`: the accessibility roster inside that struct
/// is counted, and a chip speaks as one combined element — `.ignore` would
/// drop the press.
struct PhoneAgentScreenBar: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @Binding var screen: AgentScreen
    var hosted: Bool
    var needsPress: Bool = false

    var body: some View {
        HStack(spacing: 8) {
            ForEach(AgentScreen.chips(hosted: hosted), id: \.self) { item in
                chip(item)
            }
        }
    }

    private func chip(_ item: AgentScreen) -> some View {
        let on = screen == item
        return DecryptButton {
            if item == .terminal {
                // A screen episode ticks the sheet for 1.5s. That rebuilds
                // detents under the cover that this press is about to present.
                decryptFeedback?.cancel()
            } else {
                decryptFeedback?.begin(kind: .screen)
            }
            screen = item
        } label: {
            Text(item.rawValue)
                .font(Theme.mono(13, weight: on ? .medium : .regular))
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                // A floor, never a cap: the label's own text grows past it.
                .frame(minHeight: 32)
                .background(Rectangle().fill(on ? Theme.card : Color.clear))
                .overlay(Rectangle().stroke(on ? Theme.hair : Theme.rule))
                .foregroundStyle(on ? Theme.phosphor : Theme.faint)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(item == .details && needsPress
                            ? "\(item.rawValue) tab, needs a press"
                            : "\(item.rawValue) tab")
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
    }
}

/// What a session is doing right now, in one line.
///
/// The Mac's `AgentFacts.happening` in the phone's own terms — the same
/// facts in the same order, minus the agent's reported activity, which the
/// phone's `Agent` does not decode. Pure, so both surfaces say the same
/// thing about the same row.
enum PhoneAgentFacts {
    static func happening(agent: Agent, category: Category) -> String {
        var parts: [String] = []
        switch category {
        case .waiting:
            parts.append(agent.idleSeconds > 0
                         ? "Stopped, waiting for you for \(AgentDetailView.elapsed(agent.idleSeconds))"
                         : "Stopped, waiting for you")
        case .running:
            let tool = agent.currentTool.trimmingCharacters(in: .whitespaces)
            parts.append(tool.isEmpty ? "Working" : "Working — running \(tool)")
        case .sleeping:
            parts.append("Idle in the editor")
        case .finished:
            parts.append("Recently finished")
        case .abandoned:
            parts.append("Abandoned")
        }
        if agent.subagents > 0 {
            parts.append(agent.subagents == 1 ? "1 helper" : "\(agent.subagents) helpers")
        }
        let summary = agent.lastSummary.trimmingCharacters(in: .whitespacesAndNewlines)
        if !summary.isEmpty { parts.append(summary) }
        return parts.joined(separator: " · ")
    }
}

/// The fold above the live terminal: two pure rules both the view and the
/// tests read. Foundation only, so `swiftc -parse` and the contract suite
/// can drive them without a screen.
enum TerminalStrip {
    /// Something on the strip is blocking the agent — a permission ask or
    /// a question — so the fold opens by itself and its label says so.
    static func needsPress(prompts: Int, questions: Int) -> Bool {
        prompts > 0 || questions > 0
    }

    /// The one line the fold draws: a chevron for the state, then what it
    /// holds — the ask first, since that is what a reader came to answer.
    static func foldLabel(open: Bool, prompts: Int, questions: Int) -> String {
        let mark = open ? "▾" : "▸"
        if prompts > 0 { return "\(mark) permission" }
        if questions > 0 { return "\(mark) question" }
        return "\(mark) controls"
    }
}


/// Tucks the lead still away when the person starts scrolling a pane, from
/// the scroll phase of the first scroll view inside (iOS 18). iOS 17 has no
/// such report, and a drag gesture here would compete with the emulator's
/// own touches, so there the still stays until a tap on the name tucks it.
private struct TucksPhotoOnScroll: ViewModifier {
    @Binding var tucked: Bool

    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollPhaseChange { _, phase in
                guard phase == .interacting, !tucked else { return }
                withAnimation(.easeOut(duration: 0.2)) { tucked = true }
            }
        } else {
            content
        }
    }
}
