import Foundation

/// Which of the three lenses the picture is measuring. Waiting is not one.
enum LedgerLens: String, Equatable {
    case spent, tokens, time
}

enum LedgerMode: Equatable {
    case today, week, thin
}

/// What a mark on the ledger is. `claude`, `grok` and `codex` are each
/// provider's token cost; `reported` is a dollar a provider reported, drawn
/// beside the token cost and never in amber (amber is "your turn");
/// `unknown` is the not-priced dash.
enum LedgerInk: Equatable {
    case claude, grok, codex, reported, unknown
}

/// One card the way the ledger draws it. The arithmetic lives in
/// `LedgerFold`; this is the row.
struct LedgerCardLine: Equatable {
    var id = ""
    var title = ""
    var who = ""
    var project = ""
    var model = ""
    var clock = ""
    var figure = ""
    var turns = 0
    var manual = false
    /// Share of the day's largest lens magnitude, 0 when the card has no
    /// magnitude. Unknown spend stays 0 — the view may still draw a short
    /// row, and that width is not a dollar.
    var share: Double = 0
    var ink: LedgerInk = .claude
    var day = ""
}

struct LedgerDayColumn: Equatable {
    var day = ""
    var weekday = ""
    var dayNumber = ""
    /// The day's token cost: counted tokens at each publisher's price, for
    /// the assistants that are on. This is the day's total.
    var token: Double = 0
    var claudeToken: Double = 0
    var grokToken: Double = 0
    var codexToken: Double = 0
    /// The dollars providers reported that day, beside the total and never
    /// in it. Nil where nothing was reported; a reported 0 is 0.
    var reported: Double?
    /// The lens's magnitude, for the column's bar. Spent is the token cost
    /// alone; tokens and time are their own sums. Not a dollar when the lens
    /// is not spent.
    var magnitude: Double = 0
    var anyToken = false
    var unknown = false
    var figure = ""
    /// The reported dollar as its own mark, `$0.00` included when a report
    /// exists and is zero; nil when nothing was reported.
    var reportedMark: String?
    var noNamedCard = false
    var emptyNote: String?
    var otherLine: String?
    var cards: [LedgerCardLine] = []
}

struct LedgerPersonRow: Equatable {
    var who = ""
    var amount = ""
}

struct LedgerProjectRow: Equatable {
    var name = ""
    var root = ""
    /// Nil when this root is not in the current payload. Not $0.
    var amount: String?
}

struct LedgerRun: Equatable {
    var title = ""
    var scope = ""
    var spent = ""
    var tokens = ""
    var time = ""
    /// What the providers reported for this card's sessions, when any did.
    var reported: String?
    var verdict = ""
    var rework = ""
    var phases: [String] = []
    var manual = false
    var resume: String?
}

enum LedgerDock: Equatable {
    case rest(heading: String, acceptance: String, honesty: String)
    case day(title: String, spent: String, reported: String?,
             unknown: Bool, titles: [String], other: String?)
    case run(LedgerRun)
}

struct LedgerPicture: Equatable {
    var masthead = ""
    /// The headline: the token cost of Claude, Grok and Codex together.
    var spent = ""
    /// The reported dollars, formatted, when any provider reported one.
    /// Beside the headline, never added to it.
    var reported: String?
    var tokens = ""
    var time = ""
    var waiting = ""
    var aside = ""
    var acceptance = ""
    var showLegend = false
    var showProject = true
    var mode: LedgerMode = .week
    var days: [LedgerDayColumn] = []
    var focusDay = ""
    var people: [LedgerPersonRow] = []
    var projects: [LedgerProjectRow] = []
    var dock: LedgerDock = .rest(heading: "", acceptance: "", honesty: "")
    /// Median of the seven days' token-cost totals. Spend lens and the
    /// seven-column picture only; nil everywhere else.
    var usual: Double?
}

/// How the thin columns share the pane. Thirty and ninety days fill the
/// row; a longer history stays at `minimum` and scrolls. The hatch is drawn
/// inside each column, so a column has to be wide enough to hold it.
enum LedgerColumnLayout {
    static let spacing: Double = 3
    static let minimum: Double = 8

    static func columnWidth(count: Int, available: Double) -> Double {
        guard count > 0 else { return minimum }
        let gaps = spacing * Double(count - 1)
        let room = available - gaps
        guard room > 0 else { return minimum }
        let share = room / Double(count)
        return share >= minimum ? share : minimum
    }

