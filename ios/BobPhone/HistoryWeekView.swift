import SwiftUI

/// The Menu's History screen: the Mac's last seven days at a glance. One
/// total — the token cost of Claude, Grok and Codex together — with the
/// dollar a provider reported beside it and never added in, then seven
/// columns, one per local day, each a bar stacked Claude / Grok / Codex over
/// that day's figure. A day nothing could be priced on reads "not priced",
/// never $0.00.
///
/// The arithmetic is `LedgerWeek`, the Mac History's own fold, byte-pinned
/// (`test_ledger_week.py`), so the numbers here are the Mac's numbers. The
/// week is asked for when the screen appears and when it is pulled — never
/// on the poll, the background refresh or the widget — through the sealed
/// `history_week` read, relay first when away. Read-only: nothing here
/// writes. Held for the life of the screen only; nothing goes to disk.
struct HistoryWeekView: View {
    @ObservedObject var client: PhoneClient
    @State private var report: HistoryWeekReport?
    @State private var loading = false
    @State private var asked = false

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    /// The bar's full height and its floor: a drawn segment never shrinks
    /// below the floor, so a small day does not vanish.
    private static let barHeight: CGFloat = 64
    private static let segmentFloor: CGFloat = 4
    /// The strip's full width at accessibility sizes, where a day is a row.
    private static let stripWidth: CGFloat = 180

