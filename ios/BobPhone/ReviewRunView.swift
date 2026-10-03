import SwiftUI

/// One Review run on the phone: the header, the findings as a checklist
/// (ticks enabled only while the run waits on picks), Continue, the steps and
/// the ledger the run reported, an armed End and the run's live terminal.
///
/// Source of truth: the run row of `snapshot.review`; the state word, the
/// scope sentence, the findings and the ledger are the Mac's, verbatim.
/// Continue rides `enqueue` after an arm, End the same; the Terminal button
/// opens `PhoneTerminalPane` in a full-screen cover as Comm does, shown only
/// once the run has a session.
struct ReviewRunView: View {
    @ObservedObject var client: PhoneClient
    let runId: String

    @StateObject private var arm = Arm()
    @State private var picks: Set<Int> = []
    @State private var note = ""
    @State private var terminalShown = false

    private var run: ReviewRun? { client.snapshot.review.run(id: runId) }
    private var continueScope: String { "review:continue:\(runId)" }
    private var endScope: String { "review:end:\(runId)" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let run {
                    header(run)
                    if !run.findings.isEmpty { checklist(run) }
                    if run.truncated {
                        Text("More findings than fit here — the rest are in the terminal.")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if ReviewRules.canContinue(state: run.state) { proceed(run) }
                    if !run.steps.isEmpty { steps(run) }
                    if !run.ledger.isEmpty { ledger(run) }
                    controls(run)
                    if !note.isEmpty {
                        Text(note)
                            .font(Theme.mono(12))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityLabel("Dark Army says: \(note)")
                    }
                } else {
                    Text("This review is no longer on the Mac.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .decryptSurface("ReviewRunView")
        .onDisappear { arm.disarm() }
        .fullScreenCover(isPresented: $terminalShown) {
            terminalScreen
                .environment(\.decryptActive, true)
                .onAppear { client.watchTerminal(run?.sessionId) }
                .onDisappear { client.watchTerminal(nil) }
        }
    }

    // MARK: the parts

    private func header(_ run: ReviewRun) -> some View {
        let name = run.project.isEmpty ? run.root : run.project
        return VStack(alignment: .leading, spacing: 6) {
            Text("# review · \(name)")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            Text(ReviewRules.stateWord(run.state))
                .font(Theme.mono(14, weight: .medium))
                .foregroundStyle(Theme.phosphorBright)
                .accessibilityLabel("Review state: \(ReviewRules.stateWord(run.state))")
            Text(run.scopeLine)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            if !run.verdict.isEmpty {
                Text("VERDICT: \(run.verdict)")
                    .font(Theme.mono(12, weight: .semibold))
                    .foregroundStyle(run.verdict == "STOP" ? Theme.alarm : Theme.phosphor)
            }
            if !run.error.isEmpty {
                Text(run.error)
                    .font(Theme.mono(12))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if run.state == "reviewing" {
                Text("The review is running on the Mac. The findings appear here when it is done.")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func checklist(_ run: ReviewRun) -> some View {
        let editable = ReviewRules.picksEditable(state: run.state)
        let shown = editable ? picks : Set(run.picks)
        return VStack(alignment: .leading, spacing: 4) {
            Text("Findings — tick the ones to fix")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            ForEach(run.findings) { finding in
                let on = shown.contains(finding.index)
                let line = ReviewRules.findingLine(grade: finding.grade, line: finding.line)
                DecryptButton(action: {
                    guard editable else { return }
                    if on { picks.remove(finding.index) } else { picks.insert(finding.index) }
                    arm.disarm()
                }) {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: on ? "checkmark.square" : "square")
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(line)
                                .font(Theme.mono(13, weight: .medium))
                                .fixedSize(horizontal: false, vertical: true)
                            if !finding.where.isEmpty {
                                Text(finding.where)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.faint)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            if !finding.fix.isEmpty {
                                Text(finding.fix)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.dim)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(on ? Theme.phosphorBright : Theme.phosphor)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!editable)
                .accessibilityElement(children: .combine)
                .accessibilityLabel(line)
                .accessibilityValue(on ? "ticked" : "not ticked")
                .accessibilityAddTraits(.isButton)
            }
        }
    }

    private func proceed(_ run: ReviewRun) -> some View {
        let armed = arm.reviewContinue == run.id
        return VStack(alignment: .leading, spacing: 4) {
            DecryptButton(action: { proceedPressed(run) }) {
                Text(client.queueMark(for: continueScope)
                     ?? (armed ? "Really continue?" : "Continue"))
                    .font(Theme.mono(13))
                    .foregroundStyle(armed ? Theme.alarm : Theme.phosphor)
                    .frame(minHeight: 44)
            }
            .accessibilityLabel(armed
                ? "Confirm continue with \(picks.count) fixes, step two of two"
                : "Continue with the ticked fixes, step one of two")
            Text(picks.isEmpty ? "no fixes ticked" : "\(picks.count) to fix")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            if let queued = client.queueNote(for: continueScope)?.text, !queued.isEmpty {
                Text(queued)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    // Drawn is read: the refusal closes as refused, not as pending.
                    .onAppear { client.readQueueNote(for: continueScope) }
                    .onChange(of: queued) { _, _ in client.readQueueNote(for: continueScope) }
            }
        }
    }

    private func steps(_ run: ReviewRun) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Steps")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            ForEach(run.steps) { step in
                Text(ReviewRules.stepLine(label: step.label, status: step.status,
                                          words: step.words))
                    .font(Theme.mono(12))
                    .foregroundStyle(step.status == "failed" ? Theme.alarm : Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func ledger(_ run: ReviewRun) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Report")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            ForEach(run.ledger) { line in
                Text(ReviewRules.ledgerLine(id: line.id, status: line.status, words: line.words))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func controls(_ run: ReviewRun) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            buttons(run)
            if let queued = client.queueNote(for: endScope)?.text, !queued.isEmpty {
                Text(queued)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    // Drawn is read: the refusal closes as refused, not as pending.
                    .onAppear { client.readQueueNote(for: endScope) }
                    .onChange(of: queued) { _, _ in client.readQueueNote(for: endScope) }
            }
        }
    }

    private func buttons(_ run: ReviewRun) -> some View {
        AdaptiveStack(stacked: false, spacing: 8) {
            if !run.sessionId.isEmpty {
                DecryptButton(action: { terminalShown = true }) {
                    Text("Terminal")
                        .font(Theme.mono(13))
                        .frame(minHeight: 44)
                }
                .accessibilityLabel("Open this review's live terminal")
            }
            Spacer(minLength: 0)
            if ReviewRules.showsEnd(state: run.state) {
                let armed = arm.reviewEnd == run.id
                DecryptButton(role: armed ? .destructive : nil, action: { endPressed(run) }) {
                    Text(client.queueMark(for: endScope) ?? (armed ? "Really end?" : "End"))
                        .font(Theme.mono(13))
                        .foregroundStyle(armed ? Theme.alarm : Theme.dim)
                        .frame(minHeight: 44)
                }
                .accessibilityLabel(armed ? "Confirm end this review, step two of two"
                                    : "End this review, step one of two")
            }
        }
    }

    // MARK: the cover

    private var terminalScreen: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                DecryptButton(action: { dismissTerminal() }) {
                    Text("Done")
                        .font(Theme.mono(13))
                        .frame(minHeight: 44)
                }
                .accessibilityLabel("Done with terminal, return to the review")
                Spacer(minLength: 8)
                Text("review")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 14)
            Rectangle().fill(Theme.hair).frame(height: 1)
            PhoneTerminalPane(agent: terminalAgent, client: client)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.bg)
        .foregroundStyle(Theme.phosphor)
        .overlay(ScanlineOverlay())
    }

    /// The snapshot's row, or a stub carrying only the id — the pane is
    /// drawn before the row's first hook lands.
    private var terminalAgent: Agent {
        let sid = run?.sessionId ?? ""
        return client.snapshot.agents.row(session: sid)?.0 ?? Agent.stub(sessionId: sid)
    }

    private func dismissTerminal() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder),
                                        to: nil, from: nil, for: nil)
        terminalShown = false
    }

    // MARK: the presses

    private func proceedPressed(_ run: ReviewRun) {
        guard arm.confirm(.reviewContinue, id: run.id) else {
            arm.arm(.reviewContinue, id: run.id)
            return
        }
        note = ""
        let fix = ReviewRules.fixList(picks).map(String.init).joined(separator: ",")
        Task { @MainActor in
            let result = await client.enqueue(
                action: PhoneActions.reviewContinue,
                fields: ["run_id": run.id, "fix": fix],
                scope: continueScope)
            if !result.ok { note = result.detail }
        }
    }

    private func endPressed(_ run: ReviewRun) {
        guard arm.confirm(.reviewEnd, id: run.id) else {
            arm.arm(.reviewEnd, id: run.id)
            return
        }
        note = ""
        Task { @MainActor in
            let result = await client.enqueue(
                action: PhoneActions.reviewEnd,
                fields: ["run_id": run.id], scope: endScope)
            if !result.ok { note = result.detail }
        }
    }
}
