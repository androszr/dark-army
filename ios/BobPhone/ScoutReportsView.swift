import SwiftUI

/// The Menu's Scouting screen: every scout report the Mac lists, newest
/// first, each row its title, its verdict in full, its project and when it
/// was written; a `>` line narrows the list as you type
/// (`ScoutReportSearch`, byte-pinned with the Mac), and — against a Mac
/// publishing `scout_reports_body_search_supported`, after a 300 ms pause
/// on three or more characters — the Mac searches the reports' text too and
/// only the hits come back, merged in by `ScoutReportSearch.merge`, each
/// with its snippet. The list is asked for
/// once when the screen appears — never on the poll or the background
/// refresh — and a tapped row pushes the reader, which fetches the text at
/// that moment. Drawing is on the main actor; the read suspends on the
/// sealed door, `knowledgeReport`'s route.
struct ScoutReportsView: View {
    @ObservedObject var client: PhoneClient
    @State private var index = ScoutReportIndex()
    @State private var detail = ""
    @State private var loading = false
    @State private var loaded = false
    @State private var query = ""
    /// The text search's reply, and whether one is in flight.
    @State private var hits = ScoutReportIndex()
    @State private var hitsDetail = ""
    @State private var searching = false
    /// The path of the report pushed onto this tab's stack.
    @State private var openedPath: String?
    @FocusState private var searchFocused: Bool

    private var rows: [ScoutReportRow] {
        ScoutReportSearch.merge(
            query: query, rows: index.rows,
            hits: ScoutReportSearch.hitRows(query: query, hits: hits))
    }

    /// Whether this Mac can search the reports' text at all.
    private var bodySearchSupported: Bool {
        client.snapshot.board.scoutReportsBodySearchSupported
    }

    /// The text search's key: the needle, where the Mac can search and the
    /// needle is long enough; else empty (no search, held hits cleared).
    private var bodyKey: String {
        bodySearchSupported && ScoutReportSearch.wantsBodySearch(query)
            ? ScoutReportSearch.bodyNeedle(query) : ""
    }

