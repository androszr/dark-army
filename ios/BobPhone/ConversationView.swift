import SwiftUI

struct ConversationScreen<Header: View>: View {
    let agent: Agent
    let stopped: Bool
    @ObservedObject var client: PhoneClient
    var retainedReply: PhoneReplyDraft? = nil
    /// Sheet still above the turns. Empty at every other call site.
    /// Not inside the turn list's scroll: the opening frame keeps the
    /// still, and the jump to the latest line stays in `messageList`.
    private let header: Header
    @State private var expanded: Set<Int>
    @State private var atBottom: Bool
    @State private var onScreen: Bool
    /// How many of the newest turns are drawn. The cache holds the whole
    /// conversation; the list draws a window of it, widened by
    /// `windowStep` at a time from the top.
    @State private var window: Int
    /// Zero until the column has given the list a real height. The
    /// opening scroll in a zero-height list does not land.
    @State private var listHeight: CGFloat
    // Computed, not stored: a generic type cannot hold a static stored
    // property, and the archive build refuses it.
    static var windowStep: Int { 300 }
    /// The list's vertical padding plus one line of a turn. Reserved
    /// before the still is offered its ideal height.
    static var oneTurn: CGFloat { 34 }

    init(agent: Agent, stopped: Bool, client: PhoneClient,
         retainedReply: PhoneReplyDraft? = nil,
         @ViewBuilder header: () -> Header) {
        self.agent = agent
        self.stopped = stopped
        self.client = client
        self.retainedReply = retainedReply
        self.header = header()
        self._expanded = State(initialValue: [])
        self._atBottom = State(initialValue: true)
        self._onScreen = State(initialValue: false)
        self._window = State(initialValue: Self.windowStep)
        self._listHeight = State(initialValue: 0)
    }

    private var turns: [ConversationTurn] {
        client.conversationCache.turns(agent.sessionId)
    }

    private var drawn: ArraySlice<ConversationTurn> {
        turns.suffix(window)
    }

    private var nickname: String {
        agent.nickname.isEmpty ? String(agent.sessionId.prefix(8)) : agent.nickname
    }

    private var catchingUp: Bool {
        client.conversationWatching == agent.sessionId
            && client.conversationCache.nextSeq(agent.sessionId)
            < (client.conversationCache.conversations[agent.sessionId]?.total ?? 0)
    }

    var body: some View {
        ConversationColumn(turnFloor: Self.oneTurn) {
            // A container, not the lines themselves: an empty status is
            // still one child, so the column's five slots stay put.
            VStack(alignment: .leading, spacing: 0) { statusLines }
            // One turn is reserved before this row takes sheet size.
            // `minHeight: 0` lets the photograph yield. The name under
            // it is not clipped off while the picture stays full size.
            // A container for the same reason: an `EmptyView` header,
            // frame and all, is no child, and four slots is the fallback.
            VStack(spacing: 0) { header }
                .frame(maxWidth: .infinity, minHeight: 0, alignment: .top)
            messageList
                .frame(maxWidth: .infinity, minHeight: Self.oneTurn,
                       maxHeight: .infinity)
            Rectangle().fill(Theme.hair).frame(height: 1)
            // Scrolls inside its share of the column: an interview's
            // option cards can be taller than the whole sheet once the
            // verbs sit above the tabs, and a box that cannot scroll could
            // only overflow onto the lead, the verbs and the tabs.
            BoundedAnswer {
                AnswerBox(agent: agent, stopped: stopped, client: client,
                          terminalWins: false, retainedReply: retainedReply)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
            }
            .layoutPriority(2)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Nothing the column holds may paint above its own top edge.
        .clipped()
        .onAppear {
            onScreen = true
            client.watchConversation(agent.sessionId)
            Task { await client.catchUpConversation(agent.sessionId) }
        }
        .onDisappear {
            // Only this screen's own watch: a sheet closing over another
            // agent's screen must not clear the one underneath, whose
            // `onAppear` may already have run.
            onScreen = false
            client.watchConversation(nil, ifWatching: agent.sessionId)
        }
        .onChange(of: client.conversationWatching) { _, watching in
            // Back in front with the watch gone: take it again.
            guard onScreen, watching == nil else { return }
            client.watchConversation(agent.sessionId)
        }
        .onChange(of: agent.sessionId) { _, sid in
            window = Self.windowStep
            client.watchConversation(sid)
            Task { await client.catchUpConversation(sid) }
        }
    }

    /// The turns, in the height left under the still. Opening scrolls
    /// this list to the latest line, which is then inside the visible strip.
    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if turns.count > window {
                        earlierControl
                    }
                    ForEach(drawn) { turn in
                        ConversationTurnRow(
                            turn: turn,
                            nickname: nickname,
                            expanded: expanded.contains(turn.seq),
                            result: result(for: turn),
                            onToggle: { toggle(turn.seq) }
                        )
                        .id(turn.seq)
                        .onAppear {
                            if turn.seq == turns.last?.seq { atBottom = true }
                        }
                        .onDisappear {
                            if turn.seq == turns.last?.seq { atBottom = false }
                        }
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // Behind the scroll, so this reader is not the list's height.
            .background {
                GeometryReader { geo in
                    Color.clear
                        .onAppear { noteListHeight(geo.size.height, proxy: proxy) }
                        .onChange(of: geo.size.height) { _, height in
                            noteListHeight(height, proxy: proxy)
                        }
                }
            }
            .onAppear {
                if let last = turns.last?.seq {
                    proxy.scrollTo(last, anchor: .bottom)
                }
            }
            .onChange(of: turns.count) { _, _ in
                guard atBottom, let last = turns.last?.seq else { return }
                proxy.scrollTo(last, anchor: .bottom)
            }
        }
    }

