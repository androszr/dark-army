import SwiftUI

/// The Comm tab on the Mac: talk to Mission Control, Dark Army's one
/// standing chief-of-staff session, which answers and acts, with the board beside it.
///
/// Two views. `CommRail` is the rail's content while `PanelView.Tab.comm`
/// is up — the status line, the four quick-question chips, a composer with
/// the card composer's dictation gate, Send, Open and an armed-then-confirmed
/// End. `MissionTerminalColumn` is the wide pane's trailing column: the same
/// session's live `TerminalPane`, drawn **only** on this tab so at most one
/// pane ever states `panel_terminal` for it and the pty's width keeps one
/// owner. The reply prose is not drawn here: the transcript is on screen.
///
/// Source of truth for what is drawn: `snapshot.mission` (which session,
/// alive) and `snapshot.agents.row(session:)` (the row's bucket). Nothing is
/// re-derived; the words are `CommRules`', byte-pinned to the phone.
struct CommRail: View {
    @ObservedObject var client: DaemonClient
    /// `PanelView.setStripEditing`: the key monitor stands aside while the
    /// composer holds the caret, or every letter typed is a triage verb.
    var onEditing: (Bool) -> Void = { _ in }

    @State private var draft = ""
    @State private var note = ""
    /// A verb is in flight — Send, Open or End — and the buttons wait.
    @State private var working = false
    /// Open alone is in flight: the only verb that rewrites the status
    /// word to "starting" (the phone's `opening` rule). Send and End keep
    /// the snapshot's word.
    @State private var opening = false
    @State private var endArmed = false
    @FocusState private var composerFocused: Bool

    private var mission: MissionSection { client.snapshot.mission }

