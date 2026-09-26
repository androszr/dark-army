import SwiftUI

/// The Comm tab: talk to Mission Control, Dark Army's one standing
/// chief-of-staff session on the Mac — it answers and, when asked, acts — in plain words.
///
/// One scrolling column: the identity line, the status word, then — when
/// that word is off or ended — Open Mission Control on its own row, the
/// latest reply as readable prose (the row's `lastSummary`, else `lastText`,
/// drawn the way the agent sheet draws a hosted session's last message —
/// wrapping fully, never clipped), the four quick-question chips, a composer
/// with the existing microphone, Send, Terminal (the live emulator as a
/// full-screen cover) and an armed-then-confirmed End.
///
/// **Open-on-appear.** Mission Control is started the first time the tab is
/// visited and thereafter merely found alive: where the Mac says
/// `mission.available && !mission.alive`, one `mission_open` is posted per
/// appearance (`openedThisVisit`), never on a timer. A refusal is drawn in
/// the Mac's own words under Open Mission Control, which leads the column
/// when the status is off or ended. Nothing here remembers a session id —
/// the row is re-read from the snapshot by `mission.sessionId` on every
/// body pass, so a `/clear` successor follows.
///
/// **Helpers.** While Mission Control has helpers running, a tab strip sits
/// under the identity line — Mission Control, then one tab per helper
/// (`CommHelperTabs`). A helper's tab is its own conversation, read-only
/// and paged from the helper's journal (`ConversationSubject`'s
/// `<session>#<agent>` key on the one watched conversation); the chips, the
/// composer and End stay on Mission Control's tab, because a helper takes
/// no typed line. A helper that finishes while its tab is open keeps it.
///
/// Every word of state is `CommRules`' (byte-pinned to the Mac); every write
/// rides `client.post` — the receipt token, home-sealed or relay-first —
/// and `terminal_input` here is the **text** route: one line, then Enter.
struct CommView: View {
    @ObservedObject var client: PhoneClient
    /// Whether this tab is the one showing. Open-on-appear also fires when
    /// the person switches back to it, so the check runs per visit.
    let selected: Bool
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    @State private var draft = ""
    @State private var note = ""
    @State private var opening = false
    @State private var sending = false
    @State private var openedThisVisit = false
    @State private var terminalShown = false
    /// The helper tab showing, by agent id; `""` is Mission Control.
    @State private var helper = ""
    /// The label the selected helper had while live, so its tab keeps a
    /// name after it finishes.
    @State private var helperLabel = ""
    @StateObject private var arm = Arm()
    @FocusState private var composerFocused: Bool

    private var mission: MissionSection { client.snapshot.mission }

    private var row: (Agent, String)? {
        client.snapshot.agents.row(session: mission.sessionId)
    }

    private var status: String {
        if opening { return "starting" }
        return CommRules.status(
            available: mission.available, alive: mission.alive,
            exited: mission.exited, sessionId: mission.sessionId,
            rowState: row?.1 ?? "")
    }

    /// The snapshot's row, or a stub carrying only the id — the cover is
    /// drawn before the row's first hook lands.
    private var missionAgent: Agent {
        row?.0 ?? Agent.stub(sessionId: mission.sessionId)
    }

    private var helperTabs: [CommHelperTabs.Tab] {
        let live = (row?.0.subagentRows ?? []).map { (id: $0.agentId, label: $0.label) }
        return CommHelperTabs.tabs(
            live: live, selected: helper, selectedLabel: helperLabel,
            supported: client.snapshot.board.subagentConversationSupported
                && mission.alive)
    }

