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

    var body: some View {
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
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        .decryptSurface("CommView")
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
