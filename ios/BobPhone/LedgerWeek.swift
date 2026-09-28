import Foundation

// The History ledger's money, shared by the phone and the Mac.
//
// Everything from the `enum LedgerWeek {` line down is a byte-for-byte copy
// of `panel/Sources/BobPanel/LedgerWeek.swift`, pinned by
// `host/tests/test_ledger_week.py`: edit both copies together, never one.
// The Mac's History calls it for its seven-day figures, so the numbers on
// this phone's History screen are the Mac's numbers.

enum LedgerWeek {
    static let dash = "—"
    static let notPriced = "not priced"

    /// One session of a card, as the daemon's report carries it. Absent is
    /// nil, never 0: a 0 token cost would price a day at $0.00, and a
    /// reported 0 is a real report.
    struct Session: Equatable {
        var sessionId = ""
        var provider = ""
        var phase = ""
        var boundAt: Double?
        var known = false
        var tokenCostUsd: Double?
        var claudeTokenCostUsd: Double?
        var grokTokenCostUsd: Double?
        var codexTokenCostUsd: Double?
        var tokenUnpricedTurns: Int?
        var reportedCostUsd: Double?
    }

    struct Card: Equatable {
        var id = ""
        var sessions: [Session] = []
    }

    /// One local day of turns whose session is on no named card.
    struct OtherDay: Equatable {
        var day = ""
        var claude: Double = 0
        var grok: Double = 0
        var codex: Double = 0
        var claudeReported: Double?
        var grokReported: Double?
        var claudeUnpriced = 0
        var grokUnpriced = 0
        var codexUnpriced = 0
        var claudeUnpricedIds: [String] = []
        var grokUnpricedIds: [String] = []
        var codexUnpricedIds: [String] = []
    }

    /// One card cut to the provider shares that are on, placed on a day.
    struct Work: Equatable {
        var cardId = ""
        /// The ink: the largest share that is on, else the card's provider.
        var provider = ""
        var day = ""
        var bound: Double = 0
        var claude: Double = 0
        var grok: Double = 0
        var codex: Double = 0
        var anyTokenCost = false
        /// What the providers reported for the card's on sessions. Nil where
        /// nothing was reported; a reported 0 is 0.
        var reported: Double?
        /// No token cost at all.
        var unknown = false
        /// A token cost, but some of the card's work had no price.
        var partlyUnpriced = false
        var unpricedSessions: [String] = []

        var tokenCost: Double { claude + grok + codex }
    }

    struct Column: Equatable {
        var day = ""
        var weekday = ""
        var dayNumber = ""
        var claude: Double = 0
        var grok: Double = 0
        var codex: Double = 0
        var tokenCost: Double = 0
        var anyTokenCost = false
        var reported: Double?
        var unknown = false
        /// The token cost; a dash where nothing on the day could be priced;
        /// empty on a day nothing happened. Never `$0.00` for no money.
        var figure = ""
        /// `$X reported`, `$0.00 reported` included; nil with no report.
        var reportedMark: String?
    }

    struct Picture: Equatable {
        var days: [Column] = []
        /// The token cost summed, or `not priced` — never `$0.00` — where no
        /// day had one.
        var total = ""
        var tokenCost: Double = 0
        var anyTokenCost = false
        /// `$X reported` beside the total, never in it.
        var reported: String?
        var reportedSum: Double?
        /// Cards with no token cost at all.
        var unknownCards = 0
        /// Sessions with a turn not priced, each counted once.
        var unpricedSessions = 0
        var unpriced: Int { unknownCards + unpricedSessions }
        /// The median of the days' token costs.
        var usual: Double?
        var span = ""
    }

    struct Segment: Equatable {
        var provider = ""
        var value: Double = 0
    }

    // MARK: - The week

    /// The phone's picture and the Mac's seven-day spent lens: the last
    /// seven local days, every provider on, the leftover days included, and
    /// anything dated before the first column moved onto it.
    static func picture(cards: [Card], others: [OtherDay], now: Date = Date(),
                        calendar: Calendar = .current) -> Picture {
        let keys = dayKeys(now: now, calendar: calendar)
        let works = pin(cards.compactMap { work(card: $0, calendar: calendar) },
                        ontoFirstOf: keys)
        return fold(works: works, others: pinOther(others, ontoFirstOf: keys),
                    keys: keys, calendar: calendar)
    }

