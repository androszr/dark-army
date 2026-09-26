import SwiftUI

/// The numbers and the one ink decision behind the inbox's drawing.
///
/// A table rather than literals scattered through `body`: the whole
/// diagnosis this layout answers is that an entry's own lines sat as far
/// apart as two entries did, which is `entryVPad` against `textSpacing` and
/// is asserted as such.
enum InboxLayout {
    /// The portrait's edge, and the minimum height of the title line it
    /// sits beside — a one-line entry must not draw a face taller than its
    /// own text.
    static let face: CGFloat = 28
    /// Vertical padding inside an entry, above and below.
    static let entryVPad: CGFloat = 10
    /// Spacing inside an entry's text stack. Deliberately smaller than
    /// `entryVPad`: that contrast is what makes an entry read as one thing.
    static let textSpacing: CGFloat = 4
    /// The `AlarmOutline` size an inbox action is drawn at.
    static let actionSize: CGFloat = 10

    /// The one place an armed press's colour is decided. Red is never the
    /// only clue — the wording changes too — but it is unmissable.
    static func actionInk(armed: Bool) -> Color { armed ? Theme.alarm : Theme.phosphor }
}

/// The inbox, drawn. Every judgment is `Inbox`'s; this file only arranges it.
///
/// **The inbox routes; it does not answer.** The whole row is one press,
/// and that press opens the surface that can answer — the process table for
/// a session, the board for a card. The one control on a row is
/// **Dismiss**, and the one control above the list is **Dismiss all**; both
/// mean the same thing (hide until this changes) and neither answers,
/// moves, closes or deletes anything.
struct InboxView: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var board: BoardState
    let groups: [InboxGroup]
    let onOpen: (InboxItem) -> Void
    /// The face of the agent an entry is about, resolved by the caller
    /// against the fleet — the band's own `faceFor` shape, and for its
    /// reason: this view joins nothing, and an entry whose session the fleet
    /// has forgotten draws an empty slot rather than a stranger.
    var faceFor: (InboxItem) -> (character: String, state: Cast.State)? = { _ in nil }

    /// The one Dismiss-all press, armed then confirmed. A bulk hide is
    /// still a removal of what the panel was opened to read.
    @State private var dismissAllArmed = false

    /// Every drawn entry, in draw order — what Dismiss all is counted over.
    private var allItems: [InboxItem] { groups.flatMap(\.items) }

    var body: some View {
        if groups.isEmpty {
            VStack {
                Text("Nothing is waiting on you.")
                    .font(Theme.prose(14))
                    .foregroundStyle(Theme.faint)
                    .padding(12)
                Spacer()
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                header
                ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(groups) { group in
                        VStack(alignment: .leading, spacing: 0) {
                            // A heading only where there is something to
                            // tell apart: one project's entries under one
                            // project's name is a line that says nothing.
                            if groups.count > 1 { heading(group) }
                            // The rule is drawn *before* every entry but the
                            // first — in one place only, so no two entries
                            // can put two lines between them.
                            ForEach(Array(group.items.enumerated()),
                                    id: \.element.id) { index, item in
                                if index > 0 { entryDivider }
                                row(item)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: .infinity)
            }
        }
    }

    /// A project heading with a line under it, so it stops reading like
    /// another entry's title.
    private func heading(_ group: InboxGroup) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(group.heading.uppercased())
                .font(Theme.mono(10, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Theme.faint)
            Rectangle().fill(Theme.hair).frame(height: 1)
                .accessibilityHidden(true)
        }
        .padding(.bottom, 6)
    }

    /// The line between two entries. Decoration: it says nothing a screen
    /// reader has not already been told by the entries themselves.
    private var entryDivider: some View {
        Rectangle().fill(Theme.rule).frame(height: 1)
            .accessibilityHidden(true)
    }

    /// The list's own header: one number — the same one the top bar and the
    /// tab badge draw — and the one press that hides what can honestly be
    /// hidden. The press names its own scope, because a permission ask
    /// stays.
    @ViewBuilder
    private var header: some View {
        let items = allItems
        let dismissable = Inbox.dismissable(items)
        HStack(spacing: 8) {
            Text("\(items.count) WAITING ON YOU")
                .font(Theme.mono(10, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Color.red)
            Spacer(minLength: 0)
            if client.snapshot.inbox.available, !dismissable.isEmpty {
                Button(dismissAllArmed ? "Dismiss \(dismissable.count)?"
                                       : "Dismiss all") {
                    if dismissAllArmed {
                        dismissAllArmed = false
                        for item in dismissable { dismiss(item) }
                    } else {
                        dismissAllArmed = true
                    }
                }
                .buttonStyle(AlarmOutline(color: InboxLayout.actionInk(armed: dismissAllArmed),
                                          size: InboxLayout.actionSize))
                .clickable()
                .help("Hide every entry here until it changes. "
                      + "A permission ask stays: it still blocks its tool call.")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        // A changed list is a changed question: never fire a confirmation
        // that was armed against a different set of rows.
        .onChange(of: dismissable.count) { _, _ in dismissAllArmed = false }
    }

    /// One entry: the red edge, the face, the kind's one word, the subject,
    /// how long it has waited, a line of detail, and Dark Army's own words only
    /// where it has some. The whole entry is the press that opens it;
    /// Dismiss keeps its own smaller target and takes a press ahead of it.
    ///
    /// One clock drives the drawn age and the spoken one: a dated entry is
    /// drawn inside the shared second hand while the panel can be seen
    /// (`ProcessRow.ticking`'s shape), so VoiceOver reads the same "waiting"
    /// the row shows; hidden or undated, it is drawn once from the moment.
    @ViewBuilder
    private func row(_ item: InboxItem) -> some View {
        if item.since > 0, client.visible {
            TimelineView(Clocks.SecondHand()) { context in
                entry(item, now: context.date.timeIntervalSince1970)
            }
        } else {
            entry(item, now: Date().timeIntervalSince1970)
        }
    }

    private func entry(_ item: InboxItem, now: TimeInterval) -> some View {
        // A real button, so VoiceOver reads it as one and can press it; it
        // was a tap gesture over a content shape, which a screen reader read
        // as loose fragments and could not press. `Dismiss` stays a nested button
        // with its own target, and a screen reader also finds it among the
        // row's actions.
        Button(action: { onOpen(item) }) {
            HStack(alignment: .top, spacing: 8) {
                Rectangle()
                    .fill(Color.red)
                    .frame(width: 3)
                // Who this is about, at the size a face is actually recognised
                // at. An entry whose session the fleet has forgotten keeps the
                // slot empty, so every title in the list still shares one edge.
                if let face = faceFor(item) {
                    PixelMark(character: face.character, state: face.state,
                              size: InboxLayout.face)
                } else {
                    Color.clear
                        .frame(width: InboxLayout.face, height: InboxLayout.face)
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: InboxLayout.textSpacing) {
                    HStack(spacing: 6) {
                        Text(item.kind.word)
                            .font(Theme.mono(9, weight: .semibold))
                            .tracking(0.8)
                            .foregroundStyle(Theme.faint)
                        Text(item.title)
                            .font(Theme.prose(14, weight: .semibold))
                            .multilineTextAlignment(.leading)
                        Spacer(minLength: 0)
                        if item.since > 0 { waitedText(item, now: now) }
                        dismissButton(item)
                    }
                    // The block the face is aligned with is never shorter than
                    // the face, so a one-line entry draws no overhang.
                    .frame(minHeight: InboxLayout.face)
                    if !item.detail.isEmpty {
                        Text(item.detail)
                            .font(Theme.prose(12))
                            .foregroundStyle(Theme.faint)
                            .multilineTextAlignment(.leading)
                            .lineLimit(3)
                    }
                    let line = Inbox.sentence(for: item, refusal: refusal(item),
                                              queueReason: queueReason(item))
                    if !line.isEmpty {
                        Text(line)
                            .font(Theme.mono(11))
                            .foregroundStyle(refusal(item).isEmpty ? Theme.phosphor : .orange)
                            .multilineTextAlignment(.leading)
                    }
                }
            }
            .padding(.vertical, InboxLayout.entryVPad)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        // Tabbed onto, the entry opens on Return as well as Space: on this
        // platform Return presses the default button, never a focused one.
        // Only while the entry itself is focused — a Return on its focused
        // Dismiss stays Dismiss's (`ClaimsKeyboardFocus`).
        .reportsKeyboardFocus(onPress: { onOpen(item) })
        .accessibilityLabel(Inbox.spokenLabel(
            for: item,
            waited: FleetAge.spoken(startedAt: item.since, now: now),
            sentence: Inbox.sentence(for: item, refusal: refusal(item),
                                     queueReason: queueReason(item))))
        .accessibilityActions {
            if item.wire.dismissable, client.snapshot.inbox.available {
                Button(Verbs.dismiss.label) { dismiss(item) }
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// How long this has been waiting, on the fleet's own scan-form clock
    /// (`12s` / `5m` / `1h`), at the moment `row` hands it. Undated draws
    /// nothing at all — a dash would read as "no time", and the honest
    /// statement is silence.
    private func waitedText(_ item: InboxItem, now: TimeInterval) -> some View {
        Text(FleetAge.text(startedAt: item.since, now: now))
            .font(Theme.mono(10).monospacedDigit())
            .foregroundStyle(Theme.faint)
            .accessibilityLabel(
                "waiting " + FleetAge.spoken(startedAt: item.since, now: now))
    }

    /// The row's one control, absent where the daemon would refuse it:
    /// on a permission ask, and against a daemon too old to hold a hide
    /// list.
    @ViewBuilder
    private func dismissButton(_ item: InboxItem) -> some View {
        if item.wire.dismissable, client.snapshot.inbox.available {
            Button("Dismiss") { dismiss(item) }
                .buttonStyle(AlarmOutline(color: InboxLayout.actionInk(armed: false),
                                          size: InboxLayout.actionSize))
                .clickable()
                .reportsKeyboardFocus()
                .help("Hide this until it changes. Nothing is answered, moved or closed.")
        }
    }

    /// The card behind a card entry, re-resolved against the current snapshot
    /// rather than captured at draw time — the same re-check every other press
    /// on a card makes.
    private func card(_ item: InboxItem) -> BoardCard? {
        guard !item.cardId.isEmpty else { return nil }
        return client.snapshot.board.cards.first { $0.id == item.cardId }
    }

    private func refusal(_ item: InboxItem) -> String {
        board.refusals[refusalSlot(item)] ?? ""
    }

    /// Cards keep the store id; session rows use the target key so a 409
    /// is not written to the empty `cardId` and swallowed.
    private func refusalSlot(_ item: InboxItem) -> String {
        item.cardId.isEmpty ? item.target.key : item.cardId
    }

    private func queueReason(_ item: InboxItem) -> String {
        card(item)?.queueReason ?? ""
    }

    /// Hide one entry until its subject changes — the daemon's `inbox_ack`,
    /// fingerprinted against what is live *now*, so a press on a stale
    /// entry is refused in the daemon's words rather than hiding the
    /// entry that replaced it.
    func dismiss(_ item: InboxItem) {
        let kind = item.wire.name
        let fp = liveFingerprint(item)
        write(refusalSlot(item)) {
            await client.inboxAck(key: item.target.key, kind: kind, fingerprint: fp)
        }
    }

    private func liveFingerprint(_ item: InboxItem) -> String {
        switch item.wire {
        case .question:
            let questions: [AgentQuestion]
            if !item.sessionId.isEmpty,
               let (agent, _) = client.snapshot.agents.row(session: item.sessionId) {
                questions = agent.questionList
            } else {
                questions = []
            }
            return InboxFingerprint.value(wire: item.wire, questions: questions)
        default:
            return InboxFingerprint.value(wire: item.wire, card: card(item))
        }
    }

    /// One shape for every write: run it, keep the refusal against the
    /// entry, then ask for a fresh reading.
    private func write(_ slot: String,
                       _ run: @escaping () async -> ActionResult) {
        Task { @MainActor in
            let result = await run()
            board.refusals[slot] = result.ok ? "" : result.detail
            await client.refresh()
        }
    }
}
