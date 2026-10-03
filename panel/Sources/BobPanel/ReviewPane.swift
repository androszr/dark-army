import SwiftUI

/// What the Review tab holds between its two views: the form the rail draws
/// and the run the wide pane has open. Constructed once in `PanelView`
/// (`@StateObject`), so a rail rebuild never wipes a half-made choice or the
/// ticks on a checklist.
@MainActor
final class ReviewState: ObservableObject {
    // The form.
    @Published var root = ""
    @Published var tool = ReviewRules.defaultProvider
    /// The ticked after-steps. Every step starts unticked: the tick is the
    /// permission, and a default must never tick one.
    @Published var ticked: Set<String> = []
    @Published var offer: ReviewOffer?
    @Published var checking = false
    @Published var starting = false
    @Published var startRefusal = ""

    // The open run.
    @Published var openRunId = ""
    /// The finding numbers ticked for fixing.
    @Published var picks: Set<Int> = []
    @Published var continueArmed = false
    @Published var endArmed = false
    @Published var working = false
    @Published var runRefusal = ""

    /// Open a run in the wide pane (`""` closes it). Anything armed or
    /// ticked belonged to the run before.
    func open(runId: String) {
        if runId != openRunId { picks = [] }
        openRunId = runId
        continueArmed = false
        endArmed = false
        runRefusal = ""
    }

    func close() { open(runId: "") }

    /// Fetch the offer for the chosen project; a stale answer (the choice
    /// moved while it was in flight) is dropped.
    func refreshOffer(client: DaemonClient) {
        let wanted = root
        ticked = []
        offer = nil
        guard !wanted.isEmpty else { checking = false; return }
        checking = true
        Task { @MainActor in
            let answer = await client.reviewOffer(root: wanted)
            guard root == wanted else { return }
            checking = false
            offer = answer ?? ReviewOffer()
        }
    }

    func start(client: DaemonClient) {
        guard !starting else { return }
        starting = true
        startRefusal = ""
        let steps = (offer?.steps ?? []).map(\.id).filter { ticked.contains($0) }
        let root = self.root, tool = self.tool
        Task { @MainActor in
            let result = await client.reviewStart(root: root, tool: tool, steps: steps)
            starting = false
            if result.ok {
                ticked = []
                open(runId: result.detail)
                refreshOffer(client: client)
            } else {
                startRefusal = result.detail
            }
            await client.refresh()
        }
    }

    func proceed(runId: String, client: DaemonClient) {
        guard !working else { return }
        if !continueArmed { continueArmed = true; return }
        continueArmed = false
        working = true
        runRefusal = ""
        let fix = ReviewRules.fixList(picks)
        Task { @MainActor in
            let result = await client.reviewContinue(runId: runId, fix: fix)
            working = false
            if !result.ok { runRefusal = result.detail }
            await client.refresh()
        }
    }

    func end(runId: String, client: DaemonClient) {
        guard !working else { return }
        if !endArmed { endArmed = true; return }
        endArmed = false
        working = true
        runRefusal = ""
        Task { @MainActor in
            let result = await client.reviewEnd(runId: runId)
            working = false
            if !result.ok { runRefusal = result.detail }
            await client.refresh()
        }
    }
}