    /// A zero-height scroll cannot land on the latest line. Once the
    /// column gives the list a height, jump there again.
    private func noteListHeight(_ height: CGFloat, proxy: ScrollViewProxy) {
        let opened = listHeight == 0 && height > 0
        if height != listHeight { listHeight = height }
        guard opened, let last = turns.last?.seq else { return }
        proxy.scrollTo(last, anchor: .bottom)
    }

    /// The turns above the drawn window, one press for another
    /// `windowStep` of them.
    private var earlierControl: some View {
        let hidden = turns.count - window
        return DecryptButton(action: { window += Self.windowStep }) {
            Text("// earlier… \(hidden) more turns")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Show \(min(hidden, Self.windowStep)) earlier turns")
    }

    @ViewBuilder private var statusLines: some View {
        if catchingUp {
            HStack(spacing: 8) {
                Text(catchingLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                AgentChatterView(.caret, wait: .opening, seed: agent.sessionId,
                                 spoken: "Catching up on the conversation")
                    .id(agent.sessionId)
            }
            .padding(.horizontal, 14)
            .padding(.top, 6)
        }
        if let first = turns.first, first.seq > 0 {
            Text("// earlier turns are not kept on this phone")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.top, 6)
        }
        if !client.snapshot.board.conversationSupported {
            Text("// older Mac — update Dark Army to read conversations here")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.top, 6)
        }
        if let reason = client.conversationUnavailable[agent.sessionId],
           !reason.isEmpty, turns.isEmpty {
            Text("// no conversation yet")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.top, 4)
            Text(reason)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
        }
    }

    private var catchingLine: String {
        let held = client.conversationCache.conversations[agent.sessionId]
        let next = held?.nextSeq ?? 0
        let total = held?.total ?? 0
        return "// catching up · \(next) of \(total) turns"
    }

    private func result(for turn: ConversationTurn) -> ConversationTurn? {
        guard turn.kind == "tool", expanded.contains(turn.seq),
              !turn.callId.isEmpty else { return nil }
        return turns.first { $0.kind == "result" && $0.callId == turn.callId }
    }

    private func toggle(_ seq: Int) {
        if expanded.contains(seq) { expanded.remove(seq) }
        else { expanded.insert(seq) }
    }
}

extension ConversationScreen where Header == EmptyView {
    init(agent: Agent, stopped: Bool, client: PhoneClient,
         retainedReply: PhoneReplyDraft? = nil) {
        self.init(agent: agent, stopped: stopped, client: client,
                  retainedReply: retainedReply, header: { EmptyView() })
    }
}

