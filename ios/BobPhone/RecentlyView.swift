import SwiftUI

/// The Mac's diary, under the process table: what started, finished, errored,
/// asked or moved while the screen was off, newest first, in the Mac's own
/// words (`LogEntry.text` is drawn verbatim — nothing here composes a
/// sentence). Folds and unfolds, and remembers which
/// (`fleet.recently.collapsed`).
///
/// A line that names an agent still on the fleet opens that agent; one that
/// names a card still on the board opens that card; anything else is simply
/// readable. Relative times are recomputed once a minute inside a
/// `TimelineView` — the view holds no clock of its own.
struct RecentlySection: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    @AppStorage("fleet.recently.collapsed") private var collapsed = false

    private enum Item: Identifiable {
        case day(String)
        case entry(LogEntry)

        var id: String {
            switch self {
            case .day(let label): return "day/\(label)"
            case .entry(let entry): return "entry/\(entry.id)"
            }
        }
    }

    var body: some View {
        if !client.log.isEmpty {
            DecryptButton {
                collapsed.toggle()
            } label: {
                HStack(spacing: 6) {
                    PhoneSectionHeader(title: "RECENTLY (\(client.log.count))")
                    Spacer(minLength: 0)
                    Text(collapsed ? "▸" : "▾")
                        .font(Theme.mono(11, weight: .semibold))
                        .foregroundStyle(Theme.faint)
                        .padding(.trailing, 12)
                        .padding(.top, 10)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            // On the `Button`, so `.combine`: `.ignore` would drop the press.
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Recently, \(client.log.count) entries")
            .accessibilityValue(collapsed ? "collapsed" : "expanded")
            .accessibilityHint(collapsed ? "Shows the list" : "Hides the list")
            .listRowInsets(EdgeInsets())
            .listRowSeparator(.hidden)
            .listRowBackground(Theme.bg)
            if !collapsed {
                ForEach(items) { item in
                    Group {
                        switch item {
                        case .day(let label):
                            Text(label)
                                .font(Theme.mono(10, weight: .semibold))
                                .tracking(0.8)
                                .foregroundStyle(Theme.faint)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 12)
                                .padding(.top, 8)
                                .padding(.bottom, 2)
                        case .entry(let entry):
                            row(entry)
                        }
                    }
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
                }
            }
        }
    }

    // MARK: - Rows

    @ViewBuilder
    private func row(_ entry: LogEntry) -> some View {
        let label = RecentlyRow(entry: entry)
        if let (agent, category) = agent(for: entry) {
            DecryptButton(action: { sheets.show(.agent(agent, category)) }) { label }
            .buttonStyle(.plain)
        } else if let card = card(for: entry) {
            DecryptButton(action: { sheets.show(.card(card)) }) { label }
            .buttonStyle(.plain)
        } else {
            label
        }
    }

    /// Entries newest first, with a day separator only where the list spans
    /// more than one calendar day.
    private var items: [Item] {
        let entries = client.log
        let calendar = Calendar.current
        let days = Set(entries.map { calendar.startOfDay(for: Date(timeIntervalSince1970: $0.ts)) })
        guard days.count > 1 else { return entries.map(Item.entry) }
        var out: [Item] = []
        var lastDay: Date?
        for entry in entries {
            let day = calendar.startOfDay(for: Date(timeIntervalSince1970: entry.ts))
            if day != lastDay {
                out.append(.day(Self.dayLabel(day, calendar: calendar)))
                lastDay = day
            }
            out.append(.entry(entry))
        }
        return out
    }

    static func dayLabel(_ day: Date, calendar: Calendar = .current) -> String {
        if calendar.isDateInToday(day) { return "TODAY" }
        if calendar.isDateInYesterday(day) { return "YESTERDAY" }
        let f = DateFormatter()
        f.calendar = calendar
        f.dateFormat = "d MMM"
        return f.string(from: day).uppercased()
    }

    // MARK: - Routing

    private func agent(for entry: LogEntry) -> (Agent, Category)? {
        guard !entry.sessionId.isEmpty else { return nil }
        let a = client.snapshot.agents
        let buckets: [(Category, [Agent])] = [
            (.waiting, a.waiting), (.running, a.running), (.sleeping, a.sleeping),
            (.finished, a.finished), (.abandoned, a.abandoned),
        ]
        for (category, rows) in buckets {
            if let agent = rows.first(where: { $0.sessionId == entry.sessionId }) {
                return (agent, category)
            }
        }
        return nil
    }

    private func card(for entry: LogEntry) -> BoardCard? {
        guard !entry.cardId.isEmpty else { return nil }
        return client.snapshot.board.cards.first { $0.id == entry.cardId }
    }
}

