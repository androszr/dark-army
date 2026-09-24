import SwiftUI

/// A pushed screen inside Usage, fetch-on-appear. Timeline stays inline;
/// a live card may jump through the existing one-sheet router. An older
/// Mac draws no link, so this view is never mounted there.
struct PhoneLifecycleReport: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    @State private var root = ""
    @State private var preset = 30
    @State private var sort = "queue"
    @State private var selectedCard: String?
    @State private var heldSort = "queue"
    @State private var serial = 0
    @State private var generation = ""
    @State private var from: Double = 0
    @State private var to: Double = 0
    @State private var fetch: Fetch = .idle
    @State private var lastFetch = Date.distantPast
    @State private var refreshing = false

    /// The request a drawn report answers. Keep/restore only when this
    /// matches the fetch about to start; a changed project, period, card
    /// or sort is a different screen, not a refresh of the one on it.
    private struct Request: Equatable {
        var root: String
        var from: Double
        var to: Double
        var card: String
        var sort: String
    }

    private enum Fetch {
        case idle, loading, failed
        case loaded(LifecycleReport, Request)
        case stale(LifecycleReport, Request)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if refreshing {
                    AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                     spoken: "Checking with the Mac")
                        .id("refresh")
                }
                PromptLine(path: "~/usage/timing")
                Picker("Project", selection: $root.decrypting(decryptFeedback)) {
                    Text("Choose a project").tag("")
                    ForEach(client.snapshot.board.projects, id: \.root) { project in
                        Text(project.name).tag(project.root)
                    }
                }
                .accessibilityLabel("Project")
                HStack {
                    ForEach([7, 30, 90], id: \.self) { days in
                        DecryptButton("\(days)d") {
                            preset = days
                            freezePeriod()
                            Task { await load(refresh: true) }
                        }
                        .foregroundStyle(preset == days ? Theme.phosphor : Theme.dim)
                    }
                }
                .accessibilityLabel("Period")
                switch fetch {
                case .idle:
                    EmptyView()
                case .loading:
                    note("Asking Dark Army for the report… this can take a few seconds.")
                case .failed:
                    note("Dark Army could not be reached.")
                case .stale(let report, _):
                    reportBody(report)
                    note("Refresh failed; showing the last report.")
                case .loaded(let report, _):
                    reportBody(report)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .font(Theme.mono(12))
        .background(Theme.bg)
        .navigationTitle("timing")
        .navigationBarTitleDisplayMode(.inline)
        .decryptSurface("PhoneLifecycleReport")
        .tint(Theme.phosphor)
        .refreshable {
            refreshing = true
            await load(refresh: true)
            refreshing = false
        }
        .task(id: "\(root)-\(preset)") {
            freezePeriod()
            await load(refresh: true)
        }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            if Date().timeIntervalSince(lastFetch) >= 30 {
                Task { await load(refresh: false) }
            }
        }
    }

    @ViewBuilder
    private func reportBody(_ report: LifecycleReport) -> some View {
        if !report.available {
            note(report.reason.isEmpty ? "Dark Army could not produce this report." : report.reason)
        } else if let cardId = selectedCard {
            timeline(report, cardId: cardId)
        } else {
            Text(periodCaption(report))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            Text(report.overlapNote.isEmpty ? LifecycleFormat.overlapNote : report.overlapNote)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            Text(coverageLine(report))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            let retained = LifecycleFormat.retentionLine(report.retention)
            if !retained.isEmpty {
                Text(retained)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if summariesEmpty(report) {
                note(LifecycleFormat.empty)
            }
            ForEach(LifecycleFormat.categories, id: \.self) { key in
                categoryRow(key, report.summaries[key] ?? LifecycleSummary())
            }
            ForEach(report.cards) { card in
                DecryptButton(action: {
                    heldSort = sort
                    selectedCard = card.cardId
                    Task { await load(refresh: false) }
                }) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(card.title.isEmpty ? card.cardId : card.title)
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(LifecycleFormat.seconds(card.periodObserved)
                             + (card.live ? "" : " · " + LifecycleFormat.removed))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private func categoryRow(_ key: String, _ summary: LifecycleSummary) -> some View {
        DecryptButton(action: {
            sort = key
            selectedCard = nil
            Task { await load(refresh: false) }
        }) {
            VStack(alignment: .leading, spacing: 2) {
                Text((LifecycleFormat.labels[key] ?? key)
                     + " · " + LifecycleFormat.seconds(summary.observedSeconds))
                    .foregroundStyle(sort == key ? Theme.phosphor : Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                Text("median \(LifecycleFormat.seconds(summary.p50))"
                     + " · p90 \(LifecycleFormat.seconds(summary.p90))"
                     + " · \(LifecycleFormat.coverage(summary))")
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityLabel((LifecycleFormat.labels[key] ?? key)
                            + ", " + LifecycleFormat.coverage(summary))
    }

    private func timeline(_ report: LifecycleReport, cardId: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            DecryptButton("Back") {
                selectedCard = nil
                sort = heldSort
                Task { await load(refresh: false) }
            }
            .font(Theme.mono(12))
            Text(report.title.isEmpty ? cardId : report.title)
                .font(Theme.mono(13))
                .fixedSize(horizontal: false, vertical: true)
            if !report.live {
                Text(report.removedNote.isEmpty ? LifecycleFormat.removed : report.removedNote)
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let card = client.snapshot.board.cards.first(where: { $0.id == cardId }) {
                DecryptButton("Open live card") { sheets.show(.card(card)) }
                    .font(Theme.mono(12))
            }
            ForEach(report.episodes) { episode in
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(episode.kind) · \(episode.disposition) · \(LifecycleFormat.seconds(episode.observedSeconds))")
                        .fixedSize(horizontal: false, vertical: true)
                    if episode.gapCount > 0 {
                        Text("gaps \(episode.gapCount) \(episode.gapReasons)")
                            .foregroundStyle(Theme.amber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .font(Theme.mono(11))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func note(_ text: String) -> some View {
        Text(text)
            .font(Theme.mono(12))
            .foregroundStyle(Theme.dim)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func freezePeriod() {
        to = Date().timeIntervalSince1970
        from = to - Double(preset) * 86400
    }

    private func periodCaption(_ report: LifecycleReport) -> String {
        let fmt = ISO8601DateFormatter()
        fmt.timeZone = TimeZone(secondsFromGMT: 0)
        fmt.formatOptions = [.withFullDate]
        let a = fmt.string(from: Date(timeIntervalSince1970: report.from))
        let b = fmt.string(from: Date(timeIntervalSince1970: report.to))
        return "\(a) → \(b) UTC, end excluded · nearest-rank quantiles"
    }

    private func coverageLine(_ report: LifecycleReport) -> String {
        if !report.measurementsAvailable {
            return "Collection is incomplete; figures below may undercount."
        }
        return "n of N is completed episodes over those that started in this period."
    }

    private func summariesEmpty(_ report: LifecycleReport) -> Bool {
        LifecycleFormat.categories.allSatisfy { (report.summaries[$0]?.N ?? 0) == 0 }
    }

    private func load(refresh: Bool) async {
        guard !root.isEmpty else {
            serial += 1
            selectedCard = nil
            fetch = .idle
            return
        }
        if refresh { generation = "" }
        lastFetch = Date()
        serial += 1
        let mine = serial
        let wanted = Request(root: root, from: from, to: to,
                             card: selectedCard ?? "", sort: sort)
        let previous = fetch
        switch previous {
        case .loaded(_, let shown) where shown == wanted:
            break
        case .stale(_, let shown) where shown == wanted:
            break
        default:
            fetch = .loading
        }
        let cardId = wanted.card
        let report = await client.lifecycleReport(
            root: cardId.isEmpty ? wanted.root : nil,
            cardId: cardId.isEmpty ? nil : cardId,
            from: wanted.from, to: wanted.to, sort: wanted.sort,
            generation: generation)
        guard mine == serial else { return }
        guard let report else {
            switch previous {
            case .loaded(let old, let shown) where shown == wanted:
                fetch = .stale(old, shown)
            case .stale(let old, let shown) where shown == wanted:
                fetch = .stale(old, shown)
            default:
                fetch = .failed
            }
            return
        }
        if LifecycleFormat.shouldRetry(report) {
            generation = ""
            await load(refresh: true)
            return
        }
        generation = report.generation
        fetch = .loaded(report, wanted)
    }
}
