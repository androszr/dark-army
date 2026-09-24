import SwiftUI

// How the rail groups its rows: the buckets, the foldable sections, one
// row and the table's arithmetic. Split out of `PanelView.swift` on 20 Sep 2026.

/// The five buckets, in the order you act on them.
enum Category: String, CaseIterable {
    case waiting, running, sleeping, finished, abandoned

    var title: String {
        switch self {
        case .waiting: return "Waiting for you"
        case .running: return "Running"
        case .sleeping: return "Sleeping"
        case .finished: return "Recently finished"
        case .abandoned: return "Abandoned"
        }
    }

    func rows(_ agents: Agents) -> [Agent] {
        switch self {
        case .waiting: return agents.waiting
        case .running: return agents.running
        case .sleeping: return agents.sleeping
        case .finished: return agents.finished
        case .abandoned: return agents.abandoned
        }
    }

    /// Sections that open closed. Both of these are *records* rather than work:
    /// a tombstone and a run that is already over. They are worth keeping — the
    /// cost of a finished run is the one thing nothing else reports — but they
    /// are not what the panel is opened to look at, and on a busy day they push
    /// the live sections below the fold. The count stays on the header, so a
    /// collapsed section still says how much is in it.
    var startsCollapsed: Bool {
        self == .finished || self == .abandoned
    }

    /// The buckets that hold live work — the ones flattened into their project.
    /// The order here is only the order they are *gathered* in; what a project's
    /// rows are stacked by is `byStart`, which is deliberately blind to status.
    static let live: [Category] = [.waiting, .running, .sleeping]
}

/// One heading in a list that still has headings — the right-rail bands
/// (active, recently finished, abandoned). Live work is no longer a stacked
/// list of status sections:
/// projects are tabs, and a waiter is marked in place on its own tab.
///
/// There is no machine-wide Finished tab any more: an ended run and a stuck
/// job appear under the project they ran in, as sibling sections of the
/// live table — finished first, abandoned last, so a stuck job is not
/// mistaken for a run that just ended. Each heading carries its count, so a
/// rolled-up section still says how much is in it. Active starts open; the
/// two record buckets start rolled up.
enum PanelSection: Hashable {
    case project(String)
    case category(Category)
    case active

    /// The empty project — a session in no workspace we can name. Last, and
    /// under a word that does not pretend to be a project name.
    static let other = PanelSection.project("")

    var title: String {
        switch self {
        case .project(let name): return name.isEmpty ? "Other" : name
        case .category(let category): return category.title
        case .active: return "Active"
        }
    }

    var startsCollapsed: Bool {
        switch self {
        case .project, .active: return false
        case .category(let category): return category.startsCollapsed
        }
    }

    /// Whether the user may roll this section up. A project is a tab now
    /// and cannot hide; every rail list — active and the two record
    /// buckets — folds.
    var isCollapsible: Bool {
        if case .project = self { return false }
        return true
    }

    var isProject: Bool {
        if case .project = self { return true }
        return false
    }
}

/// A row and the bucket it came from. The section no longer says what the
/// status is, so the row has to carry it — `AgentRowView` colours and captions
/// itself from it.
struct SectionRow: Identifiable {
    let agent: Agent
    let category: Category
    var id: String { agent.id }
}

/// One project tab's table: the live rows (already `byStart`-sorted by the
/// caller) when Active is open, then Recently finished when it is open, then
/// Abandoned — ended runs before stuck jobs, so a stuck job is not mistaken
/// for a run that just ended. A closed section's rows are excluded here,
/// which is what keeps them out of the keyboard walk and the selection
/// prune as well as off the screen: one list, no second computation.
func tabTableRows(live: [SectionRow], finished: [SectionRow],
                  abandoned: [SectionRow],
                  liveOpen: Bool, finishedOpen: Bool, abandonedOpen: Bool)
    -> [SectionRow] {
    (liveOpen ? live : [])
        + (finishedOpen ? finished : [])
        + (abandonedOpen ? abandoned : [])
}

/// Exact height of an Active / Recently finished / Abandoned list. Shrinks
/// to content under a 7.5-item cap; empty returns 0. `measured` is the
/// list's own reported content height (`ListHeightKey`): once it has landed
/// the row height is `measured / count`, the rows' business and not a
/// constant's, so the cap never cuts the last row mid-line. Before the
/// first measurement (`measured == 0`) the estimate is
/// `count × PanelMetrics.processRow`, the value the first frame draws with.
func processViewportHeight(count: Int, measured: CGFloat = 0) -> CGFloat {
    guard count > 0 else { return 0 }
    let row = measured > 0 ? measured / CGFloat(count) : PanelMetrics.processRow
    return min(CGFloat(count), PanelMetrics.railVisibleItems) * row
}

/// Whether a band whose content measures `measured` fits its `viewport`
/// with nothing hidden — the one condition under which its scrolling may
/// be switched off. Unmeasured content is never assumed to fit.
func processBandFits(measured: CGFloat, viewport: CGFloat) -> Bool {
    measured > 0 && measured <= viewport + 0.5
}