/// One diary line: a glyph for the kind, when, the Mac's sentence, the project.
/// The relative time sits inside its own 60 s `TimelineView`, so it is the
/// only thing that redraws on the minute and the row holds no clock.
struct RecentlyRow: View {
    let entry: LogEntry

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(Self.glyph(for: entry))
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(Self.tint(for: entry))
                .frame(width: dynamicTypeSize.isAccessibilitySize ? nil : 14,
                       alignment: .center)
                // The glyph stands for the kind; `spoken` says the kind.
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.text)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
                AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize,
                              spacing: 6) {
                    TimelineView(.periodic(from: Date(), by: 60)) { context in
                        Text(Self.when(entry.ts, now: context.date))
                            .font(Theme.mono(10).monospacedDigit())
                            .foregroundStyle(Theme.faint)
                    }
                    if !entry.project.isEmpty {
                        Text(entry.project)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// One diary line as one sentence: what kind of moment it was, the Mac's
    /// own words for it, when, and where. `entry.text` is drawn verbatim here
    /// exactly as it is on screen — nothing is composed from the daemon's
    /// fields a second time.
    private var spoken: String {
        var parts = [Self.kindWord(for: entry), entry.text,
                     Self.when(entry.ts, now: Date())]
        if !entry.project.isEmpty { parts.append(entry.project) }
        return parts.filter { !$0.isEmpty }.joined(separator: ", ")
    }

    /// The glyph, said out loud. `▶` and `⚑` are not words.
    static func kindWord(for entry: LogEntry) -> String {
        switch entry.kind {
        case "session_start": return "started"
        case "session_end": return "finished"
        case "session_error": return "error"
        case "permission_ask": return "asked permission"
        case "permission_resolved":
            switch entry.detail["outcome"] ?? "" {
            case "allow", "allowed": return "allowed"
            case "deny", "denied": return "denied"
            default: return "permission resolved"
            }
        case "card_dispatched": return "card started"
        case "card_done": return "card done"
        case "card_manual": return "manual check"
        case "card_dispatch_failed": return "card refused"
        case "card_plan_attached": return "plan attached"
        default: return ""
        }
    }

    /// One glyph per kind. Never colour alone: the glyph itself says which.
    static func glyph(for entry: LogEntry) -> String {
        switch entry.kind {
        case "session_start": return "▶"
        case "session_end": return "■"
        case "session_error": return "!"
        case "permission_ask": return "?"
        case "permission_resolved":
            switch entry.detail["outcome"] ?? "" {
            case "allow", "allowed": return "✓"
            case "deny", "denied": return "✗"
            default: return "…"
            }
        case "card_dispatched": return "⇒"
        case "card_done": return "☑"
        case "card_manual": return "⚑"
        case "card_dispatch_failed": return "⨯"
        case "card_plan_attached": return "§"
        default: return "·"
        }
    }

    static func tint(for entry: LogEntry) -> Color {
        switch entry.kind {
        case "session_error", "card_dispatch_failed": return Theme.alarm
        case "permission_ask", "card_manual": return Theme.amber
        default: return Theme.dim
        }
    }

    /// "now", "4m ago", "13:07", "yesterday 23:10", "2 Sep 09:41".
    static func when(_ ts: Double, now: Date, calendar: Calendar = .current) -> String {
        let date = Date(timeIntervalSince1970: ts)
        let age = now.timeIntervalSince(date)
        if age < 60 { return "now" }
        if age < 3600 { return "\(Int(age / 60))m ago" }
        let time = DateFormatter()
        time.calendar = calendar
        time.dateFormat = "HH:mm"
        if calendar.isDate(date, inSameDayAs: now) { return time.string(from: date) }
        if calendar.isDateInYesterday(date) { return "yesterday \(time.string(from: date))" }
        let day = DateFormatter()
        day.calendar = calendar
        day.dateFormat = "d MMM HH:mm"
        return day.string(from: date)
    }
}
