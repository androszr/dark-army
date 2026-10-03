import SwiftUI

/// The Review tab's rail: pick a project, read in one line what will be
/// reviewed, pick the provider (Claude is pre-selected) and the after-steps,
/// press Start, and below it the runs, newest first. The wide pane
/// (`ReviewPane`) draws the open run.
///
/// Source of truth: `snapshot.review` for the runs, the daemon's offer for the
/// scope sentence and the steps, `snapshot.board` for the providers.
/// Nothing is re-derived; the words are `ReviewRules`', byte-pinned to the
/// phone.
struct ReviewRail: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: ReviewState

    private var review: ReviewSection { client.snapshot.review }
    private var board: Board { client.snapshot.board }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("# darkarmy · review")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                if !review.available {
                    Text(ReviewRules.olderMacNote)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    form
                    runs
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onChange(of: state.root) { _, _ in state.refreshOffer(client: client) }
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("Project", selection: $state.root) {
                if state.root.isEmpty { Text("Choose a project…").tag("") }
                ForEach(AdhocSpawn.candidates(client.snapshot.enrollment.enrolled)) { project in
                    Text(project.label.isEmpty ? project.root : project.label)
                        .tag(project.root)
                }
            }
            .font(Theme.mono(12))
            scopeLine
            HStack(spacing: 8) {
                Text("Assistant")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                if ProviderChoice.wordChipOnly(tools: board.tools, selected: state.tool) {
                    Text(state.tool.isEmpty ? "—" : state.tool)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 2)
                        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
                        .accessibilityLabel(ProviderChoice.groupLabel)
                        .accessibilityValue(ProviderChoice.value(
                            selected: state.tool, name: ProviderMark.displayName))
                } else {
                    ProviderSwitch(tools: board.tools, installed: board.installed,
                                   selected: state.tool, interactive: true,
                                   showsNobody: false,
                                   pick: { state.tool = $0 })
                }
                Spacer(minLength: 0)
            }
            stepsList
            HStack(spacing: 8) {
                Button("Start") { state.start(client: client) }
                    .controlSize(.small)
                    .buttonStyle(.bordered)
                    .disabled(state.starting || !ReviewRules.canStart(
                        root: state.root, tool: state.tool,
                        supported: review.available,
                        dispatchEnabled: board.dispatchEnabled))
                    .accessibilityLabel("Start the review")
                if !board.dispatchEnabled {
                    Text("Dark Army is not allowed to start sessions (see the ⋯ menu)")
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !state.startRefusal.isEmpty {
                Text(state.startRefusal)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("Dark Army says: \(state.startRefusal)")
            }
        }
    }

    @ViewBuilder private var scopeLine: some View {
        if state.root.isEmpty {
            EmptyView()
        } else if state.checking {
            Text("checking…")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
        } else if let offer = state.offer {
            Text(offer.available ? offer.scopeLine
                 : (offer.reason.isEmpty ? "Dark Army could not read this project." : offer.reason))
                .font(Theme.mono(11))
                .foregroundStyle(offer.available ? Theme.dim : Theme.amber)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private var stepsList: some View {
        if let offer = state.offer, offer.available, !offer.steps.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("After the review")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                ForEach(offer.steps) { step in
                    Toggle(isOn: Binding(
                        get: { state.ticked.contains(step.id) },
                        set: { on in
                            if on { state.ticked.insert(step.id) }
                            else { state.ticked.remove(step.id) }
                        })) {
                        Text(step.label)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.phosphorBright)
                    }
                    .toggleStyle(.checkbox)
                    .accessibilityLabel("Step: \(step.label)")
                }
            }
        }
    }

    private var runs: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !review.runs.isEmpty {
                Text("Runs")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            ForEach(review.runs.sorted { $0.startedAt > $1.startedAt }) { run in
                Button { state.open(runId: run.id) } label: {
                    HStack(spacing: 6) {
                        Text(run.project.isEmpty ? run.root : run.project)
                            .font(Theme.mono(11, weight: state.openRunId == run.id ? .semibold : .regular))
                            .foregroundStyle(state.openRunId == run.id ? Theme.phosphorBright : Theme.phosphor)
                        Text(ReviewRules.stateWord(run.state))
                            .font(Theme.mono(10))
                            .foregroundStyle(run.state == "picks" ? Theme.amber : Theme.faint)
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("\(run.project), \(ReviewRules.stateWord(run.state))")
            }
        }
    }
}
