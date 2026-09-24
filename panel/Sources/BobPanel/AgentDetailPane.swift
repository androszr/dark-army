import SwiftUI

/// The selected agent, drawn large in the board's place.
///
/// The rail keeps the compact process table; picking a row there hands the
/// wide left pane to this view (`RailLayout.leftPane`). Top to bottom: a
/// **hero band** naming the agent — a large portrait, the name at reading
/// size, where it is working, and a reflowing grid of the same facts the
/// phone's detail lists (context with its meter, cost, elapsed, model,
/// tools, files, output, lines, helpers) — then the body.
///
/// Where Dark Army does not host a terminal there is one body and no tab bar: the
/// `StdoutPane` (last message, answer buttons, permission bar, reply box,
/// low-priority and wrap-up bars), under `ElsewhereTerminalBand`. Where Dark Army
/// *does* host the terminal there are two tabs, `DetailTab`: **Details** is
/// that same `StdoutPane`, and **Terminal** is the live screen with those
/// bars kept as a compact strip above it, only when they have something to
/// show. Details is where every open lands, and leaving the Terminal tab
/// takes the pane out of the hierarchy — which is the stream's disconnect
/// and the hand-back of the pty's width to the phone.
///
/// The one geometry read here takes the pane's own size to size the pty. It
/// does not size the window — the frame stays `Placement.swift`'s, and the
/// rail stays `PanelMetrics.width`. There is no divider: the terminal takes
/// every point under the hero (and under the action strip, when that strip
/// is present).
struct AgentDetailPane: View {
    let agent: Agent
    let category: Category
    let card: Notification?
    let prompt: PermissionPrompt?
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient
    var onJump: () -> Void = {}
    /// Reveal the board card this session is bound to. **Absent, not inert**:
    /// `nil` where the session is bound to no card, so the chip is not drawn.
    var onShowCard: (() -> Void)? = nil
    /// The **board** card this session is on, for the header's lead line.
    /// Not `card` above — that one is the notification. `nil` where the
    /// session is on no card, and the header then draws no title line.
    var boardCard: BoardCard? = nil
    var onBack: () -> Void = {}
    var collaborationHelper = ""
    var onCollaborationSession: (String, String, String) -> Void = { _, _, _ in }
    var onCollaborationCard: (BoardCard) -> Void = { _ in }
    var onReplyFocus: (Bool) -> Void = { _ in }
    var onInputFocus: (Bool) -> Void = { _ in }
    var onTerminalFocus: (Bool) -> Void = { _ in }

    /// Which tab a hosted row is showing. Not persisted and not remembered
    /// per session: `DetailTab.defaultTab` is where every open lands, and
    /// the `onChange` below re-applies it when the selection moves from one
    /// row to another, where SwiftUI keeps this view's identity.
    @State private var tab: DetailTab = DetailTab.defaultTab