    static func scrolls(count: Int, available: Double) -> Bool {
        guard count > 0, available > 0 else { return false }
        let gaps = spacing * Double(count - 1)
        return Double(count) * minimum + gaps > available
    }

    /// The month axis: the first of every five days, the last day, and the
    /// day that is open.
    static func showsTick(index: Int, count: Int, selected: Bool) -> Bool {
        if selected || count <= 1 { return true }
        if index == count - 1 { return true }
        return index % 5 == 0
    }
}

/// The only arithmetic the ledger draws. No SwiftUI, no daemon: a report
/// plus the lenses, and the picture comes back already decided.
enum LedgerFold {
    static let dash = "—"
    static let honesty = "The total is counted tokens at each provider's published price, for Claude, Grok and Codex. A dollar a provider reported sits beside it and is not added in. A dash is not priced, and it adds nothing. A day with money and no card is other sessions on this Mac."

    static func localDay(_ ts: Double, calendar: Calendar = .current) -> String {
        dayKey(Date(timeIntervalSince1970: ts), calendar: calendar)
    }

    /// Noon-ish on a local day, `daysAgo` before `now`. Tests and the
    /// picture share it so a card lands on the column it was aimed at.
    static func timestamp(daysAgo: Int, now: Date,
                          calendar: Calendar = .current) -> Double {
        let today = calendar.startOfDay(for: now)
        let day = calendar.date(byAdding: .day, value: -daysAgo, to: today) ?? today
        let at = calendar.date(byAdding: .hour, value: 15, to: day) ?? day
        return at.timeIntervalSince1970
    }

    static func acceptanceSentence(_ report: HistoryReport) -> String {
        let effect = report.effectiveness
        if effect.root.isEmpty {
            return "A total across the Mac is not computed."
        }
        if effect.outcomesAvailable {
            let summary = effect.summary
            let rate: String
            if let raw = summary.reworkRate {
                rate = "\(Int((raw * 100).rounded()))%"
            } else {
                rate = dash
            }
            let outstanding = summary.manualChecksOutstanding
            let checks = outstanding == 1
                ? "1 manual check outstanding"
                : "\(outstanding) manual checks outstanding"
            return "\(summary.acceptedOutcomes) accepted · "
                + "\(summary.submittedCards) submitted · rework \(rate) · \(checks)"
        }
        if effect.outcomesReason.isEmpty {
            return "Acceptance is not available for this period."
        }
        return effect.outcomesReason
    }

