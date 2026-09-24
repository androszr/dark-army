import SwiftUI

/// Read-only budget windows. Stacked, one window per row — a phone is too
/// narrow for the Mac rail's four-column `UsageRow`.
struct UsageView: View {
    let usage: [UsageBar]
    let attribution: UsageAttribution
    /// Handed in for the looking-back reports, which are the one thing on
    /// this screen that has to ask the Mac a question of its own. Optional
    /// because everything else here is drawn from the two values above: with
    /// no client the report rows are simply **absent**, which is also what an
    /// older Mac produces.
    var client: PhoneClient?
    /// `PhoneClient.refreshing`, handed in: this view holds no client.
    /// Declared before `onRefresh` so the call site keeps its trailing
    /// closure.
    var refreshing = false
    let onRefresh: () async -> Void

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    /// One group per provider, in first-appearance order — never sorted, so
    /// the daemon's own order (Claude, then Codex, then Grok) survives.
    private var groups: [(provider: String, bars: [UsageBar])] {
        var out: [(provider: String, bars: [UsageBar])] = []
        var seen: [String: Int] = [:]
        for bar in usage {
            if let index = seen[bar.provider] {
                out[index].bars.append(bar)
            } else {
                seen[bar.provider] = out.count
                out.append((provider: bar.provider, bars: [bar]))
            }
        }
        return out
    }

