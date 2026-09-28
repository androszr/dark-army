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
        var columns: [LedgerDayColumn] = []
        var tokenCostSum = 0.0
        var anyTokenCost = false
        var reportedSum = 0.0
        var anyReported = false
        var tokenSum = 0
        var anyTokens = false
        var timeSum = 0
        var anyTime = false
        var unknownCards = 0
        // Sessions with a turn not priced, by id: a leftover session with
        // unpriced turns on three days is one session, not three.
        var unpricedSessions: Set<String> = []
        var splitUnknown = 0

        for key in keys {
            let onDay = placed.filter { $0.day == key }.sorted { $0.bound < $1.bound }
            let other = otherOn ? others.first { $0.day == key } : nil
            let column = column(day: key, works: onDay, other: other,
                                lens: lens, claude: claude, grok: grok,
                                calendar: calendar)
            columns.append(column)
            if column.anyToken {
                anyTokenCost = true
                tokenCostSum += column.token
            }
            if let reported = column.reported {
                anyReported = true
                reportedSum += reported
            }
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
                // Leftover sessions some of whose turns that day had no
                // price: the day still shows what was priced.
                unpricedSessions.formUnion(otherUnpricedIds(other, claude: claude, grok: grok))
                if claude { splitUnknown += other.cacheSplitUnknownTurns }
            }
            unknownCards += onDay.filter(\.unknown).count
            for work in onDay { unpricedSessions.formUnion(work.unpricedSessions) }
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
        // no reported dollar is in it.
        if anyTokenCost {
            picture.spent = Format.usd(tokenCostSum)
        } else if unknownCards + unpricedSessions.count > 0 {
            picture.spent = "cost unknown"
        } else {
            picture.spent = Format.usd(0)
        }
        if anyReported {
            picture.reported = Format.usd(reportedSum)
        }
        picture.tokens = anyTokens ? Format.count(tokenSum) : dash
        picture.time = anyTime ? HistoryFormat.duration(ms: timeSum) : dash
        picture.aside = aside(reported: picture.reported, unknown: unknownCards,
                              partly: unpricedSessions.count,
                              codexPartial: report.effectiveness.codexHistoryPartial,
                              splitUnknown: splitUnknown, waiting: waiting)
        picture.masthead = masthead(keys: keys, report: report, projects: projects,
                                    claude: claude, grok: grok, person: person,
                                    acceptance: acceptance, calendar: calendar)
        picture.people = people(placed)
        picture.projects = projectRows(projects: projects, works: placed)
        if mode == .week, lens == .spent {
            picture.usual = median(columns.map(\.token))
        }
        picture.dock = dock(works: works, columns: columns, openCard: openCard,
                            selectedDay: selectedDay, range: range,
                            acceptance: acceptance, otherOn: otherOn)
        return picture
    }

    // MARK: - Cards

    private struct Work {
        var card: HistoryOutcomeCard
        var provider: String
        var day: String
        var bound: Double
        var clock: String
        var project: String
        var model: String
        /// Token cost, split by the provider of the session that spent it,
        /// so a Codex session is never inside Claude's share.
        var claudeToken: Double
        var grokToken: Double
        var codexToken: Double
        var anyToken: Bool
        var reported: Double
        var anyReported: Bool
        /// No token cost at all: cost unknown.
        var unknown: Bool
        /// A token cost, but some of the card's work had no price.
        var partlyUnpriced: Bool
        /// The on sessions of a priced card that carried an unpriced turn.
        var unpricedSessions: [String]
        var splitUnknown: Int
        var tokens: Int
        var anyTokens: Bool
        var timeMs: Int
        var anyTime: Bool

        var token: Double { claudeToken + grokToken + codexToken }
    }

    /// The cards the picture draws, each cut to the provider shares that are
    /// on. The filter is per share and per session, never per card: a
    /// session's token cost is split by the provider of the turns priced
    /// (`claude_` / `grok_` / `codex_token_cost_usd`), Claude and Grok follow
    /// their toggles, Codex is always on. A session is on when one of its
    /// priced shares is on — so a Codex-labelled run that was a Claude session
    /// is off with Claude off, its statusline dollar included — and, with no
    /// split to go by, when the provider it ran as is on. A card is drawn
    /// while a share or a session of it is on; one whose on sessions carry no
    /// price is drawn as cost unknown, never dropped.
    private static func included(report: HistoryReport, claude: Bool, grok: Bool,
                                 person: String, projects: [BoardProject],
                                 calendar: Calendar) -> [Work] {
        report.effectiveness.cards.compactMap { card in
            if !person.isEmpty, card.who != person { return nil }
            guard let bound = earliest(card) else { return nil }
            var claudeToken = 0.0
            var grokToken = 0.0
            var codexToken = 0.0
            var onShares: Set<String> = []
            var reported = 0.0
            var anyReported = false
            var unpricedSessions: [String] = []
            var anySessionOn = false
            var splitUnknown = 0
            var tokens = 0
            var anyTokens = false
            var timeMs = 0
            var anyTime = false
            for session in card.sessions {
                let shares = tokenShares(session)
                if claude, let value = shares.claude {
                    onShares.insert("claude")
                    claudeToken += value
                }
                if grok, let value = shares.grok {
                    onShares.insert("grok")
                    grokToken += value
                }
                if let value = shares.codex {
                    onShares.insert("codex")
                    codexToken += value
                }
                guard sessionOn(session, shares: shares, claude: claude, grok: grok) else {
                    continue
                }
                anySessionOn = true
                if !session.known || (session.tokenUnpricedTurns ?? 0) > 0 {
                    unpricedSessions.append(
                        session.sessionId.isEmpty ? "\(card.cardId)#\(unpricedSessions.count)"
                            : session.sessionId)
                }
                if let value = session.reportedCostUsd {
                    anyReported = true
                    reported += value
                }
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
            let anyToken = !onShares.isEmpty
            guard anySessionOn || anyToken else { return nil }
            // The ink follows the largest share that is on; a tie (a lone
            // $0.00 share included) goes to the provider the card ran as
            // when that one is among them, else to the first priced.
            let lead: String
            if anyToken {
                let values = ["claude": claudeToken, "grok": grokToken, "codex": codexToken]
                let top = onShares.map { values[$0] ?? 0 }.max() ?? 0
                let tied = ["claude", "grok", "codex"].filter {
                    onShares.contains($0) && values[$0] == top
                }
                let label = leadProvider(card)
                lead = tied.contains(label) ? label : (tied.first ?? label)
            } else {
                lead = leadProvider(card)
            }
            return Work(
                card: card, provider: lead,
                day: localDay(bound, calendar: calendar), bound: bound,
                clock: clock(bound, calendar: calendar),
                project: projectName(card.root, projects: projects),
                model: model(card),
                claudeToken: claudeToken, grokToken: grokToken,
                codexToken: codexToken, anyToken: anyToken,
                reported: reported, anyReported: anyReported,
                unknown: !anyToken,
                partlyUnpriced: anyToken && !unpricedSessions.isEmpty,
                unpricedSessions: anyToken ? unpricedSessions : [],
                splitUnknown: splitUnknown,
                tokens: tokens, anyTokens: anyTokens,
                timeMs: timeMs, anyTime: anyTime)
        }
    }

    /// Whether one session is on: by its priced shares when the daemon sent
    /// a split, by the provider it ran as when it did not.
    private static func sessionOn(_ session: HistoryOutcomeSession,
                                  shares: (claude: Double?, grok: Double?, codex: Double?),
                                  claude: Bool, grok: Bool) -> Bool {
        if shares.claude == nil, shares.grok == nil, shares.codex == nil {
            return providerOn(session.provider, claude: claude, grok: grok)
        }
        return (claude && shares.claude != nil) || (grok && shares.grok != nil)
            || shares.codex != nil
    }

    /// One session's token cost by provider. The daemon's split when it sent
    /// one; from an older daemon, the whole `tokenCostUsd` under the
    /// session's own provider.
    private static func tokenShares(_ session: HistoryOutcomeSession)
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

    private static func leadProvider(_ card: HistoryOutcomeCard) -> String {
        if let session = card.sessions.first(where: {
            $0.phase == "implementation" && !$0.provider.isEmpty
        }) {
            return session.provider
        }
        return card.sessions.first { !$0.provider.isEmpty }?.provider ?? ""
    }

    /// Codex is always on. Claude and Grok follow the toggles. Anything
    /// else rides with Claude. Applied per session and per share, never to a
    /// whole card.
    private static func providerOn(_ provider: String, claude: Bool,
                                   grok: Bool) -> Bool {
        if provider == "codex" { return true }
        if provider == "grok" { return grok }
        return claude
    }

    private static func earliest(_ card: HistoryOutcomeCard) -> Double? {
        let stamps = card.sessions.compactMap(\.boundAt).filter { $0 > 0 }
        return stamps.min()
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

    private static func column(day: String, works: [Work], other: HistoryOtherDay?,
                               lens: LedgerLens, claude: Bool, grok: Bool,
                               calendar: Calendar) -> LedgerDayColumn {
        var column = LedgerDayColumn()
        column.day = day
        if let date = date(from: day, calendar: calendar) {
            let weekday = calendar.component(.weekday, from: date)
            column.weekday = calendar.shortWeekdaySymbols[weekday - 1]
            column.dayNumber = String(calendar.component(.day, from: date))
        }
        var reported = 0.0
        var anyReported = false
        for work in works {
            if work.anyToken {
                column.anyToken = true
                column.claudeToken += work.claudeToken
                column.grokToken += work.grokToken
                column.codexToken += work.codexToken
            }
            if work.anyReported {
                anyReported = true
                reported += work.reported
            }
        }
        if let other {
            let claudePart = claude ? other.claudeTokenCostUsd : 0
            let grokPart = grok ? other.grokTokenCostUsd : 0
            let codexPart = other.codexTokenCostUsd
            if claudePart != 0 || grokPart != 0 || codexPart != 0 {
                column.anyToken = true
                column.claudeToken += claudePart
                column.grokToken += grokPart
                column.codexToken += codexPart
            }
            if claude, let value = other.claudeReportedCostUsd {
                anyReported = true
                reported += value
            }
            if grok, let value = other.grokReportedCostUsd {
                anyReported = true
                reported += value
            }
            if otherUnpriced(other, claude: claude, grok: grok) > 0 {
                column.unknown = true
            }
        }
        column.token = column.claudeToken + column.grokToken + column.codexToken
        if anyReported {
            column.reported = reported
            column.reportedMark = reportedWords(reported)
        }
        if works.contains(where: { $0.unknown || $0.partlyUnpriced }) {
            column.unknown = true
        }
        column.noNamedCard = works.isEmpty
        column.emptyNote = works.isEmpty ? "No named card" : nil
        // The token cost, a dash where nothing on the day could be priced,
        // and nothing at all on a day nothing happened: an empty day is not
        // $0.00, and a reported zero is the reported mark's to say.
        if column.anyToken {
            column.figure = Format.usd(column.token)
        } else if column.unknown {
            column.figure = dash
        } else {
            column.figure = ""
        }
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
            guard work.day < first else { return work }
            var pinned = work
            pinned.day = first
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
        guard lhs != nil || rhs != nil else { return nil }
        return (lhs ?? 0) + (rhs ?? 0)
    }

    /// Leftover sessions some of whose turns that day had no token price.
    /// Codex is always on, so its sessions always count.
    /// The same sessions by id. An older daemon sends counts only; its
    /// sessions get a per-day stand-in id, which is the old per-day count.
    private static func otherUnpricedIds(_ other: HistoryOtherDay, claude: Bool,
                                         grok: Bool) -> Set<String> {
        var ids: Set<String> = []
        func add(_ named: [String], _ count: Int, _ provider: String) {
            if named.isEmpty {
                for n in 0..<count { ids.insert("\(other.day)#\(provider)#\(n)") }
            } else {
                ids.formUnion(named)
            }
        }
        if claude { add(other.claudeTokenUnpricedSessionIds, other.claudeTokenUnpricedSessions, "claude") }
        if grok { add(other.grokTokenUnpricedSessionIds, other.grokTokenUnpricedSessions, "grok") }
        add(other.codexTokenUnpricedSessionIds, other.codexTokenUnpricedSessions, "codex")
        return ids
    }

    private static func otherUnpriced(_ other: HistoryOtherDay, claude: Bool,
                                      grok: Bool) -> Int {
        (claude ? other.claudeTokenUnpricedSessions : 0)
            + (grok ? other.grokTokenUnpricedSessions : 0)
            + other.codexTokenUnpricedSessions
    }

    private static func recent(_ count: Int, now: Date, calendar: Calendar) -> [String] {
        let today = calendar.startOfDay(for: now)
        return (0..<count).reversed().compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: today)
        }.map { dayKey($0, calendar: calendar) }
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
        let span: String
        if let first = keys.first, let last = keys.last, first != last {
            span = "\(label(first, calendar: calendar)) – \(label(last, calendar: calendar))"
        } else {
            span = label(keys.first ?? "", calendar: calendar)
        }
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
        "\(Format.usd(value)) reported"
    }

    private static func median(_ values: [Double]) -> Double {
        let ordered = values.sorted()
        guard !ordered.isEmpty else { return 0 }
        let mid = ordered.count / 2
        if ordered.count % 2 == 1 { return ordered[mid] }
        return (ordered[mid - 1] + ordered[mid]) / 2
    }

    private static func dayKey(_ date: Date, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d",
                      parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    private static func date(from key: String, calendar: Calendar) -> Date? {
        let parts = key.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var components = DateComponents()
        components.year = parts[0]
        components.month = parts[1]
        components.day = parts[2]
        return calendar.date(from: components)
    }

    private static func clock(_ ts: Double, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.hour, .minute],
                                            from: Date(timeIntervalSince1970: ts))
        return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }

    private static func label(_ key: String, calendar: Calendar) -> String {
        guard let date = date(from: key, calendar: calendar) else { return key }
        let weekday = calendar.component(.weekday, from: date)
        let name = calendar.shortWeekdaySymbols[weekday - 1]
        let day = calendar.component(.day, from: date)
        let month = calendar.shortMonthSymbols[calendar.component(.month, from: date) - 1]
        return "\(name) \(day) \(month)"
    }
}