    static func picture(report: HistoryReport,
                        projects: [BoardProject],
                        range: String,
                        lens: LedgerLens,
                        claude: Bool,
                        grok: Bool,
                        person: String,
                        selectedDay: String,
                        openCard: String,
                        now: Date = Date(),
                        calendar: Calendar = .current) -> LedgerPicture {
        let acceptance = acceptanceSentence(report)
        let waiting = Format.duration(report.waiting.totalSeconds)
        let otherOn = report.effectiveness.root.isEmpty && person.isEmpty
        let works = included(report: report, claude: claude, grok: grok,
                             person: person, projects: projects, calendar: calendar)
        let keys = dayKeys(range: range, now: now, calendar: calendar,
                           works: works, report: report, otherOn: otherOn)
        // The daemon window is `now - N*86400`, which reaches the local day
        // before the first drawn column. Pin that day onto the first column
        // so its cards and leftover turns are drawn, not dropped. `all`
        // already starts at the earliest day.
        let placed = pin(works, ontoFirstOf: keys, range: range)
        let others = pinOther(report.otherDays, ontoFirstOf: keys, range: range)
        let mode = mode(for: range)
        // The money — each column's token cost by provider, the reported
        // dollar beside it, the not-priced marks and their sums — is
        // `LedgerWeek`'s, the fold the phone runs byte for byte. This view
        // adds tokens, time, people, projects and the dock around it.
        let week = LedgerWeek.fold(works: placed.map(\.week),
                                   others: otherOn ? others.map(weekOther) : [],
                                   keys: keys, claude: claude, grok: grok,
                                   calendar: calendar)
        var columns: [LedgerDayColumn] = []
        var tokenSum = 0
        var anyTokens = false
        var timeSum = 0
        var anyTime = false
        var splitUnknown = 0

        for (index, key) in keys.enumerated() {
            let onDay = placed.filter { $0.day == key }.sorted { $0.bound < $1.bound }
            let other = otherOn ? others.first { $0.day == key } : nil
            let column = column(money: week.days[index], works: onDay, other: other,
                                lens: lens, claude: claude, grok: grok)
            columns.append(column)
            // Tokens and time are folded from the works, not from `daily`.
            for work in onDay where work.anyTokens {
                anyTokens = true
                tokenSum += work.tokens
            }
            for work in onDay where work.anyTime {
                anyTime = true
                timeSum += work.timeMs
            }
            splitUnknown += onDay.reduce(0) { $0 + $1.splitUnknown }
            if let other, otherOn {
                let extra = (claude ? other.claudeOutputTokens : 0)
                    + (grok ? other.grokOutputTokens : 0)
                if extra > 0 {
                    anyTokens = true
                    tokenSum += extra
                }
                if claude { splitUnknown += other.cacheSplitUnknownTurns }
            }
        }

        var picture = LedgerPicture()
        picture.acceptance = acceptance
        picture.waiting = waiting
        picture.mode = mode
        picture.showLegend = mode == .week
        picture.showProject = report.effectiveness.root.isEmpty
        picture.days = columns
        picture.focusDay = keys.contains(selectedDay) ? selectedDay : (keys.last ?? "")
        // The columns' token costs summed are the headline. Not `daily`, and
        // no reported dollar is in it. The phone says "not priced" where
        // this says "cost unknown" or a real $0.00 for an empty range.
        if week.anyTokenCost {
            picture.spent = week.total
        } else if week.unpriced > 0 {
            picture.spent = "cost unknown"
        } else {
            picture.spent = Format.usd(0)
        }
        if let reportedSum = week.reportedSum {
            picture.reported = LedgerWeek.usd(reportedSum)
        }
        picture.tokens = anyTokens ? Format.count(tokenSum) : dash
        picture.time = anyTime ? HistoryFormat.duration(ms: timeSum) : dash
        picture.aside = aside(reported: picture.reported, unknown: week.unknownCards,
                              partly: week.unpricedSessions,
                              codexPartial: report.effectiveness.codexHistoryPartial,
                              splitUnknown: splitUnknown, waiting: waiting)
        picture.masthead = masthead(keys: keys, report: report, projects: projects,
                                    claude: claude, grok: grok, person: person,
                                    acceptance: acceptance, calendar: calendar)
        picture.people = people(placed)
        picture.projects = projectRows(projects: projects, works: placed)
        if mode == .week, lens == .spent {
            picture.usual = week.usual
        }
        picture.dock = dock(works: works, columns: columns, openCard: openCard,
                            selectedDay: selectedDay, range: range,
                            acceptance: acceptance, otherOn: otherOn)
        return picture
    }

    // MARK: - Cards

    private struct Work {
        var card: HistoryOutcomeCard
        /// The card's money and placement, decided by `LedgerWeek.work`:
        /// token cost split by the provider of the session that spent it (so
        /// a Codex session is never inside Claude's share), the reported
        /// dollar, the not-priced marks and the day.
        var week: LedgerWeek.Work
        var clock: String
        var project: String
        var model: String
        var splitUnknown: Int
        var tokens: Int
        var anyTokens: Bool
        var timeMs: Int
        var anyTime: Bool

        var provider: String { week.provider }
        var day: String { week.day }
        var bound: Double { week.bound }
        var anyToken: Bool { week.anyTokenCost }
        var token: Double { week.tokenCost }
        var reported: Double { week.reported ?? 0 }
        var anyReported: Bool { week.reported != nil }
        /// No token cost at all: cost unknown.
        var unknown: Bool { week.unknown }
        /// A token cost, but some of the card's work had no price.
        var partlyUnpriced: Bool { week.partlyUnpriced }
    }

