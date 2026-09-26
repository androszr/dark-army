import Foundation

/// What the agent sheet's lead says and how big its still is, as rules with
/// no view in them. Foundation only, so the host suite compiles and runs
/// them under `swiftc` beside `PhoneSheet.swift` and `ConversationModels.swift`
/// (`host/tests/test_phone_sheet_lead.py`), and the phone job runs the same
/// table (`AgentSheetLeadTests.swift`).
///
/// The half-height sheet exists to answer from, so at that height the lead
/// is a glance: the name, one status line, the card chip. The large still
/// comes back when the sheet is dragged up.
enum AgentSheetLead {
    /// The still beside the name at half height.
    static let compactStill: CGFloat = 40
    /// The still at full height — the lead's large portrait.
    static let fullStill: CGFloat = 96

    /// The lead still's size for the sheet's current height. A view drawn
    /// outside a sheet (no detent: a saved decision, a held notification
    /// page) keeps the full size it always had.
    static func stillSize(detent: SheetDetent?) -> CGFloat {
        detent == .medium ? compactStill : fullStill
    }

    /// The one line under the name: what the agent is doing, how long it
    /// has been going and how full its context is — the fleet row's facts
    /// in words. Empty parts are dropped, and so is `FleetAge.text`'s
    /// undated `—`, which would read as a stray dash in a sentence.
    static func statusLine(head: String, age: String, ctx: String) -> String {
        let trimmedAge = age.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = [head, trimmedAge == "—" ? "" : trimmedAge, ctx]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return parts.joined(separator: " · ")
    }

    /// The fleet row's CTX cell in words: `ctx 20%↑`. Nothing where the Mac
    /// sent no figure.
    static func ctxText(pct: Double?, marker: String) -> String {
        guard let pct, pct.isFinite else { return "" }
        return "ctx \(Int(pct.rounded()))%\(marker)"
    }

    /// The status line as a screen reader says it: the same parts, commas
    /// between them and no glyphs — `FleetAge.spoken`'s age and
    /// `spokenCtx`'s figure, so the pace arrow is read as a word.
    static func spokenStatus(head: String, age: String, ctx: String) -> String {
        [head, age, ctx]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && $0 != "—" }
            .joined(separator: ", ")
    }

    /// The context figure in words: `context 20 percent, rising`. `pace` is
    /// `Trend.Pace.spoken`, empty where the Mac sent no rate.
    static func spokenCtx(pct: Double?, pace: String) -> String {
        guard let pct, pct.isFinite else { return "" }
        let figure = "context \(Int(pct.rounded())) percent"
        let word = pace.trimmingCharacters(in: .whitespacesAndNewlines)
        return word.isEmpty ? figure : figure + ", " + word
    }

    /// Which of the snapshot's buckets holds the agent now, in the order the
    /// sheet looks them up (waiting, running, sleeping, finished,
    /// abandoned): the first `(name, holds)` pair that holds, else nil — the
    /// sheet then keeps the category it was opened with. The seed category
    /// is fixed at open; an agent that starts waiting, or is answered, moves
    /// bucket while its sheet is up, and the lead must say so.
    static func liveBucket(_ buckets: [(name: String, holds: Bool)]) -> String? {
        buckets.first { $0.holds }?.name
    }

    /// Every question of a dialog, trimmed, the empty ones dropped, one
    /// blank line between them — the words Details and the Conversation
    /// screen both draw above the answer controls.
    static func questionText(_ texts: [String]) -> String {
        texts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: "\n\n")
    }
}

/// How the conversation's turns become rows. A tool result never draws a
/// row of its own (it stays one tap away behind its call), and a run of
/// tool calls reads as one line naming the count and the tools, so the
/// agent's own words are what the screen shows.
enum ConversationFold {
    enum Row: Identifiable, Equatable {
        case turn(ConversationTurn)
        case run([ConversationTurn])

        /// The turn's own `seq`, or the run's first call's — stable while a
        /// growing conversation extends the last run.
        var id: Int {
            switch self {
            case .turn(let turn): return turn.seq
            case .run(let tools): return tools.first?.seq ?? 0
            }
        }
    }

    /// Whether a folded run draws open: opened by a tap, or holding a call
    /// the person had already expanded — a single expanded call that the
    /// next call turns into a run must not snap shut under the reader.
    static func runIsOpen(_ tools: [ConversationTurn], id: Int,
                          openRuns: Set<Int>, expanded: Set<Int>) -> Bool {
        openRuns.contains(id) || tools.contains { expanded.contains($0.seq) }
    }

    /// The fewest consecutive tool calls that fold into one line.
    static let minRun = 2

    /// The rows for `turns`, in order. A `"result"` turn is never a row and
    /// never breaks a run; consecutive `"tool"` turns gather; any other kind
    /// ends the gathering — fewer than `minRun` calls stay single rows,
    /// otherwise they become one run.
    static func rows(_ turns: [ConversationTurn], minRun: Int = minRun) -> [Row] {
        var out: [Row] = []
        var pending: [ConversationTurn] = []
        func flush() {
            if pending.isEmpty { return }
            if pending.count >= max(1, minRun) {
                out.append(.run(pending))
            } else {
                out.append(contentsOf: pending.map { Row.turn($0) })
            }
            pending = []
        }
        for turn in turns {
            switch turn.kind {
            case "result":
                continue
            case "tool":
                pending.append(turn)
            default:
                flush()
                out.append(.turn(turn))
            }
        }
        flush()
        return out
    }

    /// The folded line: `⚙ 5 tool calls · Bash ×3, Read, Grep` — names in
    /// the order first seen, a count where a tool ran more than once.
    static func summary(_ tools: [ConversationTurn]) -> String {
        let names = counted(tools).map { $0.count > 1 ? "\($0.name) ×\($0.count)" : $0.name }
        return "⚙ \(calls(tools.count)) · " + names.joined(separator: ", ")
    }

    /// The same line as a screen reader says it:
    /// `5 tool calls: Bash 3 times, Read, Grep`.
    static func spoken(_ tools: [ConversationTurn]) -> String {
        let names = counted(tools).map { $0.count > 1 ? "\($0.name) \($0.count) times" : $0.name }
        return "\(calls(tools.count)): " + names.joined(separator: ", ")
    }

    private static func calls(_ count: Int) -> String {
        count == 1 ? "1 tool call" : "\(count) tool calls"
    }

    private static func counted(_ tools: [ConversationTurn]) -> [(name: String, count: Int)] {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for tool in tools {
            let trimmed = tool.tool.trimmingCharacters(in: .whitespacesAndNewlines)
            let name = trimmed.isEmpty ? "tool" : trimmed
            if counts[name] == nil { order.append(name) }
            counts[name, default: 0] += 1
        }
        return order.map { ($0, counts[$0] ?? 0) }
    }
}
