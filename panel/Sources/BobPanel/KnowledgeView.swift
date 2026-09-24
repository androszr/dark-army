import SwiftUI

/// CRT list of one enrolled project's notes. Confirm / Edit / Mark stale
/// are absent when `knowledgeWritable` is false.
struct KnowledgeView: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: KnowledgeWindowState

    private var writable: Bool { true }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(state.root)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .textSelection(.enabled)
            if !state.detail.isEmpty {
                Text(state.detail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .textSelection(.enabled)
            }
            if state.loading && state.report.entries.isEmpty {
                Text("loading…")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(state.report.entries) { entry in
                        note(entry)
                    }
                }
            }
        }
        .padding(16)
        .background(Theme.bg)
        .task(id: state.root) {
            await Self.load(client: client, state: state)
        }
    }

    @ViewBuilder
    private func note(_ entry: KnowledgeEntry) -> some View {
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
                Spacer(minLength: 0)
            }
            if !entry.question.isEmpty {
                Text(entry.question)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphorBright)
                    .textSelection(.enabled)
            }
            Text(entry.answer)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .textSelection(.enabled)
            Text(entry.isUnconfirmed
                 ? entry.unconfirmedLabel
                 : confirmedLine(entry.lastConfirmed))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            if writable {
                controls(entry)
            }
        }
        .padding(10)
        .background(Theme.card)
        .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
    }

    @ViewBuilder
    private func controls(_ entry: KnowledgeEntry) -> some View {
        HStack(spacing: 10) {
            Button(state.confirmArmed == entry.key
                   ? "Confirm — really?" : "Confirm") {
                if state.confirmArmed == entry.key {
                    state.confirmArmed = nil
                    Task { await confirm(entry.key) }
                } else {
                    state.confirmArmed = entry.key
                    state.staleArmed = nil
                }
            }
            .buttonStyle(.plain)
            .clickable()
            .foregroundStyle(Theme.phosphor)
            .font(Theme.mono(11))
            Button(state.staleArmed == entry.key
                   ? "Mark stale — really?" : "Mark stale") {
                if state.staleArmed == entry.key {
                    state.staleArmed = nil
                    Task { await markStale(entry.key) }
                } else {
                    state.staleArmed = entry.key
                    state.confirmArmed = nil
                }
            }
            .buttonStyle(.plain)
            .clickable()
            .foregroundStyle(Theme.amber)
            .font(Theme.mono(11))
            Button(state.editingKey == entry.key ? "Close editor" : "Edit") {
                if state.editingKey == entry.key {
                    state.editingKey = nil
                } else {
                    state.editingKey = entry.key
                    state.editQuestion = entry.question
                    state.editAnswer = entry.answer
                }
            }
            .buttonStyle(.plain)
            .clickable()
            .foregroundStyle(Theme.phosphor)
            .font(Theme.mono(11))
        }
        .font(Theme.mono(11))
        if state.editingKey == entry.key {
            TextField("question", text: $state.editQuestion)
                .textFieldStyle(.plain)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .padding(6)
                .background(Theme.well)
            TextEditor(text: $state.editAnswer)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .scrollContentBackground(.hidden)
                .frame(minHeight: 80)
                .padding(6)
                .background(Theme.well)
            Button("Save") {
                Task { await save(entry.key) }
            }
            .buttonStyle(.plain)
            .clickable()
            .foregroundStyle(Theme.phosphor)
            .font(Theme.mono(11, weight: .semibold))
        }
    }

    private func confirmedLine(_ stamp: Double) -> String {
        let date = Date(timeIntervalSince1970: stamp)
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return "confirmed \(formatter.string(from: date))"
    }

    static func load(client: DaemonClient, state: KnowledgeWindowState) async {
        let root = state.root
        guard !root.isEmpty else { return }
        await MainActor.run {
            state.loading = true
            state.report = KnowledgeReport()
            state.confirmArmed = nil
            state.staleArmed = nil
            state.editingKey = nil
        }
        let fetched = await client.knowledgeReport(root: root)
        await MainActor.run {
            guard let outcome = KnowledgeLoad.apply(
                requested: root, current: state.root, fetched: fetched)
            else { return }
            state.loading = false
            state.report = outcome.report
            state.detail = outcome.detail
        }
    }

    private func confirm(_ key: String) async {
        guard KnowledgeLoad.mayWrite(
            report: state.report, root: state.root, key: key) else { return }
        let result = await client.knowledgeConfirm(root: state.root, key: key)
        await afterWrite(result)
    }

    private func markStale(_ key: String) async {
        guard KnowledgeLoad.mayWrite(
            report: state.report, root: state.root, key: key) else { return }
        let result = await client.knowledgeStale(root: state.root, key: key)
        await afterWrite(result)
    }

    private func save(_ key: String) async {
        guard KnowledgeLoad.mayWrite(
            report: state.report, root: state.root, key: key) else { return }
        let result = await client.knowledgeEdit(
            root: state.root, key: key,
            question: state.editQuestion, answer: state.editAnswer)
        if result.ok {
            await MainActor.run { state.editingKey = nil }
        }
        await afterWrite(result)
    }

    private func afterWrite(_ result: ActionResult) async {
        await MainActor.run {
            state.detail = result.ok ? "" : result.detail
        }
        if result.ok {
            await Self.load(client: client, state: state)
        }
    }
}