    private var picture: LedgerWeek.Picture? {
        guard let report, report.available else { return nil }
        let input = report.asLedgerInput()
        return LedgerWeek.picture(cards: input.cards, others: input.others)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                PromptLine(path: "~/history")
                if loading {
                    AgentChatterView(.line, wait: .refreshing, seed: "history",
                                     spoken: "Checking with the Mac")
                        .id("history")
                }
                if let picture {
                    headline(picture)
                    week(picture)
                    legend
                } else if asked && !loading {
                    CommentLine(text: failure)
                    DecryptButton("Retry") { Task { await load() } }
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                        .frame(minHeight: 44)
                        .padding(.horizontal, 14)
                        .accessibilityLabel("Retry")
                        .accessibilityHint("Asks the Mac for the week again")
                }
            }
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .tint(Theme.phosphor)
        .refreshable { await load() }
        .task { await load() }
        .navigationTitle("history")
        .decryptSurface("HistoryWeekView")
    }

    /// The Mac's own sentence where it sent one; otherwise the Mac did not
    /// answer at all.
    private var failure: String {
        if let report, !report.available {
            return report.reason.isEmpty ? "the Mac could not read the week" : report.reason
        }
        return "the Mac is out of reach"
    }

    private func load() async {
        loading = true
        let fetched = await client.historyWeek()
        loading = false
        asked = true
        // A failed ask keeps the week already on screen.
        if let fetched { report = fetched } else if report?.available != true { report = nil }
    }

    // MARK: - The total

    private func headline(_ picture: LedgerWeek.Picture) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(picture.total)
                .font(Theme.mono(28, weight: .semibold))
                .foregroundStyle(picture.anyTokenCost ? Theme.phosphorBright : Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .privacySensitive()
                .accessibilityLabel(picture.anyTokenCost
                                    ? "Token cost, last seven days: \(picture.total)"
                                    : "Last seven days: \(LedgerWeek.notPriced)")
            Text("token cost · Claude, Grok and Codex")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            if let reported = picture.reported {
                Text("\(reported) — not added in")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .privacySensitive()
            }
            if picture.unpriced > 0 {
                Text("\(picture.unpriced) \(LedgerWeek.notPriced)")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(cautions, id: \.self) { line in
                Text(line)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(picture.span)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14)
    }

    /// What makes the total a floor rather than the week, said in words.
    private var cautions: [String] {
        var lines: [String] = []
        if report?.truncated == true { lines.append("some cards were left out") }
        if report?.partial == true { lines.append("the Mac's history is a partial read") }
        if report?.codexHistoryPartial == true { lines.append("Codex history is still being read") }
        return lines
    }

    // MARK: - The seven days

    @ViewBuilder
    private func week(_ picture: LedgerWeek.Picture) -> some View {
        let scale = picture.days.map(\.tokenCost).max() ?? 0
        if dynamicTypeSize.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(picture.days, id: \.day) { column in
                    row(column, scale: scale)
                }
            }
            .padding(.horizontal, 14)
        } else {
            HStack(alignment: .top, spacing: 4) {
                ForEach(picture.days, id: \.day) { column in
                    tower(column, scale: scale)
                        .frame(maxWidth: .infinity, alignment: .top)
                }
            }
            .padding(.horizontal, 10)
        }
    }

    /// One day at ordinary sizes: the stacked bar, the figure, the reported
    /// mark and the weekday.
    private func tower(_ column: LedgerWeek.Column, scale: Double) -> some View {
        VStack(spacing: 4) {
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                let segments = LedgerWeek.segments(column)
                if segments.isEmpty {
                    Rectangle().fill(Theme.line).frame(height: 1)
                } else {
                    ForEach(segments, id: \.provider) { segment in
                        Rectangle()
                            .fill(ink(segment.provider))
                            .frame(height: length(segment.value, scale: scale,
                                                  full: Self.barHeight))
                    }
                }
            }
            .frame(width: 22, height: Self.barHeight)
            Text(figure(column))
                .font(Theme.mono(10))
                .foregroundStyle(column.anyTokenCost ? Theme.phosphorBright : Theme.dim)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if let mark = column.reportedMark {
                Text(mark)
                    .font(Theme.mono(9))
                    .foregroundStyle(Theme.dim)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("\(column.weekday) \(column.dayNumber)")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(HistoryWeekWords.spoken(column))
    }

    /// One day at accessibility sizes: the weekday, a horizontal strip, then
    /// the figure and the reported mark, each on its own wrapping line.
    private func row(_ column: LedgerWeek.Column, scale: Double) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(column.weekday) \(column.dayNumber)")
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphorBright)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 0) {
                let segments = LedgerWeek.segments(column)
                if segments.isEmpty {
                    Rectangle().fill(Theme.line).frame(width: Self.stripWidth, height: 1)
                } else {
                    ForEach(segments, id: \.provider) { segment in
                        Rectangle()
                            .fill(ink(segment.provider))
                            .frame(width: length(segment.value, scale: scale,
                                                 full: Self.stripWidth),
                                   height: 10)
                    }
                }
            }
            Text(figure(column))
                .font(Theme.mono(12))
                .foregroundStyle(column.anyTokenCost ? Theme.phosphorBright : Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            if let mark = column.reportedMark {
                Text(mark)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(HistoryWeekWords.spoken(column))
    }

    /// The figure under a bar: the token cost, the dash where nothing could
    /// be priced, a dash too beside a figure part of which was not priced,
    /// and a quiet dot on a day nothing happened — never $0.00 for no money.
    private func figure(_ column: LedgerWeek.Column) -> String {
        if column.figure.isEmpty { return "·" }
        if column.unknown, column.figure != LedgerWeek.dash {
            return "\(column.figure) \(LedgerWeek.dash)"
        }
        return column.figure
    }

    private func length(_ value: Double, scale: Double, full: CGFloat) -> CGFloat {
        guard scale > 0 else { return Self.segmentFloor }
        return max(Self.segmentFloor, CGFloat(value / scale) * full)
    }

    /// Claude green, Grok the control ink, Codex the bright text ink. Never
    /// amber (your turn) and never red (needs you); the legend names all
    /// three in words, so ink is never the only signal.
    private func ink(_ provider: String) -> Color {
        switch provider {
        case "grok": return Theme.control
        case "codex": return Theme.phosphorBright
        default: return Theme.phosphor
        }
    }

    // MARK: - The legend

    private var legend: some View {
        let providers = ["claude", "grok", "codex"]
        return VStack(alignment: .leading, spacing: 6) {
            if dynamicTypeSize.isAccessibilitySize {
                ForEach(providers, id: \.self) { provider in
                    swatch(HistoryWeekWords.name(provider), ink(provider))
                }
            } else {
                HStack(spacing: 14) {
                    ForEach(providers, id: \.self) { provider in
                        swatch(HistoryWeekWords.name(provider), ink(provider))
                    }
                }
            }
            Text("Claude · Grok · Codex, in that order in each bar")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            Text("\(LedgerWeek.dash) \(LedgerWeek.notPriced)")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            Text("A dollar a provider reported sits beside the total and is not added in.")
                .font(Theme.prose(12))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14)
        .padding(.top, 4)
    }

    private func swatch(_ name: String, _ color: Color) -> some View {
        HStack(spacing: 6) {
            Rectangle()
                .fill(color)
                .frame(width: 10, height: 10)
                .accessibilityHidden(true)
            Text(name)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
