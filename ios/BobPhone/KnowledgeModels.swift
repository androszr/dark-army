import Foundation

/// One project's notes as the human reader returns them.
///
/// Tolerant: a missing key must never blank the list. Byte-copied to the
/// phone (`test_phone_knowledge.py` pins the pair).
struct KnowledgeReport: Decodable, Equatable {
    var supported = false
    var available = false
    var root = ""
    var entries: [KnowledgeEntry] = []
    var truncated = false
    var omittedKeys: [String] = []
    var offset = 0

    enum CodingKeys: String, CodingKey {
        case supported, available, root, entries, truncated, offset
        case omittedKeys = "omitted_keys"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        root = c.value(.root, "")
        entries = c.value(.entries, [])
        truncated = c.value(.truncated, false)
        omittedKeys = c.value(.omittedKeys, [])
        offset = c.value(.offset, 0)
    }

    init() {}

    static let unavailableLine = "Dark Army could not open those notes."
    static let fetchFailedLine = "Dark Army could not read those notes."

    /// Cap copy: notes were dropped, not that a note failed.
    var truncatedLine: String {
        let n = omittedKeys.count
        if n == 0 { return "Some notes were left out of this page." }
        if n == 1 {
            return "1 further note was left out of this page: \(omittedKeys[0])."
        }
        return "\(n) further notes were left out of this page: "
            + omittedKeys.joined(separator: ", ") + "."
    }
}

/// Shared load/write rules for the Mac window and the phone screen, so a
/// retarget or a failed fetch cannot keep another project's notes.
enum KnowledgeLoad {
    struct Outcome: Equatable {
        var report: KnowledgeReport
        var detail: String
    }

    /// `nil` means a stale reply — the person already moved on. A failed
    /// fetch returns an empty list plus a refusal, never the previous rows.
    static func apply(requested: String, current: String,
                      fetched: KnowledgeReport?) -> Outcome? {
        guard current == requested else { return nil }
        guard let fetched else {
            return Outcome(report: KnowledgeReport(),
                           detail: KnowledgeReport.fetchFailedLine)
        }
        if !fetched.available {
            var closed = fetched
            closed.entries = []
            return Outcome(report: closed,
                           detail: KnowledgeReport.unavailableLine)
        }
        if fetched.truncated {
            return Outcome(report: fetched, detail: fetched.truncatedLine)
        }
        return Outcome(report: fetched, detail: "")
    }

    static func mayWrite(report: KnowledgeReport, root: String,
                         key: String) -> Bool {
        !key.isEmpty && report.root == root
            && report.entries.contains { $0.key == key }
    }
}

/// One stored note. Empty `source` displays as agent (legacy channel writes).
struct KnowledgeEntry: Decodable, Equatable, Identifiable {
    var key = ""
    var question = ""
    var answer = ""
    var author = ""
    var createdAt: Double = 0
    var updatedAt: Double = 0
    var lastConfirmed: Double = 0
    var stale = ""
    var source = ""

    var id: String { key }

    var isStale: Bool { stale == "1" }
    var isUnconfirmed: Bool { lastConfirmed == 0 }
    /// Empty on old rows displays as agent.
    var sourceLabel: String { source == "person" ? "person" : "agent" }
    /// Archived advice, never an error word.
    var staleBadge: String { "archived" }
    var unconfirmedLabel: String { "not yet confirmed" }

    enum CodingKeys: String, CodingKey {
        case key, question, answer, author, stale, source
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case lastConfirmed = "last_confirmed"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.value(.key, "")
        question = c.value(.question, "")
        answer = c.value(.answer, "")
        author = c.value(.author, "")
        createdAt = c.value(.createdAt, 0)
        updatedAt = c.value(.updatedAt, 0)
        lastConfirmed = c.value(.lastConfirmed, 0)
        stale = c.value(.stale, "")
        source = c.value(.source, "")
    }

    init() {}
}
