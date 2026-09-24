import ActivityKit
import Foundation

/// The Live Activity's attributes and content state — the one shape the app
/// starts, the Mac updates through `relay/api/push.js` and the widget draws.
/// Compiled into the app, the widget extension and the widget tests.
///
/// Foundation + ActivityKit only. The content state is the closed set of
/// at most fourteen keys `push.js` writes into `aps.content-state` — the
/// six face keys `nickname`, `slug`, `kind`, `work`, `since`, `session_id`,
/// the relay's own send time `updated_at`, plus the seven optional fleet
/// fields — spelled as the Mac spells them,
/// and decoded tolerantly: a missing key takes its default, so a Mac one
/// field ahead or behind never leaves the Lock Screen blank. A missing
/// count or cost is unknown, never zero.
struct NeedsYouAttributes: ActivityAttributes {
    /// How long a card may go without an update before it is drawn dim: the
    /// Mac went quiet (asleep, relay down). `push.js` sends the same figure
    /// as `stale-date`; iOS ends the activity itself after eight hours.
    static let staleAfter: TimeInterval = 1800
    /// An ended card leaves at once: the app's own end and the Mac's push
    /// both dismiss immediately, so nothing stale sits on the Lock Screen.
    static let dismissalDelay: TimeInterval = 0

    struct ContentState: Codable, Hashable {
        /// The agent's cast name, as the row wears it.
        var nickname: String = ""
        /// The portrait slug (`Cast.character(for:)` on the app, the Mac's
        /// `cast.character_for`), lowercase; empty draws an initial.
        var slug: String = ""
        /// `permission`, `question` or `attention`; anything else is drawn
        /// as attention.
        var kind: String = "attention"
        /// The bound card's title, else the session's own name — the buzz's
        /// `work` line, clamped to 80 on the Mac.
        var work: String = ""
        /// When the row went quiet, as a UTC epoch — the timer's start.
        var since: Double = 0
        /// The session the card is about; the deep link's target. Empty
        /// means nobody is waiting and the face row is not drawn.
        var sessionId: String = ""
        /// The strip's three counts. Nil when the mailbox dropped them —
        /// drawn as a dash, never as zero.
        var working: Int?
        var needsYou: Int?
        var standingBy: Int?
        /// Measured dollars, to the cent. Nil is unknown, never zero.
        var costUsd: Double?
        /// Tokens spent, in thousands. Nil draws a dash.
        var tokensK: Int?
        /// The fleet's burn over the last hour (the Mac's `BurnMeter`):
        /// dollars and thousands of tokens per hour. Nil until the Mac has
        /// watched long enough to say one — drawn as the totals alone.
        var costUsdHour: Double?
        var tokensKHour: Int?
        /// When this state was sent, as a UTC epoch: the relay's clock for a
        /// push from the Mac, the phone's for the app's own refresh. The card
        /// says "N min ago" from it; 0 (an older relay) draws a dash.
        var updatedAt: Double = 0

        enum CodingKeys: String, CodingKey {
            case nickname, slug, kind, work, since
            case sessionId = "session_id"
            case working = "working"
            case needsYou = "needs_you"
            case standingBy = "standing_by"
            case costUsd = "cost_usd"
            case tokensK = "tokens_k"
            case costUsdHour = "cost_usd_hour"
            case tokensKHour = "tokens_k_hour"
            case updatedAt = "updated_at"
        }

