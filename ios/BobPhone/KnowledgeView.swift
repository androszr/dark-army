import SwiftUI

/// Read-only list of one enrolled project's notes. Picker over
/// `snapshot.enrollment.enrolled` only. No Confirm / Edit / Mark stale.
struct KnowledgeView: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @ObservedObject var client: PhoneClient
    @State private var root = ""
    @State private var report = KnowledgeReport()
    @State private var loading = false
    @State private var detail = ""

    private var enrolled: [EnrolledProject] {
        client.snapshot.enrollment.enrolled
    }

    var body: some View {
        List {
            if enrolled.isEmpty {
                Text("No project is enrolled")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.faint)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            } else {
                Picker(selection: $root.decrypting(decryptFeedback)) {
                    ForEach(enrolled) { project in
                        Text(project.label.isEmpty ? project.root : project.label)
                            .tag(project.root)
                    }
                } label: {
                    Text("project")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                }
                .accessibilityLabel("project")
                .font(Theme.mono(12))
                .tint(Theme.phosphor)
                .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)

                if !detail.isEmpty {
                    Text(detail)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.amber)
                        .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                        .listRowSeparator(.hidden)
                        .listRowBackground(Theme.bg)
                }

                ForEach(report.entries) { entry in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 8) {
                            Text(entry.key)
                                .font(Theme.mono(12, weight: .semibold))
                                .foregroundStyle(Theme.phosphor)
                            Text(entry.sourceLabel)
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.dim)
                            if entry.isStale {
                                Text(entry.staleBadge)
                                    .font(Theme.mono(11, weight: .semibold))
                                    .foregroundStyle(Theme.amber)
                            }
                        }
                        if !entry.question.isEmpty {
                            Text(entry.question)
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.phosphorBright)
                        }
                        Text(entry.answer)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(entry.isUnconfirmed
                             ? entry.unconfirmedLabel
                             : confirmedLine(entry.lastConfirmed))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("knowledge")
        .decryptSurface("KnowledgeView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .task {
            if root.isEmpty { root = enrolled.first?.root ?? "" }
            await load()
        }
        .onChange(of: root) { _, _ in
            Task { await load() }
        }
    }

    private func confirmedLine(_ stamp: Double) -> String {
        let date = Date(timeIntervalSince1970: stamp)
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return "confirmed \(formatter.string(from: date))"
    }

    private func load() async {
        guard !root.isEmpty else {
            report = KnowledgeReport()
            detail = ""
            return
        }
        let requested = root
        report = KnowledgeReport()
        loading = true
        let fetched = await client.knowledgeReport(root: requested)
        guard let outcome = KnowledgeLoad.apply(
            requested: requested, current: root, fetched: fetched)
        else { return }
        loading = false
        report = outcome.report
        detail = outcome.detail
    }
}