    /// The cards the picture draws, each cut to the provider shares that are
    /// on — `LedgerWeek.work`'s rule, the phone's own: per share and per
    /// session, never per card; Claude and Grok follow their toggles, Codex
    /// is always on; a card whose on sessions carry no price is drawn as
    /// cost unknown, never dropped. Tokens and time are summed here over the
    /// same on sessions (`LedgerWeek.sessionOn`).
    private static func included(report: HistoryReport, claude: Bool, grok: Bool,
                                 person: String, projects: [BoardProject],
                                 calendar: Calendar) -> [Work] {
        report.effectiveness.cards.compactMap { card in
            if !person.isEmpty, card.who != person { return nil }
            guard let week = LedgerWeek.work(card: weekCard(card), claude: claude,
                                             grok: grok, calendar: calendar)
            else { return nil }
            var splitUnknown = 0
            var tokens = 0
            var anyTokens = false
            var timeMs = 0
            var anyTime = false
            for session in card.sessions
            where LedgerWeek.sessionOn(weekSession(session), claude: claude, grok: grok) {
                if claude {
                    splitUnknown += session.cacheSplitUnknownTurns ?? 0
                }
                if let value = session.outputTokens {
                    anyTokens = true
                    tokens += value
                }
                if session.known {
                    anyTime = true
                    timeMs += session.durationMs ?? 0
                }
            }
            return Work(
                card: card, week: week,
                clock: clock(week.bound, calendar: calendar),
                project: projectName(card.root, projects: projects),
                model: model(card),
                splitUnknown: splitUnknown,
                tokens: tokens, anyTokens: anyTokens,
                timeMs: timeMs, anyTime: anyTime)
        }
    }

    // MARK: - The shared fold's input

    /// A report card as the shared fold reads it: the id and the money
    /// fields of its sessions, nothing a person reads.
    private static func weekCard(_ card: HistoryOutcomeCard) -> LedgerWeek.Card {
        LedgerWeek.Card(id: card.cardId, sessions: card.sessions.map(weekSession))
    }

    private static func weekSession(_ session: HistoryOutcomeSession) -> LedgerWeek.Session {
        LedgerWeek.Session(
            sessionId: session.sessionId, provider: session.provider,
            phase: session.phase, boundAt: session.boundAt, known: session.known,
            tokenCostUsd: session.tokenCostUsd,
            claudeTokenCostUsd: session.claudeTokenCostUsd,
            grokTokenCostUsd: session.grokTokenCostUsd,
            codexTokenCostUsd: session.codexTokenCostUsd,
            tokenUnpricedTurns: session.tokenUnpricedTurns,
            reportedCostUsd: session.reportedCostUsd)
    }

    /// A leftover day's money for the shared fold. The unpriced counts are
    /// the token-cost ones (`*_token_unpriced_sessions`), not the older
    /// measured-dollar counts.
    private static func weekOther(_ row: HistoryOtherDay) -> LedgerWeek.OtherDay {
        LedgerWeek.OtherDay(
            day: row.day,
            claude: row.claudeTokenCostUsd, grok: row.grokTokenCostUsd,
            codex: row.codexTokenCostUsd,
            claudeReported: row.claudeReportedCostUsd,
            grokReported: row.grokReportedCostUsd,
            claudeUnpriced: row.claudeTokenUnpricedSessions,
            grokUnpriced: row.grokTokenUnpricedSessions,
            codexUnpriced: row.codexTokenUnpricedSessions,
            claudeUnpricedIds: row.claudeTokenUnpricedSessionIds,
            grokUnpricedIds: row.grokTokenUnpricedSessionIds,
            codexUnpricedIds: row.codexTokenUnpricedSessionIds)
    }

    private static func model(_ card: HistoryOutcomeCard) -> String {
        let sessions = card.sessions
        if let session = sessions.first(where: {
            $0.phase == "implementation" && !($0.model ?? "").isEmpty
        }) {
            return session.model ?? ""
        }
        return sessions.compactMap(\.model).first { !$0.isEmpty } ?? ""
    }

    private static func projectName(_ root: String, projects: [BoardProject]) -> String {
        projects.first { $0.root == root }?.name ?? ""
    }

    // MARK: - Days

