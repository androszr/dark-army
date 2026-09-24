import Foundation

// The Comm tab's rules — the quick questions, how a typed line is shaped
// before it reaches `terminal_input`, and the one status word the tab
// draws. Foundation only, and **byte-pinned to the panel** from the marker
// line down (`panel/Sources/BobPanel/CommRules.swift`, `host/tests/test_comm_rules.py`):
// edit both copies together, never one.
enum CommRules {
    /// The four quick questions. `label` is the chip; `question` is the line
    /// typed into Mission Control, verbatim, when the chip is pressed.
    static let chips: [(label: String, question: String)] = [
        (label: "Everyone", question: "What is everyone working on?"),
        (label: "Find a card",
         question: "Find a card: list every card that is not done, with its column and who has it."),
        (label: "Timeline", question: "What's the timeline?"),
        (label: "Pitfalls", question: "Any pitfalls?"),
    ]

    /// The daemon's `TERMINAL_MAX_INPUT_CHARS`, restated: a longer line is
    /// refused there in words, so it is clamped here first.
    static let maxChars = 2000

    /// One line for the terminal: whitespace collapsed to single spaces,
    /// clamped to `maxChars`, `""` when nothing was typed. The clamp counts
    /// **Unicode scalars**, because the daemon counts code points (`len()`
    /// on a Python `str`): a `Character` can be several scalars, so a
    /// Character clamp could still hand the daemon a line it refuses.
    static func line(from text: String) -> String {
        let words = text.split(whereSeparator: { $0.isWhitespace || $0.isNewline })
        let joined = words.joined(separator: " ")
        if joined.isEmpty { return "" }
        return String(joined.unicodeScalars.prefix(maxChars))
    }

    /// The one status word, from the snapshot's `mission` section and the
    /// bucket the mission row sits in (`Agents.row(session:)`'s second
    /// element; `""` where the row is not on the snapshot).
    static func status(available: Bool, alive: Bool, exited: Bool,
                       sessionId: String, rowState: String) -> String {
        if !available { return "older Mac" }
        if !alive { return exited ? "ended" : "off" }
        if sessionId.isEmpty { return "starting" }
        if rowState == "running" { return "thinking" }
        return "ready"
    }

    /// Whether a question may be sent: Mission Control is up and has a
    /// session to type into. A thinking session still takes a line — the
    /// CLI queues it.
    static func canAsk(status: String) -> Bool {
        status == "ready" || status == "thinking"
    }

    /// Whether End is drawn: anything alive, the still-starting one included.
    static func showsEnd(status: String) -> Bool {
        status == "ready" || status == "thinking" || status == "starting"
    }

    /// Whether the Open button is drawn: a Mac that could start one has none.
    static func showsOpen(status: String) -> Bool {
        status == "off" || status == "ended"
    }

    /// Whether a fleet row is Mission Control's own session — the one the
    /// Comm tab draws — and so stays **out of the project lists**: it lives
    /// in Dark Army's checkout, but it is not work on that project. Matched
    /// by the snapshot's `mission.session_id` (a quiet, evicted Mission
    /// Control keeps its id on the wire) or by the `mission` origin stamp
    /// the row carries while its state is live; the two together cover the
    /// `/clear` successor before the record learns its id.
    static func isMissionRow(sessionId: String, originBy: String,
                             missionSessionId: String) -> Bool {
        if originBy == "mission" { return true }
        return !missionSessionId.isEmpty && sessionId == missionSessionId
    }
}
