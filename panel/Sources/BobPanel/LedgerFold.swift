import Foundation

/// Which of the three lenses the picture is measuring. Waiting is not one.
enum LedgerLens: String, Equatable {
    case spent, tokens, time
}

enum LedgerMode: Equatable {
    case today, week, thin
}

enum LedgerInk: Equatable {
    case claude, grok, estimated, unknown
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
    var measured: Double = 0
    var estimated: Double = 0
    var claudeMeasured: Double = 0
    var grokMeasured: Double = 0
    /// The lens's magnitude, for the column's bar. Spent is measured plus
    /// the estimate; tokens and time are their own sums. Not a dollar when
    /// the lens is not spent.
    var magnitude: Double = 0
    var anyMeasured = false
    var unknown = false
    var figure = ""
    var estimatedMark: String?
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
    var verdict = ""
    var rework = ""
    var phases: [String] = []
    var manual = false
    var resume: String?
}

enum LedgerDock: Equatable {
    case rest(heading: String, acceptance: String, honesty: String)
    case day(title: String, measured: String, estimated: String?,
             unknown: Bool, titles: [String], other: String?)
    case run(LedgerRun)
}

struct LedgerPicture: Equatable {
    var masthead = ""
    var spent = ""
    var estimated: String?
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
    /// Median of the seven days' measured totals. Spend lens and the
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
    static let honesty = "An estimate wears a tilde and stays out of the total. Cost unknown adds nothing. A day with money and no card is other sessions on this Mac."

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
        var measuredSum = 0.0
        var anyMeasured = false
        var estimatedSum = 0.0
        var tokenSum = 0
        var anyTokens = false
        var timeSum = 0
        var anyTime = false
        var unknownCards = 0

        for key in keys {
            let onDay = placed.filter { $0.day == key }.sorted { $0.bound < $1.bound }
            let other = otherOn ? others.first { $0.day == key } : nil
            let column = column(day: key, works: onDay, other: other,
                                lens: lens, claude: claude, grok: grok,
                                calendar: calendar)
            columns.append(column)
            if column.anyMeasured {
                anyMeasured = true
                measuredSum += column.measured
            } else if column.measured != 0 {
                measuredSum += column.measured
            }
            estimatedSum += column.estimated
            // Tokens and time are folded from the works, not from `daily`.
            for work in onDay where work.anyTokens {
                anyTokens = true
                tokenSum += work.tokens
            }
            for work in onDay where work.anyTime {
                anyTime = true
                timeSum += work.timeMs
            }
            if let other, otherOn {
                let extra = (claude ? other.claudeOutputTokens : 0)
                    + (grok ? other.grokOutputTokens : 0)
                if extra > 0 {
                    anyTokens = true
                    tokenSum += extra
                }
                let unpriced = otherUnpriced(other, claude: claude, grok: grok)
                if unpriced > 0 {
                    unknownCards += unpriced
                }
            }
            unknownCards += onDay.filter(\.unknown).count
        }

