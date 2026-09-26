import Foundation

// The scout-report list and reader's shared rules — the rows, the body, the
// search, the header's order and the stale-reply rule — byte-copied to the
// phone (`test_scout_reports_surface.py` pins the pair and runs this file
// alone under `swiftc`). Foundation only. Every decoder is tolerant: one
// absent key must never blank the list. Nothing here knows what a scout is
// beyond the header's labels, so the manual-check section reuses the shapes.

/// One report as the index lists it. **No body**: the text is the second,
/// separate read (`ScoutReportBody`), fetched when a row is opened.
struct ScoutReportRow: Decodable, Equatable, Identifiable {
    var path = ""
    var root = ""
    var project = ""
    var folder = ""
    var day = ""
    var writtenAt: Double = 0
    var title = ""
    var verdict = ""
    var confidence = ""
    var recommendation = ""
    var question = ""
    var cardId = ""
    var cardTitle = ""
    var cardColumn = ""
    /// The report's own shape check, where the whole file was read for the
    /// list; `nil` is unknown, never "failed".
    var checked: Bool?
    var bytes = 0
    /// A text-search hit only: the body line the term was found on
    /// (whitespace collapsed, clipped — a snippet, not a citation), its
    /// 1-based line in the body and what matched (`body`). Empty on an
    /// index row.
    var snippet = ""
    var matchLine = 0
    var match = ""

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, root, project, folder, day, title, verdict, confidence
        case recommendation, question, checked, bytes, snippet, match
        case writtenAt = "written_at"
        case cardId = "card_id"
        case cardTitle = "card_title"
        case cardColumn = "card_column"
        case matchLine = "match_line"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.value(.path, "")
        root = c.value(.root, "")
        project = c.value(.project, "")
        folder = c.value(.folder, "")
        day = c.value(.day, "")
        writtenAt = c.value(.writtenAt, 0)
        title = c.value(.title, "")
        verdict = c.value(.verdict, "")
        confidence = c.value(.confidence, "")
        recommendation = c.value(.recommendation, "")
        question = c.value(.question, "")
        cardId = c.value(.cardId, "")
        cardTitle = c.value(.cardTitle, "")
        cardColumn = c.value(.cardColumn, "")
        checked = (try? c.decodeIfPresent(Bool.self, forKey: .checked)) ?? nil
        bytes = c.value(.bytes, 0)
        snippet = c.value(.snippet, "")
        matchLine = c.value(.matchLine, 0)
        match = c.value(.match, "")
    }

    init() {}

    /// The H1, else the folder, else the file's own name — never blank.
    var displayTitle: String {
        if !title.isEmpty { return title }
        if !folder.isEmpty { return folder }
        return (path as NSString).lastPathComponent
    }

    var dateLine: String { Self.dateLine(day: day, writtenAt: writtenAt) }

    /// When the report was written: the folder's day and the clock of the
    /// file's own time. A report with no dated folder takes the day from
    /// the file's time; with neither, nothing.
    static func dateLine(day: String, writtenAt: Double,
                         locale: Locale = .current,
                         timeZone: TimeZone = .current) -> String {
        let hasTime = writtenAt > 0
        let date = Date(timeIntervalSince1970: writtenAt)
        var dayText = day
        if dayText.isEmpty, hasTime {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = timeZone
            f.dateFormat = "yyyy-MM-dd"
            dayText = f.string(from: date)
        }
        guard hasTime else { return dayText }
        let clock = DateFormatter()
        clock.locale = locale
        clock.timeZone = timeZone
        clock.dateStyle = .none
        clock.timeStyle = .short
        return dayText + " · " + clock.string(from: date)
    }
}

/// The list, newest first, as the daemon composed it.
struct ScoutReportIndex: Decodable, Equatable {
    var supported = false
    var available = false
    var rows: [ScoutReportRow] = []
    var truncated = false
    var omitted = 0
    var roots = 0
    /// A text search's reply only (`q` on the list read): the term the
    /// daemon searched for, the bodies it read, the ones it could not, and
    /// whether the reading bound or the hit cap stopped it.
    var query = ""
    var searched = 0
    var unsearched = 0
    var searchTruncated = false
    var hitsTruncated = false

