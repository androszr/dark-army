import AppKit
import SwiftUI
import WebKit
import UniformTypeIdentifiers

/// The settings window: a sidebar of eight sections (`SettingsSections`)
/// beside the selected section's page, or — while the search box has text —
/// every section's hits. Every entry drawn is read off `SettingsMenuModel.rows`
/// through `SettingsSearch.groups`; the section table places groups and adds
/// no rows. `Theme` tokens throughout; the window is deliberately unscaled.
struct SettingsWindowRoot: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: SettingsWindowState
    let actions: SettingsActions

    var body: some View {
        let rows = actions.rows()
        let groups = SettingsSearch.groups(rows)
        let pages = SettingsSections.pages(groups)
        let footer = SettingsSections.footer(groups)
        HStack(spacing: 0) {
            SettingsSidebar(state: state, actions: actions, footer: footer,
                            security: client.snapshot.security)
                .frame(width: SettingsWindowMetrics.sidebarWidth)
            Rectangle().fill(Theme.line).frame(width: SettingsWindowMetrics.dividerWidth)
            Group {
                if state.isSearching {
                    SettingsResultsView(
                        hits: SettingsSearch.hits(pages: pages, footer: footer,
                                                  query: state.query),
                        query: state.query, state: state)
                } else if let page = pages.first(where: { $0.id == state.section }) {
                    SettingsPageView(page: page, rows: rows, actions: actions, state: state)
                        .id(page.id)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .sheet(isPresented: $state.designSystemOpen) {
            VStack(spacing: 0) {
                HStack {
                    Text("Design system").font(Theme.prose(16, weight: .semibold))
                    Spacer()
                    Button("Close") { state.designSystemOpen = false }
                        .buttonStyle(.plain)
                        .clickable()
                        .accessibilityLabel("Close design system")
                }
                .padding(16)
                SignalWorkshopMac()
            }
            .frame(minWidth: 700, minHeight: 600)
            .background(Theme.canvas)
        }
        // Escape: clear the query first, then close — the panel's own
        // precedent for a window's cancel action. Zero-size so it draws nothing.
        .background(
            Button("") { state.escape() }
                .keyboardShortcut(.cancelAction)
                .frame(width: 0, height: 0)
                .opacity(0)
                .accessibilityHidden(true)
        )
        // ⌘1…⌘8: the sections, on the same zero-size precedent. On this
        // window only — never the panel's main menu, which would make them
        // app-wide.
        .background(
            ZStack {
                ForEach(SettingsSectionID.allCases) { section in
                    Button("") { state.select(section: section) }
                        .keyboardShortcut(
                            KeyEquivalent(Character(String(section.keyDigit))),
                            modifiers: .command)
                        .frame(width: 0, height: 0)
                        .opacity(0)
                        .accessibilityHidden(true)
                }
            }
        )
    }
}

/// File access is limited to the copied SwiftPM workshop directory. There is
/// no script message handler and no route from web content to DaemonClient.
private struct SignalWorkshopMac: NSViewRepresentable {
    func makeCoordinator() -> Guard { Guard() }

    func makeNSView(context: Context) -> WKWebView {
        let web = WKWebView(frame: .zero)
        web.navigationDelegate = context.coordinator
        // Import JSON is an `<input type=file>`: on macOS it opens nothing
        // unless a UI delegate answers with a panel (review, 25 Sep 2026).
        web.uiDelegate = context.coordinator
        if let root = PanelResources.url(folder: "workshop", file: "index.html") {
            context.coordinator.root = root.deletingLastPathComponent().standardizedFileURL
            web.loadFileURL(root, allowingReadAccessTo: context.coordinator.root!)
        }
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {}

    final class Guard: NSObject, WKNavigationDelegate, WKUIDelegate {
        var root: URL?

        /// The workshop's Import JSON: one JSON file, never a folder. The
        /// page itself caps and validates what it reads.
        func webView(_ webView: WKWebView,
                     runOpenPanelWith parameters: WKOpenPanelParameters,
                     initiatedByFrame frame: WKFrameInfo,
                     completionHandler: @escaping ([URL]?) -> Void) {
            let panel = NSOpenPanel()
            panel.allowedContentTypes = [.json]
            panel.allowsMultipleSelection = false
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.begin { response in
                completionHandler(response == .OK ? panel.urls : nil)
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let root, let url = action.request.url,
                  action.targetFrame != nil,
                  url.isFileURL,
                  url.standardizedFileURL.path.hasPrefix(root.path + "/") else {
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
    }
}
