import SwiftUI

/// The Reports tab's wide pane: the scout-report list, newest first, or one
/// report opened as a document — in the board's place, History's way
/// (`RailLayout.LeftPane.reports`; the board stays mounted underneath).
///
/// The list is fetched when the tab is shown, when the board's set of
/// attached reports changes and on the rail's Refresh (`loadKey`); a body is
/// fetched only when a row is opened. Once the search holds
/// `ScoutReportSearch.minBodyChars` characters and the person pauses for
/// 300 ms, the daemon is asked to search the reports' text too (`hits`,
/// keyed on the needle); `ScoutReportSearch.merge` folds those hits into the
/// instant filter's rows. All three go through `ReportsLoad.apply`,
/// so a reply for something the person has moved on from is dropped and a
/// failed fetch never keeps the previous rows. Drawing is on the main
/// actor; the fetches suspend on `URLSession`, never block it.
///
/// `ReportListView` and `ReportReaderView` take rows, header pairs and a
/// body and know nothing of scouts — the manual-check section draws through
/// them unchanged.
struct ScoutReportsPane: View {
    /// Not observed: the pane reads nothing off the snapshot. The load key
    /// arrives from `PanelView`, which already watches the client.
    let client: DaemonClient
    @Binding var index: ScoutReportIndex
    @Binding var detail: String
    @Binding var search: String
    @Binding var project: String
    @Binding var openPath: String
    /// The text search's reply, hoisted beside `index` so the rail's count
    /// reads the same merged list.
    @Binding var hits: ScoutReportIndex
    @Binding var searching: Bool
    let loadKey: String

    @State private var report: ScoutReportBody?
    @State private var bodyDetail = ""
    @State private var hitsDetail = ""
    @State private var loading = false

    /// What the list draws — and the rail counts: the instant filter's rows
    /// and the text search's hits merged (`ScoutReportSearch.merge`), then
    /// narrowed to the chosen project.
    static func visible(_ index: ScoutReportIndex, search: String,
                        project: String, hits: ScoutReportIndex) -> [ScoutReportRow] {
        ScoutReportSearch.merge(
            query: search, rows: index.rows,
            hits: ScoutReportSearch.hitRows(query: search, hits: hits))
            .filter { project.isEmpty || $0.project == project }
    }

    /// The text search's key: the needle once it is long enough, else
    /// empty (no search, and any held hits are cleared).
    private var bodySearchKey: String {
        ScoutReportSearch.wantsBodySearch(search) ? ScoutReportSearch.bodyNeedle(search) : ""
    }

    var body: some View {
        Group {
            if openPath.isEmpty { list } else { reader }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Theme.bg)
        .task(id: loadKey) { await loadIndex() }
        .task(id: openPath) { await loadBody() }
        // Keyed on the list's load key too (`ManualChecksView`'s query +
        // filter shape), so Refresh or a newly attached report re-asks a
        // held text search rather than keeping hits from before it.
        .task(id: bodySearchKey + "\u{1}" + loadKey) { await loadHits() }
    }

    private var rows: [ReportListRow] {
        Self.visible(index, search: search, project: project, hits: hits).map { row in
            ReportListRow(id: row.path, title: row.displayTitle,
                          subtitle: row.verdict, dateLine: row.dateLine,
                          projectLabel: row.project, snippet: row.snippet)
        }
    }

    /// Whether the text search has answered for what is typed now.
    private var bodySearchAnswered: Bool {
        !bodySearchKey.isEmpty && hits.query == bodySearchKey
    }

    private var emptyLine: String {
        if loading && index.rows.isEmpty { return "reading the reports…" }
        if ScoutReportSearch.isSearching(search), !index.rows.isEmpty {
            if searching { return "searching the reports' text…" }
            return ScoutReportSearch.noMatchLine(
                query: search, searchedText: bodySearchAnswered)
        }
        return index.rows.isEmpty ? "No scout reports yet." : ""
    }

    /// The text search's own words: a failed or unavailable reply, or what
    /// the reading bound and the hit cap left out.
    private var searchDetail: String {
        bodySearchKey.isEmpty ? "" : hitsDetail
    }

