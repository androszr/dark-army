import AppKit
import SwiftUI

/// A plain text press with the pointing-hand cursor: the area picker's
/// adapter (`AreaChoiceButton`, `Cursor.swift`) by another name, so the
/// Checks window and the card window's check file add no cursor call sites
/// to `test_panel_cursor`'s budget.
typealias ManualCheckButton<Label: View> = AreaChoiceButton<Label>

/// The Checks section: every enrolled project's manual checks, open first
/// then newest first, with a search box over title, steps and outcome and
/// a status filter; the reader draws one check file as a document with
/// **Open in editor**, the card it was flagged on, and **Passed** /
/// **Failed** with a note. Fetched on open, on a search or filter change
/// (debounced) and after a press — never on the stream. Every word the
/// list draws is `ManualCheckRules`'.
struct ManualChecksView: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: ManualChecksWindowState
    var onReveal: ((String) -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            listPane
                .frame(width: 330)
            Rectangle().fill(Theme.hair).frame(width: 1)
            readerPane
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.bg)
        .task(id: state.root + "\u{1F}" + state.query + "\u{1F}" + state.filter) {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            await Self.load(client: client, state: state)
        }
        .task(id: state.selected ?? "") {
            await Self.loadDocument(client: client, state: state)
        }
    }

    // MARK: - The list

    private var listPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("search title, steps, outcome", text: $state.query)
                .textFieldStyle(.plain)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .padding(6)
                .background(Theme.well)
            HStack(spacing: 10) {
                ForEach(ManualCheckRules.statusFilters, id: \.self) { filter in
                    ManualCheckButton(action: { state.filter = filter }) {
                        Text(ManualCheckRules.filterWord(filter))
                            .font(Theme.mono(11, weight: state.filter == filter
                                             ? .semibold : .regular))
                            .foregroundStyle(state.filter == filter
                                             ? Theme.phosphorBright : Theme.dim)
                    }
                }
                Spacer(minLength: 0)
            }
            if !state.detail.isEmpty {
                Text(state.detail)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.amber)
                    .textSelection(.enabled)
            }
            if state.loading && state.report.checks.isEmpty {
                Text("loading…")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            } else if !state.loading && state.report.checks.isEmpty
                        && state.detail.isEmpty {
                Text(ManualCheckRules.emptyLine)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(ManualCheckRules.openFirst(state.report.checks)) { entry in
                        row(entry)
                    }
                }
            }
        }
        .padding(12)
    }

    private func row(_ entry: ManualCheckEntry) -> some View {
        let chosen = state.selected == entry.path
        return ManualCheckButton(action: {
            state.selected = entry.path
            state.noteDraft = ""
        }) {
            VStack(alignment: .leading, spacing: 3) {
                Text(ManualCheckRules.rowLine(entry))
                    .font(Theme.mono(10))
                    .foregroundStyle(entry.malformed ? Theme.amber : Theme.dim)
                Text(entry.check.isEmpty ? entry.title : entry.check)
                    .font(Theme.mono(12, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                    .multilineTextAlignment(.leading)
                if !ManualCheckRules.isOpen(entry) || entry.malformed {
                    Text(entry.malformed ? entry.problem
                         : ManualCheckRules.outcomeLine(entry))
                        .font(Theme.mono(11))
                        .foregroundStyle(entry.malformed ? Theme.amber : Theme.dim)
                        .multilineTextAlignment(.leading)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(chosen ? Theme.well : Theme.card)
            .overlay(Rectangle().stroke(chosen ? Theme.phosphor : Theme.hair,
                                        lineWidth: 1))
        }
    }

    // MARK: - The reader

    @ViewBuilder
    private var readerPane: some View {
        if let entry = state.selectedEntry {
            VStack(alignment: .leading, spacing: 10) {
                Text(ManualCheckRules.outcomeLine(entry))
                    .font(Theme.mono(12, weight: .semibold))
                    .foregroundStyle(ManualCheckRules.isOpen(entry)
                                     ? Theme.amber : Theme.phosphor)
                HStack(spacing: 12) {
                    ManualCheckButton(action: { Self.openInEditor(entry.path) }) {
                        Text("Open in editor")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.phosphor)
                    }
                    if !entry.cardId.isEmpty {
                        ManualCheckButton(action: { onReveal?(entry.cardId) }) {
                            Text("card ⌗")
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.phosphor)
                        }
                        .accessibilityLabel("Show the card on the board")
                    }
                    Text(entry.path)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                if ManualCheckRules.mayRecord(entry) {
                    outcomeStrip(entry)
                }
                ScrollView {
                    if let document = state.document, document.available {
                        MarkdownText(source: document.text)
                            .equatable()
                            .padding(8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else if let document = state.document {
                        Text(document.reason)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.amber)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
            }
            .padding(12)
        } else {
            Text("Choose a check to read it.")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .padding(12)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }

    /// Unarmed, `clear_manual`'s ceremony rule: the press is made after the
    /// chore, and the undo is a person editing the file.
    private func outcomeStrip(_ entry: ManualCheckEntry) -> some View {
        HStack(spacing: 10) {
            TextField("note (optional)", text: $state.noteDraft)
                .textFieldStyle(.plain)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .padding(6)
                .background(Theme.well)
                .onChange(of: state.noteDraft) { _, value in
                    if value.count > ManualCheckRules.noteLimit {
                        state.noteDraft = String(value.prefix(ManualCheckRules.noteLimit))
                    }
                }
            ManualCheckButton(action: { record(entry, status: "passed") }) {
                Text("Passed")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
            }
            .disabled(state.recording)
            ManualCheckButton(action: { record(entry, status: "failed") }) {
                Text("Failed")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.alarm)
            }
            .disabled(state.recording)
        }
    }

    private func record(_ entry: ManualCheckEntry, status: String) {
        let note = ManualCheckRules.clampedNote(state.noteDraft)
        state.recording = true
        Task { @MainActor in
            let result = await client.boardManualOutcome(
                path: entry.path, status: status, note: note)
            state.recording = false
            state.detail = result.ok ? ManualCheckRules.recordedLine : result.detail
            if result.ok { state.noteDraft = "" }
            await Self.load(client: client, state: state, keepDetail: true)
            await Self.loadDocument(client: client, state: state)
            await client.refresh()
        }
    }

    // MARK: - Loading

    static func load(client: DaemonClient, state: ManualChecksWindowState,
                     keepDetail: Bool = false) async {
        let query = await MainActor.run { () -> (String, String, String) in
            state.loading = true
            return (state.query, state.filter, state.root)
        }
        let fetched = await client.manualChecks(
            root: query.2, query: query.0,
            status: query.1 == "all" ? "" : query.1)
        await MainActor.run {
            // A stale reply — the person typed on — is dropped.
            guard state.query == query.0, state.filter == query.1,
                  state.root == query.2 else { return }
            state.loading = false
            guard let fetched else {
                state.report = ManualChecksReport()
                state.detail = ManualCheckRules.fetchFailedLine
                return
            }
            if !fetched.available {
                state.report = ManualChecksReport()
                state.detail = fetched.reason.isEmpty
                    ? ManualCheckRules.unavailableLine : fetched.reason
                return
            }
            state.report = fetched
            if !keepDetail { state.detail = "" }
        }
    }

    static func loadDocument(client: DaemonClient,
                             state: ManualChecksWindowState) async {
        let path = await MainActor.run { state.selected ?? "" }
        guard !path.isEmpty else {
            await MainActor.run { state.document = nil }
            return
        }
        let fetched = await client.manualCheckText(path: path)
        await MainActor.run {
            guard state.selected == path else { return }
            state.document = fetched ?? {
                var failed = ManualCheckDocument()
                failed.reason = ManualCheckRules.fetchFailedLine
                return failed
            }()
        }
    }

    /// The card window's `revealInEditor`: VS Code where it is installed,
    /// else the default app for a `.md`.
    static func openInEditor(_ path: String) {
        let url = URL(fileURLWithPath: path)
        let code = URL(fileURLWithPath: "/Applications/Visual Studio Code.app")
        if FileManager.default.fileExists(atPath: code.path) {
            NSWorkspace.shared.open([url], withApplicationAt: code,
                                    configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(url)
        }
    }
}
