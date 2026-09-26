import Foundation

// The plan list and reader's rules — the rows, the body, the search and the
// stale-reply rule. Foundation only (`test_phone_plans.py` runs this file
// under `swiftc` beside `ScoutReports.swift`, whose generic
// `ReportsLoad.apply` it builds on). Every decoder is tolerant: one absent
// key must never blank the list. Phone-only: the Mac lists no plans yet, so
// there is no twin and no byte pin; if the Mac ever does, this file becomes
// the pinned pair.

/// One plan as the index lists it. **No body**: the text is the second,
/// separate read (`PlanBody`), fetched when a row is opened.
struct PlanRow: Decodable, Equatable, Identifiable {
    var path = ""
    var root = ""
    var project = ""
    var name = ""
    var slug = ""
    var day = ""
    var writtenAt: Double = 0
    var title = ""
    var status = ""
    var area = ""
    var cardId = ""
    var cardTitle = ""
    var cardColumn = ""
    var bytes = 0

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, root, project, name, slug, day, title, status, area, bytes
        case writtenAt = "written_at"
        case cardId = "card_id"
        case cardTitle = "card_title"
        case cardColumn = "card_column"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.value(.path, "")
        root = c.value(.root, "")
        project = c.value(.project, "")
        name = c.value(.name, "")
        slug = c.value(.slug, "")
        day = c.value(.day, "")
        writtenAt = c.value(.writtenAt, 0)
        title = c.value(.title, "")
        status = c.value(.status, "")
        area = c.value(.area, "")
        cardId = c.value(.cardId, "")
        cardTitle = c.value(.cardTitle, "")
        cardColumn = c.value(.cardColumn, "")
        bytes = c.value(.bytes, 0)
    }

    init() {}

    /// The H1, else the file's name — never blank.
    var displayTitle: String {
        if !title.isEmpty { return title }
        if !name.isEmpty { return name }
        return (path as NSString).lastPathComponent
    }

    /// The filename's day is the plan's date: a plan iterated today keeps
    /// the day it was written. A plan outside `plans/` has none.
    var dateLine: String { day }

    /// `<status> · <area>` where the plan states them, else `""`.
    var headerLine: String {
        if status.isEmpty { return area }
        if area.isEmpty { return status }
        return status + " · " + area
    }

    /// `<project> · <day>`, either alone where the other is empty.
    var metaLine: String {
        if project.isEmpty { return dateLine }
        if dateLine.isEmpty { return project }
        return project + " · " + dateLine
    }

    /// `card: <title> · <column>` for a plan attached to a card, else `""`.
    var cardLine: String {
        guard !cardTitle.isEmpty else { return "" }
        return cardColumn.isEmpty ? "card: \(cardTitle)"
            : "card: \(cardTitle) · \(cardColumn)"
    }
}

/// The list, newest first, as the daemon composed it.
struct PlanIndex: Decodable, Equatable {
    var supported = false
    var available = false
    var rows: [PlanRow] = []
    var truncated = false
    var omitted = 0
    var roots = 0

    enum CodingKeys: String, CodingKey {
        case supported, available, rows, truncated, omitted, roots
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        rows = c.value(.rows, [])
        truncated = c.value(.truncated, false)
        omitted = c.value(.omitted, 0)
        roots = c.value(.roots, 0)
    }

    init() {}

    static let unavailableLine = "Dark Army could not list the plans."
    static let fetchFailedLine = "Dark Army could not read the plans."

    /// The page bound dropped the oldest rows; say how many.
    var truncatedLine: String {
        omitted == 1 ? "1 older plan was left out of this list."
            : "\(omitted) older plans were left out of this list."
    }
}

/// One plan opened: its header fields and the text without its H1.
struct PlanBody: Decodable, Equatable {
    var available = false
    var path = ""
    var root = ""
    var project = ""
    var name = ""
    var day = ""
    var title = ""
    var status = ""
    var area = ""
    var body = ""
    var writtenAt: Double = 0
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, path, root, project, name, day, title, status, area
        case body, reason
        case writtenAt = "written_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        root = c.value(.root, "")
        project = c.value(.project, "")
        name = c.value(.name, "")
        day = c.value(.day, "")
        title = c.value(.title, "")
        status = c.value(.status, "")
        area = c.value(.area, "")
        body = c.value(.body, "")
        writtenAt = c.value(.writtenAt, 0)
        reason = c.value(.reason, "")
    }

    init() {}

    static let fetchFailedLine = "Dark Army could not read that plan."
}

/// The list's grep rule, `ScoutReportSearch`'s shape: trim, empty matches
/// everything, then `localizedStandardContains` (case- and
/// diacritic-insensitive) over what a row draws or names.
enum PlanSearch {
    static let placeholder = "grep title, project, status, area…"

    static func needle(_ query: String) -> String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func isSearching(_ query: String) -> Bool {
        !needle(query).isEmpty
    }

    static func matches(query: String, row: PlanRow) -> Bool {
        let n = needle(query)
        guard !n.isEmpty else { return true }
        return [row.title, row.project, row.status, row.area, row.slug,
                row.cardTitle]
            .contains { $0.localizedStandardContains(n) }
    }

    static func noMatchLine(query: String) -> String {
        "“\(needle(query))” is not in a title, a project, a status, an area or a card."
    }
}

/// The stale-reply rule for a plan read, on `ReportsLoad.apply`'s generic:
/// a reply for a request the person has moved on from is dropped (`nil`); a
/// failed fetch never keeps the previous value.
enum PlansLoad {
    typealias Outcome<Value: Equatable> = ReportsLoad.Outcome<Value>

    /// The list: unavailable empties the rows and says so; a truncated page
    /// keeps its rows and says how many older ones were left out.
    static func apply(requested: String, current: String,
                      fetched: PlanIndex?) -> Outcome<PlanIndex>? {
        guard let outcome = ReportsLoad.apply(
            requested: requested, current: current, fetched: fetched,
            empty: PlanIndex(), failedLine: PlanIndex.fetchFailedLine)
        else { return nil }
        guard fetched != nil else { return outcome }
        if !outcome.value.available {
            var closed = outcome.value
            closed.rows = []
            return Outcome(value: closed, detail: PlanIndex.unavailableLine)
        }
        if outcome.value.truncated {
            return Outcome(value: outcome.value,
                           detail: outcome.value.truncatedLine)
        }
        return outcome
    }

    /// One body: unavailable carries the daemon's own words in `detail`.
    static func apply(requested: String, current: String,
                      fetched: PlanBody?) -> Outcome<PlanBody>? {
        guard let outcome = ReportsLoad.apply(
            requested: requested, current: current, fetched: fetched,
            empty: PlanBody(), failedLine: PlanBody.fetchFailedLine)
        else { return nil }
        guard fetched != nil else { return outcome }
        if !outcome.value.available {
            let words = outcome.value.reason.isEmpty
                ? PlanBody.fetchFailedLine : outcome.value.reason
            return Outcome(value: outcome.value, detail: words)
        }
        return outcome
    }
}