    private var selectedHelper: SubagentRow? {
        row?.0.subagentRows.first { $0.agentId == helper }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            let tabs = helperTabs
            if !tabs.isEmpty {
                tabStrip(tabs)
            }
            if !helper.isEmpty, tabs.contains(where: { $0.id == helper }) {
                HelperConversationPane(
                    client: client,
                    session: mission.sessionId,
                    agentId: helper,
                    label: CommHelperTabs.label(helperLabel, id: helper),
                    row: selectedHelper,
                    selected: selected)
                    .id(helper)
            } else {
                missionColumn
            }
        }
        .background(Theme.bg)
        .onAppear {
            openedThisVisit = false
            openIfNeeded(force: false)
        }
        .onChange(of: selected) { _, isSelected in
            if isSelected {
                openedThisVisit = false
                openIfNeeded(force: false)
            } else {
                arm.disarm()
            }
        }
        .onDisappear { arm.disarm() }
        .fullScreenCover(isPresented: $terminalShown) {
            terminalScreen
                .environment(\.decryptActive, true)
                .onAppear { client.watchTerminal(mission.sessionId) }
                .onDisappear { client.watchTerminal(nil) }
        }
        .onChange(of: helperTabs.map(\.id)) { _, _ in
            let next = CommHelperTabs.selection(helper, tabs: helperTabs)
            if next != helper { helper = next }
        }
        .onChange(of: selectedHelper?.label ?? "") { _, label in
            if !label.isEmpty { helperLabel = label }
        }
    }

    /// Mission Control, then each helper. Scrolls sideways when the names
    /// outgrow the width; the showing tab is bright and underlined.
    private func tabStrip(_ tabs: [CommHelperTabs.Tab]) -> some View {
        VStack(spacing: 0) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 4) {
                    ForEach(tabs, id: \.id) { tab in
                        let on = tab.id == helper
                        DecryptButton(action: { choose(tab) }) {
                            VStack(spacing: 3) {
                                Text(tab.live ? tab.label : "\(tab.label) · done")
                                    .font(Theme.mono(12, weight: on ? .semibold : .regular))
                                    .foregroundStyle(on ? Theme.phosphorBright : Theme.dim)
                                Rectangle()
                                    .fill(on ? Theme.phosphorBright : Color.clear)
                                    .frame(height: 2)
                            }
                            .padding(.horizontal, 8)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(tab.id.isEmpty ? "Mission Control"
                                            : "Helper \(tab.label)\(tab.live ? "" : ", finished")")
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
                .padding(.horizontal, 10)
            }
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
    }

    private func choose(_ tab: CommHelperTabs.Tab) {
        helper = tab.id
        helperLabel = tab.id.isEmpty ? "" : tab.label
        composerFocused = false
    }

    private var missionColumn: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("# darkarmy · mission control")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .accessibilityAddTraits(.isHeader)
                Text("Mission Control · \(status)")
                    .font(Theme.mono(13, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("Mission Control status: \(status)")
                if CommRules.showsOpen(status: status) {
                    DecryptButton(action: { openIfNeeded(force: true) }) {
                        Text("Open Mission Control")
                            .font(Theme.mono(13))
                            .frame(minHeight: 44)
                    }
                    .disabled(opening)
                    refusal
                }
                if !mission.available {
                    Text(Self.olderMacSentence)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    reply
                    chips
                    composer
                    controls
                }
                if !CommRules.showsOpen(status: status) {
                    refusal
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        .decryptSurface("CommView")
    }

    static let olderMacSentence = "This Mac's Dark Army is too old for Mission Control."

    /// The Mac's refusal, in its own words. Under Open Mission Control when
    /// that button is leading; otherwise at the foot, where Send and End
    /// report. The leading button is the retry — there is no second one.
    @ViewBuilder
    private var refusal: some View {
        if !note.isEmpty {
            Text(note)
                .font(Theme.mono(12))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("Dark Army says: \(note)")
        }
    }

    // MARK: the reply

    @ViewBuilder
    private var reply: some View {
        if let (agent, bucket) = row {
            let summary = agent.lastSummary.trimmingCharacters(in: .whitespacesAndNewlines)
            let text = agent.lastText.trimmingCharacters(in: .whitespacesAndNewlines)
            if !summary.isEmpty {
                Text(summary)
                    .font(Theme.mono(13))
                    .foregroundStyle(Theme.dim)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !text.isEmpty {
                MarkdownText(source: text, base: 13, mono: true)
                    .equatable()
                    .foregroundStyle(Theme.dim)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("— nothing yet —")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if bucket == "running" {
                AgentChatterView(.line, wait: .sending, seed: agent.sessionId,
                                 spoken: "Mission Control is thinking")
                    .id(agent.sessionId)
            }
        } else {
            Text("— nothing yet —")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: the chips

    private var chips: some View {
        AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize, spacing: 8) {
            ForEach(Array(CommRules.chips.enumerated()), id: \.offset) { _, chip in
                DecryptButton(action: { send(chip.question) }) {
                    Text(chip.label.uppercased())
                        .font(Theme.mono(11, weight: .semibold))
                        .foregroundStyle(Theme.phosphor)
                        .padding(.horizontal, 8)
                        .frame(minHeight: 44)
                        .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!CommRules.canAsk(status: status) || sending)
                .accessibilityLabel(chip.question)
            }
        }
    }

    // MARK: the composer

    private var composer: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text("Ask")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                Spacer(minLength: 0)
                MicButton(id: "comm.ask", text: $draft)
            }
            TextField("", text: $draft,
                      prompt: Text("What do you want to know?").foregroundStyle(Theme.faint),
                      axis: .vertical)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphor)
                .textInputAutocapitalization(.sentences)
                .focused($composerFocused)
                .lineLimit(5...)
                .fieldWell(focused: composerFocused, multiline: true,
                           onTap: { composerFocused = true })
                .accessibilityLabel("Question for Mission Control")
        }
    }

    // MARK: the controls

    private var controls: some View {
        AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize, spacing: 8) {
            DecryptButton(action: { send(draft) }) {
                Text(sending ? "SENDING…" : "Send")
                    .font(Theme.mono(13))
                    .frame(minHeight: 44)
            }
            .disabled(CommRules.line(from: draft).isEmpty
                      || !CommRules.canAsk(status: status) || sending)
            .accessibilityLabel("Send the question to Mission Control")
            if mission.alive, !mission.sessionId.isEmpty {
                DecryptButton(action: { terminalShown = true }) {
                    Text("Terminal")
                        .font(Theme.mono(13))
                        .frame(minHeight: 44)
                }
                .accessibilityLabel("Open Mission Control's live terminal")
            }
            Spacer(minLength: 0)
            if CommRules.showsEnd(status: status) {
                let armed = arm.missionEnd != nil
                DecryptButton(role: armed ? .destructive : nil, action: { end() }) {
                    Text(armed ? "Really end?" : "End")
                        .font(Theme.mono(13))
                        .foregroundStyle(armed ? Theme.alarm : Theme.dim)
                        .frame(minHeight: 44)
                }
                .accessibilityLabel(armed ? "Confirm end Mission Control, step two of two"
                                    : "End Mission Control, step one of two")
            }
        }
    }

    // MARK: the cover

    /// The live screen under one Done row. Done resigns the emulator before
    /// the cover comes down (`AgentDetailView.dismissTerminal`'s two lines).
    private var terminalScreen: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                DecryptButton(action: { dismissTerminal() }) {
                    Text("Done")
                        .font(Theme.mono(13))
                        .frame(minHeight: 44)
                }
                .accessibilityLabel("Done with terminal, return to Comm")
                Spacer(minLength: 8)
                Text("mission control")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 14)
            Rectangle().fill(Theme.hair).frame(height: 1)
            PhoneTerminalPane(agent: missionAgent, client: client)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.bg)
        .foregroundStyle(Theme.phosphor)
        .overlay(ScanlineOverlay())
    }

    private func dismissTerminal() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder),
                                        to: nil, from: nil, for: nil)
        terminalShown = false
    }

    // MARK: the writes

    /// One `mission_open` per appearance where the Mac says it is not
    /// alive; `force` is the Open press, which is a gesture of its own
    /// and never gated on the visit.
    private func openIfNeeded(force: Bool) {
        guard selected || force else { return }
        guard mission.available, !mission.alive, !opening else { return }
        if !force {
            guard !openedThisVisit else { return }
            openedThisVisit = true
        }
        opening = true
        note = ""
        Task { @MainActor in
            let result = await client.post(action: PhoneActions.missionOpen)
            opening = false
            if !result.ok { note = result.detail }
        }
    }

    /// A chip is Send with the chip's question as the line.
    private func send(_ text: String) {
        let line = CommRules.line(from: text)
        let sid = mission.sessionId
        guard !line.isEmpty, !sid.isEmpty, !sending else { return }
        sending = true
        note = ""
        Task { @MainActor in
            let result = await client.post(
                action: PhoneActions.terminalInput,
                fields: ["session_id": sid, "text": line])
            sending = false
            if result.ok {
                if text == draft { draft = "" }
            } else {
                note = result.detail
            }
        }
    }

    private func end() {
        guard arm.confirm(.missionEnd) else {
            arm.arm(.missionEnd)
            return
        }
        note = ""
        Task { @MainActor in
            let result = await client.post(action: PhoneActions.missionEnd)
            if !result.ok { note = result.detail }
        }
    }
}