/// The wide pane on the Review tab: the open run's header, its findings as a
/// checklist, Continue, the step ledger, an armed End and the run's live
/// terminal. The terminal is drawn **only** on this tab and only once the
/// run has a session, so at most one pane ever states `panel_terminal` for
/// it. Everything drawn is the snapshot's: the state word, the scope
/// sentence, the findings and the ledger are the daemon's, verbatim.
struct ReviewPane: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: ReviewState
    var onInputFocus: (Bool) -> Void = { _ in }
    var onTerminalFocus: (Bool) -> Void = { _ in }

    private var run: ReviewRun? {
        client.snapshot.review.run(id: state.openRunId)
    }

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                if let run {
                    details(run)
                } else {
                    Text("Pick a run from the list, or start a review.")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .padding(14)
                        .frame(maxWidth: .infinity, maxHeight: .infinity,
                               alignment: .topLeading)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            if let run {
                Rectangle().fill(Theme.rule).frame(width: 1)
                terminal(run)
                    .frame(width: PanelMetrics.commTerminalWidth)
            }
        }
        .background(Theme.bg)
    }

    private func details(_ run: ReviewRun) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("# review · \(run.project.isEmpty ? run.root : run.project) · \(ProviderMark.displayName(run.tool))")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                Text(ReviewRules.stateWord(run.state))
                    .font(Theme.mono(13, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .accessibilityLabel("Review state: \(ReviewRules.stateWord(run.state))")
                Text(run.scopeLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                if !run.verdict.isEmpty {
                    Text("VERDICT: \(run.verdict)")
                        .font(Theme.mono(11, weight: .semibold))
                        .foregroundStyle(run.verdict == "STOP" ? Color.red : Theme.phosphor)
                }
                if !run.error.isEmpty {
                    Text(run.error)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.amber)
                }
                if run.state == "reviewing" {
                    Text("The review is running in the terminal. The findings appear here when it is done.")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !run.findings.isEmpty { checklist(run) }
                if run.truncated {
                    Text("More findings than fit here — the rest are in the terminal.")
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                }
                if ReviewRules.canContinue(state: run.state) { proceed(run) }
                if !run.steps.isEmpty { steps(run) }
                if !run.ledger.isEmpty { ledger(run) }
                if !state.runRefusal.isEmpty {
                    Text(state.runRefusal)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel("Dark Army says: \(state.runRefusal)")
                }
                if ReviewRules.showsEnd(state: run.state) {
                    Button(state.endArmed ? "Really end?" : "End") {
                        state.end(runId: run.id, client: client)
                    }
                    .controlSize(.small)
                    .buttonStyle(.bordered)
                    .tint(state.endArmed ? Color.red : nil)
                    .disabled(state.working)
                    .accessibilityLabel(state.endArmed ? "Confirm end this review"
                                        : "End this review")
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func checklist(_ run: ReviewRun) -> some View {
        let editable = ReviewRules.picksEditable(state: run.state)
        let shown = editable ? state.picks : Set(run.picks)
        return VStack(alignment: .leading, spacing: 8) {
            Text("Findings — tick the ones to fix")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
            ForEach(run.findings) { finding in
                Toggle(isOn: Binding(
                    get: { shown.contains(finding.index) },
                    set: { on in
                        if on { state.picks.insert(finding.index) }
                        else { state.picks.remove(finding.index) }
                        state.continueArmed = false
                    })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ReviewRules.findingLine(grade: finding.grade, line: finding.line))
                            .font(Theme.mono(11, weight: .medium))
                            .foregroundStyle(Theme.phosphorBright)
                            .fixedSize(horizontal: false, vertical: true)
                        if !finding.where.isEmpty {
                            Text(finding.where)
                                .font(Theme.mono(10))
                                .foregroundStyle(Theme.faint)
                        }
                        if !finding.fix.isEmpty {
                            Text(finding.fix)
                                .font(Theme.mono(10))
                                .foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .toggleStyle(.checkbox)
                .disabled(!editable)
                .accessibilityLabel(ReviewRules.findingLine(grade: finding.grade, line: finding.line))
            }
        }
    }

    private func proceed(_ run: ReviewRun) -> some View {
        HStack(spacing: 8) {
            Button(state.continueArmed ? "Really continue?" : "Continue") {
                state.proceed(runId: run.id, client: client)
            }
            .controlSize(.small)
            .buttonStyle(.bordered)
            .tint(state.continueArmed ? Color.red : nil)
            .disabled(state.working)
            .accessibilityLabel(state.continueArmed
                                ? "Confirm continue with \(state.picks.count) fixes"
                                : "Continue with the ticked fixes")
            Text(state.picks.isEmpty ? "no fixes ticked"
                 : "\(state.picks.count) to fix")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
        }
    }

    private func steps(_ run: ReviewRun) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Steps")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
            ForEach(run.steps) { step in
                Text(ReviewRules.stepLine(label: step.label, status: step.status,
                                          words: step.words))
                    .font(Theme.mono(11))
                    .foregroundStyle(step.status == "failed" ? Color.red : Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func ledger(_ run: ReviewRun) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Report")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
            ForEach(run.ledger) { line in
                Text(ReviewRules.ledgerLine(id: line.id, status: line.status, words: line.words))
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func terminal(_ run: ReviewRun) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("# terminal")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .padding(.horizontal, 12)
                .frame(height: PanelMetrics.terminalHeader, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            if run.sessionId.isEmpty {
                Text("starting…")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .padding(12)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            } else {
                TerminalPane(
                    agent: client.snapshot.agents.row(session: run.sessionId)?.0
                        ?? Agent.stub(sessionId: run.sessionId),
                    client: client,
                    onInputFocus: onInputFocus,
                    onTerminalFocus: onTerminalFocus)
                    .id(run.sessionId)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}