/// Answer at its ideal, then one turn, then the still in what remains.
/// The still yields first. A non-finite probe is answered with a finite
/// size — returning the infinity SwiftUI offers would trap the arithmetic.
private struct ConversationColumn: Layout {
    var turnFloor: CGFloat
    /// The most of the column the answer box may take before it scrolls.
    static let answerShare: CGFloat = 0.5

    private func finite(_ value: CGFloat) -> CGFloat {
        value.isFinite ? max(0, value) : 0
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let width = finite(proposal.width ?? 0)
        if let height = proposal.height, height.isFinite, height > 0 {
            return CGSize(width: width, height: height)
        }
        let ideal = subviews.reduce(CGFloat(0)) { partial, sub in
            partial + finite(sub.sizeThatFits(
                ProposedViewSize(width: width, height: nil)).height)
        }
        return CGSize(width: width, height: ideal)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        let width = finite(bounds.width > 0 ? bounds.width : (proposal.width ?? 0))
        guard subviews.count == 5 else {
            var y = bounds.minY
            for subview in subviews {
                let h = finite(subview.sizeThatFits(
                    ProposedViewSize(width: width, height: nil)).height)
                subview.place(at: CGPoint(x: bounds.minX, y: y),
                              anchor: .topLeading,
                              proposal: ProposedViewSize(width: width, height: h))
                y += h
            }
            return
        }
        let open = ProposedViewSize(width: width, height: nil)
        let statusH = finite(subviews[0].sizeThatFits(open).height)
        let headerIdeal = finite(subviews[1].sizeThatFits(open).height)
        let hairH = finite(subviews[3].sizeThatFits(open).height)
        let height = finite(bounds.height)
        // The answer takes its ideal up to half of what the status and the
        // hairline leave, and scrolls past that (`BoundedAnswer`). Taking
        // its ideal whole, with the column shifted up by the overflow, is
        // what drew an interview's options over the tabs (22 Sep 2026).
        let answerRoom = max(0, height - statusH - hairH)
        let answerH = min(finite(subviews[4].sizeThatFits(open).height),
                          answerRoom * Self.answerShare)
        let room = max(0, height - statusH - hairH - answerH)
        let reserved = min(finite(turnFloor), room)
        let still = min(headerIdeal, max(0, room - reserved))
        let list = max(0, room - still)
        var y = bounds.minY
        let heights = [statusH, still, list, hairH, answerH]
        for (subview, item) in zip(subviews, heights) {
            subview.place(at: CGPoint(x: bounds.minX, y: y),
                          anchor: .topLeading,
                          proposal: ProposedViewSize(width: width, height: item))
            y += item
        }
    }
}

/// The answer box, scrolling once `ConversationColumn` gives it less than
/// its ideal height. Asked with no height, a vertical scroll view reports
/// its content's ideal height, so the column still measures the box whole.
private struct BoundedAnswer<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ScrollView(.vertical) { content }
            .scrollBounceBehavior(.basedOnSize)
    }
}

struct ConversationTurnRow: View {
    let turn: ConversationTurn
    let nickname: String
    let expanded: Bool
    let result: ConversationTurn?
    let onToggle: () -> Void

    var body: some View {
        let clock = ConversationRows.clock(turn.ts)
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                rowBody
                Spacer(minLength: 8)
                if !clock.isEmpty {
                    Text(clock)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                }
            }
            if expanded, let result {
                Text(ConversationRows.resultLine(result))
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(ConversationRows.spoken(turn, nickname: nickname))
    }

    @ViewBuilder private var rowBody: some View {
        switch turn.kind {
        case "user":
            Text("> \(turn.text)")
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphorBright)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        case "agent":
            VStack(alignment: .leading, spacing: 2) {
                Text(nickname)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                MarkdownText(source: turn.text, base: 13, mono: true)
                    .equatable()
            }
        case "tool":
            DecryptButton(action: onToggle) {
                Text(ConversationRows.toolLine(turn))
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .buttonStyle(.plain)
            if expanded {
                Text(turn.brief)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        case "result":
            EmptyView()
        case "note":
            Text(turn.text)
                .font(Theme.mono(12))
                .italic()
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        default:
            Text(turn.text)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
