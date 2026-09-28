import SwiftUI

/// The History tab's rail: the range, the two assistants, the project, the
/// person, the acceptance sentence and the way back to the board. The
/// picture itself is `LedgerPane`, in the board's place. Nothing here
/// refetches for a lens, a person or a day.
struct HistoryView: View {
    @ObservedObject var client: DaemonClient
    @Binding var lens: LedgerLens
    @Binding var person: String
    @Binding var day: String
    @Binding var run: String
    var onBoard: () -> Void

    /// `ApiServer.HISTORY_RANGES` in full. The report is fetched, not pushed,
    /// so a wider choice costs nothing until somebody asks for it.
    private static let ranges = ["today", "7d", "30d", "90d", "all"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    rangeBar
                    providerRow
                    projectPicker
                    personList
                    if let picture = currentPicture() {
                        acceptance(picture.acceptance)
                            .font(.system(size: 11))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
            }
            Button(action: onBoard) {
                Text("Board")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .clickable()
            .padding(.horizontal, 14)
            .padding(.bottom, 10)
            .accessibilityLabel("Back to the board")
        }
        .task(id: "\(client.historyRange)\u{1}\(client.historyRoot)") {
            // Fetch on first appearance and on every range or project change,
            // but never re-fetch an answer already in hand — this query costs
            // seconds. The project is part of the identity because acceptance
            // comes back scoped to it and cannot be sliced here.
            if case .loaded(let report) = client.historyLoad,
               report.range == client.historyRange,
               report.effectiveness.root == client.historyRoot { return }
            await client.loadHistory(range: client.historyRange,
                                     root: client.historyRoot)
        }
    }

    private func currentPicture() -> LedgerPicture? {
        guard case .loaded(let report) = client.historyLoad else { return nil }
        return ledgerPicture(report: report, client: client, lens: lens,
                             person: person, day: day, run: run)
    }

    /// The acceptance sentence in two weights: the three counts bold, the
    /// rest faint. A reason sentence is drawn faint in one run.
    private func acceptance(_ sentence: String) -> Text {
        let split = LedgerWeekLayout.acceptanceSplit(sentence)
        return Text(split.lead).fontWeight(.semibold).foregroundStyle(Theme.dim)
            + Text(split.rest).foregroundStyle(Theme.faint)
    }

