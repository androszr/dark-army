import SwiftUI

struct OutcomeEditor: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: BoardState
    let card: BoardCard
    @State private var requests = OutcomeRequestState()
    private var report: OutcomeReport? { requests.report }
    @State private var evidence = ""
    @State private var reason = ""
    @State private var note = ""
    @State private var key = UUID().uuidString
    @State private var lastFetch = Date.distantPast
    private var draftMatches: Bool { state.draft.outcomeRevision == report?.card?.revision }
    private var hasObjectiveEdits: Bool { state.draft.outcome != report?.card?.objective }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Benefit and success").font(.headline)
            if let report, report.available {
                TextField("Who benefits", text: $state.draft.outcome.beneficiary)
                TextField("Intended benefit", text: $state.draft.outcome.intendedBenefit, axis: .vertical)
                TextField("Success criterion", text: $state.draft.outcome.successCriterion, axis: .vertical)
                TextField("Later check (YYYY-MM-DD, optional)", text: $state.draft.outcome.checkOn)
                if report.card?.accepted == true && hasObjectiveEdits {
                    Toggle("Confirm objective change and remove current acceptance", isOn: $state.draft.confirmOutcomeScopeChange)
                }
                Text("Save the objective before recording a decision. Done and Reviewed do not accept an outcome.").font(.caption)
                OutcomeReadView(report: report)
                if let next = report.nextOffset {
                    Button("Earlier decisions") { Task { await load(offset: next) } }
                }
                if !draftMatches {
                    Text("The outcome changed. Your draft is preserved. Review the current objective and evidence above.").foregroundStyle(.orange)
                    Button("Use current revision, keep my draft") {
                        if let revision = report.card?.revision { state.draft.outcomeRevision = revision }
                        key = UUID().uuidString
                    }
                }
                TextField("Evidence of success (required to accept)", text: $evidence, axis: .vertical)
                Button("Accept outcome") { Task { await decide(accept: true) } }
                    .disabled(evidence.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !draftMatches || hasObjectiveEdits)
                TextField("Reason for revision (required)", text: $reason, axis: .vertical)
                Button("Request revision") { Task { await decide(accept: false) } }
                    .disabled(reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !draftMatches || hasObjectiveEdits)
                Text("These decisions change no column and start no work.").font(.caption)
            } else { Text("Outcome information unavailable").foregroundStyle(.orange) }
            if !note.isEmpty { Text(note).foregroundStyle(.orange) }
        }
        .font(Theme.mono(12))
        .disabled(state.outcomeSaving)
        .task(id: "\(card.id)-\(card.outcomeRevision)") { await load() }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            if Date().timeIntervalSince(lastFetch) >= 30 { Task { await load() } }
        }
    }

    private var currentRevision: Int {
        max(state.draft.outcomeRevision,
            client.snapshot.board.cards.first(where: { $0.id == card.id })?.outcomeRevision ?? card.outcomeRevision)
    }

    private func load(offset: Int = 0) async {
        lastFetch = Date()
        let wanted = card.id
        let request = requests.begin(cardId: wanted, revision: currentRevision, offset: offset)
        guard let fresh = await client.outcomeReport(cardId: wanted, offset: request.offset),
              !Task.isCancelled, state.editing == wanted,
              requests.apply(fresh, request: request, currentCardId: state.editing ?? "",
                             currentRevision: currentRevision) else { return }
        state.applyOutcomeReport(fresh, cardId: wanted,
                                 expectedRevision: max(request.revision, fresh.card?.revision ?? 0))
    }

    private func decide(accept: Bool) async {
        guard !state.outcomeSaving, draftMatches, !hasObjectiveEdits else { return }
        state.outcomeSaving = true
        defer { state.outcomeSaving = false }
        let wanted = card.id
        let submittedRevision = state.draft.outcomeRevision
        requests.invalidate(cardId: wanted, revision: submittedRevision)
        let reply = await client.outcomeWrite(
            action: accept ? "board_accept_outcome" : "board_request_revision", cardId: wanted,
            fields: ["expected_outcome_revision": String(submittedRevision),
                     "request_key": key, "evidence": accept ? evidence : reason])
        guard state.applyOutcomeDecision(reply, cardId: wanted, expectedRevision: submittedRevision) else { return }
        requests.invalidate(cardId: wanted, revision: state.draft.outcomeRevision)
        note = reply.detail
        if reply.ok, reply.revision != nil {
            key = UUID().uuidString
            evidence = ""
            reason = ""
        }
        await client.refresh()
        // A stale refusal retains both text drafts, including evidence.
        guard state.editing == wanted else { return }
        await load()
    }
}

struct MacOutcomeProjects: View {
    @ObservedObject var client: DaemonClient
    @State private var root = ""
    @State private var expanded = false
    @State private var report: OutcomeReport?
    @State private var lastFetch = Date.distantPast
    var body: some View {
        DisclosureGroup("Project outcomes · last 30 UTC days", isExpanded: $expanded) {
            Picker("Project", selection: $root) {
                Text("Choose a project").tag("")
                ForEach(client.snapshot.board.projects, id: \.root) { project in
                    Text(project.name).tag(project.root)
                }
            }
            if let report { OutcomeSummaryView(report: report) }
        }
        .padding(8)
        .task(id: "\(expanded)-\(root)") { await load() }
        .onChange(of: client.snapshot.board) { _, _ in if expanded { Task { await load() } } }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            if expanded && Date().timeIntervalSince(lastFetch) >= 30 { Task { await load() } }
        }
    }
    private func load() async {
        guard expanded, !root.isEmpty else { report = nil; return }
        lastFetch = Date()
        let wanted = root
        let fresh = await client.outcomeReport(root: wanted)
        guard !Task.isCancelled, root == wanted else { return }
        report = fresh
    }
}