    private var list: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("# darkarmy · scout reports")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .padding(.horizontal, 18)
                .padding(.top, 14)
                .padding(.bottom, 6)
            if !detail.isEmpty {
                Text(detail)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 18)
                    .padding(.bottom, 6)
            }
            if !searchDetail.isEmpty {
                Text(searchDetail)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 18)
                    .padding(.bottom, 6)
            }
            ReportListView(rows: rows, emptyLine: emptyLine) { path in
                openPath = path
            }
        }
    }

    @ViewBuilder
    private var reader: some View {
        let row = index.rows.first { $0.path == openPath }
            ?? hits.rows.first { $0.path == openPath }
        if let report, report.available {
            ReportReaderView(
                title: report.title.isEmpty
                    ? (row?.displayTitle ?? "") : report.title,
                subtitle: [report.project, row?.dateLine ?? ""]
                    .filter { !$0.isEmpty }.joined(separator: " · "),
                headerRows: ScoutReportHeader.rows(report.header)
                    .map { ($0.label, $0.value) },
                text: report.body, path: report.path,
                backLabel: "‹ Reports",
                onBack: { openPath = "" })
        } else {
            ReportReaderView(
                title: row?.displayTitle ?? "",
                subtitle: "", headerRows: [], text: "", path: openPath,
                unavailable: report == nil ? "" : bodyDetail,
                waiting: report == nil,
                backLabel: "‹ Reports",
                onBack: { openPath = "" })
        }
    }

    private func loadIndex() async {
        let requested = loadKey
        loading = true
        let fetched = await client.scoutReportsIndex()
        loading = false
        // `.task(id:)` cancels this load when the key moves on; a
        // cancelled load's reply is the stale one.
        guard !Task.isCancelled,
              let outcome = ReportsLoad.apply(requested: requested,
                                              current: requested,
                                              fetched: fetched)
        else { return }
        index = outcome.value
        detail = outcome.detail
    }

    /// The text search: nothing below the threshold (and the held hits
    /// cleared at once, so no stale snippet lingers), else a 300 ms pause —
    /// `.task(id:)` cancels it when the needle moves, which is the
    /// stale-reply rule — then one read with `q`.
    private func loadHits() async {
        let key = bodySearchKey
        searching = false
        guard !key.isEmpty else {
            hits = ScoutReportIndex()
            hitsDetail = ""
            return
        }
        try? await Task.sleep(nanoseconds: 300_000_000)
        guard !Task.isCancelled else { return }
        searching = true
        let fetched = await client.scoutReportsIndex(query: key)
        guard !Task.isCancelled else { return }
        searching = false
        guard let outcome = ReportsLoad.applyHits(requested: key,
                                                  current: bodySearchKey,
                                                  fetched: fetched)
        else { return }
        hits = outcome.value
        hitsDetail = outcome.detail
    }

    private func loadBody() async {
        let requested = openPath
        report = nil
        bodyDetail = ""
        guard !requested.isEmpty else { return }
        let fetched = await client.scoutReportBody(path: requested)
        guard let outcome = ReportsLoad.apply(requested: requested,
                                              current: openPath,
                                              fetched: fetched)
        else { return }
        report = outcome.value
        bodyDetail = outcome.detail
    }
}

/// One line of a report list, composed by the section that owns the rows.
struct ReportListRow: Identifiable, Equatable {
    let id: String
    let title: String
    let subtitle: String
    let dateLine: String
    let projectLabel: String
    /// A line quoting where a text search found its term; `""` for none.
    var snippet = ""
}

/// A dated list of documents, one press per row. Generic: it draws what it
/// is handed and names no kind of report.
struct ReportListView: View {
    let rows: [ReportListRow]
    var emptyLine = ""
    let onOpen: (String) -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                if rows.isEmpty, !emptyLine.isEmpty {
                    Text(emptyLine)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 18)
                        .padding(.vertical, 10)
                }
                ForEach(rows) { row in
                    Button { onOpen(row.id) } label: { line(row) }
                        .buttonStyle(.plain)
                        .clickable()
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(spoken(row))
                        .accessibilityAddTraits(.isButton)
                    Rectangle().fill(Theme.hair).frame(height: 1)
                        .padding(.horizontal, 18)
                }
            }
            .padding(.bottom, 12)
        }
    }

    private func line(_ row: ReportListRow) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(row.title)
                    .font(Theme.mono(12, weight: .semibold))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Text(row.dateLine)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            }
            if !row.subtitle.isEmpty {
                Text(row.subtitle)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !row.snippet.isEmpty {
                Text(row.snippet)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !row.projectLabel.isEmpty {
                Text(row.projectLabel)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 18)
        .padding(.vertical, 9)
        .contentShape(Rectangle())
    }

    private func spoken(_ row: ReportListRow) -> String {
        [row.title, row.subtitle,
         row.snippet.isEmpty ? "" : "found: " + row.snippet,
         row.projectLabel, row.dateLine]
            .filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

/// One document opened: a way back, the title, the header as labelled
/// lines, then the body through the same renderer plans and cards use.
/// Generic: the header's labels are whatever the caller hands in.
struct ReportReaderView: View {
    let title: String
    var subtitle = ""
    let headerRows: [(String, String)]
    let text: String
    let path: String
    /// The daemon's words when the document could not be served.
    var unavailable = ""
    /// Still fetching.
    var waiting = false
    /// The label drawn in the reader's brightest ink.
    var highlight = "Verdict"
    var backLabel = "‹ Back"
    let onBack: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onBack) {
                Text(backLabel)
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
            }
            .buttonStyle(.plain)
            .clickable()
            .accessibilityLabel("Back to the list")
            .padding(.horizontal, 18)
            .padding(.top, 12)
            .padding(.bottom, 8)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !title.isEmpty {
                        Text(title)
                            .font(Theme.mono(15, weight: .semibold))
                            .foregroundStyle(Theme.phosphorBright)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !subtitle.isEmpty {
                        Text(subtitle)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.faint)
                    }
                    if waiting {
                        Text("fetching…")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                    }
                    if !unavailable.isEmpty {
                        Text(unavailable)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.amber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !headerRows.isEmpty { header }
                    if !text.isEmpty {
                        MarkdownText(source: text)
                            .equatable()
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if !path.isEmpty {
                        Text(path)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.faint)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.bottom, 16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var header: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14,
             verticalSpacing: 5) {
            ForEach(Array(headerRows.enumerated()), id: \.offset) { _, pair in
                GridRow {
                    Text(pair.0)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .gridColumnAlignment(.trailing)
                    Text(pair.1)
                        .font(Theme.mono(12, weight: pair.0 == highlight ? .semibold : .regular))
                        .foregroundStyle(pair.0 == highlight
                                         ? Theme.phosphorBright : Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .padding(10)
        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
    }
}