    private var rangeBar: some View {
        VStack(alignment: .leading, spacing: 4) {
            kicker("Range")
            HStack(spacing: 4) {
                ForEach(Self.ranges, id: \.self) { range in
                    let active = client.historyRange == range
                    Button { client.historyRange = range } label: {
                        Text(range)
                            .font(Theme.mono(11, weight: active ? .semibold : .regular))
                            .foregroundStyle(active ? AnyShapeStyle(Theme.phosphor)
                                                    : AnyShapeStyle(Theme.faint))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .overlay(Rectangle().strokeBorder(
                                active ? Theme.phosphor : Theme.hair, lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                    .clickable()
                }
            }
        }
    }

    private var providerRow: some View {
        HStack(spacing: 12) {
            providerToggle(provider: "claude", on: $client.historyClaude,
                           otherOn: client.historyGrok)
            providerToggle(provider: "grok", on: $client.historyGrok,
                           otherOn: client.historyClaude)
        }
    }

    private func providerToggle(provider: String, on: Binding<Bool>,
                                otherOn: Bool) -> some View {
        Button {
            if on.wrappedValue && !otherOn { return }
            on.wrappedValue.toggle()
            run = ""
        } label: {
            HStack(spacing: 4) {
                ProviderMark(provider: provider, size: 10)
                Text(provider == "grok" ? "Grok" : "Claude")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            .opacity(on.wrappedValue ? 1 : 0.28)
            .padding(.vertical, 2)
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityLabel(provider == "grok" ? "Grok" : "Claude")
        .accessibilityAddTraits(on.wrappedValue ? .isSelected : [])
    }

    /// Which project the acceptance sentence is about. Picking refetches:
    /// acceptance is per project and cannot be sliced out of a machine-wide
    /// answer. Amounts are this payload only — a project the refetch no
    /// longer includes shows its name and no figure.
    private var projectPicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            kicker("Project")
            let rows = currentPicture()?.projects ?? boardProjects
            Button {
                client.historyRoot = ""
                run = ""
            } label: {
                rowLabel("All", amount: nil, on: client.historyRoot.isEmpty)
            }
            .buttonStyle(.plain)
            .clickable()
            ForEach(rows, id: \.root) { row in
                Button {
                    client.historyRoot = client.historyRoot == row.root ? "" : row.root
                    run = ""
                } label: {
                    rowLabel(row.name.isEmpty ? row.root : row.name,
                             amount: row.amount, on: client.historyRoot == row.root)
                }
                .buttonStyle(.plain)
                .clickable()
            }
        }
    }

    private var boardProjects: [LedgerProjectRow] {
        client.snapshot.board.projects.filter { !$0.root.isEmpty }.map { project in
            var row = LedgerProjectRow()
            row.name = project.name
            row.root = project.root
            return row
        }
    }

    private var personList: some View {
        VStack(alignment: .leading, spacing: 4) {
            kicker("Person")
            if let picture = currentPicture() {
                Button {
                    person = ""
                    run = ""
                } label: {
                    rowLabel("All", amount: nil, on: person.isEmpty)
                }
                .buttonStyle(.plain)
                .clickable()
                ForEach(picture.people, id: \.who) { row in
                    Button {
                        person = person == row.who ? "" : row.who
                        run = ""
                    } label: {
                        rowLabel(row.who, amount: row.amount, on: person == row.who)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                }
            }
        }
    }

    private func rowLabel(_ name: String, amount: String?, on: Bool) -> some View {
        HStack(spacing: 8) {
            Text(name)
                .font(Theme.mono(11, weight: on ? .semibold : .regular))
                .foregroundStyle(on ? Theme.phosphor : Theme.faint)
                .lineLimit(1)
            Spacer(minLength: 4)
            if let amount {
                Text(amount)
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(Theme.faint)
            }
        }
        .padding(.vertical, 2)
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(Theme.mono(10, weight: .semibold))
            .tracking(0.8)
            .foregroundStyle(Theme.faint)
    }
}

/// The wide picture. Fetched with the rail; a lens, a person and a day do
/// not ask again.
struct LedgerPane: View {
    @ObservedObject var client: DaemonClient
    @Binding var lens: LedgerLens
    @Binding var person: String
    @Binding var day: String
    @Binding var run: String

    var body: some View {
        switch client.historyLoad {
        case .idle, .loading:
            loading
        case .failed(let why):
            Text(why)
                .font(.callout)
                .foregroundStyle(Theme.dim)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(24)
        case .loaded(let report):
            loaded(ledgerPicture(report: report, client: client, lens: lens,
                                 person: person, day: day, run: run))
        }
    }

    private var loading: some View {
        VStack(spacing: 8) {
            AgentChatterView(.line, wait: .readingHistory, seed: client.historyRange,
                             spoken: "Reading history")
                .id(client.historyRange)
                .padding(.horizontal, 24)
            Text("A wide range takes a few seconds.")
                .font(.system(size: 10)).foregroundStyle(Theme.faint)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func loaded(_ picture: LedgerPicture) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                Text(picture.masthead)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(alignment: .firstTextBaseline, spacing: 26) {
                    figureButton("Token cost", picture.spent, .spent, picture)
                    figureButton("Tokens out", picture.tokens, .tokens, picture)
                    figureButton("On the cards", picture.time, .time, picture)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(picture.waiting)
                            .font(.system(size: 18, weight: .semibold).monospacedDigit())
                            .foregroundStyle(Theme.amber)
                        Text("waiting on you")
                            .font(.system(size: 10))
                            .foregroundStyle(Theme.faint)
                    }
                    Spacer(minLength: 0)
                }
                Text(picture.aside)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.faint)
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if picture.showLegend {
                        legend
                            .padding(.bottom, 6)
                    }
                    stage(picture)
                    if !opensUnderRow(picture) {
                        dock(picture)
                            .padding(.top, CGFloat(LedgerWeekLayout.dockGap))
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Theme.bg)
    }

    private func figureButton(_ label: String, _ value: String, _ next: LedgerLens,
                              _ picture: LedgerPicture) -> some View {
        Button { lens = next } label: {
            VStack(alignment: .leading, spacing: 1) {
                Text(value)
                    .font(.system(size: 18, weight: .semibold).monospacedDigit())
                    .foregroundStyle(lens == next ? Theme.phosphor : Theme.dim)
                if next == .spent, let reported = picture.reported {
                    // A different kind of money: named, not amber, no tilde.
                    Text("\(reported) reported")
                        .font(.system(size: 10).monospacedDigit())
                        .foregroundStyle(Theme.dim)
                }
                Text(label.lowercased())
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.faint)
            }
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityLabel(label)
        .accessibilityAddTraits(lens == next ? .isSelected : [])
    }

    /// The spent bar's three providers, each named so the ink is not the only
    /// signal, then what a reported dollar and a dash are.
    private var legend: some View {
        HStack(spacing: 12) {
            swatch(.claude, "Claude tokens")
            swatch(.grok, "Grok tokens")
            swatch(.codex, "Codex tokens")
            Text("reported: beside, not added")
                .font(.system(size: 10))
                .foregroundStyle(Theme.dim)
            Text("— not priced")
                .font(.system(size: 10))
                .foregroundStyle(Theme.faint)
        }
    }

    private func swatch(_ swatchInk: LedgerInk, _ name: String) -> some View {
        HStack(spacing: 4) {
            Rectangle().fill(ink(swatchInk)).frame(width: 8, height: 8)
            Text(name)
                .font(.system(size: 10))
                .foregroundStyle(Theme.faint)
        }
    }

    @ViewBuilder
    private func stage(_ picture: LedgerPicture) -> some View {
        switch picture.mode {
        case .today:
            today(picture)
        case .week:
            week(picture)
        case .thin:
            thin(picture)
        }
    }

    private func today(_ picture: LedgerPicture) -> some View {
        let column = picture.days.first
        let cards = column?.cards ?? []
        return VStack(alignment: .leading, spacing: 4) {
            if cards.isEmpty {
                Text("No named card")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.faint)
            }
            ForEach(cards, id: \.id) { card in
                wideRow(card, picture)
            }
            if let other = column?.otherLine {
                Text(other)
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(Theme.faint)
            }
        }
    }

    private func week(_ picture: LedgerPicture) -> some View {
        let scale = max(picture.days.map(\.magnitude).max() ?? 0, 0.0001)
        let padding = CGFloat(LedgerWeekLayout.columnPadding)
        return HStack(alignment: .top, spacing: CGFloat(LedgerWeekLayout.columnGap)) {
            ForEach(Array(picture.days.enumerated()), id: \.element.day) { index, column in
                VStack(alignment: .leading, spacing: 4) {
                    Button {
                        pick(column.day)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(column.weekday)
                                .font(.system(size: 10))
                                .foregroundStyle(Theme.faint)
                            Text(column.dayNumber)
                                .font(.system(size: 13, weight: .semibold).monospacedDigit())
                                .foregroundStyle(Theme.phosphorBright)
                            bar(column, scale: scale, usual: picture.usual,
                                word: index == 0, hot: day == column.day,
                                width: CGFloat(LedgerWeekLayout.barWidth),
                                dashReach: padding)
                            figureLine(column)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    if column.noNamedCard {
                        Text(column.emptyNote ?? "No named card")
                            .font(.system(size: 10))
                            .foregroundStyle(Theme.faint)
                    }
                    ForEach(column.cards, id: \.id) { card in
                        cardButton(card, showProject: picture.showProject)
                    }
                    if let other = column.otherLine {
                        Text(other)
                            .font(.system(size: 10).monospacedDigit())
                            .foregroundStyle(Theme.faint)
                    }
                }
                .padding(padding)
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .background(Theme.well)
                .overlay(Rectangle().strokeBorder(
                    day == column.day ? Theme.phosphor : Theme.rule, lineWidth: 1))
            }
        }
    }

    private func thin(_ picture: LedgerPicture) -> some View {
        let scale = max(picture.days.map(\.magnitude).max() ?? 0, 0.0001)
        let focus = picture.days.first { $0.day == picture.focusDay }
        return VStack(alignment: .leading, spacing: 10) {
            GeometryReader { geo in
                let width = CGFloat(LedgerColumnLayout.columnWidth(
                    count: picture.days.count, available: Double(geo.size.width)))
                let scrolls = LedgerColumnLayout.scrolls(
                    count: picture.days.count, available: Double(geo.size.width))
                ScrollView(.horizontal) {
                    HStack(alignment: .bottom, spacing: CGFloat(LedgerColumnLayout.spacing)) {
                        ForEach(Array(picture.days.enumerated()), id: \.element.day) { index, column in
                            let open = day == column.day
                            Button { pick(column.day) } label: {
                                VStack(spacing: 2) {
                                    bar(column, scale: scale, usual: nil, word: false, hot: open)
                                        .frame(width: width)
                                    Text(LedgerColumnLayout.showsTick(
                                        index: index, count: picture.days.count,
                                        selected: open) ? column.dayNumber : " ")
                                        .font(.system(size: 8).monospacedDigit())
                                        .foregroundStyle(open ? Theme.phosphor : Theme.faint)
                                }
                            }
                            .buttonStyle(.plain)
                            .clickable()
                            .accessibilityLabel("\(column.day) \(column.figure)")
                        }
                    }
                    .frame(minWidth: scrolls ? nil : geo.size.width, alignment: .leading)
                }
                .scrollDisabled(!scrolls)
            }
            .frame(height: 90)
            if let focus {
                Text(focus.weekday + " " + focus.dayNumber)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.faint)
                if focus.noNamedCard {
                    Text("No named card")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.faint)
                }
                ForEach(focus.cards, id: \.id) { card in
                    wideRow(card, picture)
                }
            }
        }
    }

    /// The day's token cost, its reported dollar and the not-priced dash on
    /// one baseline, in the order `LedgerWeekLayout.figureLine` gives. A day
    /// nothing happened keeps the line's height with a blank, not $0.00.
    private func figureLine(_ column: LedgerDayColumn) -> some View {
        let parts = LedgerWeekLayout.figureLine(column)
        return HStack(alignment: .firstTextBaseline, spacing: 3) {
            if parts.isEmpty {
                Text(" ").font(.system(size: 11).monospacedDigit())
            }
            ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                Text(part.text)
                    .font(.system(size: part.ink == .claude ? 11 : 10).monospacedDigit())
                    .foregroundStyle(part.ink == .claude ? Theme.phosphorBright
                                     : part.ink == .reported ? Theme.dim : Theme.faint)
            }
        }
    }

