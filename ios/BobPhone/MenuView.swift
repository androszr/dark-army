import SwiftUI
import WebKit

/// The menu's sections, in the order the grid draws them. Raw values are the
/// words a deep link and an older draft use: `usage` and `comm` were tabs of
/// their own until the menu gathered them, so `bobphone://usage` (the
/// widget's meter) and a draft stamped `comm` still name them.
enum MenuSection: String, CaseIterable, Hashable, Identifiable {
    // `case rebuild` (raw value `rebuild`) is Rebuild & restart's tile, kept in
    // the one list below so the grid's order and every pin on it stay a single line.
    case usage, history, comm, scouting, checks, plans, rebuild, designSystem

    var id: String { rawValue }

    /// The tile's name, and the pushed screen's title.
    var title: String {
        switch self {
        case .usage: return "Usage"
        case .history: return "History"
        case .comm: return "Comm"
        case .scouting: return "Scouting"
        case .checks: return "Manual checks"
        case .plans: return "Plans"
        case .rebuild: return "Rebuild & restart"
        case .designSystem: return "Design system"
        }
    }

    var symbol: String {
        switch self {
        case .usage: return "chart.bar"
        case .history: return "calendar"
        case .comm: return "text.bubble"
        case .scouting: return "binoculars"
        case .checks: return "checklist"
        case .plans: return "doc.text"
        case .rebuild: return "arrow.triangle.2.circlepath"
        case .designSystem: return "square.grid.2x2"
        }
    }

    /// `built` is what a tile is against a Mac that says nothing. History,
    /// Scouting, Manual checks and Plans have their screens, but each needs
    /// a Mac that serves its read; against an older one the tile opens a
    /// page that says so, never an empty screen that looks like "none". They
    /// light through `lit(scoutReports:manualChecks:plans:historyWeek:)`.
    var built: Bool {
        switch self {
        case .usage, .comm, .designSystem: return true
        case .scouting, .checks, .plans, .history, .rebuild: return false
        }
    }

    /// Whether the tile is lit and opens its screen against this Mac:
    /// Scouting once the Mac lists its scout reports
    /// (`Board.scoutReportsSupported`), Manual checks once it serves the
    /// Checks section (`Board.manualChecksSupported`), Plans once it lists
    /// its projects' plans (`Board.plansSupported`), History once it serves
    /// the week (`Board.historyWeekSupported`), Rebuild & restart once the
    /// Mac says it can rebuild and the phone is on the home door
    /// (`RebuildRules.canPress`'s two terms), every other section as `built`.
    func lit(scoutReports: Bool, manualChecks: Bool = false,
             plans: Bool = false, historyWeek: Bool = false,
             rebuild: Bool = false) -> Bool {
        switch self {
        case .scouting: return scoutReports
        case .checks: return manualChecks
        case .plans: return plans
        case .history: return historyWeek
        case .rebuild: return rebuild
        default: return built
        }
    }
}

