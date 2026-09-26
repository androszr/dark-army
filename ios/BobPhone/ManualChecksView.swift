import SwiftUI

/// Menu → Manual checks: every enrolled project's manual checks, open first
/// then newest first, with a search box over title, steps and outcome and a
/// status filter. A row opens the check file as a document; where this Mac
/// takes the press from a phone (`Board.manualOutcomeWritable`) the reader
/// offers **Passed** / **Failed**, each armed then confirmed, with a note.
/// Fetched when the screen appears, on a search or filter change and after a
/// press — never on the poll. Every word is `ManualCheckRules`'.
struct ManualChecksView: View {
    @ObservedObject var client: PhoneClient
    @State private var query = ""
    @State private var filter = "all"
    @State private var report = ManualChecksReport()
    @State private var loading = false
    @State private var detail = ""
    /// The check being read, by path; the reader is pushed off the list.
    @State private var openedPath: String?
    /// The entry the reader was opened on, kept so a reload that no longer
    /// lists it (a recorded check under the OPEN filter) does not blank the
    /// pushed page.
    @State private var openedEntry: ManualCheckEntry?

    var body: some View {
        List {
            TextField("search title, steps, outcome", text: $query)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphor)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(8)
                .background(Theme.well)
                .hidesKeyboard()
                .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            Picker(selection: $filter) {
                ForEach(ManualCheckRules.statusFilters, id: \.self) { word in
                    Text(ManualCheckRules.filterWord(word)).tag(word)
                }
            } label: {
                Text("status")
            }
            .pickerStyle(.segmented)
            .accessibilityLabel("status")
            .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
            .listRowSeparator(.hidden)
            .listRowBackground(Theme.bg)
            if !detail.isEmpty {
                row(text: detail, colour: Theme.amber)
            } else if loading && report.checks.isEmpty {
                row(text: "loading…", colour: Theme.dim)
            } else if !loading && report.checks.isEmpty {
                row(text: ManualCheckRules.emptyLine, colour: Theme.dim)
            }
            ForEach(ManualCheckRules.openFirst(report.checks)) { entry in
                DecryptButton(action: {
                    openedEntry = entry
                    openedPath = entry.path
                }) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(ManualCheckRules.rowLine(entry))
                            .font(Theme.mono(11))
                            .foregroundStyle(entry.malformed ? Theme.amber : Theme.dim)
                        Text(entry.check.isEmpty ? entry.title : entry.check)
                            .font(Theme.mono(13, weight: .semibold))
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                        if entry.malformed {
                            Text(entry.problem)
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.amber)
                                .fixedSize(horizontal: false, vertical: true)
                        } else if !ManualCheckRules.isOpen(entry) {
                            Text(ManualCheckRules.outcomeLine(entry))
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.vertical, 4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
                .accessibilityHint("Opens the check")
                .listRowBackground(Theme.bg)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("manual checks")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        // On the list itself, never inside a lazy row.
        .navigationDestination(item: $openedPath) { path in
            if let entry = report.checks.first(where: { $0.path == path })
                ?? (openedEntry?.path == path ? openedEntry : nil) {
                ManualCheckReaderView(client: client, entry: entry) { updated in
                    // The recorded outcome, so a reload that no longer lists
                    // the check (the OPEN filter) keeps an up-to-date page.
                    if let updated { openedEntry = updated }
                    await load()
                }
            }
        }
        .task(id: query + "\u{1F}" + filter) {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            await load()
        }
    }

    private func row(text: String, colour: Color) -> some View {
        Text(text)
            .font(Theme.mono(12))
            .foregroundStyle(colour)
            .fixedSize(horizontal: false, vertical: true)
            .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
            .listRowSeparator(.hidden)
            .listRowBackground(Theme.bg)
    }

    private func load() async {
        let asked = (query, filter)
        loading = true
        let fetched = await client.manualChecks(
            query: asked.0, status: asked.1 == "all" ? "" : asked.1)
        // A stale reply — the person typed on — is dropped.
        guard query == asked.0, filter == asked.1 else { return }
        loading = false
        guard let fetched else {
            report = ManualChecksReport()
            detail = ManualCheckRules.fetchFailedLine
            return
        }
        guard fetched.available else {
            report = ManualChecksReport()
            detail = fetched.reason.isEmpty
                ? ManualCheckRules.unavailableLine : fetched.reason
            return
        }
        report = fetched
        detail = ""
    }
}

/// One check file as a document, with Passed / Failed where the Mac takes
/// the press. Armed then confirmed: the first press arms, the second sends.
struct ManualCheckReaderView: View {
    @ObservedObject var client: PhoneClient
    let entry: ManualCheckEntry
    /// Called after every press; with the entry as recorded when it landed.
    let onRecorded: (ManualCheckEntry?) async -> Void
    /// The entry as this page last recorded it — the header reads this, so
    /// a landed Passed / Failed is drawn at once, never the stale OPEN.
    @State private var recordedEntry: ManualCheckEntry?