/// One of Mission Control's helpers, read-only: who it is, what it is on,
/// then its own turns — the brief it was handed first — drawn with the
/// conversation screen's rows. It watches `<session>#<agent>` on the one
/// watched conversation, so the ordinary check-in keeps it current; it
/// takes the watch on appear and gives back only its own.
struct HelperConversationPane: View {
    @ObservedObject var client: PhoneClient
    let session: String
    let agentId: String
    let label: String
    /// The live row; nil once the helper has finished.
    let row: SubagentRow?
    /// Whether the Comm tab is the one showing.
    let selected: Bool

    @State private var expanded: Set<Int> = []
    @State private var openRuns: Set<Int> = []
    @State private var atBottom = true

    private var key: String { ConversationSubject.key(session: session, agent: agentId) }

    private var turns: [ConversationTurn] { client.conversationCache.turns(key) }

    private var rows: [ConversationFold.Row] {
        ConversationFold.rows(Array(turns.suffix(300)))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            lead
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
            Rectangle().fill(Theme.hair).frame(height: 1)
            list
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        .onAppear { take() }
        .onDisappear { client.watchConversation(nil, ifWatching: key) }
        .onChange(of: selected) { _, isSelected in
            if isSelected { take() } else { client.watchConversation(nil, ifWatching: key) }
        }
        .onChange(of: client.conversationWatching) { _, watching in
            // Back in front with the watch gone: take it again.
            guard selected, watching == nil else { return }
            take()
        }
    }