    /// The columns and their sums over placed works and leftover days.
    static func fold(works: [Work], others: [OtherDay], keys: [String],
                     claude: Bool = true, grok: Bool = true,
                     calendar: Calendar = .current) -> Picture {
        var picture = Picture()
        var sum = 0.0
        var anyTokenCost = false
        var reportedSum = 0.0
        var anyReported = false
        var unknownCards = 0
        var unpriced: Set<String> = []
        for key in keys {
            let onDay = works.filter { $0.day == key }.sorted { $0.bound < $1.bound }
            let other = others.first { $0.day == key }
            let column = column(day: key, works: onDay, other: other,
                                claude: claude, grok: grok, calendar: calendar)
            picture.days.append(column)
            if column.anyTokenCost {
                anyTokenCost = true
                sum += column.tokenCost
            }
            if let reported = column.reported {
                anyReported = true
                reportedSum += reported
            }
            if let other {
                unpriced.formUnion(unpricedIds(other, claude: claude, grok: grok))
            }
            unknownCards += onDay.filter(\.unknown).count
            for work in onDay { unpriced.formUnion(work.unpricedSessions) }
        }
        picture.anyTokenCost = anyTokenCost
        picture.tokenCost = sum
        picture.total = anyTokenCost ? usd(sum) : notPriced
        if anyReported {
            picture.reportedSum = reportedSum
            picture.reported = reportedWords(reportedSum)
        }
        picture.unknownCards = unknownCards
        picture.unpricedSessions = unpriced.count
        picture.usual = keys.isEmpty ? nil : median(picture.days.map(\.tokenCost))
        picture.span = span(keys, calendar: calendar)
        return picture
    }

    // MARK: - Cards

    /// One card cut to the provider shares that are on. Claude and Grok
    /// follow their switches, Codex is always on. A session is on when one
    /// of its priced shares is on and, with no split to go by, when the
    /// provider it ran as is on. Nil for a card with no bound session or
    /// with nothing on.
    static func work(card: Card, claude: Bool = true, grok: Bool = true,
                     calendar: Calendar = .current) -> Work? {
        guard let bound = earliest(card) else { return nil }
        var work = Work()
        work.cardId = card.id
        work.bound = bound
        work.day = dayKey(Date(timeIntervalSince1970: bound), calendar: calendar)
        var onShares: Set<String> = []
        var anySessionOn = false
        var reported = 0.0
        var anyReported = false
        var unpriced: [String] = []
        for session in card.sessions {
            let shares = tokenShares(session)
            if claude, let value = shares.claude {
                onShares.insert("claude")
                work.claude += value
            }
            if grok, let value = shares.grok {
                onShares.insert("grok")
                work.grok += value
            }
            if let value = shares.codex {
                onShares.insert("codex")
                work.codex += value
            }
            guard sessionOn(session, claude: claude, grok: grok) else { continue }
            anySessionOn = true
            if !session.known || (session.tokenUnpricedTurns ?? 0) > 0 {
                unpriced.append(session.sessionId.isEmpty
                                ? "\(card.id)#\(unpriced.count)" : session.sessionId)
            }
            if let value = session.reportedCostUsd {
                anyReported = true
                reported += value
            }
        }
        let anyToken = !onShares.isEmpty
        guard anySessionOn || anyToken else { return nil }
        // The ink follows the largest share that is on; a tie (a lone $0.00
        // share included) goes to the provider the card ran as when that one
        // is among them, else to the first priced.
        if anyToken {
            let values = ["claude": work.claude, "grok": work.grok, "codex": work.codex]
            let top = onShares.map { values[$0] ?? 0 }.max() ?? 0
            let tied = ["claude", "grok", "codex"].filter {
                onShares.contains($0) && values[$0] == top
            }
            let label = leadProvider(card)
            work.provider = tied.contains(label) ? label : (tied.first ?? label)
        } else {
            work.provider = leadProvider(card)
        }
        work.anyTokenCost = anyToken
        work.reported = anyReported ? reported : nil
        work.unknown = !anyToken
        work.partlyUnpriced = anyToken && !unpriced.isEmpty
        work.unpricedSessions = anyToken ? unpriced : []
        return work
    }

    /// Whether one session is on: by its priced shares when the daemon sent
    /// a split, by the provider it ran as when it did not.
    static func sessionOn(_ session: Session, claude: Bool, grok: Bool) -> Bool {
        let shares = tokenShares(session)
        if shares.claude == nil, shares.grok == nil, shares.codex == nil {
            return providerOn(session.provider, claude: claude, grok: grok)
        }
        return (claude && shares.claude != nil) || (grok && shares.grok != nil)
            || shares.codex != nil
    }

