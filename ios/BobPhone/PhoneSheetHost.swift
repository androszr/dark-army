import SwiftUI

/// A route carries exactly one subject; equality means the same destination,
/// so another press refreshes its payload without adding a duplicate rung.
struct PhoneSheet: Identifiable, Equatable {
    enum Subject {
        case agent(Agent, Category)
        case card(BoardCard)
        case catchUp([DecisionItem]?)
        case decision(DecisionItem)
        case workFile(String, Int, WorkRecordFile)
        case notification(PendingReceipt)
    }
    let subject: Subject

    static func agent(_ agent: Agent, _ category: Category) -> Self { Self(subject: .agent(agent, category)) }
    static func card(_ card: BoardCard) -> Self { Self(subject: .card(card)) }
    static func catchUp(_ items: [DecisionItem]? = nil) -> Self { Self(subject: .catchUp(items)) }
    static func decision(_ item: DecisionItem) -> Self { Self(subject: .decision(item)) }
    static func workFile(_ cardId: String, _ index: Int, _ file: WorkRecordFile) -> Self {
        Self(subject: .workFile(cardId, index, file))
    }
    static func notification(_ receipt: PendingReceipt) -> Self { Self(subject: .notification(receipt)) }

    // Aliases keep the Foundation-only rule table authoritative.
    static let initialDetent = PhoneSheetKind.initialDetent
    static let detents = PhoneSheetKind.detents
    static let onKeyboard = PhoneSheetKind.onKeyboard
    static let presentsTerminalFullScreen = PhoneSheetKind.presentsTerminalFullScreen
    static let answers = PhoneSheetKind.answers

    var kind: PhoneSheetKind {
        switch subject {
        case .agent: return .agent
        case .card: return .card
        case .catchUp: return .catchUp
        case .decision: return .decision
        case .workFile: return .workFile
        case .notification: return .notification
        }
    }
    /// The one subject fact the rule table reads: a card's column decides
    /// whether it is answered from half height. Nil for every other kind.
    var cardColumn: String? {
        if case .card(let card) = subject { return card.column }
        return nil
    }
    var id: String {
        let key: String
        switch subject {
        case .agent(let agent, _): key = agent.sessionId
        case .card(let card): key = card.id
        case .catchUp(let items): key = items?.map(\.id).joined(separator: "/") ?? "all"
        case .decision(let item): key = item.id
        case .workFile(let cardId, let index, _): key = "\(cardId)/\(index)"
        case .notification(let receipt): key = "\(receipt.generation)/\(receipt.receiptId)/\(receipt.sequence)"
        }
        return kind.rawValue + "/" + key
    }
    var title: String {
        switch subject {
        case .agent(let agent, _): return agent.nickname.isEmpty ? String(agent.sessionId.prefix(8)) : agent.nickname
        case .card(let card): return card.title.isEmpty ? "untitled" : card.title
        case .catchUp(let items): return items == nil ? "Catch up" : "Notification group"
        case .decision(let item): return item.title
        case .workFile(_, _, let file): return file.path
        case .notification: return "Notification"
        }
    }
    static func == (lhs: Self, rhs: Self) -> Bool { lhs.id == rhs.id }
}

/// The trail as data, for the saved place (`PhonePlace.swift`).
extension PhoneSheet {
    /// Kind and key for the three subjects the place restores — an agent,
    /// a card, the whole Catch up list — and nil for the rest.
    var placeEntry: PhonePlace.Entry? {
        switch subject {
        case .agent(let agent, _): return PhonePlace.Entry(kind: kind.rawValue, key: agent.sessionId)
        case .card(let card): return PhonePlace.Entry(kind: kind.rawValue, key: card.id)
        case .catchUp(.none): return PhonePlace.Entry(kind: kind.rawValue, key: "all")
        default: return nil
        }
    }

