import SwiftUI

/// One decision list: the Mac Inbox's three kinds, projected by
/// `PhoneInbox`, drawn as short rows.
///
/// Classification lives in that reducer. This view draws one grouped list
/// of compact rows — face, the kind's word, title, how long it has waited,
/// the detail in full, Dark Army's own line where it has one — and opens the
/// agent or card screen through `PhoneInboxRoute`, never a second
/// presenter. **The tab routes; it does not answer**: the answer box, the
/// permission verdict, Mark done and the rest live on the screen a row
/// opens. The swipe carries **Dismiss** (and Dismiss all above the list),
/// which hides an entry until it changes, and — on an agent's row — that
/// agent's own verbs from its screen (`InboxAgentVerb`: Close terminal,
/// Hide, Low priority, Stop — the words are `Verbs`), each gated on the same `can_*` flag, so a
/// finished agent is acknowledged and closed without opening it.
struct NeedsYouView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    /// A refused dismiss, in the Mac's words, under the row it was about.
    @State private var notes: [String: String] = [:]
    /// Dismiss all is armed against the exact set of rows it would hide,
    /// and a changed list disarms it — the Mac's own rule.
    @State private var dismissAllArmed: Set<String> = []
    @State private var dismissingAll = false
    /// A swiped Close terminal, waiting on its confirmation.
    @State private var closing: PhoneInboxItem?
    /// The row whose More was pressed: its other agent verbs, in a dialog.
    @State private var moreFor: PhoneInboxItem?

    private var snapshot: Snapshot { client.snapshot }
    private var groups: [PhoneInboxGroup] {
        PhoneInbox.groups(snapshot.decisionItems)
    }

    var body: some View {
        List {
            if client.refreshing {
                AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                 spoken: "Checking with the Mac")
                    .id("refresh")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            dismissAllRow
            if snapshot.decisionItems.isEmpty {
                CommentLine(text: "nobody needs you")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            ForEach(groups) { group in
                // A heading only where there is something to tell apart.
                if groups.count > 1 {
                    PhoneSectionHeader(title: group.heading.uppercased())
                }
                ForEach(group.items) { item in
                    entry(item)
                        .listRowInsets(EdgeInsets())
                        .listRowSeparator(.hidden)
                        .listRowBackground(Theme.bg)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .tint(Theme.phosphor)
        .refreshable { await client.refreshNow() }
        .confirmationDialog(
            Verbs.closeTerminal.noUndo ?? "",
            isPresented: Binding(get: { closing != nil },
                                 set: { if !$0 { closing = nil } }),
            titleVisibility: .visible,
            presenting: closing
        ) { item in
            DecryptButton(InboxAgentVerb.close.label, role: .destructive) {
                press(.close, on: item)
            }
            DecryptButton("Cancel", role: .cancel) {}
        }
        .confirmationDialog(
            moreFor.map { "More for \($0.title)" } ?? "More",
            isPresented: Binding(get: { moreFor != nil },
                                 set: { if !$0 { moreFor = nil } }),
            titleVisibility: .visible,
            presenting: moreFor
        ) { item in
            ForEach(agentVerbs(item).filter { $0 != .close }) { verb in
                DecryptButton(verb.label, role: verb.destructive ? .destructive : nil) {
                    press(verb, on: item)
                }
            }
            DecryptButton("Cancel", role: .cancel) {}
        }
    }

    /// The agent verbs this row offers — the agent screen's own, read off
    /// the same row the fleet draws, and none where there is no such row.
    private func agentVerbs(_ item: PhoneInboxItem) -> [InboxAgentVerb] {
        guard !item.sessionId.isEmpty,
              let (agent, _) = PhoneInbox.uniqueAgent(
                  session: item.sessionId, agents: snapshot.agents)
        else { return [] }
        return InboxAgentVerb.offered(for: agent)
    }

    /// Queued under the agent, like the agent screen's own press, so the
    /// row wears the same mark and a refusal lands under it.
    private func press(_ verb: InboxAgentVerb, on item: PhoneInboxItem) {
        Task {
            let result = await client.enqueue(
                action: verb.action, fields: verb.fields(session: item.sessionId),
                scope: scope(item))
            notes[item.id] = result.ok ? "" : result.detail
        }
    }

    /// **Dismiss all**: `inbox_ack` per dismissable row, armed then
    /// confirmed. Absent when nothing is dismissable or the Mac keeps no
    /// hide list; the armed label names the count. A permission ask stays.
    @ViewBuilder
    private var dismissAllRow: some View {
        let rows = snapshot.inbox.available
            ? PhoneInbox.dismissable(snapshot.decisionItems) : []
        let ids = Set(rows.map(\.id))
        if !rows.isEmpty {
            let armed = !dismissAllArmed.isEmpty && dismissAllArmed == ids
            DecryptButton(dismissingAll ? "HIDING…"
                          : armed ? "Dismiss \(rows.count)?" : "Dismiss all") {
                if armed {
                    dismissAllArmed = []
                    Task { await dismissAll(rows) }
                } else {
                    dismissAllArmed = ids
                }
            }
            .buttonStyle(AlarmOutline())
            .disabled(dismissingAll)
            .accessibilityLabel(armed ? "Dismiss \(rows.count) rows, press again to confirm"
                                : "Dismiss all; asks first. A permission ask stays.")
            .padding(.horizontal, 12)
            .padding(.vertical, 4)
            .listRowInsets(EdgeInsets())
            .listRowSeparator(.hidden)
            .listRowBackground(Theme.bg)
            .onChange(of: ids) { _, fresh in
                // A changed list disarms: the press was about *these* rows.
                if dismissAllArmed != fresh { dismissAllArmed = [] }
            }
        }
    }

    /// One `inbox_ack` per row, each written into the queue under its own
    /// subject and gone in turn; the poll brings the list back. Nothing is
    /// awaited on the transport and no refresh is forced: a press is
    /// queued, not awaited.
    private func dismissAll(_ rows: [PhoneInboxItem]) async {
        dismissingAll = true
        defer { dismissingAll = false }
        for row in rows {
            let result = await client.enqueue(
                action: PhoneActions.inboxAck,
                fields: ackFields(row), scope: scope(row))
            if !result.ok { notes[row.id] = result.detail }
        }
    }

    /// A refusal under the row: the immediate one `enqueue` answered (a
    /// duplicate, the Face ID sentence), else the Mac's own words about the
    /// queued dismiss, arriving later on the receipt.
    private func rowNote(_ item: PhoneInboxItem) -> String? {
        if let own = notes[item.id], !own.isEmpty { return own }
        return client.queueNote(for: scope(item))?.text
    }

    /// The subject a dismiss is queued under — the agent, else the card —
    /// so it wears the same mark on the agent or card screen and queues
    /// behind that subject's earlier presses. Never the bare verb: every
    /// row's dismiss would then wait behind, and be refused as a copy of,
    /// every other row's.
    private func scope(_ item: PhoneInboxItem) -> String {
        item.sessionId.isEmpty ? item.cardId : item.sessionId
    }

    /// One row: the whole thing is the press that opens the subject's
    /// screen; the swipe is Dismiss where the Mac would honour it.
    @ViewBuilder
    private func entry(_ item: PhoneInboxItem) -> some View {
        let opens = PhoneInboxRoute.opens(item, snapshot: snapshot)
        VStack(alignment: .leading, spacing: 4) {
            if opens {
                DecryptButton(action: {
                    PhoneInboxRoute.open(item, snapshot: snapshot, sheets: sheets)
                }) {
                    InboxRow(item: item, face: face(item))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(PhoneInbox.spokenLabel(for: item, card: item.card(in: snapshot)))
                .accessibilityAddTraits(.isButton)
            } else {
                // An orphan permission: a published prompt with no agent
                // row behind it, so nothing to open. Read where it is.
                InboxRow(item: item, face: nil)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(PhoneInbox.spokenLabel(for: item))
            }
            bobsLine(item)
            if let mark = client.queueMark(for: scope(item)) {
                Text(mark)
                    .font(Theme.mono(9, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.faint)
                    .padding(.horizontal, 12)
            }
            if let note = rowNote(item), !note.isEmpty {
                Text(note)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
                    // Drawn is read: the queued dismiss's refusal ages off
                    // the QUEUE list as finished business.
                    .onAppear { client.readQueueNote(for: scope(item)) }
                    .onChange(of: note) { _, _ in
                        client.readQueueNote(for: scope(item))
                    }
            }
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .contain)
        .modifier(InboxSwipeModifier(
            item: item,
            available: snapshot.inbox.available,
            verbs: agentVerbs(item),
            onDismiss: { dismiss(item) },
            onClose: { closing = item },
            onMore: { moreFor = item }))
    }

    /// The face the fleet's own row wears for this session — never a
    /// hashed stranger for a session the fleet has forgotten.
    private func face(_ item: PhoneInboxItem) -> (character: String, state: Cast.State)? {
        guard !item.sessionId.isEmpty,
              let (agent, category) = PhoneInbox.uniqueAgent(
                  session: item.sessionId, agents: snapshot.agents)
        else { return nil }
        return (Cast.character(for: agent), Cast.state(for: agent, category: category))
    }

    /// Dark Army's own words, drawn only where it has some: a refusal in orange,
    /// a queued wait or the orphan line in the quiet tone.
    @ViewBuilder
    private func bobsLine(_ item: PhoneInboxItem) -> some View {
        let card = item.card(in: snapshot)
        let refusal = card?.dispatchError ?? ""
        let line = PhoneInbox.sentence(for: item, refusal: refusal,
                                       queueReason: card?.queueReason ?? "")
        if !line.isEmpty {
            Text(line)
                .font(Theme.mono(11))
                .foregroundStyle(refusal.isEmpty ? Theme.dim : .orange)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
        }
    }

    private func ackFields(_ item: PhoneInboxItem) -> [String: String] {
        [
            "key": item.target.key,
            "kind": item.wire.name,
            "fingerprint": PhoneInbox.fingerprint(item, snapshot: snapshot),
        ]
    }

    private func dismiss(_ item: PhoneInboxItem) {
        Task {
            let result = await client.enqueue(
                action: PhoneActions.inboxAck, fields: ackFields(item),
                scope: scope(item))
            notes[item.id] = result.ok ? "" : result.detail
        }
    }
}

/// One compact entry: face, the kind's word, title and age on the first
/// line; the detail in full beneath. Nothing here is clipped — a truncated
/// question is a walk to the desk, which is the thing this tab exists to
/// save — and nothing here is a second control.
struct InboxRow: View {
    let item: PhoneInboxItem
    let face: (character: String, state: Cast.State)?

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if let face {
                PixelMark(character: face.character, state: face.state, size: 24)
            } else {
                Color.clear.frame(width: 24, height: 24)
            }
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(item.kind.word)
                        .font(Theme.mono(9, weight: .semibold))
                        .tracking(0.8)
                        .foregroundStyle(Theme.faint)
                    Text(item.title)
                        .font(Theme.prose(17, weight: .semibold))
                        .foregroundStyle(Theme.text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                    age
                }
                if !item.detail.isEmpty {
                    Text(item.detail)
                        .font(Theme.prose(15))
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.red).frame(width: 2)
        }
        .accessibilityHidden(true)
    }

    /// How long this has waited, on the fleet's own clock; undated draws
    /// nothing rather than a dash that would read as "no time".
    @ViewBuilder
    private var age: some View {
        if item.since > 0 {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let now = context.date.timeIntervalSince1970
                Text(FleetAge.text(startedAt: item.since, now: now))
                    .font(Theme.mono(11).monospacedDigit())
                    .foregroundStyle(Theme.dim)
            }
        }
    }
}

/// Trailing swipe on Needs you: **Dismiss**, on every row the Mac would
/// honour it for — never a permission ask, and nothing against a Mac that
/// keeps no hide list — then the agent's own verbs: **Close terminal**
/// (confirmed in a dialog) and **More** for the rest.
private struct InboxSwipeModifier: ViewModifier {
    let item: PhoneInboxItem
    let available: Bool
    let verbs: [InboxAgentVerb]
    let onDismiss: () -> Void
    let onClose: () -> Void
    let onMore: () -> Void

    private var dismissable: Bool {
        PhoneInboxAck.showsDismiss(wire: item.wire, available: available)
    }
    private var closes: Bool { verbs.contains(.close) }
    private var more: Bool { verbs.contains { $0 != .close } }

    @ViewBuilder
    func body(content: Content) -> some View {
        if dismissable || closes || more {
            content
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    if PhoneInboxAck.showsDismiss(wire: item.wire, available: available) {
                        DecryptButton(action: onDismiss) {
                            Label(Verbs.dismiss.label, systemImage: "checkmark")
                        }
                        .tint(SwipeInk.dismiss)
                    }
                    if closes {
                        DecryptButton(action: onClose) {
                            Label(Verbs.closeTerminal.label, systemImage: "xmark.square")
                        }
                        .tint(SwipeInk.close)
                    }
                    if more {
                        DecryptButton(action: onMore) {
                            Label("More", systemImage: "ellipsis")
                        }
                        .tint(SwipeInk.more)
                    }
                }
                .accessibilityActions {
                    if dismissable { DecryptButton(Verbs.dismiss.label, action: onDismiss) }
                    if closes { DecryptButton(InboxAgentVerb.close.label, action: onClose) }
                    if more { DecryptButton("More actions", action: onMore) }
                }
        } else {
            content
        }
    }
}

/// The agent screen's verbs, offered on a Needs you row too, so a person
/// acknowledges and closes a finished agent without opening it. Each is
/// gated on the same `can_*` flag the agent screen reads and sends the
/// same action with the same fields; the permission verdict and the answer stay on
/// the agent screen, and an abandoned row's removal is never offered here.
enum InboxAgentVerb: String, Identifiable, CaseIterable {
    case close, hide, lowPriority, stop

    var id: String { rawValue }

    static func offered(for agent: Agent) -> [InboxAgentVerb] {
        var verbs: [InboxAgentVerb] = []
        if agent.canClose { verbs.append(.close) }
        if agent.canHide { verbs.append(.hide) }
        if agent.canLowPriority { verbs.append(.lowPriority) }
        if agent.canStop { verbs.append(.stop) }
        return verbs
    }

    var action: String {
        switch self {
        case .close: return PhoneActions.closeTerminal
        case .hide: return PhoneActions.hideSession
        case .lowPriority: return PhoneActions.lowPriority
        case .stop: return PhoneActions.stopSession
        }
    }

    /// `by_person` on Close is the agent screen's own assertion that a
    /// human pressed it: only a person's close finishes the board card.
    func fields(session: String) -> [String: String] {
        switch self {
        case .close: return ["session_id": session, "by_person": "1"]
        case .hide, .lowPriority, .stop: return ["session_id": session]
        }
    }

    var label: String {
        switch self {
        case .close: return Verbs.closeTerminal.label
        case .hide: return Verbs.hide.label
        case .lowPriority: return Verbs.lowPriority.label
        case .stop: return Verbs.stop.label
        }
    }

    var destructive: Bool { self == .close || self == .stop }
}

/// Typed destination adapter onto the existing `PhoneSheet` factories.
/// Outside the Foundation-only reducer so tests can drive routing without
/// SwiftUI, and so Needs you never presents a sheet of its own.
@MainActor
enum PhoneInboxRoute {
    static func sheet(for item: PhoneInboxItem, snapshot: Snapshot) -> PhoneSheet? {
        switch item.target {
        case .session(let id):
            guard let (agent, category) = PhoneInbox.uniqueAgent(
                session: id, agents: snapshot.agents)
            else { return nil }
            return .agent(agent, category)
        case .card(let id):
            // A card whose assistant is still in the fleet opens that agent —
            // its Main / Conversation / Details tabs, with OPEN CARD one press
            // away — because the entry is about what the agent did (3 Oct
            // 2026: "tapping Needs you should lead to the agent view with
            // tabs"). The card sheet is the fallback once the agent is gone.
            if !item.sessionId.isEmpty,
               let (agent, category) = PhoneInbox.uniqueAgent(
                   session: item.sessionId, agents: snapshot.agents) {
                return .agent(agent, category)
            }
            guard let card = snapshot.board.cards.first(where: { $0.id == id })
            else { return nil }
            return .card(card)
        case .review:
            // A run opens on the Menu tab's Review screen, not in a sheet.
            return nil
        }
    }

    /// Whether a press on this entry has anywhere to go — false for an
    /// orphan permission, which is read where it is.
    static func opens(_ item: PhoneInboxItem, snapshot: Snapshot) -> Bool {
        if case .review(let id) = item.target {
            return snapshot.review.run(id: id) != nil
        }
        return sheet(for: item, snapshot: snapshot) != nil
    }

    static func open(_ item: PhoneInboxItem, snapshot: Snapshot,
                     sheets: PhoneSheetRouter) {
        // A press on a run only moves the screen: it posts no action.
        if case .review(let id) = item.target {
            PhoneRouter.shared.openReview(runId: id)
            return
        }
        if let sheet = sheet(for: item, snapshot: snapshot) {
            sheets.show(sheet)
        }
    }
}

/// Swipe-action background. The list's `.tint(Theme.phosphor)` would paint
/// the button bright green under the system's white label — about 1.6:1,
/// unreadable in daylight and below every WCAG rung. Dark phosphor reads at
/// ~5.2:1 against white (AA for normal text), and the button also carries a
/// glyph, so colour is never the only thing saying what it is.
enum SwipeInk {
    static let dismiss = Color(red: 61 / 255, green: 122 / 255, blue: 61 / 255)
    /// Close ends the agent: the alarm's red, darkened to hold white text.
    static let close = Color(red: 150 / 255, green: 40 / 255, blue: 40 / 255)
    static let more = Color(red: 70 / 255, green: 70 / 255, blue: 70 / 255)
}