    /// One day as the ledger draws it. `money` is `LedgerWeek.column`'s —
    /// the token cost by provider, the reported mark, the not-priced dash
    /// and the figure (a dash where nothing on the day could be priced,
    /// nothing at all on a day nothing happened: an empty day is not $0.00)
    /// — and this adds the lens, the leftover line and the cards.
    private static func column(money: LedgerWeek.Column, works: [Work],
                               other: HistoryOtherDay?, lens: LedgerLens,
                               claude: Bool, grok: Bool) -> LedgerDayColumn {
        var column = LedgerDayColumn()
        column.day = money.day
        column.weekday = money.weekday
        column.dayNumber = money.dayNumber
        column.claudeToken = money.claude
        column.grokToken = money.grok
        column.codexToken = money.codex
        column.token = money.tokenCost
        column.anyToken = money.anyTokenCost
        column.reported = money.reported
        column.reportedMark = money.reportedMark
        column.unknown = money.unknown
        column.figure = money.figure
        column.noNamedCard = works.isEmpty
        column.emptyNote = works.isEmpty ? "No named card" : nil
        let tokenTotal = works.reduce(0) { $0 + ($1.anyTokens ? $1.tokens : 0) }
            + (other.map { (claude ? $0.claudeOutputTokens : 0) + (grok ? $0.grokOutputTokens : 0) } ?? 0)
        let timeTotal = works.reduce(0) { $0 + ($1.anyTime ? $1.timeMs : 0) }
        if lens == .tokens {
            column.figure = (tokenTotal > 0 || works.contains(where: \.anyTokens))
                ? Format.count(tokenTotal) : dash
            column.magnitude = Double(tokenTotal)
        } else if lens == .time {
            column.figure = works.contains(where: \.anyTime)
                ? HistoryFormat.duration(ms: timeTotal) : dash
            column.magnitude = Double(timeTotal)
        } else {
            column.magnitude = column.token
        }
        if let other {
            if lens == .spent {
                let money = (claude ? other.claudeTokenCostUsd : 0)
                    + (grok ? other.grokTokenCostUsd : 0) + other.codexTokenCostUsd
                if money > 0 { column.otherLine = "other \(Format.usd(money))" }
            } else if lens == .tokens {
                let tokens = (claude ? other.claudeOutputTokens : 0)
                    + (grok ? other.grokOutputTokens : 0)
                if tokens > 0 { column.otherLine = "other \(Format.count(tokens))" }
            }
        }
        let magnitudes = works.map { magnitude($0, lens: lens) }
        let peak = magnitudes.max() ?? 0
        column.cards = works.map { work in
            var line = LedgerCardLine()
            line.id = work.card.cardId
            line.title = work.card.title.isEmpty ? dash : work.card.title
            line.who = work.card.who
            line.project = work.project
            line.model = work.model
            line.clock = work.clock
            line.figure = figure(work, lens: lens)
            line.turns = work.card.turns
            line.manual = work.card.manualCheckOutstanding
            line.day = work.day
            let mag = magnitude(work, lens: lens)
            line.share = peak > 0 ? mag / peak : 0
            line.ink = ink(work)
            return line
        }
        return column
    }

    private static func magnitude(_ work: Work, lens: LedgerLens) -> Double {
        switch lens {
        case .spent:
            return work.anyToken ? work.token : 0
        case .tokens:
            return work.anyTokens ? Double(work.tokens) : 0
        case .time:
            return work.anyTime ? Double(work.timeMs) : 0
        }
    }

    private static func figure(_ work: Work, lens: LedgerLens) -> String {
        switch lens {
        case .spent:
            return spentWords(work)
        case .tokens:
            return work.anyTokens ? Format.count(work.tokens) : dash
        case .time:
            return work.anyTime ? HistoryFormat.duration(ms: work.timeMs) : dash
        }
    }

    /// A card's money: its token cost, then what was reported beside it.
    /// "cost unknown" is never followed by a reported dollar pretending to
    /// be the cost.
    private static func spentWords(_ work: Work) -> String {
        guard work.anyToken else {
            return work.anyReported ? "\(dash) · \(reportedWords(work.reported))" : dash
        }
        let token = Format.usd(work.token)
        return work.anyReported ? "\(token) · \(reportedWords(work.reported))" : token
    }

    private static func ink(_ work: Work) -> LedgerInk {
        if work.unknown { return .unknown }
        if work.provider == "grok" { return .grok }
        if work.provider == "codex" { return .codex }
        return .claude
    }

    private static func dayKeys(range: String, now: Date, calendar: Calendar,
                                works: [Work], report: HistoryReport,
                                otherOn: Bool) -> [String] {
        switch range {
        case "today":
            return [dayKey(now, calendar: calendar)]
        case "7d":
            return recent(7, now: now, calendar: calendar)
        case "30d":
            return recent(30, now: now, calendar: calendar)
        case "90d":
            return recent(90, now: now, calendar: calendar)
        default:
            var earliest = dayKey(now, calendar: calendar)
            for day in works.map(\.day) where day < earliest { earliest = day }
            if otherOn {
                for day in report.otherDays.map(\.day) where day < earliest {
                    earliest = day
                }
            }
            guard let start = date(from: earliest, calendar: calendar) else {
                return [dayKey(now, calendar: calendar)]
            }
            let today = calendar.startOfDay(for: now)
            var keys: [String] = []
            var cursor = calendar.startOfDay(for: start)
            while cursor <= today {
                keys.append(dayKey(cursor, calendar: calendar))
                guard let next = calendar.date(byAdding: .day, value: 1, to: cursor) else { break }
                cursor = next
                if keys.count > 4000 { break }
            }
            return keys.isEmpty ? [dayKey(now, calendar: calendar)] : keys
        }
    }