    private var status: String {
        CommRules.status(
            available: mission.available, alive: mission.alive,
            exited: mission.exited, sessionId: mission.sessionId,
            rowState: client.snapshot.agents.row(session: mission.sessionId)?.1 ?? "")
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("# darkarmy · mission control")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                Text("Mission Control · \(opening ? "starting" : status)")
                    .font(Theme.mono(12, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .accessibilityLabel("Mission Control status: \(opening ? "starting" : status)")
                if !mission.available {
                    Text("This Mac's Dark Army is too old for Mission Control.")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    chips
                    composer
                    controls
                }
                if !note.isEmpty {
                    Text(note)
                        .font(Theme.mono(11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel("Dark Army says: \(note)")
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onChange(of: composerFocused) { _, focused in onEditing(focused) }
        .onDisappear {
            endArmed = false
            onEditing(false)
        }
    }

    private var chips: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(CommRules.chips.enumerated()), id: \.offset) { _, chip in
                Button { send(chip.question) } label: {
                    Text(chip.label.uppercased())
                        .font(Theme.mono(10, weight: .semibold))
                        .tracking(0.8)
                        .foregroundStyle(Theme.phosphor)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .disabled(!CommRules.canAsk(status: status) || working)
                .accessibilityLabel(chip.question)
                .clickable()
            }
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text("Ask")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                Spacer(minLength: 0)
                if DictateButton.available(client.context.settings) {
                    DictateButton(focus: { composerFocused = true })
                }
            }
            TextEditor(text: $draft)
                .font(.system(size: 12))
                .frame(minHeight: 60)
                .focused($composerFocused)
                .accessibilityLabel("Question for Mission Control")
        }
    }

    private var controls: some View {
        HStack(spacing: 8) {
            Button("Send") { send(draft) }
                .disabled(CommRules.line(from: draft).isEmpty
                          || !CommRules.canAsk(status: status) || working)
                .accessibilityLabel("Send the question to Mission Control")
            if CommRules.showsOpen(status: status) {
                Button("Open Mission Control") { open() }
                    .disabled(working)
            }
            Spacer(minLength: 0)
            if CommRules.showsEnd(status: status) {
                Button(endArmed ? "Really end?" : "End") { end() }
                    .tint(endArmed ? Color.red : nil)
                    .disabled(working)
                    .accessibilityLabel(endArmed ? "Confirm end Mission Control"
                                        : "End Mission Control")
            }
        }
        .controlSize(.small)
        .buttonStyle(.bordered)
    }

    /// Type one line into the mission session; the daemon's refusal is
    /// drawn verbatim under the composer.
    private func send(_ text: String) {
        let line = CommRules.line(from: text)
        let sid = mission.sessionId
        guard !line.isEmpty, !sid.isEmpty, !working else { return }
        working = true
        note = ""
        Task { @MainActor in
            let result = await client.terminalInput(session: sid, text: line)
            working = false
            if result.ok {
                if text == draft { draft = "" }
            } else {
                note = result.detail
            }
        }
    }

    private func open() {
        guard !working else { return }
        working = true
        opening = true
        note = ""
        Task { @MainActor in
            let result = await client.missionOpen()
            working = false
            opening = false
            if !result.ok { note = result.detail }
            await client.refresh()
        }
    }

    /// Armed then confirmed — `RowActions`' idiom for a destructive verb.
    private func end() {
        guard !working else { return }
        if !endArmed {
            endArmed = true
            return
        }
        endArmed = false
        working = true
        note = ""
        Task { @MainActor in
            let result = await client.missionEnd()
            working = false
            if !result.ok { note = result.detail }
            await client.refresh()
        }
    }
}

/// The wide pane's trailing column on the Comm tab: Mission Control's live
/// terminal beside the board, or the status sentence and an Open button.
struct MissionTerminalColumn: View {
    @ObservedObject var client: DaemonClient
    var onInputFocus: (Bool) -> Void = { _ in }
    var onTerminalFocus: (Bool) -> Void = { _ in }

    /// Open is the column's only verb, so in flight it *is* opening and
    /// the header may say "starting".
    @State private var opening = false
    @State private var note = ""

    private var mission: MissionSection { client.snapshot.mission }

    private var status: String {
        CommRules.status(
            available: mission.available, alive: mission.alive,
            exited: mission.exited, sessionId: mission.sessionId,
            rowState: client.snapshot.agents.row(session: mission.sessionId)?.1 ?? "")
    }

    /// The snapshot's row, or a stub carrying only the id — the pane is
    /// drawn before the row's first hook lands.
    private var missionAgent: Agent {
        client.snapshot.agents.row(session: mission.sessionId)?.0
            ?? Agent.stub(sessionId: mission.sessionId)
    }

    var body: some View {
        HStack(spacing: 0) {
            Rectangle().fill(Theme.rule).frame(width: 1)
            VStack(alignment: .leading, spacing: 0) {
                Text("# mission control · \(opening ? "starting" : status)")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .padding(.horizontal, 12)
                    .frame(height: PanelMetrics.terminalHeader, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if mission.alive, !mission.sessionId.isEmpty {
                    TerminalPane(agent: missionAgent, client: client,
                                 onInputFocus: onInputFocus,
                                 onTerminalFocus: onTerminalFocus)
                        .id(mission.sessionId)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(mission.available
                             ? "Mission Control is \(status). Open it to ask what the agents are doing."
                             : "This Mac's Dark Army is too old for Mission Control.")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                        if mission.available, CommRules.showsOpen(status: status) || mission.sessionId.isEmpty {
                            Button("Open Mission Control") { open() }
                                .controlSize(.small)
                                .buttonStyle(.bordered)
                                .disabled(opening || mission.alive)
                        }
                        if !note.isEmpty {
                            Text(note)
                                .font(Theme.mono(11))
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                }
            }
        }
        .background(Theme.bg)
    }

    private func open() {
        guard !opening else { return }
        opening = true
        note = ""
        Task { @MainActor in
            let result = await client.missionOpen()
            opening = false
            if !result.ok { note = result.detail }
            await client.refresh()
        }
    }
}
