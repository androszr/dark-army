import AppKit
import SwiftUI

// The rail's small furniture: the enrol button, the budget chips, the fleet
// signal, a tally and the shared section header. Split out of
// `PanelView.swift` on 20 Sep 2026.


/// Enrol one folder, and say what happened when it does not work. Its own view
/// because the prompt and the banner both raise it, and a refusal has to land
/// somewhere the presser is looking.
struct EnrolButton: View {
    let label: String
    let root: String
    @ObservedObject var client: DaemonClient
    var compact = false
    @State private var working = false
    @State private var error = ""

    var body: some View {
        VStack(spacing: 3) {
            Button(working ? "Enrolling…" : (compact ? label : "Enrol \(label)")) {
                working = true
                error = ""
                Task {
                    let result = await client.enrollProject(root)
                    working = false
                    error = result.ok ? "" : result.detail
                }
            }
            .disabled(working)
            .font(.caption)
            if !error.isEmpty {
                Text(error)
                    .font(.caption2).foregroundStyle(.orange)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// One account's budget reading, as the header's right-hand chip.
struct BudgetChip: Identifiable {
    var id: String { provider }
    let provider: String
    let label: String
    let tint: Color
}

/// Drawn as its own view rather than inline in `tallies`: the header stack is
/// already at the edge of what the type checker will solve in one expression.
struct BudgetChipView: View {
    let chip: BudgetChip

    var body: some View {
        HStack(spacing: 4) {
            ProviderMark(provider: chip.provider, size: 9, tint: chip.tint)
            Text(chip.label)
                .font(.caption.monospacedDigit())
        }
        .foregroundStyle(chip.tint)
    }
}

/// One fleet-wide signal, drawn as a full-width strip under the tallies.
struct FleetSignal: View {
    let signal: Signal

    private var tint: Color {
        switch signal.severity {
        case "crit": return .red
        case "warn": return .orange
        default: return .secondary
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Image(systemName: signal.severity == "crit"
                  ? "exclamationmark.triangle.fill" : "info.circle")
                .font(.system(size: 10))
            // Whose budget this is. The two providers' windows are different
            // accounts, and the text says "5h" or "weekly" rather than naming
            // one, so the mark is the only thing that answers it at a glance.
            if !signal.provider.isEmpty {
                ProviderMark(provider: signal.provider, size: 9, tint: tint)
                    .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
            }
            Text(signal.text)
                .font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            Rectangle()
                .fill(tint.opacity(0.12))
        )
        // Inset from the panel's edges and held off its neighbours: a
        // full-bleed band welded to the tallies above and the tabs below reads
        // as a seam in the chrome rather than as something the fleet is saying.
        .padding(.horizontal, 12)
    }
}

struct Tally: View {
    let label: String
    let value: Int
    let tint: Color
    var footnote: Int = 0

    var body: some View {
        HStack(spacing: 5) {
            Text("\(value)")
                .font(.system(size: 15, weight: .semibold).monospacedDigit())
                .foregroundStyle(value == 0 ? AnyShapeStyle(.tertiary) : AnyShapeStyle(tint))
            if footnote > 0 {
                Text("+\(footnote)")
                    .font(.system(size: 9, weight: .medium).monospacedDigit())
                    .foregroundStyle(.tertiary)
                    .baselineOffset(5)
            }
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

struct SectionHeader: View {
    let title: String
    let count: Int
    let collapsed: Bool
    /// The attention section is pinned open: no chevron, no hover wash, and the
    /// button is disabled rather than removed — keeping one body means the
    /// header cannot drift into a second layout that only differs when someone
    /// looks at the un-collapsible case.
    var collapsible: Bool = true
    /// Optional dim run after the count. Empty draws nothing, so Active and
    /// Recently finished stay the heading they already were.
    var note: String = ""
    let toggle: () -> Void

    @State private var hover = false

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 6) {
                // Rotated rather than swapped for a second glyph, so the chevron
                // animates between the two states instead of cutting. Absent
                // entirely when the section cannot fold: a chevron is a promise.
                if collapsible {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8, weight: .semibold))
                        .rotationEffect(.degrees(collapsed ? -90 : 0))
                        .foregroundStyle(.tertiary)
                }
                Text(title.uppercased())
                    .font(Theme.mono(10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.phosphor)
                // The count is what a collapsed section still has to say: it is
                // the difference between "nothing there" and "not shown".
                Text("\(count)")
                    .font(Theme.mono(10).monospacedDigit())
                    .foregroundStyle(Theme.faint)
                if !note.isEmpty {
                    Text(note)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.dim)
                        .lineLimit(1)
                }
                Spacer()
            }
            .padding(.horizontal, 14)
            // Even, not 12-over-5. The band is drawn (a phosphor hairline
            // under the strip, plus the hover wash), so its padding is visible
            // as the label's position inside a filled rectangle rather than as
            // air between two things — and 12 over 5 reads as a label sinking
            // to the floor of its own band. 7 and 7 also make the drawn height
            // land on `PanelMetrics.sectionHeader` (27) instead of the ~29 it
            // has always been: the formula reserves the row, so a header that
            // draws taller than its constant spends the difference out of the
            // body below it.
            .padding(.vertical, 7)
            // The whole strip is the hit area, not just the text.
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) {
                Rectangle().fill(Theme.hair).frame(height: 1)
            }
        }
        .buttonStyle(.plain)
        .disabled(!collapsible)
        // Same test as the chevron and the wash: a heading that cannot fold
        // must not offer a pointer that says it can.
        .clickable(collapsible)
        .onHover { hover = collapsible && $0 }
        .background(hover ? Color.primary.opacity(0.06) : .clear)
    }
}





/// The measured height of the scrollable body's content.
///
/// `max` rather than a sum: the geometry reader is one background behind the
/// whole content, so there is only ever one value per pass, and taking the
/// larger of a stale and a fresh one is the safe way to combine them.
struct ListHeightKey: PreferenceKey {
    static var defaultValue: CGFloat { 0 }
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
