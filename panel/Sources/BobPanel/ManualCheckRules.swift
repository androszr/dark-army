import Foundation

// The Checks section's wording and ordering, shared by the Mac window and the
// phone's Menu → Manual checks screen. Foundation only; the phone copy is
// byte-equal from the `enum ManualCheckRules {` line down
// (`test_phone_manual_checks.py`), so edit both copies together.

enum ManualCheckRules {
    /// The person's note on Passed / Failed, the daemon's own clamp.
    static let noteLimit = 400

    /// The status filter's words, in the order both screens draw them.
    static let statusFilters = ["all", "open", "passed", "failed"]

    static let emptyLine = "No manual checks match."
    static let unavailableLine = "Dark Army could not open the manual checks."
    static let fetchFailedLine = "Dark Army could not read the manual checks."
    static let recordedLine = "Recorded. The check file says so now."

    /// The status as one capitalised word; a file out of shape says so
    /// whatever its `Status` line claims.
    static func statusWord(_ entry: ManualCheckEntry) -> String {
        if entry.malformed { return "MALFORMED" }
        switch entry.status {
        case "open": return "OPEN"
        case "passed": return "PASSED"
        case "failed": return "FAILED"
        default: return entry.status.isEmpty ? "OPEN" : entry.status.uppercased()
        }
    }

    /// Open checks first, then the rest, each newest first by `created`.
    /// Stable: two checks created together keep the daemon's order.
    static func openFirst(_ entries: [ManualCheckEntry]) -> [ManualCheckEntry] {
        let ranked = entries.enumerated().map { (index: $0.offset, entry: $0.element) }
        let sorted = ranked.sorted { a, b in
            let aOpen = isOpen(a.entry)
            let bOpen = isOpen(b.entry)
            if aOpen != bOpen { return aOpen }
            let aTime = moment(a.entry.created)
            let bTime = moment(b.entry.created)
            if aTime != bTime { return aTime > bTime }
            return a.index < b.index
        }
        return sorted.map(\.entry)
    }

    /// `<date time> · <project> · <status>` — the row's first line. The
    /// date and time are the file's own words, never re-zoned.
    static func rowLine(_ entry: ManualCheckEntry) -> String {
        var parts: [String] = []
        let when = stamp(entry.created)
        if !when.isEmpty { parts.append(when) }
        if !entry.project.isEmpty { parts.append(entry.project) }
        parts.append(statusWord(entry))
        return parts.joined(separator: " · ")
    }

    /// `PASSED — <note>` / `FAILED — <note>` / `OPEN`: what the person
    /// recorded, in their words.
    static func outcomeLine(_ entry: ManualCheckEntry) -> String {
        let word = statusWord(entry)
        let note = entry.outcome.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !entry.malformed, entry.status == "passed" || entry.status == "failed",
              !note.isEmpty, note.lowercased() != "none"
        else { return word }
        return "\(word) — \(note)"
    }

    /// The filter word as drawn.
    static func filterWord(_ filter: String) -> String {
        filter.uppercased()
    }

    /// Whether Passed / Failed may be offered on this entry: an open check
    /// in the shape, with a file to write into.
    static func mayRecord(_ entry: ManualCheckEntry) -> Bool {
        !entry.malformed && !entry.path.isEmpty && isOpen(entry)
    }

    /// The note as it will be sent: one line, clamped.
    static func clampedNote(_ note: String) -> String {
        let flat = note.split(whereSeparator: \.isNewline).joined(separator: " ")
        return String(flat.trimmingCharacters(in: .whitespaces).prefix(noteLimit))
    }

    static func isOpen(_ entry: ManualCheckEntry) -> Bool {
        entry.status == "open" || (entry.status.isEmpty && !entry.malformed)
    }

    /// `2026-09-25T14:32:00+02:00` → `2026-09-25 14:32`; anything shorter
    /// is shown as written.
    static func stamp(_ created: String) -> String {
        let text = created.trimmingCharacters(in: .whitespaces)
        guard text.count >= 16 else { return text }
        let head = String(text.prefix(16))
        return head.replacingOccurrences(of: "T", with: " ")
    }

    /// Seconds since 1970 for ordering; an unreadable stamp sorts oldest.
    static func moment(_ created: String) -> Double {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let text = created.trimmingCharacters(in: .whitespaces)
        if let date = formatter.date(from: text) {
            return date.timeIntervalSince1970
        }
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: text) {
            return date.timeIntervalSince1970
        }
        return 0
    }
}