    /// One session's token cost by provider. The daemon's split when it sent
    /// one; otherwise the whole `tokenCostUsd` under the session's provider.
    static func tokenShares(_ session: Session)
        -> (claude: Double?, grok: Double?, codex: Double?) {
        if session.claudeTokenCostUsd != nil || session.grokTokenCostUsd != nil
            || session.codexTokenCostUsd != nil {
            return (session.claudeTokenCostUsd, session.grokTokenCostUsd,
                    session.codexTokenCostUsd)
        }
        guard let total = session.tokenCostUsd else { return (nil, nil, nil) }
        switch session.provider {
        case "grok": return (nil, total, nil)
        case "codex": return (nil, nil, total)
        default: return (total, nil, nil)
        }
    }

    /// The implementation session's provider, else the first named one.
    static func leadProvider(_ card: Card) -> String {
        if let session = card.sessions.first(where: {
            $0.phase == "implementation" && !$0.provider.isEmpty
        }) {
            return session.provider
        }
        return card.sessions.first { !$0.provider.isEmpty }?.provider ?? ""
    }

    /// Codex is always on. Claude and Grok follow the switches. Anything
    /// else rides with Claude.
    static func providerOn(_ provider: String, claude: Bool, grok: Bool) -> Bool {
        if provider == "codex" { return true }
        if provider == "grok" { return grok }
        return claude
    }

    /// A card is placed on its first bound session's local day.
    static func earliest(_ card: Card) -> Double? {
        let stamps = card.sessions.compactMap(\.boundAt).filter { $0 > 0 }
        return stamps.min()
    }

    // MARK: - Days

    /// One day's money: the token cost by provider for the assistants that
    /// are on, the reported dollar beside it, and whether any of it could
    /// not be priced.
    static func column(day: String, works: [Work], other: OtherDay?,
                       claude: Bool = true, grok: Bool = true,
                       calendar: Calendar = .current) -> Column {
        var column = Column()
        column.day = day
        if let date = date(from: day, calendar: calendar) {
            let weekday = calendar.component(.weekday, from: date)
            column.weekday = calendar.shortWeekdaySymbols[weekday - 1]
            column.dayNumber = String(calendar.component(.day, from: date))
        }
        var reported = 0.0
        var anyReported = false
        for work in works {
            if work.anyTokenCost {
                column.anyTokenCost = true
                column.claude += work.claude
                column.grok += work.grok
                column.codex += work.codex
            }
            if let value = work.reported {
                anyReported = true
                reported += value
            }
        }
        if let other {
            let claudePart = claude ? other.claude : 0
            let grokPart = grok ? other.grok : 0
            let codexPart = other.codex
            if claudePart != 0 || grokPart != 0 || codexPart != 0 {
                column.anyTokenCost = true
                column.claude += claudePart
                column.grok += grokPart
                column.codex += codexPart
            }
            if claude, let value = other.claudeReported {
                anyReported = true
                reported += value
            }
            if grok, let value = other.grokReported {
                anyReported = true
                reported += value
            }
            if unpricedCount(other, claude: claude, grok: grok) > 0 {
                column.unknown = true
            }
        }
        column.tokenCost = column.claude + column.grok + column.codex
        if anyReported {
            column.reported = reported
            column.reportedMark = reportedWords(reported)
        }
        if works.contains(where: { $0.unknown || $0.partlyUnpriced }) {
            column.unknown = true
        }
        if column.anyTokenCost {
            column.figure = usd(column.tokenCost)
        } else if column.unknown {
            column.figure = dash
        } else {
            column.figure = ""
        }
        return column
    }

    /// The bar's segments, top to bottom: Claude, Grok, Codex. A zero is
    /// omitted; a reported dollar is never a segment.
    static func segments(claude: Double, grok: Double, codex: Double) -> [Segment] {
        [Segment(provider: "claude", value: claude),
         Segment(provider: "grok", value: grok),
         Segment(provider: "codex", value: codex)].filter { $0.value > 0 }
    }

    static func segments(_ column: Column) -> [Segment] {
        segments(claude: column.claude, grok: column.grok, codex: column.codex)
    }

