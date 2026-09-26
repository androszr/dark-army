import SwiftUI

/// One scout report opened on the phone: the title (drawn in the body — a
/// bar title cannot wrap), the answer block as labelled lines, then the rest
/// through the same renderer the card screen uses. The text is fetched when
/// this screen appears and at no other time — never on the poll, the
/// background refresh or the widget. The header's labels come from
/// `ScoutReportHeader`, byte-pinned with the Mac; the layout takes pairs and
/// a body, so the manual-check reader can draw the same way.
struct ScoutReportReaderView: View {
    @ObservedObject var client: PhoneClient
    let row: ScoutReportRow

    @State private var report: ScoutReportBody?
    @State private var detail = ""

    private var title: String {
        if let report, !report.title.isEmpty { return report.title }
        return row.displayTitle
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(title)
                    .font(Theme.mono(15, weight: .semibold))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                if !meta.isEmpty {
                    Text(meta)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                content
                Text(report.map { $0.path.isEmpty ? row.path : $0.path } ?? row.path)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .navigationTitle("report")
        .decryptSurface("ScoutReportReaderView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .task { await load() }
    }

    private var meta: String {
        [row.project, row.dateLine].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    @ViewBuilder
    private var content: some View {
        if let report {
            if report.available {
                let lines = ScoutReportHeader.rows(report.header)
                if !lines.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            VStack(alignment: .leading, spacing: 1) {
                                Text(line.label)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.faint)
                                Text(line.value)
                                    .font(Theme.mono(12, weight: line.label == "Verdict"
                                                     ? .semibold : .regular))
                                    .foregroundStyle(line.label == "Verdict"
                                                     ? Theme.phosphorBright : Theme.phosphor)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .accessibilityElement(children: .combine)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                }
                if !report.body.isEmpty {
                    MarkdownText(source: report.body, base: 12, mono: true)
                        .equatable()
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text(detail.isEmpty ? ScoutReportBody.fetchFailedLine : detail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            CommentLine(text: "fetching…")
        }
    }

    private func load() async {
        guard report == nil else { return }
        let requested = row.path
        let fetched = await client.scoutReportBody(path: requested)
        guard let outcome = ReportsLoad.apply(requested: requested,
                                              current: row.path,
                                              fetched: fetched)
        else { return }
        report = outcome.value
        detail = outcome.detail
    }
}