        // A day column's measured already includes other dollars. Summing
        // the columns is the headline, and it is not `daily.costUsd`.
        if !anyMeasured {
            anyMeasured = columns.contains { $0.anyMeasured }
            if anyMeasured {
                measuredSum = columns.reduce(0) { $0 + $1.measured }
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
        if anyMeasured {
            picture.spent = Format.usd(measuredSum)
        } else if unknownCards > 0 {
            picture.spent = "cost unknown"
        } else {
            picture.spent = Format.usd(0)
        }
        if estimatedSum > 0 {
            picture.estimated = tilde(estimatedSum)
        }
        picture.tokens = anyTokens ? Format.count(tokenSum) : dash
        picture.time = anyTime ? HistoryFormat.duration(ms: timeSum) : dash
        picture.aside = aside(estimated: picture.estimated,
                              unknown: unknownCards, waiting: waiting)
        picture.masthead = masthead(keys: keys, report: report, projects: projects,
                                    claude: claude, grok: grok, person: person,
                                    acceptance: acceptance, calendar: calendar)
        picture.people = people(placed)
        picture.projects = projectRows(projects: projects, works: placed)
        if mode == .week, lens == .spent {
            picture.usual = median(columns.map(\.measured))
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
        var measured: Double
        var estimated: Double
        var anyMeasured: Bool
        var anyEstimated: Bool
        var unknown: Bool
        var tokens: Int
        var anyTokens: Bool
        var timeMs: Int
        var anyTime: Bool
    }

    private static func included(report: HistoryReport, claude: Bool, grok: Bool,
                                 person: String, projects: [BoardProject],
                                 calendar: Calendar) -> [Work] {
        report.effectiveness.cards.compactMap { card in
            let provider = leadProvider(card)
            guard providerOn(provider, claude: claude, grok: grok) else { return nil }
            if !person.isEmpty, card.who != person { return nil }
            guard let bound = earliest(card) else { return nil }
            var measured = 0.0
            var estimated = 0.0
            var anyMeasured = false
            var anyEstimated = false
            var tokens = 0
            var anyTokens = false
            var timeMs = 0
            var anyTime = false
            for session in card.sessions {
                if let value = session.measuredCostUsd {
                    anyMeasured = true
                    measured += value
                }
                if let value = session.estimatedCostUsd {
                    anyEstimated = true
                    estimated += value
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
                card: card, provider: provider,
                day: localDay(bound, calendar: calendar), bound: bound,
                clock: clock(bound, calendar: calendar),
                project: projectName(card.root, projects: projects),
                model: model(card), measured: measured, estimated: estimated,
                anyMeasured: anyMeasured, anyEstimated: anyEstimated,
                unknown: !anyMeasured && !anyEstimated,
                tokens: tokens, anyTokens: anyTokens,
                timeMs: timeMs, anyTime: anyTime)
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
    /// else rides with Claude, and one provider is the whole card.
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
        var claudeMeasured = 0.0
        var grokMeasured = 0.0
        for work in works where work.anyMeasured {
            column.anyMeasured = true
            column.measured += work.measured
            if work.provider == "grok" {
                grokMeasured += work.measured
            } else if work.provider == "claude" {
                claudeMeasured += work.measured
            }
        }
        column.estimated = works.reduce(0) { $0 + ($1.anyEstimated ? $1.estimated : 0) }
        if let other {
            let claudePart = claude ? other.claudeMeasuredCostUsd : 0
            let grokPart = grok ? other.grokMeasuredCostUsd : 0
            if claudePart != 0 || grokPart != 0 {
                column.anyMeasured = true
                column.measured += claudePart + grokPart
                claudeMeasured += claudePart
                grokMeasured += grokPart
            }
            let estimated = otherEstimated(other, claude: claude, grok: grok)
            if estimated > 0 {
                column.estimated += estimated
            }
            if otherUnpriced(other, claude: claude, grok: grok) > 0 {
                column.unknown = true
            }
        }
        column.claudeMeasured = claudeMeasured
        column.grokMeasured = grokMeasured
        if works.contains(where: \.unknown) {
            column.unknown = true
        }
        column.noNamedCard = works.isEmpty
        column.emptyNote = works.isEmpty ? "No named card" : nil
        if column.anyMeasured {
            column.figure = Format.usd(column.measured)
        } else if column.unknown {
            column.figure = dash
        } else {
            column.figure = Format.usd(0)
        }
        if column.estimated > 0 {
            column.estimatedMark = tilde(column.estimated)
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
            column.magnitude = column.measured + column.estimated
        }
        if let other {
            if lens == .spent {
                let money = (claude ? other.claudeMeasuredCostUsd : 0)
                    + (grok ? other.grokMeasuredCostUsd : 0)
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
            if work.anyMeasured { return work.measured }
            if work.anyEstimated { return work.estimated }
            return 0
        case .tokens:
            return work.anyTokens ? Double(work.tokens) : 0
        case .time:
            return work.anyTime ? Double(work.timeMs) : 0
        }
    }

    private static func figure(_ work: Work, lens: LedgerLens) -> String {
        switch lens {
        case .spent:
            if work.anyMeasured, work.anyEstimated, work.estimated > 0 {
                return "\(Format.usd(work.measured)) · \(tilde(work.estimated))"
            }
            if work.anyMeasured { return Format.usd(work.measured) }
            if work.anyEstimated, work.estimated > 0 { return tilde(work.estimated) }
            return dash
        case .tokens:
            return work.anyTokens ? Format.count(work.tokens) : dash
        case .time:
            return work.anyTime ? HistoryFormat.duration(ms: work.timeMs) : dash
        }
    }

    private static func ink(_ work: Work) -> LedgerInk {
        if work.unknown { return .unknown }
        if !work.anyMeasured, work.anyEstimated { return .estimated }
        if work.provider == "grok" { return .grok }
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
        }
        var rest = rows.filter { $0.day > first }
        rest.append(merged)
        return rest
    }

    private static func otherEstimated(_ other: HistoryOtherDay, claude: Bool,
                                       grok: Bool) -> Double {
        (claude ? other.claudeEstimatedCostUsd : 0)
            + (grok ? other.grokEstimatedCostUsd : 0)
    }

    private static func otherUnpriced(_ other: HistoryOtherDay, claude: Bool,
                                      grok: Bool) -> Int {
        (claude ? other.claudeUnpricedSessions : 0)
            + (grok ? other.grokUnpricedSessions : 0)
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

    private static func aside(estimated: String?, unknown: Int, waiting: String) -> String {
        var bits: [String] = []
        if let estimated {
            bits.append("\(estimated) estimated, not in the total")
        }
        if unknown > 0 {
            bits.append("\(unknown) cost unknown")
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
        let assistants: String
        if claude && grok { assistants = "Claude and Grok" }
        else if claude { assistants = "Claude" }
        else { assistants = "Grok" }
        var parts = [span, place, assistants]
        if !person.isEmpty { parts.append(person) }
        parts.append(acceptance)
        return parts.joined(separator: " · ")
    }

    private static func people(_ works: [Work]) -> [LedgerPersonRow] {
        struct Acc {
            var measured = 0.0
            var anyMeasured = false
            var estimated = 0.0
            var anyEstimated = false
            var unknown = false
        }
        var byWho: [String: Acc] = [:]
        var order: [String] = []
        for work in works {
            let who = work.card.who
            if who.isEmpty { continue }
            if byWho[who] == nil { order.append(who) }
            var acc = byWho[who] ?? Acc()
            if work.anyMeasured {
                acc.anyMeasured = true
                acc.measured += work.measured
            }
            if work.anyEstimated {
                acc.anyEstimated = true
                acc.estimated += work.estimated
            }
            if work.unknown { acc.unknown = true }
            byWho[who] = acc
        }
        return order.map { who in
            let acc = byWho[who] ?? Acc()
            let amount: String
            if acc.unknown, !acc.anyMeasured, !acc.anyEstimated {
                amount = "cost unknown"
            } else if acc.anyEstimated, !acc.anyMeasured, acc.estimated > 0 {
                amount = tilde(acc.estimated)
            } else if acc.anyMeasured {
                amount = acc.anyEstimated && acc.estimated > 0
                    ? "\(Format.usd(acc.measured)) · \(tilde(acc.estimated))"
                    : Format.usd(acc.measured)
            } else {
                amount = "cost unknown"
            }
            return LedgerPersonRow(who: who, amount: amount)
        }.sorted { lhs, rhs in
            let left = byWho[lhs.who]?.measured ?? 0
            let right = byWho[rhs.who]?.measured ?? 0
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
            } else if rows.contains(where: \.anyMeasured) {
                let measured = rows.reduce(0) { $0 + ($1.anyMeasured ? $1.measured : 0) }
                let estimated = rows.reduce(0) { $0 + ($1.anyEstimated ? $1.estimated : 0) }
                row.amount = estimated > 0
                    ? "\(Format.usd(measured)) · \(tilde(estimated))"
                    : Format.usd(measured)
            } else if rows.contains(where: \.anyEstimated) {
                let estimated = rows.reduce(0) { $0 + $1.estimated }
                row.amount = estimated > 0 ? tilde(estimated) : "cost unknown"
            } else {
                row.amount = "cost unknown"
            }
            return row
        }
    }

    // MARK: - Dock

    private static func dock(works: [Work], columns: [LedgerDayColumn],
                             openCard: String, selectedDay: String, range: String,
                             acceptance: String, otherOn: Bool) -> LedgerDock {
        if !openCard.isEmpty, let work = works.first(where: { $0.card.cardId == openCard }) {
            return .run(run(work))
        }
        if !selectedDay.isEmpty, let column = columns.first(where: { $0.day == selectedDay }) {
            let measured = column.anyMeasured
                ? Format.usd(column.measured)
                : (column.unknown ? "cost unknown" : Format.usd(0))
            let titles = column.cards.isEmpty
                ? ["No named card"]
                : column.cards.map(\.title)
            var other: String?
            if otherOn, let line = column.otherLine {
                other = "Other sessions on this Mac \(line.replacingOccurrences(of: "other ", with: ""))."
            }
            return .day(title: label(column.day, calendar: .current),
                        measured: measured, estimated: column.estimatedMark,
                        unknown: column.unknown, titles: titles, other: other)
        }
        return .rest(heading: heading(range), acceptance: acceptance, honesty: honesty)
    }

    private static func run(_ work: Work) -> LedgerRun {
        var run = LedgerRun()
        let card = work.card
        run.title = card.title.isEmpty ? dash : card.title
        let priced: String
        if work.anyMeasured, work.anyEstimated, work.estimated > 0 {
            priced = "measured"
        } else if work.anyMeasured {
            priced = "measured"
        } else if work.anyEstimated {
            priced = "estimated"
        } else {
            priced = "not priced"
        }
        run.scope = [work.project, card.who, work.model, priced]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        if work.anyMeasured {
            run.spent = Format.usd(work.measured)
        } else if work.anyEstimated, work.estimated > 0 {
            run.spent = tilde(work.estimated)
        } else {
            run.spent = "cost unknown"
        }
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
            let cost: String
            if let measured = session.measuredCostUsd {
                cost = Format.usd(measured)
            } else if let estimated = session.estimatedCostUsd, estimated > 0 {
                cost = tilde(estimated)
            } else {
                cost = dash
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

    private static func tilde(_ value: Double) -> String {
        "~\(Format.usd(value))"
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