    /// A card dated before the first column keeps its money by moving onto
    /// that column. `all` is left alone: its columns already start early.
    private static func pin(_ works: [Work], ontoFirstOf keys: [String],
                            range: String) -> [Work] {
        guard range != "all", let first = keys.first else { return works }
        return works.map { work in
            var pinned = work
            pinned.week = LedgerWeek.pinned(work.week, onto: first)
            return pinned
        }
    }

    /// Leftover rows from before the first column join that column. Their
    /// sessions are already excluded from the named cards, so dropping the
    /// row would drop the dollars entirely.
    private static func pinOther(_ rows: [HistoryOtherDay], ontoFirstOf keys: [String],
                                 range: String) -> [HistoryOtherDay] {
        guard range != "all", let first = keys.first else { return rows }
        let early = rows.filter { $0.day < first }
        guard !early.isEmpty else { return rows }
        var merged = rows.first { $0.day == first } ?? HistoryOtherDay()
        merged.day = first
        for row in early {
            merged.claudeMeasuredCostUsd += row.claudeMeasuredCostUsd
            merged.grokMeasuredCostUsd += row.grokMeasuredCostUsd
            merged.claudeEstimatedCostUsd += row.claudeEstimatedCostUsd
            merged.grokEstimatedCostUsd += row.grokEstimatedCostUsd
            merged.estimatedCostUsd += row.estimatedCostUsd
            merged.unpricedSessions += row.unpricedSessions
            merged.claudeUnpricedSessions += row.claudeUnpricedSessions
            merged.grokUnpricedSessions += row.grokUnpricedSessions
            merged.claudeOutputTokens += row.claudeOutputTokens
            merged.grokOutputTokens += row.grokOutputTokens
            merged.claudeTokenCostUsd += row.claudeTokenCostUsd
            merged.grokTokenCostUsd += row.grokTokenCostUsd
            merged.codexTokenCostUsd += row.codexTokenCostUsd
            merged.claudeTokenUnpricedSessions += row.claudeTokenUnpricedSessions
            merged.grokTokenUnpricedSessions += row.grokTokenUnpricedSessions
            merged.codexTokenUnpricedSessions += row.codexTokenUnpricedSessions
            merged.claudeTokenUnpricedSessionIds += row.claudeTokenUnpricedSessionIds
            merged.grokTokenUnpricedSessionIds += row.grokTokenUnpricedSessionIds
            merged.codexTokenUnpricedSessionIds += row.codexTokenUnpricedSessionIds
            merged.cacheSplitUnknownTurns += row.cacheSplitUnknownTurns
            // A report only where one existed: nil plus nil stays nil.
            merged.claudeReportedCostUsd = sum(merged.claudeReportedCostUsd,
                                               row.claudeReportedCostUsd)
            merged.grokReportedCostUsd = sum(merged.grokReportedCostUsd,
                                             row.grokReportedCostUsd)
        }
        var rest = rows.filter { $0.day > first }
        rest.append(merged)
        return rest
    }

    private static func sum(_ lhs: Double?, _ rhs: Double?) -> Double? {
        LedgerWeek.sum(lhs, rhs)
    }

    private static func recent(_ count: Int, now: Date, calendar: Calendar) -> [String] {
        LedgerWeek.dayKeys(count: count, now: now, calendar: calendar)
    }

    private static func mode(for range: String) -> LedgerMode {
        switch range {
        case "today": return .today
        case "7d": return .week
        default: return .thin
        }
    }

    // MARK: - Headlines