    /// The subject a saved rung names, against the picture the phone holds:
    /// an agent by its session, a card by its id. Nil where the picture no
    /// longer lists it — never a guess, never a fetch.
    static func fromPlace(_ entry: PhonePlace.Entry, snapshot: Snapshot) -> PhoneSheet? {
        switch entry.kind {
        case PhoneSheetKind.agent.rawValue:
            guard let (agent, category) = PhoneInbox.uniqueAgent(
                session: entry.key, agents: snapshot.agents) else { return nil }
            return .agent(agent, category)
        case PhoneSheetKind.card.rawValue:
            guard let card = snapshot.board.cards.first(where: { $0.id == entry.key }) else { return nil }
            return .card(card)
        case PhoneSheetKind.catchUp.rawValue:
            return entry.key == "all" ? .catchUp() : nil
        default:
            return nil
        }
    }
}

@MainActor
extension PhonePlace.CardDraft {
    /// What a card rung holds, or nil when nothing was typed or opened.
    /// The editors' text is kept only once the person touched them, with
    /// the revision it was typed against; an untouched editor is seeded from
    /// the card again. `note` is the Mac's words, not the person's: not kept.
    init?(held: PhoneCardDraftState) {
        let base = held.draftTouched ? held.editRevision : nil
        let touched = base != nil
        guard touched || held.editing || held.messageOpen
                || !held.messageText.isEmpty else { return nil }
        self.init(title: touched ? held.draftTitle : "",
                  summary: touched ? held.draftSummary : "",
                  prompt: touched ? held.draftPrompt : "",
                  priority: touched ? held.draftPriority : "",
                  area: touched ? held.draftArea : "",
                  draftFor: touched ? held.draftFor : "",
                  touched: touched, baseRevision: base, editing: held.editing,
                  messageOpen: held.messageOpen, messageText: held.messageText)
    }

    /// Hand the saved text back to a fresh rung's draft. `draftFor` and
    /// `draftTouched` stop the card screen reseeding the editors over it;
    /// `editRevision` makes its Save quote the revision the text was typed
    /// against, so a card changed since is refused with the Mac's copy.
    /// Touched text with no revision (an older file) is not restored.
    func fill(_ held: PhoneCardDraftState) {
        held.messageOpen = messageOpen
        held.messageText = messageText
        guard touched, let baseRevision else { return }
        // The editor reopens only with its own text: an open editor seeded
        // afresh would mark itself touched and block the full read's refill.
        held.editing = editing
        held.editRevision = baseRevision
        held.draftTitle = title
        held.draftSummary = summary
        held.draftPrompt = prompt
        held.draftPriority = priority
        held.draftArea = area
        held.draftFor = draftFor
        held.draftTouched = true
    }
}

/// Text and its revision context survive Back; live controls never live here.
@MainActor
final class PhoneCardDraftState: ObservableObject {
    @Published var note = ""
    @Published var cardFull: CardFull?
    @Published var draftTitle = ""
    @Published var draftSummary = ""
    @Published var draftPrompt = ""
    @Published var draftPriority = ""
    @Published var draftArea = ""
    @Published var draftFor = ""
    @Published var draftTouched = false
    /// The revision the touched editors were typed against; nil while
    /// untouched. What a guarded Save quotes, so a restored edit is still
    /// refused against a card changed since.
    @Published var editRevision: Int?
    @Published var editing = false
    @Published var conflict: CardStated?
    @Published var cachedPlanText: String?
    @Published var planDetailOpen = false
    @Published var messageOpen = false
    @Published var messageText = ""
    @Published var sendNote = ""
}

@MainActor
final class PhoneReplyDraft: ObservableObject {
    @Published var text = ""
}

/// One actual rung, even when the same subject occurs twice in a trail.
/// Memory only, discarded with that rung, never a second persisted cache.
@MainActor
final class PhoneSheetEntryState {
    /// A subject can occur twice in the trail; the mounted view belongs to
    /// this particular entry and must recover this entry's StateObjects.
    let id = UUID()
    private var cards: [String: PhoneCardDraftState] = [:]
    private var replies: [String: PhoneReplyDraft] = [:]
    private var notification: (route: PendingReceipt, epoch: Int, page: CatchUpPage)?
    /// The agent page this rung shows, so Back and a restore return to it.
    /// A fresh rung says Main, so every fresh open still lands there.
    var agentScreen: AgentScreen = AgentScreen.defaultScreen

    init() {}