    enum CodingKeys: String, CodingKey {
        case supported, available, rows, truncated, omitted, roots
        case query, searched, unsearched
        case searchTruncated = "search_truncated"
        case hitsTruncated = "hits_truncated"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        rows = c.value(.rows, [])
        truncated = c.value(.truncated, false)
        omitted = c.value(.omitted, 0)
        roots = c.value(.roots, 0)
        query = c.value(.query, "")
        searched = c.value(.searched, 0)
        unsearched = c.value(.unsearched, 0)
        searchTruncated = c.value(.searchTruncated, false)
        hitsTruncated = c.value(.hitsTruncated, false)
    }

    init() {}

    static let unavailableLine = "Dark Army could not list the scout reports."
    static let fetchFailedLine = "Dark Army could not read the scout reports."
    /// A text search that failed — its own words, so a list that loaded
    /// fine is never captioned as unread.
    static let searchFailedLine = "Dark Army could not search the reports' text."

    /// The page bound dropped the oldest rows; say how many.
    var truncatedLine: String {
        omitted == 1 ? "1 older report was left out of this list."
            : "\(omitted) older reports were left out of this list."
    }

    /// What a text search could not cover, in words; `""` where it covered
    /// everything.
    var bodySearchLine: String {
        var parts: [String] = []
        if hitsTruncated {
            parts.append("The text search stopped at \(rows.count) hits.")
        }
        if searchTruncated {
            parts.append("The text search read the newest \(searched + unsearched) reports only.")
        }
        if unsearched > 0 {
            parts.append(unsearched == 1
                ? "1 report was not read for this search."
                : "\(unsearched) reports were not read for this search.")
        }
        return parts.joined(separator: " ")
    }

    /// The distinct projects the rows name, in first-seen order.
    var projects: [String] {
        var seen: [String] = []
        for row in rows where !row.project.isEmpty && !seen.contains(row.project) {
            seen.append(row.project)
        }
        return seen
    }
}

/// One answer-block value: a line of text, or a list (`sources`, and
/// `follow_ups`, whose `{title, summary}` items become "title — summary").
enum ScoutHeaderValue: Decodable, Equatable {
    case text(String)
    case items([String])

    private struct FollowUp: Decodable {
        var title = ""
        var summary = ""
        enum CodingKeys: String, CodingKey { case title, summary }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            title = c.value(.title, "")
            summary = c.value(.summary, "")
        }
    }

    init(from decoder: Decoder) throws {
        let single = try? decoder.singleValueContainer()
        if let text = try? single?.decode(String.self) {
            self = .text(text)
        } else if let list = try? single?.decode([String].self) {
            self = .items(list)
        } else if let list = try? single?.decode([FollowUp].self) {
            self = .items(list.map { item in
                item.summary.isEmpty ? item.title
                    : item.title + " — " + item.summary
            })
        } else {
            self = .text("")
        }
    }

    var lines: [String] {
        switch self {
        case .text(let text): return text.isEmpty ? [] : [text]
        case .items(let list): return list.filter { !$0.isEmpty }
        }
    }
}

/// One report opened: the answer block and the body with the block removed.
struct ScoutReportBody: Decodable, Equatable {
    var available = false
    var path = ""
    var root = ""
    var project = ""
    var title = ""
    var header: [String: ScoutHeaderValue] = [:]
    var body = ""
    var hasHeader = false
    var checked: Bool?
    var writtenAt: Double = 0
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, path, root, project, title, header, body, checked
        case reason
        case hasHeader = "has_header"
        case writtenAt = "written_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        root = c.value(.root, "")
        project = c.value(.project, "")
        title = c.value(.title, "")
        header = c.value(.header, [:])
        body = c.value(.body, "")
        hasHeader = c.value(.hasHeader, false)
        checked = (try? c.decodeIfPresent(Bool.self, forKey: .checked)) ?? nil
        writtenAt = c.value(.writtenAt, 0)
        reason = c.value(.reason, "")
    }

    init() {}

    static let fetchFailedLine = "Dark Army could not read that report."
}

/// The list's grep rule, the Board search's shape: trim, empty matches
/// everything, then `localizedStandardContains` (case- and
/// diacritic-insensitive) over what a row draws or names.
enum ScoutReportSearch {
    static let placeholder = "grep title, verdict, question, project…"
    /// The prompt where the Mac searches the reports' text too.
    static let bodyPlaceholder = "grep title, verdict, question, project, text…"
    /// A text search is asked for from this many characters of needle —
    /// `scout_index.MIN_QUERY_CHARS`, pinned equal by grep.
    static let minBodyChars = 3
    /// ... and sends at most this many — `scout_index.MAX_QUERY_CHARS`.
    static let maxBodyChars = 200