    private func take() {
        guard selected, !session.isEmpty else { return }
        client.watchConversation(key)
        let own = key
        Task { await client.catchUpConversation(own) }
    }

    private var lead: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("helper · \(label)\(row == nil ? " · done" : "")")
                .font(Theme.mono(13, weight: .medium))
                .foregroundStyle(Theme.phosphorBright)
                .fixedSize(horizontal: false, vertical: true)
            if let row, !row.description.isEmpty {
                Text(row.description)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let row, !row.activity.isEmpty {
                HStack(spacing: 8) {
                    Text(row.activity)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                    AgentChatterView(.caret, wait: .sending, seed: agentId,
                                     spoken: "\(label) is working")
                        .id(agentId)
                }
            }
            Text("read-only · a helper takes no typed line")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
        }
        .accessibilityElement(children: .combine)
    }

    private var list: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if turns.isEmpty {
                        Text(emptyLine)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    let shown = rows
                    ForEach(shown) { item in
                        rowView(item)
                            .id(item.id)
                            .onAppear { if item.id == shown.last?.id { atBottom = true } }
                            .onDisappear { if item.id == shown.last?.id { atBottom = false } }
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onAppear {
                if let last = rows.last?.id { proxy.scrollTo(last, anchor: .bottom) }
            }
            .onChange(of: turns.count) { _, _ in
                guard atBottom, let last = rows.last?.id else { return }
                proxy.scrollTo(last, anchor: .bottom)
            }
        }
    }

    private var emptyLine: String {
        if let reason = client.conversationUnavailable[key], !reason.isEmpty {
            return "// no messages yet — \(reason)"
        }
        return "// no messages yet"
    }

    @ViewBuilder private func rowView(_ item: ConversationFold.Row) -> some View {
        switch item {
        case .turn(let turn):
            ConversationTurnRow(
                turn: turn, nickname: label,
                expanded: expanded.contains(turn.seq),
                result: result(for: turn),
                onToggle: { toggle(turn.seq) })
        case .run(let tools):
            ConversationRunRow(
                tools: tools,
                open: ConversationFold.runIsOpen(tools, id: item.id,
                                                 openRuns: openRuns, expanded: expanded),
                nickname: label,
                expanded: expanded,
                result: { result(for: $0) },
                onToggleRun: {
                    if ConversationFold.runIsOpen(tools, id: item.id,
                                                  openRuns: openRuns, expanded: expanded) {
                        openRuns.remove(item.id)
                        for tool in tools { expanded.remove(tool.seq) }
                    } else {
                        openRuns.insert(item.id)
                    }
                },
                onToggleTool: { toggle($0) })
        }
    }

    private func result(for turn: ConversationTurn) -> ConversationTurn? {
        guard turn.kind == "tool", expanded.contains(turn.seq),
              !turn.callId.isEmpty else { return nil }
        return turns.first { $0.kind == "result" && $0.callId == turn.callId }
    }

    private func toggle(_ seq: Int) {
        if expanded.contains(seq) { expanded.remove(seq) } else { expanded.insert(seq) }
    }
}
