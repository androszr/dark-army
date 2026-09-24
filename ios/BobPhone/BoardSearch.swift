import Foundation

/// The phone Board tab's grep rule.
///
/// Mirrors the Mac's `BoardState.matches` — trim, empty matches everything,
/// then `localizedStandardContains` — but scans **project** in place of the
/// Mac's prompt: the phone snapshot's prompt is clamped, and the person
/// asked to find a card by the project it sits in. `#card <hex>` is out of
/// scope here. Foundation only, so `swiftc` can run this file alone from
/// the host suite.
enum BoardSearch {
    static let placeholder = "grep title, summary, project…"

    static func needle(_ query: String) -> String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func isSearching(_ query: String) -> Bool {
        !needle(query).isEmpty
    }

    static func matches(query: String, title: String, summary: String,
                        project: String) -> Bool {
        let n = needle(query)
        guard !n.isEmpty else { return true }
        return title.localizedStandardContains(n)
            || summary.localizedStandardContains(n)
            || project.localizedStandardContains(n)
    }

    static func noMatchLine(query: String) -> String {
        "“\(needle(query))” is not in a title, a summary or a project."
    }
}
