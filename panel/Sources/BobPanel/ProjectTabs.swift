import SwiftUI

/// One tab in the project strip. Name-keyed, never index-keyed: a vanished
/// project's neighbours stay put, and the selection falls back by name.
/// Every tab is a real project now — ended runs and stuck jobs live under
/// the project they ran in, so there is no machine-wide Finished tab.
enum ProjectTab: Hashable {
    case project(String)

    var title: String {
        switch self {
        case .project(let name): return name.isEmpty ? "Other" : name
        }
    }
}

/// What the content region is drilled into. Kept as its own enum rather than
/// a bare `String?` so a drill target stays a decision, not a name that might
/// collide with a project called something odd.
enum DrillTarget: Hashable {
    case project(String)
}

/// Stable project order: localized name, empty-name (`Other`) last.
func projectNameOrder(_ a: String, _ b: String) -> Bool {
    if a.isEmpty != b.isEmpty { return b.isEmpty }
    return a.localizedStandardCompare(b) == .orderedAscending
}

func compareProjectNames(_ a: String, _ b: String) -> ComparisonResult {
    if a.isEmpty != b.isEmpty {
        return a.isEmpty ? .orderedDescending : .orderedAscending
    }
    return a.localizedStandardCompare(b)
}

/// When the selected tab vanishes, land on the next surviving project in the
/// stable order, else the previous, else the first tab, else nil.
func fallbackTab(from current: ProjectTab?, in tabs: [ProjectTab]) -> ProjectTab? {
    if tabs.isEmpty { return nil }
    if let current, tabs.contains(current) { return current }
    guard let current else { return tabs.first }

    switch current {
    case .project(let name):
        let projects = tabs.compactMap { tab -> String? in
            if case .project(let n) = tab { return n }
            return nil
        }
        if let next = projects.first(where: {
            compareProjectNames($0, name) == .orderedDescending
        }) {
            return .project(next)
        }
        if let prev = projects.last(where: {
            compareProjectNames($0, name) == .orderedAscending
        }) {
            return .project(prev)
        }
        return tabs.first
    }
}

/// The strip's tabs: every project with anything left to show — a live
/// session, a run that ended inside the retention window, or a stuck job.
/// Unique names in `projectNameOrder` (the empty name, drawn as `Other`,
/// last). A project whose sessions have all ended keeps its tab until its
/// last tombstone ages out, so nothing on screen is ever unreachable.
func projectTabUnion(live: [String], finished: [String],
                     abandoned: [String]) -> [ProjectTab] {
    Array(Set(live + finished + abandoned))
        .sorted(by: projectNameOrder)
        .map { ProjectTab.project($0) }
}

/// The number beside a tab counts live rows only — a finished-only project
/// shows 0. Ended and stuck rows never inflate it; the footer sections carry
/// their own counts on their headings.
func projectTabCounts(liveNames: [String],
                      tabs: [ProjectTab]) -> [ProjectTab: Int] {
    var out: [ProjectTab: Int] = [:]
    for tab in tabs {
        if case .project(let name) = tab {
            out[tab] = liveNames.filter { $0 == name }.count
        }
    }
    return out
}

/// Horizontal strip of project tabs. Order is a caller-supplied constant;
/// this view never sorts. The selected tab is scrolled into view on change.
struct ProjectTabStrip: View {
    let tabs: [ProjectTab]
    let selected: ProjectTab?
    let counts: [ProjectTab: Int]
    let flagged: Set<ProjectTab>
    var enabled: Bool = true
    let onSelect: (ProjectTab) -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 2) {
                    ForEach(tabs, id: \.self) { tab in
                        tabChip(tab).id(tab)
                    }
                }
                .padding(.horizontal, 14)
            }
            .onChange(of: selected) { _, tab in
                if let tab {
                    Motion.animate(.snappy(duration: 0.12), reduced: reduceMotion) {
                        proxy.scrollTo(tab, anchor: .center)
                    }
                }
            }
            .onAppear {
                if let selected { proxy.scrollTo(selected, anchor: .center) }
            }
        }
        .opacity(enabled ? 1 : 0.45)
        .allowsHitTesting(enabled)
        .padding(.bottom, 8)
    }

    private func tabChip(_ tab: ProjectTab) -> some View {
        let isOn = selected == tab
        let count = counts[tab] ?? 0
        let flag = flagged.contains(tab)
        return Button {
            onSelect(tab)
        } label: {
            HStack(spacing: 5) {
                if flag {
                    // Form before colour: the red dot alone is invisible to
                    // a colour-blind reader, so a mark stands beside it.
                    Circle()
                        .fill(Color.red)
                        .frame(width: 5, height: 5)
                    Text("!")
                        .font(Theme.mono(9, weight: .semibold))
                        .foregroundStyle(Color.red)
                }
                Text(tab.title)
                    .font(.system(size: 11.5, weight: isOn ? .semibold : .regular))
                    .lineLimit(1)
                Text("\(count)")
                    .font(.system(size: 10, weight: .regular).monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(isOn ? Color.primary.opacity(0.10) : .clear)
            )
            .foregroundStyle(isOn ? Color.primary : Color.secondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable(enabled)
        // A greyed strip is not pressable, so not a Tab stop either.
        .disabled(!enabled)
        .reportsKeyboardFocus()
        .accessibilityLabel(flag ? "\(tab.title), \(count), needs you" : "\(tab.title), \(count)")
    }
}

/// Standing facts for the selected project, composed as optional segments.
/// A missing branch or missing history facts omit their segment, never faked.
struct ProjectFactsLine: Equatable {
    /// Branch is the only unbounded segment; 40 + "…" keeps the joined
    /// line inside the rail at `Theme.mono(10)`.
    static let maxBranchChars = 40

    var branch: String?
    var running: Int
    var lastActive: String?

    static func compose(branches: [String], running: Int,
                        facts: ProjectFacts?, now: Double) -> ProjectFactsLine {
        let branch = branches.first { !$0.isEmpty }
        let lastActive: String?
        if running > 0 {
            lastActive = "now"
        } else if let stamp = facts?.lastActive, stamp > 0 {
            lastActive = Format.duration(now - stamp) + " ago"
        } else {
            lastActive = nil
        }
        return ProjectFactsLine(branch: branch, running: running,
                                lastActive: lastActive)
    }

    /// Branch, "N running", last-active — omitted segments dropped. The
    /// branch is clamped so a long name cannot push the rest off the line.
    var segments: [String] {
        var parts: [String] = []
        if let branch {
            if branch.count > Self.maxBranchChars {
                parts.append(String(branch.prefix(Self.maxBranchChars)) + "…")
            } else {
                parts.append(branch)
            }
        }
        parts.append("\(running) running")
        if let lastActive {
            parts.append(lastActive == "now" ? "active now" : lastActive)
        }
        return parts
    }
}

/// True only for a real project on the Agents tab. A nil tab is
/// absent, not empty — the PipelineBand rule.
func projectFactsVisible(tab: PanelView.Tab, resolvedTab: ProjectTab?) -> Bool {
    guard tab == .agents else { return false }
    if case .project = resolvedTab { return true }
    return false
}

/// One caption under the project tabs, facts joined by the rail's middot.
/// No buttons, no chart.
struct ProjectFactsBar: View {
    let line: ProjectFactsLine

    var body: some View {
        Text(line.segments.joined(separator: " · "))
            .font(Theme.mono(10))
            .foregroundStyle(Theme.dim)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: PanelMetrics.projectFacts, alignment: .center)
            .accessibilityElement(children: .combine)
    }
}