    static func needle(_ query: String) -> String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func isSearching(_ query: String) -> Bool {
        !needle(query).isEmpty
    }

    static func matches(query: String, row: ScoutReportRow) -> Bool {
        let n = needle(query)
        guard !n.isEmpty else { return true }
        return [row.title, row.verdict, row.question, row.project,
                row.cardTitle, row.recommendation]
            .contains { $0.localizedStandardContains(n) }
    }

    /// What a text search sends: the whitespace collapsed to single spaces
    /// (the daemon's own rule, so its echoed `query` equals this) and cut
    /// to `maxBodyChars` scalars, so a long line is never a refusal.
    static func bodyNeedle(_ query: String) -> String {
        let collapsed = query.split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
        let cut = String(String.UnicodeScalarView(
            collapsed.unicodeScalars.prefix(maxBodyChars)))
        return cut.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Whether the needle is long enough to ask the Mac to search the text.
    static func wantsBodySearch(_ query: String) -> Bool {
        bodyNeedle(query).count >= minBodyChars
    }

    /// The hits a text-search reply holds for this query, or none: a reply
    /// for another needle (typed past, or the field cleared) is not merged.
    static func hitRows(query: String, hits: ScoutReportIndex) -> [ScoutReportRow] {
        guard wantsBodySearch(query), hits.query == bodyNeedle(query) else { return [] }
        return hits.rows
    }

    static func noMatchLine(query: String, searchedText: Bool = false) -> String {
        searchedText
            ? "“\(needle(query))” is not in a title, a verdict, a question, a project or any report's text."
            : "“\(needle(query))” is not in a title, a verdict, a question or a project."
    }

    /// The instant filter's rows and the text search's hits as one list:
    /// the index rows passing `matches`, then every hit whose path is not
    /// among them — a path found both ways is listed once, the index row
    /// carrying the hit's snippet — newest first (`writtenAt` descending,
    /// then `path`). Below `minBodyChars` the hits are ignored.
    static func merge(query: String, rows: [ScoutReportRow],
                      hits: [ScoutReportRow]) -> [ScoutReportRow] {
        var out = rows.filter { matches(query: query, row: $0) }
        guard wantsBodySearch(query), !hits.isEmpty else { return out }
        var at: [String: Int] = [:]
        for (i, row) in out.enumerated() where at[row.path] == nil {
            at[row.path] = i
        }
        let known = Dictionary(rows.map { ($0.path, $0) },
                               uniquingKeysWith: { first, _ in first })
        for hit in hits {
            if let i = at[hit.path] {
                if out[i].snippet.isEmpty {
                    out[i].snippet = hit.snippet
                    out[i].matchLine = hit.matchLine
                    out[i].match = hit.match
                }
                continue
            }
            var row = known[hit.path] ?? hit
            row.snippet = hit.snippet
            row.matchLine = hit.matchLine
            row.match = hit.match
            at[row.path] = out.count
            out.append(row)
        }
        return out.sorted { a, b in
            a.writtenAt != b.writtenAt ? a.writtenAt > b.writtenAt : a.path < b.path
        }
    }
}

/// The answer block as labelled lines, in `scout_report.HEADER_KEYS`'
/// order: one line per follow-up, the sources joined, a missing key
/// omitted.
enum ScoutReportHeader {
    static let order: [(key: String, label: String)] = [
        ("card", "Card"), ("project", "Project"), ("question", "Question"),
        ("verdict", "Verdict"), ("confidence", "Confidence"),
        ("recommendation", "Recommendation"), ("follow_ups", "Follow-up"),
        ("sources", "Sources"),
    ]

    static func rows(_ header: [String: ScoutHeaderValue])
        -> [(label: String, value: String)] {
        var out: [(label: String, value: String)] = []
        for (key, label) in order {
            guard let value = header[key] else { continue }
            let lines = value.lines
            if lines.isEmpty { continue }
            if key == "follow_ups" {
                for line in lines { out.append((label, line)) }
            } else {
                out.append((label, lines.joined(separator: ", ")))
            }
        }
        return out
    }
}

/// The verdict line a finished scout's card draws — on the Mac's board
/// tile, at the top of the card window's report section and above the
/// report on the phone's card screen — from the two values the daemon
/// stored at the attach (`report_verdict`, `report_recommendation`). One
/// rule, byte-pinned Mac/phone; no client parses a report to find it.
enum ScoutVerdictLine {
    /// The recommendation token as words (`scout_report.RECOMMENDATIONS`).
    static let words: [String: String] = [
        "build": "build",
        "do-not-build": "do not build",
        "needs-decision": "needs a decision",
        "more-scouting": "more scouting",
    ]