    /// The line under the headline: what was reported, and that it is a
    /// different kind of money; what could not be priced; what is still
    /// being read; then the wait. The token cost itself is the total and is
    /// not qualified here.
    private static func aside(reported: String?, unknown: Int, partly: Int,
                              codexPartial: Bool,
                              splitUnknown: Int, waiting: String) -> String {
        var bits: [String] = []
        if let reported {
            bits.append("\(reported) reported by the providers, beside the token cost and not added to it")
        }
        if unknown > 0 {
            bits.append("\(unknown) cost unknown")
        }
        if partly > 0 {
            bits.append(partly == 1 ? "1 session with turns not priced"
                        : "\(partly) sessions with turns not priced")
        }
        if codexPartial {
            bits.append("Codex history is still being read")
        }
        if splitUnknown > 0 {
            bits.append("some cache writes had no 5-minute or 1-hour split and were priced at the 5-minute rate")
        }
        bits.append("waiting \(waiting) on you, the whole Mac")
        return bits.joined(separator: " · ")
    }

    private static func masthead(keys: [String], report: HistoryReport,
                                 projects: [BoardProject], claude: Bool, grok: Bool,
                                 person: String, acceptance: String,
                                 calendar: Calendar) -> String {
        let span = LedgerWeek.span(keys, calendar: calendar)
        let root = report.effectiveness.root
        let place = root.isEmpty
            ? "the whole Mac"
            : {
                let name = projectName(root, projects: projects)
                return name.isEmpty ? "this project" : name
            }()
        // Codex has no toggle and is always on, so it is always named.
        let assistants: String
        if claude && grok { assistants = "Claude, Grok and Codex" }
        else if claude { assistants = "Claude and Codex" }
        else if grok { assistants = "Grok and Codex" }
        else { assistants = "Codex" }
        var parts = [span, place, assistants]
        if !person.isEmpty { parts.append(person) }
        parts.append(acceptance)
        return parts.joined(separator: " · ")
    }

    private static func people(_ works: [Work]) -> [LedgerPersonRow] {
        struct Acc {
            var token = 0.0
            var anyToken = false
            var reported = 0.0
            var anyReported = false
        }
        var byWho: [String: Acc] = [:]
        var order: [String] = []
        for work in works {
            let who = work.card.who
            if who.isEmpty { continue }
            if byWho[who] == nil { order.append(who) }
            var acc = byWho[who] ?? Acc()
            if work.anyToken {
                acc.anyToken = true
                acc.token += work.token
            }
            if work.anyReported {
                acc.anyReported = true
                acc.reported += work.reported
            }
            byWho[who] = acc
        }
        return order.map { who in
            let acc = byWho[who] ?? Acc()
            return LedgerPersonRow(who: who, amount: amountWords(
                token: acc.anyToken ? acc.token : nil,
                reported: acc.anyReported ? acc.reported : nil))
        }.sorted { lhs, rhs in
            let left = byWho[lhs.who]?.token ?? 0
            let right = byWho[rhs.who]?.token ?? 0
            if left != right { return left > right }
            return lhs.who < rhs.who
        }
    }

    private static func projectRows(projects: [BoardProject], works: [Work]) -> [LedgerProjectRow] {
        projects.filter { !$0.root.isEmpty }.map { project in
            let rows = works.filter { $0.card.root == project.root }
            var row = LedgerProjectRow()
            row.name = project.name
            row.root = project.root
            if rows.isEmpty {
                row.amount = nil
            } else {
                let token: Double? = rows.contains(where: \.anyToken)
                    ? rows.reduce(0) { $0 + ($1.anyToken ? $1.token : 0) } : nil
                let reported: Double? = rows.contains(where: \.anyReported)
                    ? rows.reduce(0) { $0 + ($1.anyReported ? $1.reported : 0) } : nil
                row.amount = amountWords(token: token, reported: reported)
            }
            return row
        }
    }

    /// A person's or a project's money: the token cost first, a reported
    /// dollar after it only when one exists, and "cost unknown" — never
    /// $0.00 — where nothing was priced.
    private static func amountWords(token: Double?, reported: Double?) -> String {
        let lead = token.map { Format.usd($0) } ?? "cost unknown"
        guard let reported else { return lead }
        return "\(lead) · \(reportedWords(reported))"
    }

    // MARK: - Dock