    /// A rung rebuilt from the saved place (or a displaced draft): the page
    /// it was on — Terminal demoted to Details — and the text typed into it.
    init(restoring saved: PhonePlace.Entry?) {
        guard let saved else { return }
        agentScreen = AgentScreen(rawValue: PhonePlaceRules.restoredScreen(saved.agentScreen))
            ?? AgentScreen.defaultScreen
        if !saved.replyText.isEmpty {
            replyDraft(for: saved.key).text = saved.replyText
        }
        if let draft = saved.cardDraft {
            draft.fill(cardDraft(for: saved.key))
        }
    }

    /// This rung as the saved place records it; nil for a subject the place
    /// never restores.
    func placeEntry(for sheet: PhoneSheet) -> PhonePlace.Entry? {
        guard var entry = sheet.placeEntry else { return nil }
        if sheet.kind == .agent { entry.agentScreen = agentScreen.rawValue }
        entry.replyText = replies[entry.key]?.text ?? ""
        entry.cardDraft = cards[entry.key].flatMap(PhonePlace.CardDraft.init(held:))
        return entry
    }

    func cardDraft(for id: String) -> PhoneCardDraftState {
        if let held = cards[id] { return held }
        let draft = PhoneCardDraftState()
        cards[id] = draft
        return draft
    }

    func replyDraft(for sessionId: String) -> PhoneReplyDraft {
        if let held = replies[sessionId] { return held }
        let draft = PhoneReplyDraft()
        replies[sessionId] = draft
        return draft
    }

    /// Called only after the original receipt authorization has succeeded.
    func rememberNotification(_ page: CatchUpPage, route: PendingReceipt,
                              authorized: Bool, requestGeneration: Int) {
        guard authorized, page.available else { return }
        notification = (route, requestGeneration, page)
    }

    /// Consuming a receipt does not revoke the page already being read.
    /// A lock or pairing change does: neither may recover that old page.
    func resolvedNotification(for route: PendingReceipt, unlocked: Bool,
                              generation: String, requestGeneration: Int) -> CatchUpPage? {
        guard unlocked, route.generation == generation,
              let held = notification, held.route == route,
              held.epoch == requestGeneration else { return nil }
        return held.page
    }
}

private struct PhoneSheetEntryKey: EnvironmentKey {
    static let defaultValue: PhoneSheetEntryState? = nil
}

/// The sheet's current height, for content that draws differently at half
/// height (the agent sheet's compact lead). A `SheetDetent`, never a
/// `PresentationDetent`, so the rule reading it stays Foundation-only
/// (`AgentSheetLead.stillSize`). Nil outside a sheet.
private struct PhoneSheetDetentKey: EnvironmentKey {
    static let defaultValue: SheetDetent? = nil
}

extension EnvironmentValues {
    var phoneSheetEntry: PhoneSheetEntryState? {
        get { self[PhoneSheetEntryKey.self] }
        set { self[PhoneSheetEntryKey.self] = newValue }
    }
    var phoneSheetDetent: SheetDetent? {
        get { self[PhoneSheetDetentKey.self] }
        set { self[PhoneSheetDetentKey.self] = newValue }
    }
}

@MainActor
final class PhoneSheetRouter: ObservableObject {
    static let MAX_DEPTH = 3
    @Published private(set) var stack: [PhoneSheet] = []
    var top: PhoneSheet? { stack.last }
    private var entryStates: [PhoneSheetEntryState] = []
    var topState: PhoneSheetEntryState? { entryStates.last }
    /// Whether the hosted-terminal cover is up. **Not published**: flipping it
    /// must not rebuild the detent sheet under a first-responder cover, which
    /// is the UIKit crash. The keyboard observer reads it; nothing draws it.
    var terminalPresented = false

    /// UIKit presents one stable item for the whole trail. Changing the
    /// subject must not dismiss the presenter and write nil into its binding.
    struct Presentation: Identifiable { let id = UUID() }
    private let presentationIdentity = Presentation()
    var presentation: Presentation? { top == nil ? nil : presentationIdentity }

