import Foundation

struct ConversationTurn: Codable, Identifiable, Equatable {
    var seq: Int = 0
    var kind: String = ""
    var ts: Double = 0
    var text: String = ""
    var tool: String = ""
    var brief: String = ""
    var callId: String = ""
    var resultBytes: Int = 0
    var truncated: Bool = false

    var id: Int { seq }

    enum CodingKeys: String, CodingKey {
        case seq, kind, ts, text, tool, brief
        case callId = "call_id"
        case resultBytes = "result_bytes"
        case truncated
    }

    init(seq: Int = 0, kind: String = "", ts: Double = 0, text: String = "",
         tool: String = "", brief: String = "", callId: String = "",
         resultBytes: Int = 0, truncated: Bool = false) {
        self.seq = seq
        self.kind = kind
        self.ts = ts
        self.text = text
        self.tool = tool
        self.brief = brief
        self.callId = callId
        self.resultBytes = resultBytes
        self.truncated = truncated
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = c.value(.seq, 0)
        kind = c.value(.kind, "")
        ts = c.value(.ts, 0)
        text = c.value(.text, "")
        tool = c.value(.tool, "")
        brief = c.value(.brief, "")
        callId = c.value(.callId, "")
        resultBytes = c.value(.resultBytes, 0)
        truncated = c.value(.truncated, false)
    }
}

struct ConversationPage: Decodable {
    var available: Bool = false
    var provider: String = ""
    var key: String = ""
    var reset: Bool = false
    var turns: [ConversationTurn] = []
    var nextSeq: Int = 0
    var total: Int = 0
    var more: Bool = false
    var reason: String = ""

    enum CodingKeys: String, CodingKey {
        case available, provider, key, reset, turns, total, more, reason
        case nextSeq = "next_seq"
    }

    init(available: Bool = false, provider: String = "", key: String = "",
         reset: Bool = false, turns: [ConversationTurn] = [], nextSeq: Int = 0,
         total: Int = 0, more: Bool = false, reason: String = "") {
        self.available = available
        self.provider = provider
        self.key = key
        self.reset = reset
        self.turns = turns
        self.nextSeq = nextSeq
        self.total = total
        self.more = more
        self.reason = reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        provider = c.value(.provider, "")
        key = c.value(.key, "")
        reset = c.value(.reset, false)
        turns = c.value(.turns, [])
        nextSeq = c.value(.nextSeq, 0)
        total = c.value(.total, 0)
        more = c.value(.more, false)
        reason = c.value(.reason, "")
    }
}

/// Where the catch-up cursor goes after a page.
///
/// A page is `pageTurns` turns (`conversation.py`'s `PAGE_TURNS`), and the
/// hops left can bring `hopsLeft × pageTurns` more. When the present is
/// further off than that — a long session opened away, three hops — paging
/// from the front would stop minutes short of it, so the cursor jumps to
/// the last page instead and that page lands as a reset (the daemon slices
/// any `since` up to `len(turns)`). Nil: keep paging in order.
enum ConversationCatchUp {
    static let pageTurns = 150

    static func jump(nextSeq: Int, total: Int, hopsLeft: Int) -> Int? {
        guard hopsLeft > 0, total - nextSeq > hopsLeft * pageTurns else { return nil }
        return max(0, total - pageTurns)
    }
}

enum ConversationRows {
    /// One formatter for every row: a `DateFormatter` is dear to make and
    /// the list holds thousands of them.
    private static let clockFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = .current
        formatter.dateFormat = "HH:mm"
        return formatter
    }()

    static func clock(_ ts: Double) -> String {
        guard ts != 0 else { return "" }
        return clockFormatter.string(from: Date(timeIntervalSince1970: ts))
    }

    static func toolLine(_ turn: ConversationTurn) -> String {
        let brief = turn.brief.trimmingCharacters(in: .whitespacesAndNewlines)
        if brief.isEmpty { return "⚙ \(turn.tool)" }
        return "⚙ \(turn.tool) · \(brief)"
    }

    static func resultLine(_ turn: ConversationTurn) -> String {
        let summary = turn.text.trimmingCharacters(in: .whitespacesAndNewlines)
        let bytes = turn.resultBytes
        if summary.isEmpty { return "↳ \(bytes) bytes" }
        return "↳ \(summary) · \(bytes) bytes"
    }

    static func spoken(_ turn: ConversationTurn, nickname: String) -> String {
        switch turn.kind {
        case "user":
            return "You said: \(turn.text)"
        case "agent":
            let name = nickname.isEmpty ? "the agent" : nickname
            return "\(name) said: \(turn.text)"
        case "tool":
            return "Tool \(turn.tool): \(turn.brief)"
        case "result":
            return "Result, \(turn.resultBytes) bytes: \(turn.text)"
        case "note":
            return turn.text
        default:
            return turn.text
        }
    }
}