    private static func dock(works: [Work], columns: [LedgerDayColumn],
                             openCard: String, selectedDay: String, range: String,
                             acceptance: String, otherOn: Bool) -> LedgerDock {
        if !openCard.isEmpty, let work = works.first(where: { $0.card.cardId == openCard }) {
            return .run(run(work))
        }
        if !selectedDay.isEmpty, let column = columns.first(where: { $0.day == selectedDay }) {
            let spent: String
            if column.anyToken {
                spent = Format.usd(column.token)
            } else if column.unknown {
                spent = "cost unknown"
            } else {
                spent = "nothing spent"
            }
            let titles = column.cards.isEmpty
                ? ["No named card"]
                : column.cards.map(\.title)
            var other: String?
            if otherOn, let line = column.otherLine {
                other = "Other sessions on this Mac \(line.replacingOccurrences(of: "other ", with: ""))."
            }
            return .day(title: label(column.day, calendar: .current),
                        spent: spent, reported: column.reportedMark,
                        unknown: column.unknown, titles: titles, other: other)
        }
        return .rest(heading: heading(range), acceptance: acceptance, honesty: honesty)
    }

    private static func run(_ work: Work) -> LedgerRun {
        var run = LedgerRun()
        let card = work.card
        run.title = card.title.isEmpty ? dash : card.title
        let priced: String
        if work.unknown {
            priced = "not priced"
        } else if work.partlyUnpriced {
            priced = "token cost, some turns not priced"
        } else {
            priced = "token cost"
        }
        run.scope = [work.project, card.who, work.model, priced]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        run.spent = work.anyToken ? Format.usd(work.token) : "cost unknown"
        run.reported = work.anyReported ? Format.usd(work.reported) : nil
        run.tokens = work.anyTokens ? Format.count(work.tokens) : dash
        run.time = work.anyTime ? HistoryFormat.duration(ms: work.timeMs) : dash
        if card.accepted == nil {
            run.verdict = "not submitted"
        } else if card.accepted == true {
            run.verdict = "accepted"
        } else {
            run.verdict = "not accepted"
        }
        if let rework = card.reworkCount {
            run.rework = "rework \(rework)"
        } else {
            run.rework = "rework —"
        }
        let attempts = card.sessions.filter { $0.phase == "implementation" }.count
        run.phases = card.sessions.map { session in
            let name: String
            if session.phase == "refinement" { name = "refine" }
            else if session.phase == "implementation" { name = "build" }
            else { name = session.phase.isEmpty ? "session" : session.phase }
            var cost = session.tokenCostUsd.map { Format.usd($0) } ?? dash
            if let reported = session.reportedCostUsd {
                cost += " (\(reportedWords(reported)))"
            }
            let time = session.known
                ? HistoryFormat.duration(ms: session.durationMs ?? 0) : dash
            let model = (session.model?.isEmpty == false) ? (session.model ?? dash) : dash
            var line = "\(name) \(cost) · \(time) · \(model)"
            if session.phase == "implementation" {
                line += " · \(attempts) attempts"
            }
            if let input = session.inputTokens {
                line += " · \(Format.count(input)) in"
            }
            if let ratio = session.cacheHitRatio {
                line += " · cache \(Int((ratio * 100).rounded()))%"
            }
            return line
        }
        run.manual = card.manualCheckOutstanding
        let sessions = card.sessions.filter { !$0.sessionId.isEmpty }
        if sessions.count == 1,
           sessions[0].provider == "claude" || sessions[0].provider == "grok" {
            run.resume = "\(sessions[0].provider) --resume \(sessions[0].sessionId)"
        }
        return run
    }

    private static func heading(_ range: String) -> String {
        switch range {
        case "today": return "Today"
        case "7d": return "Seven days"
        case "30d": return "Thirty days"
        case "90d": return "Ninety days"
        case "all": return "Everything on record"
        default: return range
        }
    }

    // MARK: - Words

    /// A reported dollar in words, so it reads as a different kind of money
    /// from the token cost it sits beside. `$0.00 reported` is a real zero.
    static func reportedWords(_ value: Double) -> String {
        LedgerWeek.reportedWords(value)
    }

    private static func dayKey(_ date: Date, calendar: Calendar) -> String {
        LedgerWeek.dayKey(date, calendar: calendar)
    }

    private static func date(from key: String, calendar: Calendar) -> Date? {
        LedgerWeek.date(from: key, calendar: calendar)
    }

    private static func clock(_ ts: Double, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.hour, .minute],
                                            from: Date(timeIntervalSince1970: ts))
        return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }

    private static func label(_ key: String, calendar: Calendar) -> String {
        LedgerWeek.label(key, calendar: calendar)
    }
}
