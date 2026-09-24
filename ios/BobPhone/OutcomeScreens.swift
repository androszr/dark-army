import SwiftUI

struct PhoneOutcomeCard: View {
    @ObservedObject var client: PhoneClient
    let card: BoardCard
    @State private var requests = OutcomeRequestState()
    private var report: OutcomeReport? { requests.report }
    @State private var lastFetch = Date.distantPast
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let report {
                OutcomeReadView(report: report)
                if let next = report.nextOffset {
                    DecryptButton("Earlier decisions") { Task { await load(offset: next) } }
                        .accessibilityHint("Loads the page before this one")
                }
            } else { Text("Outcome information unavailable").font(Theme.mono(12)) }
        }
        .task(id: "\(card.id)-\(card.outcomeRevision)") { await load() }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            if Date().timeIntervalSince(lastFetch) >= 30 { Task { await load() } }
        }
    }
    private var currentRevision: Int {
        client.snapshot.board.cards.first(where: { $0.id == card.id })?.outcomeRevision ?? card.outcomeRevision
    }
    private func load(offset: Int = 0) async {
        lastFetch = Date()
        let wanted = card.id
        let request = requests.begin(cardId: wanted, revision: currentRevision, offset: offset)
        guard let fresh = await client.outcomeReport(cardId: wanted, offset: request.offset),
              !Task.isCancelled else { return }
        requests.apply(fresh, request: request, currentCardId: card.id, currentRevision: currentRevision)
    }
}

struct PhoneOutcomeProjects: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @ObservedObject var client: PhoneClient
    @State private var root = ""
    @State private var expanded = false
    @State private var report: OutcomeReport?
    @State private var lastFetch = Date.distantPast
    var body: some View {
        // The hint belongs to the *header*, never to the group. SwiftUI
        // propagates a label, value, hint or trait put on a container that is
        // not itself an element down onto everything inside it — on the group
        // this hint reached the Project picker and every row of the report,
        // describing none of them.
        DisclosureGroup(isExpanded: $expanded.decrypting(decryptFeedback)) {
            Picker("Project", selection: $root.decrypting(decryptFeedback)) {
                Text("Choose a project").tag("")
                ForEach(client.snapshot.board.projects, id: \.root) { project in
                    Text(project.name).tag(project.root)
                }
            }
            .accessibilityLabel("Project")
            if let report { ScrollView { OutcomeSummaryView(report: report) }.frame(maxHeight: 260) }
        } label: {
            Text("Project outcomes · last 30 UTC days")
                .accessibilityHint(expanded ? "Hides the project outcome report"
                                            : "Shows the project outcome report")
        }
        .font(Theme.mono(12))
        .padding(8)
        .task(id: "\(expanded)-\(root)") { await load() }
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