/// The Menu tab: a grid of sections, four across, each an icon over its
/// name. A tile pushes its section onto this tab's own stack, so the
/// system back button returns to the grid.
///
/// The destination is registered on the scroll view, never inside the lazy
/// grid — a `navigationDestination` inside a lazy container is the
/// documented failure mode of this API.
struct MenuView: View {
    @ObservedObject var client: PhoneClient
    /// Held by `ContentView`, so a deep link (`bobphone://usage`) can open a
    /// section and so Comm knows when it is the screen in front.
    @Binding var open: MenuSection?
    /// Whether the Menu tab is the one showing.
    let selected: Bool

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    /// Four across; two at an accessibility text size, where a quarter of a
    /// phone's width cannot hold "Manual checks" at all.
    private var columns: [GridItem] {
        let count = dynamicTypeSize.isAccessibilitySize ? 2 : 4
        return Array(repeating: GridItem(.flexible(), spacing: 10, alignment: .top),
                     count: count)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                PromptLine(path: "~/menu")
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(MenuSection.allCases) { section in
                        tile(section)
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .decryptSurface("MenuView")
        .navigationDestination(item: $open) { section in
            MenuSectionScreen(section: section, client: client,
                              selected: selected && open == section)
        }
    }

    private func tile(_ section: MenuSection) -> some View {
        let lit = section.lit(
            scoutReports: client.snapshot.board.scoutReportsSupported,
            manualChecks: client.snapshot.board.manualChecksSupported,
            plans: client.snapshot.board.plansSupported,
            historyWeek: client.snapshot.board.historyWeekSupported,
            rebuild: client.snapshot.rebuild.available && client.via != .relay)
        return DecryptButton(action: { open = section }) {
            VStack(spacing: 8) {
                Image(systemName: section.symbol)
                    .font(Theme.mono(22))
                    .frame(height: 28)
                    .accessibilityHidden(true)
                Text(section.title)
                    .font(Theme.prose(14, weight: .medium))
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .foregroundStyle(lit ? Theme.phosphor : Theme.faint)
            .padding(.vertical, 12)
            .padding(.horizontal, 4)
            .frame(maxWidth: .infinity, minHeight: 88)
            .background(Theme.card)
            .clipShape(RoundedRectangle(cornerRadius: Theme.cardRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius)
                .stroke(Theme.line, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(section.title)
        .accessibilityHint(lit ? "Opens \(section.title)"
                               : "Not built yet")
        .accessibilityAddTraits(.isButton)
    }
}

/// One section, pushed from the menu, with the system bar and its back
/// button. Each title is a literal (`test_phone_text_in_full`).
struct MenuSectionScreen: View {
    let section: MenuSection
    @ObservedObject var client: PhoneClient
    let selected: Bool

    var body: some View {
        content
            .background(Theme.bg)
            .overlay(ScanlineOverlay())
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(.visible, for: .navigationBar)
            .toolbarBackground(Theme.bar, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .tint(Theme.phosphor)
    }

    @ViewBuilder
    private var content: some View {
        switch section {
        case .usage:
            UsageView(usage: client.usage,
                      attribution: client.attribution,
                      client: client,
                      refreshing: client.refreshing) {
                await client.refreshNow()
            }
            .navigationTitle("usage")
        case .history:
            // The week only against a Mac that serves it; an older Mac keeps
            // the page that says so rather than an empty screen.
            if client.snapshot.board.historyWeekSupported {
                HistoryWeekView(client: client)
                    .navigationTitle("history")
            } else {
                MenuNotYet(path: "~/history",
                           sentence: "History opens here once the Mac's Dark Army serves the week.")
                    .navigationTitle("history")
            }
        case .comm:
            CommView(client: client, selected: selected)
                .navigationTitle("comm")
        case .scouting:
            // The list only against a Mac that serves it; an older Mac keeps
            // the page that says so rather than an empty list.
            if client.snapshot.board.scoutReportsSupported {
                ScoutReportsView(client: client)
            } else {
                MenuNotYet(path: "~/scouting",
                           sentence: "Scout reports open here once the Mac's Dark Army lists them.")
                    .navigationTitle("scouting")
            }
        case .checks:
            // The list only against a Mac that serves it, Scouting's rule.
            if client.snapshot.board.manualChecksSupported {
                ManualChecksView(client: client)
                    .navigationTitle("manual checks")
            } else {
                MenuNotYet(path: "~/checks",
                           sentence: "Manual checks open here once the Mac's Dark Army lists them.")
                    .navigationTitle("manual checks")
            }
        case .plans:
            // The list only against a Mac that serves it, Scouting's rule.
            if client.snapshot.board.plansSupported {
                PlansView(client: client)
            } else {
                MenuNotYet(path: "~/plans",
                           sentence: "Plans open here once the Mac's Dark Army lists them.")
                    .navigationTitle("plans")
            }
        case .rebuild:
            // Only against a Mac that publishes the section; an older Mac
            // keeps the page that says so.
            if client.snapshot.rebuild.available {
                RebuildView(client: client)
                    .navigationTitle("rebuild")
            } else {
                MenuNotYet(path: "~/rebuild",
                           sentence: "Rebuild & restart opens here once the Mac's Dark Army can rebuild from a source checkout.")
                    .navigationTitle("rebuild")
            }
        case .designSystem:
            SignalWorkshopPhone()
                .navigationTitle("Design system")
        }
    }
}

/// Bundled local HTML only. The workshop has no native bridge or daemon client.
private struct SignalWorkshopPhone: UIViewRepresentable {
    func makeCoordinator() -> Guard { Guard() }

    func makeUIView(context: Context) -> WKWebView {
        let web = WKWebView(frame: .zero)
        web.navigationDelegate = context.coordinator
        if let folder = Bundle.main.url(forResource: "workshop", withExtension: nil) {
            let root = folder.standardizedFileURL
            context.coordinator.root = root
            web.loadFileURL(root.appendingPathComponent("index.html"),
                            allowingReadAccessTo: root)
        }
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {}

    final class Guard: NSObject, WKNavigationDelegate {
        var root: URL?
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let root, let url = action.request.url,
                  action.targetFrame != nil, url.isFileURL,
                  url.standardizedFileURL.path.hasPrefix(root.path + "/") else {
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
    }
}

/// The page behind a tile whose screen is not built yet: it says so, rather
/// than drawing an empty list that reads as "nothing here".
private struct MenuNotYet: View {
    let path: String
    let sentence: String

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                PromptLine(path: path)
                CommentLine(text: sentence)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
