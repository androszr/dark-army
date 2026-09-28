import Foundation

/// The Conversation tab kept live while a person watches it (26 Sep 2026):
/// the words they just sent drawn at once, the page followed quickly while
/// the agent works, and one line saying what it is doing right now. Pure
/// rules; the screen and `PhoneClient` only wire them.
enum ConversationLive {
    /// A message the person sent that the agent's journal has not recorded
    /// yet, drawn under the turns until it has.
    struct Echo: Identifiable, Equatable {
        var id: String
        var text: String
        /// The Mac took it (`.accepted` or later); before that it is still
        /// on its way.
        var delivered: Bool
        var createdAt: Double
    }

    /// How long an echo may wait for its turn before it is dropped. A
    /// channel message the agent never records must not sit under the
    /// conversation for ever; three minutes outlasts any real delay.
    static let echoLifetime: TimeInterval = 180
    /// A sent message is matched only to a person's turn written after the
    /// press, less this much for the Mac's and the phone's clocks to differ.
    static let clockSlack: TimeInterval = 30
    /// The quick pace at home while following. Away, the Mac's own pushed
    /// picture is what asks (`kicksOnPush`), never a faster relay loop.
    static let followPause: UInt64 = 1_000_000_000

    /// Words compared the way they are drawn: whitespace runs as one space,
    /// ends trimmed. The journal keeps the person's text as typed, but a
    /// terminal may re-wrap it.
    static func normalised(_ text: String) -> String {
        text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    /// Has a person's turn in `turns` recorded this message? Equal words,
    /// or a turn that holds them (a harness can wrap a message), written no
    /// earlier than the press less `clockSlack`. A turn with no time still
    /// counts: an older Mac sends none, and a duplicate beats a stuck echo.
    static func landed(text: String, createdAt: Double,
                       turns: [ConversationTurn]) -> Bool {
        let want = normalised(text)
        guard !want.isEmpty else { return true }
        for turn in turns.reversed() where turn.kind == "user" {
            if turn.ts > 0, createdAt > 0, turn.ts < createdAt - clockSlack { break }
            let have = normalised(turn.text)
            if have == want || have.contains(want) { return true }
        }
        return false
    }

    /// The echoes to draw for one session: its messages (`reply` presses
    /// carrying words) not refused, not stuck, younger than `echoLifetime`
    /// and not yet in the journal, oldest first. A refused or stuck message
    /// is the answer box's note and the Queue's row, never an echo.
    static func echoes(receipts: [Receipt], sessionId: String,
                       turns: [ConversationTurn], now: Double) -> [Echo] {
        guard !sessionId.isEmpty else { return [] }
        let live: Set<Receipt.State> = [.queued, .sending, .sent, .accepted, .done]
        return receipts
            .filter { r in
                r.action == PhoneActions.reply
                    && (r.fields["session_id"] ?? "") == sessionId
                    && live.contains(r.state)
                    && now - r.createdAt < echoLifetime
            }
            .sorted { $0.createdAt < $1.createdAt }
            .compactMap { r in
                let text = (r.fields["text"] ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty,
                      !landed(text: text, createdAt: r.createdAt, turns: turns)
                else { return nil }
                return Echo(id: r.id, text: text,
                            delivered: r.state == .accepted || r.state == .done,
                            createdAt: r.createdAt)
            }
    }

    /// Follow quickly while the agent is working or a sent message has not
    /// shown yet; otherwise the ordinary check-in is enough.
    static func follows(running: Bool, echoes: Int) -> Bool {
        running || echoes > 0
    }

    /// A pushed picture away from home asks for the watched conversation
    /// only while it is being followed and no ask is already out.
    static func kicksOnPush(following: Bool, watching: String?,
                            inFlight: Bool) -> Bool {
        following && !(watching ?? "").isEmpty && !inFlight
    }

    /// The echo's mark, drawn and spoken.
    static func mark(_ echo: Echo) -> String {
        echo.delivered ? "delivered" : "sending"
    }

    /// The one line under the turns while the agent works — the fleet
    /// row's own words (`PhoneAgentFacts.head`), and nothing when it is
    /// not working.
    static func nowLine(running: Bool, head: String) -> String {
        guard running else { return "" }
        return head.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The pill shown when new rows arrived while the person was scrolled
    /// up: "↓ 1 new", "↓ 3 new"; nothing for none.
    static func newPill(_ count: Int) -> String {
        count > 0 ? "↓ \(count) new" : ""
    }
}