    /// Show `entry` on the trail. The agent sheet's swipe does not come through
    /// here: it replaces the top rung with `replaceTop(_:)`.
    func show(_ entry: PhoneSheet) {
        if top == entry {
            // Refresh the seed while preserving the text already being edited.
            stack[stack.count - 1] = entry
        } else if stack.count == Self.MAX_DEPTH {
            swapTop(entry)
        } else {
            entryStates.append(freshState(for: entry))
            stack.append(entry)
        }
    }

    /// Put `entry` in the top rung with a fresh state, so the content remounts.
    private func swapTop(_ entry: PhoneSheet) {
        entryStates[entryStates.count - 1] = freshState(for: entry)
        stack[stack.count - 1] = entry
    }

    /// The agent sheet's swipe: the top rung becomes `entry` without the
    /// sheet closing. Not `back()` then `show()`: the trail must not grow with
    /// agents swiped past, Back must return to the list, and at one rung
    /// `back()` would empty the stack and dismiss the sheet. The displaced
    /// rung's typed text is parked (memory only) for a swipe back; an equal
    /// subject is a refresh, never a remount.
    func replaceTop(_ entry: PhoneSheet) {
        guard let current = top, let state = topState else { return }
        guard current != entry else { show(entry); return }
        if let kept = state.placeEntry(for: current) {
            PhonePlaceStore.shared.keepDrafts(of: [kept])
        }
        swapTop(entry)
    }

    /// A new rung, carrying the text a tap or a draft displaced from the
    /// saved place when this is that subject (`PhonePlaceStore.setAside`).
    private func freshState(for entry: PhoneSheet) -> PhoneSheetEntryState {
        guard let displaced = PhonePlaceStore.shared.takeOrphanDraft(for: entry.id) else {
            return PhoneSheetEntryState()
        }
        return PhoneSheetEntryState(restoring: displaced)
    }

    /// Replace the trail with rungs rebuilt from the saved place, in one
    /// assignment each (`route(_:)`'s shape) so the presenter flips nil to
    /// item once rather than animating a presentation per rung.
    func restore(_ trail: [(PhoneSheet, PhonePlace.Entry)]) {
        let rungs = Array(trail.prefix(Self.MAX_DEPTH))
        entryStates = rungs.map { PhoneSheetEntryState(restoring: $0.1) }
        stack = rungs.map(\.0)
    }

    /// The trail as the saved place records it, bottom first, stopping at
    /// the first rung it never restores.
    func placeTrail() -> [PhonePlace.Entry] {
        var trail: [PhonePlace.Entry] = []
        for (sheet, state) in zip(stack, entryStates) {
            guard let entry = state.placeEntry(for: sheet) else { break }
            trail.append(entry)
        }
        return trail
    }
    func back() {
        guard !stack.isEmpty else { return }
        entryStates.removeLast()
        stack.removeLast()
    }
    func close() {
        entryStates.removeAll()
        stack.removeAll()
    }
    func route(_ receipt: PendingReceipt) {
        entryStates = [PhoneSheetEntryState()]
        stack = [.notification(receipt)]
    }
}

private extension SheetDetent {
    var presentation: PresentationDetent { self == .medium ? .medium : .large }
}