        init(nickname: String = "", slug: String = "", kind: String = "attention",
             work: String = "", since: Double = 0, sessionId: String = "",
             working: Int? = nil, needsYou: Int? = nil, standingBy: Int? = nil,
             costUsd: Double? = nil, tokensK: Int? = nil,
             costUsdHour: Double? = nil, tokensKHour: Int? = nil,
             updatedAt: Double = 0) {
            self.nickname = nickname
            self.slug = slug
            self.kind = kind
            self.work = work
            self.since = since
            self.sessionId = sessionId
            self.working = working
            self.needsYou = needsYou
            self.standingBy = standingBy
            self.costUsd = costUsd
            self.tokensK = tokensK
            self.costUsdHour = costUsdHour
            self.tokensKHour = tokensKHour
            self.updatedAt = updatedAt
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            nickname = (try? c.decodeIfPresent(String.self, forKey: .nickname)) ?? ""
            slug = (try? c.decodeIfPresent(String.self, forKey: .slug)) ?? ""
            let kindWord = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? ""
            kind = kindWord.isEmpty ? "attention" : kindWord
            work = (try? c.decodeIfPresent(String.self, forKey: .work)) ?? ""
            if let number = try? c.decodeIfPresent(Double.self, forKey: .since) {
                since = number
            } else if let whole = try? c.decodeIfPresent(Int.self, forKey: .since) {
                since = Double(whole)
            } else {
                since = 0
            }
            sessionId = (try? c.decodeIfPresent(String.self, forKey: .sessionId)) ?? ""
            working = (try? c.decodeIfPresent(Int.self, forKey: .working)) ?? nil
            needsYou = (try? c.decodeIfPresent(Int.self, forKey: .needsYou)) ?? nil
            standingBy = (try? c.decodeIfPresent(Int.self, forKey: .standingBy)) ?? nil
            if let number = try? c.decodeIfPresent(Double.self, forKey: .costUsd) {
                costUsd = number
            } else if let whole = try? c.decodeIfPresent(Int.self, forKey: .costUsd) {
                costUsd = Double(whole)
            } else {
                costUsd = nil
            }
            tokensK = (try? c.decodeIfPresent(Int.self, forKey: .tokensK)) ?? nil
            if let number = try? c.decodeIfPresent(Double.self, forKey: .costUsdHour) {
                costUsdHour = number
            } else if let whole = try? c.decodeIfPresent(Int.self, forKey: .costUsdHour) {
                costUsdHour = Double(whole)
            } else {
                costUsdHour = nil
            }
            tokensKHour = (try? c.decodeIfPresent(Int.self, forKey: .tokensKHour)) ?? nil
            if let number = try? c.decodeIfPresent(Double.self, forKey: .updatedAt) {
                updatedAt = number
            } else if let whole = try? c.decodeIfPresent(Int.self, forKey: .updatedAt) {
                updatedAt = Double(whole)
            } else {
                updatedAt = 0
            }
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(nickname, forKey: .nickname)
            try c.encode(slug, forKey: .slug)
            try c.encode(kind, forKey: .kind)
            try c.encode(work, forKey: .work)
            try c.encode(since, forKey: .since)
            try c.encode(sessionId, forKey: .sessionId)
            try c.encodeIfPresent(working, forKey: .working)
            try c.encodeIfPresent(needsYou, forKey: .needsYou)
            try c.encodeIfPresent(standingBy, forKey: .standingBy)
            try c.encodeIfPresent(costUsd, forKey: .costUsd)
            try c.encodeIfPresent(tokensK, forKey: .tokensK)
            try c.encodeIfPresent(costUsdHour, forKey: .costUsdHour)
            try c.encodeIfPresent(tokensKHour, forKey: .tokensKHour)
            if updatedAt > 0 { try c.encode(updatedAt, forKey: .updatedAt) }
        }

        /// The moment the card was last refreshed, or nil when no sender
        /// stamped it.
        var updatedDate: Date? {
            updatedAt > 0 ? Date(timeIntervalSince1970: updatedAt) : nil
        }

        /// A face row is drawn only while somebody is waiting.
        var hasFace: Bool { !sessionId.isEmpty }

        /// The one place the cost is spelled. A missing figure is the
        /// words, never `$0`.
        var costWord: String {
            guard let costUsd, costUsd.isFinite else { return "cost unknown" }
            return String(format: "$%.2f", locale: Locale(identifier: "en_US_POSIX"), costUsd)
        }

        /// Tokens, scaled to read at a glance, or a dash when the Mac
        /// published none: 45k, 990k, 1.1M, 23M.
        var tokensWord: String {
            guard let tokensK else { return "\u{2013}" }
            return "\(Self.tokenFigure(tokensK)) tok"
        }

        /// Dollars per hour over the last hour, or nil until the Mac said.
        var costRateWord: String? {
            guard let costUsdHour, costUsdHour.isFinite else { return nil }
            return String(format: "$%.2f/h", locale: Locale(identifier: "en_US_POSIX"),
                          costUsdHour)
        }

        /// Tokens per hour over the last hour, or nil until the Mac said.
        var tokensRateWord: String? {
            guard let tokensKHour else { return nil }
            return "\(Self.tokenFigure(tokensKHour)) tok/h"
        }

        /// Whether the card has a rate to lead with.
        var hasRate: Bool { costRateWord != nil || tokensRateWord != nil }

        /// A thousands figure as the eye reads it: under a thousand "k",
        /// under ten million one decimal "M", then whole "M".
        static func tokenFigure(_ thousands: Int) -> String {
            let k = max(0, thousands)
            if k < 1000 { return "\(k)k" }
            if k < 10_000 {
                return String(format: "%.1fM", locale: Locale(identifier: "en_US_POSIX"),
                              Double(k) / 1000)
            }
            return "\(k / 1000)M"
        }

        /// The kind's word for the card, from the closed set; a word this
        /// build does not know is attention.
        var kindWord: String {
            switch kind {
            case "permission": return "permission"
            case "question": return "question"
            default: return "attention"
            }
        }

        /// The instant the clock counts from.
        var sinceDate: Date { Date(timeIntervalSince1970: since) }
    }
}