    var body: some View {
        List {
            if refreshing {
                AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                 spoken: "Checking with the Mac")
                    .id("refresh")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            if let client, client.snapshot.board.agentReportSupported {
                NavigationLink {
                    PhoneAgentReport(client: client)
                } label: {
                    Text("Agents · last 30 days")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHint("Shows what each kind of helper cost")
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            }
            if let client, client.snapshot.board.lifecycleSupported {
                NavigationLink {
                    PhoneLifecycleReport(client: client)
                } label: {
                    Text("Where time went")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHint("Shows queue, execution, review and rework time")
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            }
            if usage.isEmpty {
                CommentLine(text: "no usage reading yet")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            ForEach(groups, id: \.provider) { group in
                PhoneSectionHeader(
                    title: PhoneProviderMark.displayName(group.provider),
                    provider: group.provider)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
                ForEach(group.bars, id: \.id) { bar in
                    UsageWindowRow(bar: bar)
                        .listRowInsets(EdgeInsets())
                        .listRowSeparator(.hidden)
                        .listRowBackground(Theme.bg)
                }
            }
            ForEach(attribution.spenderGroups) { group in
                PhoneSectionHeader(title: group.heading, provider: group.provider)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
                VStack(alignment: .leading, spacing: 4) {
                    Text(group.caption)
                    if let period = group.period { Text(period) }
                    if group.partial { Text("Partial local history") }
                    if !group.reason.isEmpty {
                        Text(group.reason)
                    } else if !group.available {
                        Text("Local history is unavailable.")
                    } else if group.models.isEmpty {
                        Text("No locally recorded work in this period.")
                    }
                }
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
                ForEach(Array(group.models.prefix(5).enumerated()),
                        id: \.offset) { _, row in
                    spenderRow(row)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .tint(Theme.phosphor)
        .refreshable { await onRefresh() }
    }

    /// A spender and its share. Two cells on one line at ordinary sizes; at an
    /// accessibility size a model name and a percentage cannot share a phone's
    /// width without one of them ending in dots, so they stack.
    @ViewBuilder
    private func spenderRow(_ row: UsageAttribution.ModelShare) -> some View {
        let name = Text(row.model.isEmpty ? "model" : row.model)
            .font(Theme.mono(12))
            .foregroundStyle(Theme.phosphor)
        let share = Text(row.pct.map { "\(Int($0))%" } ?? "–")
            .font(Theme.mono(12))
            .foregroundStyle(Theme.dim)
            .monospacedDigit()
        let spoken = (row.model.isEmpty ? "model" : row.model) + ", "
            + (row.pct.map { "\(Int($0)) percent" } ?? "no reading")
        Group {
            if dynamicTypeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: 2) {
                    name.fixedSize(horizontal: false, vertical: true)
                    share
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                HStack {
                    name.fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    share
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }
}

private struct UsageWindowRow: View {
    let bar: UsageBar

    private var tint: Color {
        guard let percent = bar.percent, !bar.stale else { return Theme.faint }
        return percent >= 90 ? .red : percent >= 75 ? .orange : Theme.phosphor
    }

    private var critical: Bool {
        guard let percent = bar.percent, !bar.stale else { return false }
        return percent >= 90
    }

    /// One line rather than four fragments: the window, its figure and when it
    /// resets are one fact about one budget.
    private var spoken: String {
        var parts = [UsageChipText.label(kind: bar.kind,
                                         shortLabel: bar.shortLabel,
                                         title: bar.title)]
        if let percent = bar.percent, !bar.stale {
            parts.append("\(Int(percent)) percent")
        } else {
            parts.append("no reading")
        }
        if resetLabel != "—" { parts.append("resets \(resetLabel)") }
        return parts.filter { !$0.isEmpty }.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(UsageChipText.label(kind: bar.kind,
                                     shortLabel: bar.shortLabel,
                                     title: bar.title))
                .font(Theme.mono(12))
                .foregroundStyle(Theme.faint)
            Text(UsageChipText.figure(percent: bar.percent, stale: bar.stale))
                .font(Theme.mono(16, weight: critical ? .semibold : .regular))
                .foregroundStyle(tint)
                .monospacedDigit()
            meter
                .frame(height: 3)
            Text(resetLabel)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .monospacedDigit()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    @ViewBuilder
    private var meter: some View {
        Group {
            if let percent = bar.percent, !bar.stale {
                ZStack(alignment: .leading) {
                    Rectangle().fill(Theme.hair)
                    Rectangle().fill(tint)
                        .frame(maxWidth: .infinity)
                        .scaleEffect(x: min(percent, 100) / 100, y: 1, anchor: .leading)
                }
                .clipped()
            } else {
                Rectangle().fill(Theme.hair)
            }
        }
        // A drawn bar of the figure spoken above it. Decoration, twice over.
        .accessibilityHidden(true)
    }

    /// "resets 06:00 · read 16 h ago". The age is appended rather than given a
    /// line of its own: it qualifies the figure above it, and a row that grows
    /// a fifth line only sometimes is a row whose height jumps as the network
    /// comes and goes.
    private var resetLabel: String {
        var text = "—"
        if let at = bar.resetsAt, !bar.stale {
            let date = Date(timeIntervalSince1970: at)
            let formatter = DateFormatter()
            formatter.dateFormat = Calendar.current.isDateInToday(date)
                ? "HH:mm" : "d MMM HH:mm"
            text = formatter.string(from: date)
        }
        if let note = UsageChipText.ageNote(asOf: bar.asOf, source: bar.source) {
            text += " · \(note)"
        }
        return text
    }
}

/// Short names for a rate-limit window. Copied from the panel's
/// `UsageChipText` — `test_phone_usage_tab.py` compares the three function
/// bodies so a silent rename of `7d·Fable` fails there.
enum UsageChipText {
    static func label(kind: String, shortLabel: String, title: String) -> String {
        switch kind {
        case "session": return "5h"
        case "weekly_all": return "7d"
        // `weekly_scoped` is the real kind — the per-model window, titled from
        // its own scope ("Current week (Fable)"). Guessing at `weekly_fable`
        // meant this chip fell through to the full title and was three times
        // the width of its neighbours.
        case "weekly_scoped": return "7d·\(modelSuffix(title: title))"
        case "grok_weekly": return "7d"
        case let kind where kind.hasPrefix("codex_"):
            return shortLabel.isEmpty ? "7d" : shortLabel
        default: return shortLabel.isEmpty ? title : shortLabel
        }
    }

    static func modelSuffix(title: String) -> String {
        guard let open = title.firstIndex(of: "("),
              let close = title.firstIndex(of: ")"),
              open < close else { return "model" }
        return String(title[title.index(after: open)..<close])
    }

    static func figure(percent: Double?, stale: Bool) -> String {
        guard let percent = percent, !stale else { return "–" }
        return "\(Int(percent))%"
    }

    /// Above this age a reading stops being allowed to pass as current. Half an
    /// hour: comfortably clear of the live fetch's own five-minute cycle, so a
    /// working feature never draws the note, and short enough that a figure
    /// nobody could refresh admits it within one sitting.
    static let staleReadSeconds: Double = 1800

    /// The one source whose age is never a failure. Claude Code reports the two
    /// account-wide windows on every turn, so a figure hours old there means
    /// only that nobody took a turn — there is no refresh to have failed. The
    /// note went out on those bars too at first, which put a false alarm on
    /// both meters every time the Mac sat idle for half an hour: exactly how a
    /// warning stops being read before the one real case arrives.
    static let selfReportedSource = "statusline"

    /// "read 41 min ago", for a figure being **held** rather than refreshed.
    ///
    /// nil while the reading is current, because a note on every bar all the
    /// time is noise, and noise is the thing that stops being read. nil too for
    /// a bar carrying no `as_of` at all — an older daemon publishes none, and
    /// guessing an age for it would be the same untruth pointing the other way.
    /// nil, finally, for a self-reported figure: see `selfReportedSource`. An
    /// unnamed source keeps the note — the Codex and Grok bars carry none, and
    /// they genuinely have no local fallback to age gracefully.
    ///
    /// This is not `stale`. `stale` says the window itself has reset and the
    /// figure is now wrong; this says the figure may still be right and nobody
    /// has checked in a while. Both can be true, and they read differently.
    static func ageNote(asOf: Double?, source: String = "",
                        now: Double = Date().timeIntervalSince1970) -> String? {
        guard let asOf = asOf, source != selfReportedSource else { return nil }
        let age = now - asOf
        guard age >= staleReadSeconds else { return nil }
        if age < 3600 { return "read \(Int(age / 60)) min ago" }
        if age < 86400 { return "read \(Int(age / 3600)) h ago" }
        return "read \(Int(age / 86400)) d ago"
    }
}

/// Claude a radiating burst, Grok a slanted X, Codex a six-loop blossom —
/// the silhouettes `_draw_brand_mark` already uses on the strip. An empty
/// provider is the wire default Claude's own rows arrive with and draws
/// Claude's burst, as `label(for:)` already says; a provider with no
/// silhouette here draws nothing and keeps its slot, and the text beside it
/// still says its name.
struct PhoneProviderMark: View {
    let provider: String
    var size: CGFloat = 10

    /// The silhouette to draw: empty is Claude.
    private var brand: String { provider.isEmpty ? "claude" : provider }

    var body: some View {
        Group {
            switch brand {
            case "claude", "grok", "codex":
                Canvas { context, canvasSize in
                    draw(brand, in: &context, size: canvasSize)
                }
                // The drawing is a silhouette; the name beside it is the fact.
                .accessibilityHidden(true)
            default:
                Color.clear
            }
        }
        .frame(width: size, height: size)
        .accessibilityElement()
        .accessibilityLabel(Self.label(for: provider))
    }

    /// The provider's name in capitals, for a group heading. An empty string
    /// is the wire default Claude's own bars arrive with; an unknown provider
    /// keeps its own name rather than being drawn as nobody.
    static func displayName(_ provider: String) -> String {
        provider.isEmpty ? "CLAUDE" : provider.uppercased()
    }

    static func label(for provider: String) -> String {
        switch provider {
        case "grok": return "Grok"
        case "codex": return "Codex"
        case "claude": return "Claude"
        default: return provider.isEmpty ? "Claude" : provider.capitalized
        }
    }

    private func draw(_ brand: String, in context: inout GraphicsContext,
                      size: CGSize) {
        let s = min(size.width, size.height)
        let cx = s / 2
        let cy = s / 2
        let ink = GraphicsContext.Shading.color(Theme.phosphor)

        if brand == "grok" {
            var path = Path()
            let inset = s * 0.12
            let slant = s * 0.10
            path.move(to: CGPoint(x: inset, y: inset))
            path.addLine(to: CGPoint(x: s - inset + slant, y: s - inset))
            path.move(to: CGPoint(x: s - inset, y: inset))
            path.addLine(to: CGPoint(x: inset + slant, y: s - inset))
            context.stroke(
                path, with: ink,
                style: StrokeStyle(lineWidth: max(1, s * 0.20), lineCap: .butt))
            return
        }

        if brand == "codex" {
            var path = Path()
            let outer = s * 0.39
            let inner = s * 0.17
            for i in 0..<6 {
                let a0 = Double.pi * 2 * Double(i) / 6 - Double.pi / 6
                let a1 = a0 + Double.pi / 3
                path.move(to: CGPoint(x: cx + CGFloat(cos(a0)) * inner,
                                      y: cy + CGFloat(sin(a0)) * inner))
                path.addCurve(
                    to: CGPoint(x: cx + CGFloat(cos(a1)) * inner,
                                y: cy + CGFloat(sin(a1)) * inner),
                    control1: CGPoint(x: cx + CGFloat(cos(a0)) * outer,
                                      y: cy + CGFloat(sin(a0)) * outer),
                    control2: CGPoint(x: cx + CGFloat(cos(a1)) * outer,
                                      y: cy + CGFloat(sin(a1)) * outer))
            }
            context.stroke(
                path, with: ink,
                style: StrokeStyle(lineWidth: max(1, s * 0.16), lineCap: .round))
            return
        }

        var path = Path()
        let outer = s * 0.46
        let inner = s * 0.13
        for i in 0..<8 {
            let angle = Double.pi * 2 * Double(i) / 8 + Double.pi / 8
            let dx = CGFloat(cos(angle))
            let dy = CGFloat(sin(angle))
            path.move(to: CGPoint(x: cx + dx * inner, y: cy + dy * inner))
            path.addLine(to: CGPoint(x: cx + dx * outer, y: cy + dy * outer))
        }
        context.stroke(
            path, with: ink,
            style: StrokeStyle(lineWidth: max(1, s * 0.17), lineCap: .round))
    }
}