    /// The mapped phrase, an unknown non-empty token as it came, `""` for
    /// none.
    static func word(_ recommendation: String) -> String {
        let token = recommendation.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return "" }
        return words[token] ?? token
    }

    /// `nil` where the verdict is empty (nothing is drawn); the verdict
    /// alone where there is no recommendation; else `<verdict> · <word>`.
    static func text(verdict: String, recommendation: String) -> String? {
        let line = verdict.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else { return nil }
        let phrase = word(recommendation)
        return phrase.isEmpty ? line : "\(line) · \(phrase)"
    }

    /// The accessibility label, composed from the two fields the line draws
    /// and nothing else; `""` where `text` is `nil`.
    static func spoken(verdict: String, recommendation: String) -> String {
        guard text(verdict: verdict, recommendation: recommendation) != nil else {
            return ""
        }
        let line = verdict.trimmingCharacters(in: .whitespacesAndNewlines)
        let phrase = word(recommendation)
        return phrase.isEmpty
            ? "verdict: \(line)"
            : "verdict: \(line). recommendation: \(phrase)"
    }
}

/// The stale-reply rule for a report read, `KnowledgeLoad.apply`'s shape and
/// generic over what was fetched: a reply for a request the person has
/// moved on from is dropped (`nil`); a failed fetch never keeps the previous
/// value.
enum ReportsLoad {
    struct Outcome<Value: Equatable>: Equatable {
        var value: Value
        var detail: String
    }

    static func apply<Value: Equatable>(requested: String, current: String,
                                        fetched: Value?, empty: Value,
                                        failedLine: String) -> Outcome<Value>? {
        guard current == requested else { return nil }
        guard let fetched else {
            return Outcome(value: empty, detail: failedLine)
        }
        return Outcome(value: fetched, detail: "")
    }

    /// The list: unavailable empties the rows and says so; a truncated page
    /// keeps its rows and says how many older ones were left out.
    static func apply(requested: String, current: String,
                      fetched: ScoutReportIndex?) -> Outcome<ScoutReportIndex>? {
        guard let outcome = apply(requested: requested, current: current,
                                  fetched: fetched, empty: ScoutReportIndex(),
                                  failedLine: ScoutReportIndex.fetchFailedLine)
        else { return nil }
        guard fetched != nil else { return outcome }
        if !outcome.value.available {
            var closed = outcome.value
            closed.rows = []
            return Outcome(value: closed,
                           detail: ScoutReportIndex.unavailableLine)
        }
        if outcome.value.truncated {
            return Outcome(value: outcome.value,
                           detail: outcome.value.truncatedLine)
        }
        return outcome
    }

    /// A text search's hits: a failed or unavailable reply empties them and
    /// says so in the search's own words (`searchFailedLine`), never the
    /// list's; an answered one says what the bounds left out, if anything.
    static func applyHits(requested: String, current: String,
                          fetched: ScoutReportIndex?) -> Outcome<ScoutReportIndex>? {
        guard current == requested else { return nil }
        guard let fetched, fetched.available else {
            return Outcome(value: ScoutReportIndex(),
                           detail: ScoutReportIndex.searchFailedLine)
        }
        return Outcome(value: fetched, detail: fetched.bodySearchLine)
    }

    /// One body: unavailable carries the daemon's own words in `detail`.
    static func apply(requested: String, current: String,
                      fetched: ScoutReportBody?) -> Outcome<ScoutReportBody>? {
        guard let outcome = apply(requested: requested, current: current,
                                  fetched: fetched, empty: ScoutReportBody(),
                                  failedLine: ScoutReportBody.fetchFailedLine)
        else { return nil }
        guard fetched != nil else { return outcome }
        if !outcome.value.available {
            let words = outcome.value.reason.isEmpty
                ? ScoutReportBody.fetchFailedLine : outcome.value.reason
            return Outcome(value: outcome.value, detail: words)
        }
        return outcome
    }
}