/// The one sheet's chrome stays mounted while its keyed subject changes.
struct PhoneSheetFrame: View {
    @EnvironmentObject private var router: PhoneSheetRouter
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.decryptFeedback) private var decryptFeedback
    @ObservedObject var client: PhoneClient
    @ObservedObject var outbox: OutboxStore
    @State private var detent: SheetDetent = .large
    @State private var offeredDetents: Set<PresentationDetent> = [.medium, .large]
    @State private var didAimInitialDetent = false
    @State private var resolvedNotification: UUID?
    /// The surface inside this sheet names itself here on arrival, and the
    /// header plays its caption; the content reserves no strip of its own.
    @StateObject private var captionHost = DecryptCaptionHost()

    private var kind: PhoneSheetKind { router.top?.kind ?? .card }
    private var column: String? { router.top?.cardColumn }
    /// The session ids Needs you draws, in drawn order, keeping the ones that
    /// open an agent sheet. Read at the moment of use, never cached.
    private var waitingOrder: [String] {
        AgentSheetSwipe.order(PhoneInbox.groups(client.snapshot.decisionItems).map(\.items)) { item in
            guard case .session(let id) = item.target,
                  PhoneInbox.uniqueAgent(session: id, agents: client.snapshot.agents) != nil
            else { return nil }
            return id
        }
    }

    /// The sheet's agent in the Needs you order, read from `waitingOrder` at
    /// the moment of the draw so the mark moves as agents are answered; nil
    /// for one waiter, an agent no longer waiting and every other sheet kind.
    private var position: AgentSheetSwipe.Position? {
        guard case .agent(let agent, _) = router.top?.subject else { return nil }
        return AgentSheetSwipe.position(of: agent.sessionId, in: waitingOrder)
    }

    /// The mark is a run of the title, never a slot in the `HStack`, so Back,
    /// the caption and Close keep their room and the name wraps as before; at
    /// accessibility sizes the run is dropped, never the name.
    private func titleText(_ position: AgentSheetSwipe.Position?) -> Text {
        let title = router.top?.title ?? ""
        let name = Text(title)
        guard let position,
              AgentSheetSwipe.showsMark(position, accessibilitySize: dynamicTypeSize.isAccessibilitySize)
        else { return name }
        return name + Text("  " + AgentSheetSwipe.mark(position))
            .font(Theme.mono(12))
            .foregroundStyle(Theme.dim)
    }

    /// The title as VoiceOver reads it: the name, then the position, at every
    /// text size.
    private func spokenTitle(_ position: AgentSheetSwipe.Position?) -> String {
        let name = router.top?.title ?? ""
        guard let position else { return name }
        return name + ", " + AgentSheetSwipe.spokenMark(position)
    }

    private func neighbour(_ direction: AgentSheetSwipe.Direction) -> (Agent, Category)? {
        guard case .agent(let agent, _) = router.top?.subject else { return nil }
        guard let id = AgentSheetSwipe.neighbour(of: agent.sessionId, in: waitingOrder, direction) else {
            return nil
        }
        return PhoneInbox.uniqueAgent(session: id, agents: client.snapshot.agents)
    }

    private func move(_ direction: AgentSheetSwipe.Direction) {
        guard !router.terminalPresented, let (next, category) = neighbour(direction) else { return }
        router.replaceTop(.agent(next, category))
    }

    private var presentationBinding: Binding<PresentationDetent> {
        Binding(get: { detent.presentation }, set: { detent = $0 == .medium ? .medium : .large })
    }

    var body: some View {
        Group {
            if let entry = router.top {
                content(entry)
                    .environment(\.phoneSheetEntry, router.topState)
                    .environment(\.phoneSheetDetent, detent)
                    .environment(\.decryptCaptionHost, captionHost)
            }
        }
        .id(router.topState?.id)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .safeAreaInset(edge: .top, spacing: 0) { header }
        .presentationDetents(offeredDetents, selection: presentationBinding)
        .presentationDragIndicator(.visible)
        .presentationBackground(Theme.bg)
        .overlay(ScanlineOverlay())
        .environment(\.decryptActive, true)
        .interactiveDismissDisabled(kind == .notification && resolvedNotification != router.topState?.id)
        .onAppear {
            // Returning from a full-screen terminal can appear again. Only
            // the first arrival initializes height; Done preserves it.
            guard !didAimInitialDetent else { return }
            didAimInitialDetent = true
            aimDetent()
        }
        .onChange(of: router.topState?.id) { _, _ in aimDetent() }
        .onChange(of: dynamicTypeSize) { _, _ in aimDetent() }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
            // A terminal keyboard belongs to the cover. Preserve the sheet's
            // height so Done returns to the same reading position.
            guard !router.terminalPresented else { return }
            detent = PhoneSheet.onKeyboard(detent)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            let position = self.position
            HStack(alignment: .top, spacing: 10) {
                if router.stack.count > 1 {
                    DecryptButton(action: { router.back() }) {
                        Image(systemName: "chevron.left")
                            .frame(minWidth: 44, minHeight: 44)
                    }
                    .accessibilityLabel("Back to \(router.stack[router.stack.count - 2].title)")
                }
                titleText(position)
                    .font(Theme.mono(14, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityLabel(spokenTitle(position))
                    .accessibilityActions {
                        if neighbour(.next) != nil {
                            DecryptButton("Next waiting agent", action: { move(.next) })
                        }
                        if neighbour(.previous) != nil {
                            DecryptButton("Previous waiting agent", action: { move(.previous) })
                        }
                    }
                if let decryptFeedback {
                    SheetArrivalCaption(feedback: decryptFeedback, host: captionHost)
                        .frame(minHeight: 44)
                }
                DecryptButton(action: {
                    if case .notification(let receipt) = router.top?.subject {
                        PhoneRouter.shared.consume(receipt)
                    }
                    router.close()
                }) {
                    Text("Close").font(Theme.mono(12))
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(minHeight: 44)
                }
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.phosphor)
            .contentShape(Rectangle())
            // Header only, by design: the body's drag resizes the sheet and the
            // conversation scrolls; a horizontal reading there would fight both.
            .simultaneousGesture(DragGesture(minimumDistance: 24, coordinateSpace: .local).onEnded { value in
                if let direction = AgentSheetSwipe.direction(dx: value.translation.width,
                                                             dy: value.translation.height) {
                    move(direction)
                }
            })
            if let decryptFeedback {
                SheetArrivalRule(feedback: decryptFeedback, host: captionHost)
            } else {
                Rectangle().fill(Theme.hair).frame(height: 1)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .background(Theme.bg)
    }

    @ViewBuilder private func content(_ entry: PhoneSheet) -> some View {
        let entryIdentity = router.topState?.id
        switch entry.subject {
        case .agent(let agent, let category):
            AgentDetailView(seed: agent, category: category, client: client)
        case .card(let card):
            PhoneCardDetailView(seed: card, client: client,
                                retained: router.topState?.cardDraft(for: card.id))
        case .catchUp(let items):
            CatchUpView(client: client, notificationItems: items)
        case .decision(let item):
            DecisionDetailView(item: item, client: client)
        case .workFile(let cardId, let index, let file):
            PhoneWorkRecordFileView(client: client, cardId: cardId, index: index, file: file)
        case .notification(let receipt):
            NotificationDestinationView(route: receipt, client: client, onAvailability: { available in
                guard router.topState?.id == entryIdentity else { return }
                resolvedNotification = available ? entryIdentity : nil
            })
        }
    }

    private func aimDetent() {
        // Restating detents while the terminal cover owns the keyboard is the
        // nested-presentation crash. The cover's own onAppear already flipped
        // `terminalPresented`; a poll or decrypt tick must not re-aim.
        guard !router.terminalPresented else { return }
        detent = PhoneSheet.initialDetent(kind, column, dynamicTypeSize.isAccessibilitySize)
        offeredDetents = Set(PhoneSheet.detents(kind, column, dynamicTypeSize.isAccessibilitySize).map(\.presentation))
    }
}

/// The arrival caption of the surface inside a sheet, drawn in the sheet's
/// header row beside the title rather than in a band reserved under it.
/// `DecryptCaption` holds its own geometry with hidden text, so the row
/// never jumps when the scramble starts or ends.
private struct SheetArrivalCaption: View {
    @ObservedObject var feedback: DecryptFeedback
    @ObservedObject var host: DecryptCaptionHost

    var body: some View {
        DecryptCaption(caption: "OPEN", frame: SheetArrival.glyphs(feedback, host))
    }
}

/// The rule under the sheet's header: lit while the caption plays, the
/// ordinary hairline otherwise.
private struct SheetArrivalRule: View {
    @ObservedObject var feedback: DecryptFeedback
    @ObservedObject var host: DecryptCaptionHost

    var body: some View {
        let glyphs = SheetArrival.glyphs(feedback, host)
        Rectangle().fill(glyphs != nil ? Theme.phosphor : Theme.hair).frame(height: 1)
    }
}

@MainActor
private enum SheetArrival {
    /// The frame to draw, only while the playing episode is this sheet's
    /// own surface.
    static func glyphs(_ feedback: DecryptFeedback, _ host: DecryptCaptionHost) -> String? {
        guard let surface = host.surface, feedback.state.surface == surface else { return nil }
        return feedback.state.screen?.frame(at: feedback.instant, reduced: feedback.reduced)
    }
}