    var body: some View {
        VStack(spacing: 0) {
            AgentDetailHeader(agent: agent, category: category,
                              asOf: client.snapshot.agentsStamp,
                              ticking: client.visible,
                              onBack: onBack, onJump: onJump,
                              onShowCard: onShowCard, boardCard: boardCard)
            Rectangle().fill(Theme.hair).frame(height: 1)
            if DetailTab.showsTabBar(hosted: agent.ownTerminal) {
                DetailTabBar(tab: $tab)
                Rectangle().fill(Theme.hair).frame(height: 1)
            }
            if DetailTab.terminalAttached(hosted: agent.ownTerminal, tab: tab) {
                VStack(spacing: 0) {
                    if showsActions {
                        stdout(messageHidden: true)
                    }
                    GeometryReader { geo in
                        TerminalPane(agent: agent, client: client,
                                     cols: PanelMetrics.terminalCols(forWidth: geo.size.width),
                                     rows: PanelMetrics.terminalRows(forHeight: geo.size.height),
                                     onInputFocus: onInputFocus,
                                     onTerminalFocus: onTerminalFocus)
                            .frame(width: geo.size.width, height: geo.size.height)
                    }
                }
            } else {
                if !agent.ownTerminal {
                    ElsewhereTerminalBand(agent: agent, category: category,
                                          asOf: client.snapshot.agentsStamp,
                                          ticking: client.visible,
                                          onJump: onJump)
                }
                CollaborationView(client: client, focus: .session(provider: agent.provider, id: agent.sessionId),
                                  helperFocus: collaborationHelper,
                                  onSession: onCollaborationSession, onCard: onCollaborationCard)
                stdout(messageHidden: false)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .onChange(of: agent.sessionId) { _, _ in
            tab = DetailTab.defaultTab
        }
    }

    /// Permission, a question, reply, wrap-up, low-priority, or a refusal
    /// — the bars that still do something. The last-message dump does not
    /// count: the terminal already is that document.
    private var showsActions: Bool {
        if prompt != nil { return true }
        if agent.canHide { return true }
        let stopped = category == .waiting || category == .sleeping
        let layout = StdoutPane.answerLayout(
            hasOptions: !agent.question.options.isEmpty,
            questionCount: agent.questionList.count,
            multiSelect: agent.question.multiSelect,
            canType: agent.canType,
            promptUp: prompt != nil,
            stopped: stopped,
            channel: agent.channel)
        if layout.options != .none { return true }
        if layout.reply { return true }
        if stopped, agent.canLowPriority { return true }
        if stopped, agent.canClose { return true }
        if !actions.refusal(for: agent).isEmpty { return true }
        return false
    }

    private func stdout(messageHidden: Bool) -> some View {
        StdoutPane(
            agent: agent,
            category: category,
            card: card,
            prompt: prompt,
            actions: actions,
            client: client,
            onReplyFocus: onReplyFocus,
            messageHidden: messageHidden)
    }
}

/// The two labels beneath the hero band on a hosted row: **DETAILS** and
/// **TERMINAL**.
///
/// `PanelView.tabs`' idiom at the detail's measure — the selected cell is
/// *filled* as well as tinted, so the selection is form before colour. It
/// takes no key binding: the rail's single-letter verbs must not gain a
/// third meaning, and Escape's rungs are unchanged.
struct DetailTabBar: View {
    @Binding var tab: DetailTab

    var body: some View {
        HStack(spacing: 0) {
            ForEach(DetailTab.allCases, id: \.self) { item in
                Button { tab = item } label: {
                    Text(item.rawValue.uppercased())
                        .font(Theme.mono(10, weight: tab == item ? .semibold : .regular))
                        .tracking(0.8)
                        .foregroundStyle(tab == item ? Theme.phosphor : Theme.faint)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                        .background(tab == item ? Theme.well : Color.clear)
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("\(item.rawValue) tab")
                .accessibilityAddTraits(tab == item ? [.isButton, .isSelected] : .isButton)
            }
        }
        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

/// The detail's hero: the way back to the board and the two chips that used
/// to sit on the stdout pane's header — `card ⌗` (absent where the session
/// is bound to no card) and `jump ↗` (absent unless the row can be jumped to
/// an editor, and absent on a hosted session: this pane *is* that terminal)
/// — then the agent itself, drawn at a size the wide pane can afford: a
/// large portrait beside its name, its place, its provider and its state,
/// over `AgentFactsGrid`.
///
/// The wide pane is where the facts belong. The rail's row has six narrow
/// columns and can only say `run` and a context percentage; here there is
/// room for everything the daemon already publishes about the session, and
/// the phone has been showing that list for months.
struct AgentDetailHeader: View {
    let agent: Agent
    let category: Category
    /// When the fleet section this row came from was composed, and whether the
    /// panel is on screen — both handed straight to `AgentFactsGrid`, whose
    /// two relative figures count up on the panel's own second hand.
    var asOf: Double = 0
    var ticking: Bool = true
    var onBack: () -> Void = {}
    var onJump: () -> Void = {}
    var onShowCard: (() -> Void)? = nil
    /// The board card this session is on, drawn as the header's lead line.
    /// Named `boardCard` because the pane's `card` is the notification.
    var boardCard: BoardCard? = nil

    /// The portrait's size in the hero. Large enough to read as a person
    /// rather than a bullet — this is the one place a face has the room.
    static let avatar: CGFloat = 76

    private var name: String {
        agent.nickname.isEmpty ? String(agent.sessionId.prefix(8)) : agent.nickname
    }

    private var place: String {
        agent.branch.isEmpty ? agent.project : "\(agent.project) · \(agent.branch)"
    }

    /// The character's one line under the name — this header is a large
    /// portrait, the Mac's one place a face carries a quote. Empty for a
    /// nickname off the roster (`CastQuotes.line(forNickname:)`).
    private var quote: String {
        CastQuotes.line(forNickname: agent.nickname)
    }

    /// The card's title as the header's lead line. A card *bound* to this
    /// session is being worked; one only *refining* under this session is
    /// being planned, and says so — plain words, so a reader can tell the
    /// two apart without knowing what a refinement is. Empty for no card.
    static func cardLine(card: BoardCard?, sessionId: String) -> String {
        guard let card else { return "" }
        let title = card.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return "" }
        if card.sessionId != sessionId, card.isRefining,
           card.refineSessionId == sessionId {
            return "Planning: " + title
        }
        return title
    }

    private var cardLine: String {
        Self.cardLine(card: boardCard, sessionId: agent.sessionId)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Button(action: onBack) {
                    Text("‹ Board")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.phosphor)
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityLabel("Back to the board")
                Spacer(minLength: 4)
                if let onShowCard {
                    Button(action: onShowCard) {
                        Text("card ⌗")
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.phosphor)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    .accessibilityLabel("Show this session's card on the board")
                }
                if agent.canJump && !agent.ownTerminal {
                    Button(action: onJump) {
                        Text("jump ↗")
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.phosphor)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    .accessibilityLabel("Jump to terminal")
                }
            }
            HStack(alignment: .center, spacing: 14) {
                AvatarView(character: Cast.character(for: agent),
                           state: Cast.state(for: agent, category: category),
                           size: Self.avatar)
                VStack(alignment: .leading, spacing: 6) {
                    if !cardLine.isEmpty {
                        Button(action: { onShowCard?() }) {
                            Text(cardLine)
                                .font(Theme.mono(20, weight: .semibold))
                                .foregroundStyle(Theme.phosphorBright)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .buttonStyle(.plain)
                        .clickable()
                        .disabled(onShowCard == nil)
                        .accessibilityLabel("Card: \(cardLine). Show it on the board")
                    }
                    Text(name)
                        .font(Theme.mono(20, weight: cardLine.isEmpty ? .semibold : .regular))
                        .foregroundStyle(cardLine.isEmpty ? Theme.phosphorBright : Theme.phosphor)
                    if !quote.isEmpty {
                        Text(quote)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 6) {
                        if !place.isEmpty {
                            Text(place)
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.dim)
                            Text("·")
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.faint)
                        }
                        Text(ProviderMark.displayName(agent.provider))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                        Text("·")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                        Text(category.rawValue)
                            .font(Theme.mono(11, weight: .medium))
                            .foregroundStyle(category == .waiting ? Theme.alarm : Theme.dim)
                        Spacer(minLength: 4)
                    }
                    // Where this session came from, in the daemon's own
                    // words and never re-composed here. Absent — not empty —
                    // on a session the person started themselves, which is
                    // what makes it worth reading: it appears exactly where
                    // you did not press the button.
                    if !agent.originLine.isEmpty {
                        Text(agent.originLine)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityLabel(agent.originLine)
                    }
                    if !agent.areaLine.isEmpty {
                        Text(agent.areaLine).font(Theme.mono(11)).foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    AgentFactsGrid(agent: agent, category: category,
                                   asOf: asOf, ticking: ticking)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .padding(.bottom, 10)
    }
}

/// The session's numbers, as reflowing tiles.
///
/// The same list the phone's `AgentDetailView.factLines` draws, laid out for
/// a pane that is wide rather than tall: a `LazyVGrid` of adaptive columns,
/// so the row count follows the pane's width without anything measuring it.
/// A fact the daemon did not publish is **absent**, never a zero or a dash
/// standing in for one — an unsent cost and a cost of nothing are different
/// news. What does not fit a tile (the helpers' names, the command line)
/// falls to a plain line underneath.
struct AgentFactsGrid: View {
    let agent: Agent
    let category: Category
    /// When the fleet section these figures came from was composed. `ELAPSED`
    /// and `WAITING FOR YOU` are relative numbers and are aged from it —
    /// see `AgentFacts.aged`.
    var asOf: Double = 0
    /// Gate the 1s clock on panel visibility — `TimelineView` does not stop
    /// because the window is ordered out. `ProcessRow.ticking`'s rule.
    var ticking: Bool = true

    /// Narrowest a column may be. The pane divides its width into as many
    /// equal columns of at least this as it can hold, so the tiles are one
    /// grid rather than a ragged row — four or five on the ~1100pt pane a
    /// 520pt rail leaves on a laptop, which wraps this list to two rows.
    static let tileMin: CGFloat = 150
    /// Every tile is this tall, whatever it holds. A `MODEL` whose name
    /// wraps must not be the one box taller than its neighbours, so the
    /// wrapping ones ask for two columns instead (`AgentFacts.Tile.span`).
    static let tileHeight: CGFloat = 52

    private var live: Bool {
        category == .waiting || category == .running || category == .sleeping
    }

    private func tiles(now: Double) -> [AgentFacts.Tile] {
        AgentFacts.tiles(agent: agent, category: category, live: live,
                         asOf: asOf, now: now)
    }

    private var helpers: String {
        guard live, agent.subagents > 0 else { return "" }
        let names = agent.subagentRows.map(\.qualified).filter { !$0.isEmpty }
        if names.isEmpty { return agent.subagentSummary }
        return "\(agent.subagents) · " + names.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // The two relative figures here count up on the panel's own second
            // hand: the fleet section only rides a frame when it has news, so
            // printing them as they arrived would freeze them on a quiet
            // machine (see `AgentFacts.aged`).
            if ticking {
                TimelineView(Clocks.SecondHand()) { context in
                    flow(now: context.date.timeIntervalSince1970)
                }
            } else {
                flow(now: Date().timeIntervalSince1970)
            }
            if !helpers.isEmpty {
                factLine(label: "helpers", value: helpers)
            }
            if !agent.currentTool.isEmpty {
                factLine(label: "tool", value: agent.currentTool)
            }
            if !agent.name.isEmpty {
                factLine(label: "cmd", value: agent.name)
            }
        }
    }

    private func flow(now: Double) -> some View {
        FactFlow(unit: Self.tileMin, spacing: 8, rowHeight: Self.tileHeight) {
            ForEach(tiles(now: now)) { tile in
                AgentFactTile(tile: tile)
                    .factSpan(tile.span)
            }
        }
    }

    private func factLine(label: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .frame(width: 46, alignment: .leading)
            Text(value)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label), \(value)")
    }
}

/// One tile: a label, a figure, an optional meter and an optional note.
/// Every tile reads the same whether or not it has a meter, and the meter is
/// never the only thing that says the number — the figure is above it.
struct AgentFactTile: View {
    let tile: AgentFacts.Tile

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(tile.label)
                .font(Theme.mono(9, weight: .medium))
                .foregroundStyle(Theme.faint)
                .tracking(0.6)
            Text(tile.value)
                .font(Theme.mono(14, weight: .medium))
                .foregroundStyle(tile.alarming ? Theme.alarm : Theme.phosphorBright)
            if let fill = tile.meter {
                meter(fill)
            }
            if !tile.note.isEmpty {
                Text(tile.note)
                    .font(Theme.mono(9))
                    .foregroundStyle(Theme.faint)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(Theme.well)
        .overlay(Rectangle().stroke(Theme.rule, lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(tile.spoken)
    }

    private func meter(_ fill: Double) -> some View {
        ZStack(alignment: .leading) {
            Rectangle()
                .fill(Theme.rule)
                .frame(height: 3)
            Rectangle()
                .fill(tile.alarming ? Theme.alarm : Theme.phosphor)
                .frame(width: max(0, min(1, fill)) * 90, height: 3)
        }
        .frame(width: 90, alignment: .leading)
    }
}

/// Which tiles a row earns, and how each figure is spelled.
///
/// Pure and static so the table is testable and cannot drift into the view:
/// the phone's `factLines` and this share one list of facts, differing only
/// in that a phone draws them as lines and the Mac as tiles.
enum AgentFacts {
    struct Tile: Identifiable {
        let id: String
        let label: String
        let value: String
        var note: String = ""
        /// 0…1 where the figure has a natural full. `nil` draws no meter.
        var meter: Double? = nil
        /// Drawn in the alarm colour — never the *only* thing that says so,
        /// the note beside it says it in words.
        var alarming: Bool = false
        /// How many grid columns the tile takes. Two for a figure that is a
        /// name rather than a number (a model id), so it sits on one line
        /// and every box stays the same height as every other.
        var span: Int = 1
        var spoken: String {
            let tail = note.isEmpty ? "" : ", \(note)"
            return "\(label) \(value)\(tail)"
        }
    }

    /// Above this share of the window a context figure reads as an alarm —
    /// the menu bar's usage cluster uses the same threshold.
    static let ctxCrit: Double = 90

    /// A *relative* figure the daemon composed at `asOf`, read at `now`.
    ///
    /// The fleet section is only re-sent when something about it changes, so
    /// `idleSeconds` and `durationSeconds` are as old as the frame that
    /// carried them — on a genuinely idle machine, minutes old. Printed as
    /// they arrive they freeze; aged against the panel's own second hand they
    /// keep counting, which is what a person watching a stopped agent expects.
    ///
    /// `asOf <= 0` means nothing has stamped the fleet yet (a default
    /// `Snapshot`, or a test), so the figure is returned untouched, and a
    /// clock that has somehow gone backwards adds nothing rather than
    /// subtracting.
    static func aged(_ seconds: Double, asOf: Double, now: Double) -> Double {
        guard asOf > 0 else { return seconds }
        return seconds + max(0, now - asOf)
    }

    static func tiles(agent: Agent, category: Category, live: Bool,
                      asOf: Double = 0, now: Double = 0) -> [Tile] {
        var out: [Tile] = []
        if let ctx = agent.metrics.ctxUsedPct {
            out.append(Tile(id: "ctx", label: "CONTEXT",
                            value: "\(Int(ctx.rounded()))%",
                            note: contextNote(trend: agent.trend),
                            meter: ctx / 100,
                            alarming: ctx >= ctxCrit))
        }
        if let cost = agent.metrics.costUsd {
            out.append(Tile(id: "cost", label: "COST",
                            value: money(cost),
                            note: rate(agent.trend.costUsdPerHour)))
        }
        if agent.stats.durationSeconds > 0 {
            out.append(Tile(id: "elapsed", label: "ELAPSED",
                            value: elapsed(aged(agent.stats.durationSeconds,
                                                asOf: asOf, now: now))))
        }
        if category == .waiting {
            out.append(Tile(id: "waiting", label: "WAITING FOR YOU",
                            value: elapsed(aged(agent.idleSeconds,
                                                asOf: asOf, now: now)),
                            alarming: true))
        }
        if let model = agent.stats.model, !model.isEmpty {
            out.append(Tile(id: "model", label: "MODEL", value: model,
                            note: agent.stats.effort,
                            span: model.count > 12 ? 2 : 1))
        }
        if agent.stats.totalToolCalls > 0 {
            out.append(Tile(id: "tools", label: "TOOL CALLS",
                            value: count(agent.stats.totalToolCalls)))
        }
        if agent.stats.filesTouched > 0 {
            out.append(Tile(id: "files", label: "FILES TOUCHED",
                            value: count(agent.stats.filesTouched)))
        }
        if agent.stats.outputTokens > 0 {
            out.append(Tile(id: "output", label: "OUTPUT",
                            value: count(agent.stats.outputTokens) + " tok",
                            note: perMinute(agent.trend.outputTokensPerMin)))
        }
        if let added = agent.metrics.linesAdded, let removed = agent.metrics.linesRemoved {
            out.append(Tile(id: "lines", label: "LINES",
                            value: "+\(added) −\(removed)"))
        }
        if live, agent.subagents > 0 {
            out.append(Tile(id: "helpers", label: "HELPERS",
                            value: "\(agent.subagents)"))
        }
        return out
    }

    /// One line saying what the session is doing right now, for the pane
    /// that cannot show the screen it is doing it on.
    ///
    /// Composed from what the daemon already publishes and nothing else: the
    /// category, the tool the session is inside, its own reported activity,
    /// its helpers, and how long a stopped session has been waiting. It never
    /// guesses — a row with nothing to say gets the plain state.
    static func happening(agent: Agent, category: Category,
                          asOf: Double = 0, now: Double = 0) -> String {
        var parts: [String] = []
        switch category {
        case .waiting:
            let waited = elapsed(aged(agent.idleSeconds, asOf: asOf, now: now))
            parts.append(agent.idleSeconds > 0
                         ? "Stopped, waiting for you for \(waited)"
                         : "Stopped, waiting for you")
        case .running:
            let tool = agent.currentTool.trimmingCharacters(in: .whitespaces)
            let activity = agent.agentActivity.trimmingCharacters(in: .whitespaces)
            if !tool.isEmpty {
                parts.append("Working — running \(tool)")
            } else if !activity.isEmpty {
                parts.append("Working — \(activity)")
            } else {
                parts.append("Working")
            }
        case .sleeping:
            parts.append("Idle in the editor")
        case .finished, .abandoned:
            parts.append(category.title)
        }
        if agent.subagents > 0 {
            parts.append(agent.subagents == 1 ? "1 helper" : "\(agent.subagents) helpers")
        }
        let summary = agent.lastSummary.trimmingCharacters(in: .whitespacesAndNewlines)
        if !summary.isEmpty {
            parts.append(summary)
        }
        return parts.joined(separator: " · ")
    }

    /// `signals.py`'s own sentence, at tile measure: the estimated time to a
    /// full window on a rising line, then the signed rate. Nothing here ever
    /// prints a rate the Mac did not send.
    static func contextNote(trend: Trend) -> String {
        guard let rate = trend.ctxPctPerMin else { return "" }
        var parts: [String] = []
        if rate >= 0.05, let runway = trend.ctxRunwaySeconds {
            parts.append("full in ~\(elapsed(runway))")
        }
        if rate >= 0.05 || rate <= -0.05 {
            parts.append(String(format: "%+.1f%%/min", rate))
        } else {
            parts.append("steady")
        }
        return parts.joined(separator: " · ")
    }

    static func rate(_ perHour: Double?) -> String {
        guard let perHour, perHour > 0 else { return "" }
        return String(format: "$%.2f/h", perHour)
    }

    static func perMinute(_ perMin: Double?) -> String {
        guard let perMin, perMin > 0 else { return "" }
        return "\(count(Int(perMin.rounded())))/min"
    }

    static func money(_ usd: Double) -> String {
        usd < 10 ? String(format: "$%.2f", usd) : String(format: "$%.1f", usd)
    }

    static func count(_ n: Int) -> String {
        if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
        if n >= 1_000 { return String(format: "%.1fk", Double(n) / 1_000) }
        return "\(n)"
    }

    static func elapsed(_ seconds: Double) -> String {
        let total = max(Int(seconds), 0)
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        if hours > 0 { return "\(hours)h \(minutes)m" }
        if minutes > 0 { return "\(minutes)m" }
        return "\(total)s"
    }
}


/// Where the terminal is, when it is not here.
///
/// A session running in a VS Code window has no screen Dark Army can draw — the
/// pane below it is the last message, not the live terminal — so the band
/// says so in one line, says what the session is doing right now in a
/// second, and offers the one route to the real screen. `jump ↗` in the
/// hero is the same verb; this is the one a person reaches for when they
/// have just read "runs in the editor" and want to go there.
///
/// **Absent, never inert**: the button is drawn only where the daemon says
/// the editor can raise this terminal, and where it cannot, the reason is
/// one dim line instead.
struct ElsewhereTerminalBand: View {
    let agent: Agent
    let category: Category
    /// When the fleet section this row came from was composed — the line says
    /// how long a stopped session has been waiting, which is a relative figure
    /// and has to keep counting (see `AgentFacts.aged`).
    var asOf: Double = 0
    /// Gate the 1s clock on panel visibility, `ProcessRow.ticking`'s rule.
    var ticking: Bool = true
    var onJump: () -> Void = {}

    private var whereabouts: TerminalWhereabouts {
        TerminalWhereabouts.of(originBy: agent.originBy, hosted: agent.ownTerminal, tabGone: agent.tabGone)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text("# terminal")
                    .foregroundStyle(Theme.faint)
                Text("·")
                    .foregroundStyle(Theme.faint)
                Text(whereabouts.label)
                    .foregroundStyle(Theme.dim)
                Spacer(minLength: 4)
                if agent.canJump && whereabouts == .editor {
                    Button(action: onJump) {
                        Text("jump to terminal ↗")
                            .font(Theme.mono(10, weight: .medium))
                            .foregroundStyle(Theme.phosphor)
                    }
                    .buttonStyle(.plain)
                    .clickable()
                    .accessibilityLabel("Jump to this session's terminal in the editor")
                }
            }
            .font(Theme.mono(10))
            if ticking {
                TimelineView(Clocks.SecondHand()) { context in
                    happeningLine(now: context.date.timeIntervalSince1970)
                }
            } else {
                happeningLine(now: Date().timeIntervalSince1970)
            }
            if !agent.canJump || whereabouts == .gone || whereabouts == .lost {
                Text(whereabouts.hint)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Theme.bar)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        .accessibilityElement(children: .combine)
    }

    private func happeningLine(now: Double) -> some View {
        Text(AgentFacts.happening(agent: agent, category: category,
                                  asOf: asOf, now: now))
            .font(Theme.mono(12))
            .foregroundStyle(Theme.phosphorBright)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// How many columns of the facts grid a tile takes.
private struct FactSpanKey: LayoutValueKey {
    static let defaultValue = 1
}

extension View {
    /// Ask for `n` columns in the enclosing `FactFlow`. Clamped there to the
    /// number of columns that exist, so a two-column tile on a narrow pane
    /// takes the single column it can have rather than overflowing.
    func factSpan(_ n: Int) -> some View {
        layoutValue(key: FactSpanKey.self, value: n)
    }
}

/// The facts grid's own layout: equal columns, equal rows, wrapping.
///
/// A `LazyVGrid` cannot do either of the two things this grid needs — a tile
/// that spans two columns, and every box the same height whatever it holds —
/// so the arithmetic is here. It divides the width it is offered into as many
/// equal columns of at least `unit` as fit, packs the tiles left to right
/// wrapping when the next one will not fit, and gives every one of them the
/// same height. Nothing measures the window: the width is the proposal the
/// parent already made, which is what a `Layout` is handed.
struct FactFlow: Layout {
    var unit: CGFloat
    var spacing: CGFloat
    var rowHeight: CGFloat

    /// How many equal columns fit in `width`.
    ///
    /// **Every proposal is answered, including the ones that are not
    /// numbers.** SwiftUI probes a `Layout` with infinite and zero widths, and
    /// `Int(_:)` of an infinite or NaN `Double` is a *trap*, not a large
    /// number: on 6 Sep 2026 the first such probe killed the whole panel the
    /// moment a row was selected. A non-finite or empty width is one column.
    static func columnCount(width: CGFloat, unit: CGFloat,
                            spacing: CGFloat) -> Int {
        guard width.isFinite, width > 0, unit + spacing > 0 else { return 1 }
        return max(1, Int((width + spacing) / (unit + spacing)))
    }

    /// The width to answer an unbounded proposal with: every tile in one row.
    /// A `Layout` may never *return* an infinite size, so the ideal width is
    /// stated rather than passed through.
    static func idealWidth(spans: [Int], unit: CGFloat,
                           spacing: CGFloat) -> CGFloat {
        let total = spans.reduce(0) { $0 + max(1, $1) }
        guard total > 0 else { return unit }
        return CGFloat(total) * unit + CGFloat(total - 1) * spacing
    }

    private func columns(_ width: CGFloat) -> Int {
        Self.columnCount(width: width, unit: unit, spacing: spacing)
    }

    /// Where each subview sits, as (column, row, span), plus the row count.
    private func pack(_ subviews: Subviews, columns cols: Int)
        -> (slots: [(col: Int, row: Int, span: Int)], rows: Int) {
        var slots: [(col: Int, row: Int, span: Int)] = []
        var col = 0
        var row = 0
        for view in subviews {
            let span = max(1, min(view[FactSpanKey.self], cols))
            if col + span > cols {
                row += 1
                col = 0
            }
            slots.append((col, row, span))
            col += span
        }
        return (slots, subviews.isEmpty ? 0 : row + 1)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let proposed = proposal.replacingUnspecifiedDimensions(
            by: CGSize(width: unit, height: rowHeight)).width
        // An unbounded proposal asks what this grid *would like*; answering
        // with the infinity it was handed would trap the arithmetic below.
        let width = proposed.isFinite
            ? max(0, proposed)
            : Self.idealWidth(spans: subviews.map { max(1, $0[FactSpanKey.self]) },
                              unit: unit, spacing: spacing)
        let cols = columns(width)
        let rows = pack(subviews, columns: cols).rows
        guard rows > 0 else { return CGSize(width: width, height: 0) }
        let height = CGFloat(rows) * rowHeight + CGFloat(rows - 1) * spacing
        return CGSize(width: width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        let cols = columns(bounds.width)
        let colWidth = (bounds.width - spacing * CGFloat(cols - 1)) / CGFloat(cols)
        let slots = pack(subviews, columns: cols).slots
        for (view, slot) in zip(subviews, slots) {
            let width = colWidth * CGFloat(slot.span)
                + spacing * CGFloat(slot.span - 1)
            let x = bounds.minX + CGFloat(slot.col) * (colWidth + spacing)
            let y = bounds.minY + CGFloat(slot.row) * (rowHeight + spacing)
            view.place(at: CGPoint(x: x, y: y), anchor: .topLeading,
                       proposal: ProposedViewSize(width: width, height: rowHeight))
        }
    }
}