    /// The last `count` local days, oldest first, ending today.
    static func dayKeys(count: Int = 7, now: Date = Date(),
                        calendar: Calendar = .current) -> [String] {
        let today = calendar.startOfDay(for: now)
        return (0..<count).reversed().compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: today)
        }.map { dayKey($0, calendar: calendar) }
    }

    /// A work dated before the first column keeps its money by moving onto
    /// that column.
    static func pinned(_ work: Work, onto first: String) -> Work {
        guard work.day < first else { return work }
        var moved = work
        moved.day = first
        return moved
    }

    static func pin(_ works: [Work], ontoFirstOf keys: [String]) -> [Work] {
        guard let first = keys.first else { return works }
        return works.map { pinned($0, onto: first) }
    }

    /// Leftover rows from before the first column join that column; their
    /// sessions are on no named card, so dropping the row would drop the
    /// dollars entirely.
    static func pinOther(_ rows: [OtherDay], ontoFirstOf keys: [String]) -> [OtherDay] {
        guard let first = keys.first else { return rows }
        let early = rows.filter { $0.day < first }
        guard !early.isEmpty else { return rows }
        var merged = rows.first { $0.day == first } ?? OtherDay()
        merged.day = first
        for row in early {
            merged.claude += row.claude
            merged.grok += row.grok
            merged.codex += row.codex
            merged.claudeUnpriced += row.claudeUnpriced
            merged.grokUnpriced += row.grokUnpriced
            merged.codexUnpriced += row.codexUnpriced
            merged.claudeUnpricedIds += row.claudeUnpricedIds
            merged.grokUnpricedIds += row.grokUnpricedIds
            merged.codexUnpricedIds += row.codexUnpricedIds
            // A report only where one existed: nil plus nil stays nil.
            merged.claudeReported = sum(merged.claudeReported, row.claudeReported)
            merged.grokReported = sum(merged.grokReported, row.grokReported)
        }
        var rest = rows.filter { $0.day > first }
        rest.append(merged)
        return rest
    }

    static func sum(_ lhs: Double?, _ rhs: Double?) -> Double? {
        guard lhs != nil || rhs != nil else { return nil }
        return (lhs ?? 0) + (rhs ?? 0)
    }

    /// Leftover sessions some of whose turns that day had no token price.
    static func unpricedCount(_ other: OtherDay, claude: Bool, grok: Bool) -> Int {
        (claude ? other.claudeUnpriced : 0) + (grok ? other.grokUnpriced : 0)
            + other.codexUnpriced
    }

    /// The same sessions by id, so a week counts a session once. Without
    /// ids a session gets a per-day stand-in, which is the per-day count.
    static func unpricedIds(_ other: OtherDay, claude: Bool, grok: Bool) -> Set<String> {
        var ids: Set<String> = []
        func add(_ named: [String], _ count: Int, _ provider: String) {
            if named.isEmpty {
                for n in 0..<count { ids.insert("\(other.day)#\(provider)#\(n)") }
            } else {
                ids.formUnion(named)
            }
        }
        if claude { add(other.claudeUnpricedIds, other.claudeUnpriced, "claude") }
        if grok { add(other.grokUnpricedIds, other.grokUnpriced, "grok") }
        add(other.codexUnpricedIds, other.codexUnpriced, "codex")
        return ids
    }

    // MARK: - Words

    /// `$12.40`; `<$0.01` for a real sliver; `$0.00` only for a real zero.
    static func usd(_ value: Double) -> String {
        if value > 0 && value < 0.01 { return "<$0.01" }
        return String(format: "$%.2f", value)
    }

    /// A reported dollar in words, so it reads as a different kind of money
    /// from the token cost it sits beside.
    static func reportedWords(_ value: Double) -> String {
        "\(usd(value)) reported"
    }

    static func median(_ values: [Double]) -> Double {
        let ordered = values.sorted()
        guard !ordered.isEmpty else { return 0 }
        let mid = ordered.count / 2
        if ordered.count % 2 == 1 { return ordered[mid] }
        return (ordered[mid - 1] + ordered[mid]) / 2
    }

    static func dayKey(_ date: Date, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d",
                      parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    static func date(from key: String, calendar: Calendar) -> Date? {
        let parts = key.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var components = DateComponents()
        components.year = parts[0]
        components.month = parts[1]
        components.day = parts[2]
        return calendar.date(from: components)
    }

    /// `Wed 24 Sep`.
    static func label(_ key: String, calendar: Calendar) -> String {
        guard let date = date(from: key, calendar: calendar) else { return key }
        let weekday = calendar.component(.weekday, from: date)
        let name = calendar.shortWeekdaySymbols[weekday - 1]
        let day = calendar.component(.day, from: date)
        let month = calendar.shortMonthSymbols[calendar.component(.month, from: date) - 1]
        return "\(name) \(day) \(month)"
    }

    /// `Wed 24 Sep – Tue 30 Sep`, or one label for one day.
    static func span(_ keys: [String], calendar: Calendar) -> String {
        if let first = keys.first, let last = keys.last, first != last {
            return "\(label(first, calendar: calendar)) – \(label(last, calendar: calendar))"
        }
        return label(keys.first ?? "", calendar: calendar)
    }
}