    /// One day's bar. The segments stack as `LedgerWeekLayout.segments`
    /// orders them, at the foot of a fixed-height box; `width` nil fills the
    /// column (the thin columns set their own width). `dashReach` pulls the
    /// usual line out to the column's border.
    private func bar(_ column: LedgerDayColumn, scale: Double, usual: Double?,
                     word: Bool, hot: Bool = false, width: CGFloat? = nil,
                     dashReach: CGFloat = 0) -> some View {
        let segments = LedgerWeekLayout.segments(column, lens: lens, scale: scale)
        return ZStack(alignment: .bottomLeading) {
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                if segments.isEmpty {
                    Rectangle()
                        .fill(hot ? Theme.phosphor : Theme.hair)
                        .frame(height: 2)
                } else {
                    ForEach(Array(segments.enumerated()), id: \.offset) { _, segment in
                        segmentView(segment)
                    }
                }
            }
            .frame(width: width)
            .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
            if let lift = LedgerWeekLayout.usualLift(usual: usual, lens: lens, scale: scale) {
                VStack(alignment: .leading, spacing: 1) {
                    if word {
                        Text("usual")
                            .font(.system(size: 8))
                            .foregroundStyle(Theme.faint)
                            .padding(.leading, dashReach)
                    }
                    UsualDash()
                }
                .padding(.horizontal, -dashReach)
                .padding(.bottom, CGFloat(lift))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .frame(height: CGFloat(LedgerWeekLayout.barHeight))
    }

    @ViewBuilder
    private func segmentView(_ segment: LedgerWeekLayout.Segment) -> some View {
        let height = CGFloat(segment.height)
        switch segment.ink {
        case .claude:
            // Claude keeps its own ink on the open column too: the bright
            // text ink is Codex's. The open column is marked by its outline
            // (week) or its lit day number (thin), never by a segment ink.
            Rectangle().fill(Theme.phosphor)
                .frame(maxWidth: .infinity).frame(height: height)
        case .grok, .codex, .reported, .unknown:
            Rectangle().fill(ink(segment.ink))
                .frame(maxWidth: .infinity).frame(height: height)
        }
    }

    /// The wide rows (today, and the focused day of a long range) open a
    /// run's details directly under the row that was clicked; the week's
    /// narrow tiles keep them in the dock beneath the columns.
    private func opensUnderRow(_ picture: LedgerPicture) -> Bool {
        guard case .run = picture.dock, !run.isEmpty else { return false }
        let rows: [LedgerCardLine]
        switch picture.mode {
        case .today: rows = picture.days.first?.cards ?? []
        case .thin: rows = picture.days.first { $0.day == picture.focusDay }?.cards ?? []
        case .week: return false
        }
        return rows.contains { $0.id == run }
    }

    private func wideRow(_ card: LedgerCardLine, _ picture: LedgerPicture) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            wideRowButton(card, showProject: picture.showProject)
            if run == card.id, opensUnderRow(picture) {
                dock(picture)
                    .padding(.leading, 54)
                    .padding(.top, 2)
                    .padding(.bottom, 6)
            }
        }
    }

    private func wideRowButton(_ card: LedgerCardLine, showProject: Bool) -> some View {
        Button { open(card) } label: {
            HStack(spacing: 8) {
                Text(card.clock)
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.faint)
                    .frame(width: 40, alignment: .leading)
                VStack(alignment: .leading, spacing: 1) {
                    Text(card.title)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(run == card.id ? Theme.phosphor : Theme.phosphorBright)
                    Text([card.who, showProject ? card.project : "", card.model]
                            .filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.faint)
                        .lineLimit(1)
                }
                share(card)
                    .frame(width: 80)
                Text(card.figure)
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.dim)
            }
            .padding(.vertical, 4)
            .padding(.leading, 6)
            .overlay(alignment: .leading) { edge(card) }
        }
        .buttonStyle(.plain)
        .clickable()
    }

    private func cardButton(_ card: LedgerCardLine, showProject: Bool) -> some View {
        Button { open(card) } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(card.title)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(run == card.id ? Theme.phosphor : Theme.phosphorBright)
                    .lineLimit(2)
                if showProject, !card.project.isEmpty {
                    Text(card.project)
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.faint)
                        .lineLimit(1)
                }
                Text([card.who, card.model].filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(1)
                tileMeta(card)
                    .font(.system(size: 10).monospacedDigit())
                    .lineLimit(1)
            }
            .padding(5)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card)
            .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
            .overlay(alignment: .leading) { edge(card) }
        }
        .buttonStyle(.plain)
        .clickable()
    }

    /// `figure · N turns` as one line, the figure in the heavier ink.
    private func tileMeta(_ card: LedgerCardLine) -> Text {
        let meta = LedgerWeekLayout.tileMeta(card)
        let rest = meta.hasPrefix(card.figure) ? String(meta.dropFirst(card.figure.count)) : meta
        let lead = meta.hasPrefix(card.figure) ? card.figure : ""
        return Text(lead).fontWeight(.medium).foregroundStyle(Theme.dim)
            + Text(rest).foregroundStyle(Theme.faint)
    }

    @ViewBuilder
    private func edge(_ card: LedgerCardLine) -> some View {
        if card.manual {
            Rectangle().fill(Theme.amber).frame(width: 3)
        }
    }

    private func share(_ card: LedgerCardLine) -> some View {
        GeometryReader { geo in
            Rectangle()
                .fill(ink(card.ink))
                .frame(width: max(6, geo.size.width * card.share), height: 6)
                .frame(maxHeight: .infinity, alignment: .center)
        }
        .frame(height: 10)
    }

    /// Codex is the bright text ink: not Claude's green, Grok's dim green,
    /// amber (your turn) or the alarm red (needs you).
    private func ink(_ ink: LedgerInk) -> Color {
        switch ink {
        case .claude: return Theme.phosphor
        case .grok: return Theme.dim
        case .codex: return Theme.phosphorBright
        case .reported: return Theme.dim
        case .unknown: return Theme.faint
        }
    }

    private func open(_ card: LedgerCardLine) {
        if run == card.id {
            run = ""
        } else {
            run = card.id
            day = card.day
        }
    }

    private func pick(_ picked: String) {
        if day == picked {
            day = ""
        } else {
            day = picked
        }
        run = ""
    }

    @ViewBuilder
    private func dock(_ picture: LedgerPicture) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            switch picture.dock {
            case .rest(let heading, let acceptance, let honesty):
                Text(heading)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.phosphorBright)
                Text(acceptance)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.faint)
                Text(honesty)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.faint)
            case .day(let title, let spent, let reported, let unknown, let titles, let other):
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.phosphorBright)
                Text(dayLine(spent: spent, reported: reported, unknown: unknown))
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.faint)
                Text(titles.joined(separator: " · "))
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.dim)
                if let other {
                    Text(other)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.faint)
                }
            case .run(let run):
                Text(run.title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.phosphorBright)
                Text(run.scope)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.faint)
                HStack(spacing: 22) {
                    dockFig(run.spent, "token cost")
                    if let reported = run.reported {
                        dockFig(reported, "reported, not added")
                    }
                    dockFig(run.tokens, "tokens out")
                    dockFig(run.time, "on the card")
                    dockFig(run.verdict, run.rework)
                }
                ForEach(run.phases, id: \.self) { phase in
                    Text(phase)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                }
                if run.manual {
                    Text("manual check outstanding")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.amber)
                }
                if let resumeCommand = run.resume {
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(resumeCommand, forType: .string)
                    } label: {
                        Text("resume")
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.phosphor)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    .accessibilityLabel("Copy `\(resumeCommand)`")
                }
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.well)
        .overlay(Rectangle().strokeBorder(Theme.rule, lineWidth: 1))
    }

    private func dayLine(spent: String, reported: String?, unknown: Bool) -> String {
        var parts = [spent == "cost unknown" || spent == "nothing spent"
                     ? spent : "\(spent) token cost"]
        if let reported { parts.append(reported) }
        if unknown, spent != "cost unknown" { parts.append("some turns not priced") }
        return parts.joined(separator: " · ")
    }

    private func dockFig(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value)
                .font(.system(size: 12, weight: .semibold).monospacedDigit())
                .foregroundStyle(Theme.phosphorBright)
            Text(label)
                .font(.system(size: 10))
                .foregroundStyle(Theme.faint)
        }
    }

}

/// The seven-column usual mark. Dashed on every column; the word sits
/// beside it only on the first, from the caller.
private struct UsualDash: View {
    var body: some View {
        GeometryReader { geo in
            Path { path in
                path.move(to: CGPoint(x: 0, y: 0.5))
                path.addLine(to: CGPoint(x: geo.size.width, y: 0.5))
            }
            .stroke(Theme.faint, style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
        }
        .frame(height: 1)
    }
}

@MainActor
private func ledgerPicture(report: HistoryReport, client: DaemonClient,
                           lens: LedgerLens, person: String, day: String,
                           run: String) -> LedgerPicture {
    LedgerFold.picture(
        report: report, projects: client.snapshot.board.projects,
        range: client.historyRange, lens: lens,
        claude: client.historyClaude, grok: client.historyGrok,
        person: person, selectedDay: day, openCard: run)
}