    var body: some View {
        List {
            searchField
                .listRowInsets(EdgeInsets(top: 8, leading: 0, bottom: 4, trailing: 0))
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            if !detail.isEmpty {
                Text(detail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            if !bodyKey.isEmpty, !hitsDetail.isEmpty {
                Text(hitsDetail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            if let line = emptyLine {
                CommentLine(text: line)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            ForEach(rows) { row in
                ScoutReportListRow(row: row) { openedPath = row.path }
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg)
        .navigationTitle("scout reports")
        .decryptSurface("ScoutReportsView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        // On the list itself, never inside a lazy row.
        .navigationDestination(item: $openedPath) { path in
            ScoutReportReaderView(client: client, row: row(for: path))
        }
        .task { await load() }
        .task(id: bodyKey) { await searchText() }
    }

    private var emptyLine: String? {
        if loading && index.rows.isEmpty { return "reading the reports…" }
        if ScoutReportSearch.isSearching(query), rows.isEmpty, !index.rows.isEmpty {
            if searching { return "searching the reports' text…" }
            return ScoutReportSearch.noMatchLine(
                query: query,
                searchedText: !bodyKey.isEmpty && hits.query == bodyKey)
        }
        if loaded, index.rows.isEmpty, detail.isEmpty { return "no scout reports yet" }
        return nil
    }

    private func row(for path: String) -> ScoutReportRow {
        if let row = index.rows.first(where: { $0.path == path }) { return row }
        if let row = hits.rows.first(where: { $0.path == path }) { return row }
        var row = ScoutReportRow()
        row.path = path
        return row
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            Text(">")
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphor)
                .accessibilityHidden(true)
            TextField("", text: $query,
                      prompt: Text(bodySearchSupported ? ScoutReportSearch.bodyPlaceholder
                                                       : ScoutReportSearch.placeholder)
                          .foregroundStyle(Theme.faint))
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphorBright)
                .textFieldStyle(.plain)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .onSubmit { searchFocused = false }
                .focused($searchFocused)
                .accessibilityLabel("Search the scout reports")
                .accessibilityHint(bodySearchSupported
                    ? "Narrows the list to reports whose title, verdict, question or project contain the words, and after a pause adds reports whose text contains them"
                    : "Narrows the list to reports whose title, verdict, question or project contain the words")
            if searchFocused || !query.isEmpty {
                DecryptButton(query.isEmpty ? "× DONE" : "× CLEAR") {
                    query = ""
                    searchFocused = false
                }
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                    .accessibilityLabel(query.isEmpty ? "Hide the keyboard"
                                                      : "Clear the search")
            }
        }
        .frame(maxWidth: .infinity, minHeight: 36, alignment: .leading)
        .padding(.horizontal, 12)
        .fieldWell(focused: searchFocused, onTap: { searchFocused = true })
        .padding(.horizontal, 12)
    }

    /// Once per screen: a return from the reader keeps the list it had.
    private func load() async {
        guard !loaded else { return }
        let requested = "index"
        index = ScoutReportIndex()
        detail = ""
        loading = true
        let fetched = await client.scoutReportsIndex()
        loading = false
        guard let outcome = ReportsLoad.apply(requested: requested,
                                              current: "index",
                                              fetched: fetched)
        else { return }
        index = outcome.value
        detail = outcome.detail
        // A failed fetch stays retryable: the next visit asks again.
        loaded = fetched != nil
    }

    /// The text search, `ManualChecksView`'s debounce: nothing where the key
    /// is empty (held hits cleared at once), else a 300 ms pause —
    /// `.task(id:)` cancels it when the needle moves — then one read with
    /// `q`; a reply for a needle typed past is dropped by `ReportsLoad`.
    /// No reload key here: the list loads once per screen, so there is no
    /// Refresh to re-ask a held search for.
    private func searchText() async {
        let asked = bodyKey
        searching = false
        guard !asked.isEmpty else {
            hits = ScoutReportIndex()
            hitsDetail = ""
            return
        }
        try? await Task.sleep(nanoseconds: 300_000_000)
        guard !Task.isCancelled else { return }
        searching = true
        let fetched = await client.scoutReportsIndex(query: asked)
        guard !Task.isCancelled else { return }
        searching = false
        guard let outcome = ReportsLoad.applyHits(requested: asked,
                                                  current: bodyKey,
                                                  fetched: fetched)
        else { return }
        hits = outcome.value
        hitsDetail = outcome.detail
    }
}

/// One report in the list: the title, the verdict wrapping in full, then
/// the project and the date. One element for a screen reader, spoken from
/// the fields it draws. Takes a row and a press, and nothing about a scout,
/// so the manual-check list can draw through it.
struct ScoutReportListRow: View {
    let row: ScoutReportRow
    let onOpen: () -> Void

    var body: some View {
        DecryptButton(action: onOpen) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.displayTitle)
                        .font(Theme.mono(12, weight: .semibold))
                        .foregroundStyle(Theme.phosphorBright)
                        .fixedSize(horizontal: false, vertical: true)
                    if !row.verdict.isEmpty {
                        Text(row.verdict)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !row.snippet.isEmpty {
                        Text(row.snippet)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text(meta)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 4)
                Text("›")
                    .font(Theme.mono(14))
                    .foregroundStyle(Theme.faint)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken)
        .accessibilityHint("Opens the report")
    }

    private var meta: String {
        [row.project, row.dateLine].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private var spoken: String {
        var words = row.displayTitle
        if !row.verdict.isEmpty { words += ". " + row.verdict }
        if !row.snippet.isEmpty { words += ". found: " + row.snippet }
        if !row.project.isEmpty { words += ". " + row.project }
        if !row.dateLine.isEmpty { words += ", " + row.dateLine }
        return words
    }
}