    /// What the header draws: the recorded outcome once one landed.
    private var shown: ManualCheckEntry { recordedEntry ?? entry }
    @State private var document: ManualCheckDocument?
    @State private var note = ""
    @State private var armed = ""
    @State private var sending = false
    @State private var detail = ""
    @State private var recorded = false

    private var mayRecord: Bool {
        client.snapshot.board.manualOutcomeWritable
            && ManualCheckRules.mayRecord(entry) && !recorded
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(ManualCheckRules.outcomeLine(shown))
                    .font(Theme.mono(13, weight: .semibold))
                    .foregroundStyle(ManualCheckRules.isOpen(shown)
                                     ? Theme.amber : Theme.phosphor)
                Text(ManualCheckRules.rowLine(shown))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                if !detail.isEmpty {
                    Text(detail)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if mayRecord {
                    TextField("note (optional)", text: $note, axis: .vertical)
                        .font(Theme.mono(13))
                        .foregroundStyle(Theme.phosphor)
                        .padding(8)
                        .background(Theme.well)
                        .hidesKeyboard()
                        .onChange(of: note) { _, value in
                            if value.count > ManualCheckRules.noteLimit {
                                note = String(value.prefix(ManualCheckRules.noteLimit))
                            }
                        }
                    HStack(spacing: 12) {
                        DecryptButton(label("passed")) { press("passed") }
                            .buttonStyle(AlarmOutline())
                            .disabled(sending)
                        DecryptButton(label("failed")) { press("failed") }
                            .buttonStyle(AlarmOutline())
                            .disabled(sending)
                    }
                }
                if let document, document.available {
                    MarkdownText(source: document.text, base: 12, mono: true)
                        .equatable()
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let document {
                    Text(document.reason)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("the check is on the Mac — fetching")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                }
                Text(entry.path)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .textSelection(.enabled)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .navigationTitle("manual checks")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .task { await loadDocument() }
    }

    private func label(_ status: String) -> String {
        let word = status == "passed" ? "Passed" : "Failed"
        if sending && armed == status { return "SENDING…" }
        return armed == status ? word + "?" : word
    }

    private func press(_ status: String) {
        guard armed == status else {
            armed = status
            return
        }
        let fields = ManualOutcomeFields.fields(path: entry.path, status: status,
                                                note: note)
        sending = true
        Task {
            let result = await client.post(action: PhoneActions.boardManualOutcome,
                                           fields: fields, scope: entry.path)
            sending = false
            armed = ""
            var updated: ManualCheckEntry?
            if result.ok {
                recorded = true
                note = ""
                detail = ManualCheckRules.recordedLine
                var landed = entry
                landed.status = status
                landed.outcome = fields["note"] ?? ""
                recordedEntry = landed
                updated = landed
            } else {
                detail = result.detail
            }
            await loadDocument()
            await onRecorded(updated)
        }
    }

    private func loadDocument() async {
        document = await client.manualCheckText(path: entry.path) ?? {
            var failed = ManualCheckDocument()
            failed.reason = ManualCheckRules.fetchFailedLine
            return failed
        }()
    }
}
