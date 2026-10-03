import SwiftUI

/// Scan-form age for a fleet row (`12s` / `5m` / `1h`); minutes past the
/// hour are dropped so the figure fits the column. Not `Format.duration`.
enum FleetAge {
    static let width: CGFloat = 32

    static func text(startedAt: Double?, now: Double) -> String {
        guard let start = startedAt, start > 0 else { return "—" }
        let s = Int(max(0, now - start))
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m" }
        return "\(s / 3600)h"
    }

    static func spoken(startedAt: Double?, now: Double) -> String {
        guard let start = startedAt, start > 0 else { return "" }
        let s = Int(max(0, now - start))
        if s < 60 { return s == 1 ? "1 second" : "\(s) seconds" }
        if s < 3600 { return s / 60 == 1 ? "1 minute" : "\(s / 60) minutes" }
        return s / 3600 == 1 ? "1 hour" : "\(s / 3600) hours"
    }
}

/// One line in the htop-style process table. Compact on purpose: the
/// message, the reply and the wrap-up live in `StdoutPane`, drawn by
/// `AgentDetailPane` in the board's place for the selected row, never
/// inside the row or under the table.
struct ProcessRow: View {
    let agent: Agent
    let category: Category
    var selected: Bool = false
    /// The row needs a human for a reason its category cannot say — an open
    /// permission prompt or a notification card on a row still categorised
    /// `running`. It widens the red edge below; the STATE column keeps
    /// telling the truth (`run` stays `run` on a prompt-blocked row).
    var attention: Bool = false
    /// Gate the 1s age clock on panel visibility — `TimelineView` does not
    /// stop because the window is ordered out.
    var ticking: Bool = true
    var onSelect: () -> Void = {}
    /// `⌗` — put this session's card in front of the reader on the board,
    /// the same request the detail header's `card ⌗` chip makes. Nil where
    /// the session is bound to no card: the chip is absent, never inert.
    var onShowCard: (() -> Void)? = nil
    /// `↗` — raise the terminal this session runs in (an editor window, or
    /// the hosted pane where Dark Army owns the pty). Nil where there is
    /// none to raise; the caller checks `isJumpable`, the daemon's word.
    var onJump: (() -> Void)? = nil
    /// The bound board card's title, worded as the detail header words it
    /// (`AgentDetailHeader.cardLine`). Empty for no card.
    var cardLine: String = ""

    @State private var cardHover = false
    @State private var jumpHover = false

    static let providerMarkSize: CGFloat = 10
    /// One chip's slot. Both slots are reserved on every row, chip or no
    /// chip, so the CTX column stays a straight edge down the table.
    static let chipSize: CGFloat = 16

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 6) {
                PixelMark(character: Cast.character(for: agent),
                          state: Cast.state(for: agent, category: category),
                          size: 14)
                ProviderMark(provider: agent.provider, size: Self.providerMarkSize)
                    .accessibilityHidden(true)
                Text(name)
                    .font(Theme.mono(11, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .lineLimit(1)
                    .frame(width: 56, alignment: .leading)
                Text(stateLabel)
                    .font(Theme.mono(10))
                    .foregroundStyle(stateColor)
                    .frame(width: 36, alignment: .leading)
                ageCell
                commandCell
                chip("⌗", action: onShowCard, hover: $cardHover,
                     label: "Show this session's card on the board")
                chip("↗", action: onJump, hover: $jumpHover,
                     label: "Jump to terminal")
                Text(ctxLabel)
                    .font(Theme.mono(10))
                    .foregroundStyle(ctxColor)
                    .frame(width: 36, alignment: .trailing)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
            .contentShape(Rectangle())
            .background {
                if selected {
                    Theme.phosphor.opacity(0.12)
                }
            }
            .overlay(alignment: .leading) {
                if selected {
                    Rectangle().fill(Theme.phosphor).frame(width: 2)
                } else if attention || category == .waiting {
                    // The in-place attention mark: the row keeps its slot in
                    // the arrival order and goes red right there.
                    Rectangle().fill(Color.red).frame(width: 2)
                }
            }
        }
        .buttonStyle(.plain)
        .clickable()
        .reportsKeyboardFocus()
        // Spoken content: the bare name and a three-letter state code say
        // almost nothing, and the red attention edge is colour-only. The
        // label carries the name, the provider and the state; the value is
        // the context figure.
        .accessibilityLabel("\(name), \(ProviderMark.displayName(agent.provider)), \(fullState)"
                            + (attention || category == .waiting ? ", needs you" : "")
                            + (agent.areaLine.isEmpty ? "" : ", " + agent.areaLine)
                            + (agent.tabGone ? ", tab gone" : ""))
        .help(agent.areaLine)
        .accessibilityValue(ctxLabel == "—" ? "context not reported"
                                            : "context \(ctxLabel)")
    }

    /// A chip in its slot, or the empty slot. A nested `Button` inside the
    /// row's own: the press lands on the chip and never also selects the
    /// row, so `⌗` reveals the card with the board still in view.
    @ViewBuilder
    private func chip(_ glyph: String, action: (() -> Void)?,
                      hover: Binding<Bool>, label: String) -> some View {
        if let action {
            Button(action: action) {
                Text(glyph)
                    .font(Theme.mono(10, weight: .semibold))
                    .foregroundStyle(hover.wrappedValue ? Theme.phosphorBright : Theme.phosphor)
                    .frame(width: Self.chipSize, height: Self.chipSize)
                    .background(Theme.phosphor.opacity(hover.wrappedValue ? 0.16 : 0.08))
                    .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
                    // A 16pt square under a 26pt row is a miss-prone press:
                    // the target reaches 6pt past the drawn chip, the slot
                    // (and so the CTX edge) stays where it was.
                    .contentShape(Rectangle().inset(by: -6))
            }
            .buttonStyle(.plain)
            .clickable()
            .reportsKeyboardFocus()
            .onHover { hover.wrappedValue = $0 }
            .accessibilityLabel(label)
        } else {
            Color.clear.frame(width: Self.chipSize, height: Self.chipSize)
        }
    }

    private var dated: Bool {
        guard let start = agent.startedAt else { return false }
        return start > 0
    }

    @ViewBuilder
    private var ageCell: some View {
        if dated, ticking {
            TimelineView(Clocks.SecondHand()) { context in
                ageText(now: context.date.timeIntervalSince1970)
            }
            .frame(width: FleetAge.width, alignment: .trailing)
        } else if dated {
            ageText(now: Date().timeIntervalSince1970)
                .frame(width: FleetAge.width, alignment: .trailing)
        } else {
            ageText(now: 0)
                .frame(width: FleetAge.width, alignment: .trailing)
        }
    }

    private func ageText(now: Double) -> some View {
        let label = FleetAge.text(startedAt: agent.startedAt, now: now)
        let spoken = FleetAge.spoken(startedAt: agent.startedAt, now: now)
        return Text(label)
            .font(Theme.mono(10).monospacedDigit())
            .foregroundStyle(label == "—" ? Theme.faint : Theme.dim)
            .lineLimit(1)
            .frame(width: FleetAge.width, alignment: .trailing)
            .accessibilityLabel(spoken)
            .accessibilityHidden(spoken.isEmpty)
    }

    private var name: String {
        agent.nickname.isEmpty ? String(agent.sessionId.prefix(6)) : agent.nickname
    }

    /// The state in full words for the screen reader — `slp` is a column
    /// width decision, not a pronunciation.
    private var fullState: String {
        switch category {
        case .waiting: return "waiting"
        case .running: return "running"
        case .sleeping: return "sleeping"
        case .finished: return "finished"
        case .abandoned: return "abandoned"
        }
    }

    /// The CMD column's words: the bound board card's title first — the
    /// same line the detail header leads with (`AgentDetailHeader.cardLine`),
    /// so the row a person clicks names what opens — then the daemon's
    /// `card_title`, the session's name, its tool. A finished run's report
    /// headline is **not** put in front: it pushed the title past the
    /// column's two lines, so the row read "Changed: …" over a detail
    /// titled something else. The headline stays in the detail's report.
    static func baseTitle(for agent: Agent, cardLine: String = "") -> String {
        // The daemon publishes a card title only beside its own non-empty
        // placeholder name — a rung below `name` never fires.
        if !cardLine.isEmpty { return cardLine }
        if !agent.cardTitle.isEmpty { return agent.cardTitle }
        if !agent.name.isEmpty { return agent.name }
        if !agent.currentTool.isEmpty { return agent.currentTool }
        return "—"
    }

    /// The CMD column as one line: the working helper leads the title,
    /// `tab gone` leads both. `category` is kept for callers; the column no
    /// longer changes with it.
    static func commandText(for agent: Agent, category: Category? = nil,
                            cardLine: String = "") -> String {
        let base = baseTitle(for: agent, cardLine: cardLine)
        // The column drops the tail, so the words have to lead.
        let work: String
        if agent.subagents > 0 {
            let helper = agent.subagentRows.last?.label ?? "\(agent.subagents) helpers"
            work = base == "—" ? helper : "\(helper) · \(base)"
        } else {
            work = base
        }
        if !agent.tabGone { return work }
        return work == "—" ? "tab gone" : "tab gone \(work)"
    }

    /// The CMD column as two lines: the helper (and `tab gone`) small on top,
    /// the title on its own line below with room for two lines of words —
    /// one shared line cut every title to its first few words.
    static func commandLines(for agent: Agent, category: Category? = nil,
                             cardLine: String = "") -> (top: String, title: String) {
        let base = baseTitle(for: agent, cardLine: cardLine)
        var top = ""
        var title = base
        if agent.subagents > 0 {
            let helper = agent.subagentRows.last?.label ?? "\(agent.subagents) helpers"
            if base == "—" { title = helper } else { top = helper }
        }
        if agent.tabGone { top = top.isEmpty ? "tab gone" : "tab gone · \(top)" }
        return (top, title)
    }

    private var commandCell: some View {
        let lines = Self.commandLines(for: agent, category: category, cardLine: cardLine)
        return VStack(alignment: .leading, spacing: 1) {
            if !lines.top.isEmpty {
                Text(lines.top)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(1)
            }
            Text(lines.title)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.phosphor)
                .lineLimit(2)
                .truncationMode(.tail)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(lines.top.isEmpty ? lines.title : "\(lines.top) · \(lines.title)")
    }

    private var stateLabel: String {
        switch category {
        case .waiting: return "wait"
        case .running: return "run"
        case .sleeping: return "slp"
        // The daemon's word, "done" only where a report says so; an older
        // daemon sends none and the column keeps its old word.
        case .finished: return agent.finishWord.isEmpty ? "done" : agent.finishWord
        case .abandoned: return "dead"
        }
    }

    private var stateColor: Color {
        switch category {
        case .waiting: return .red
        case .running: return Theme.phosphor
        case .sleeping, .finished, .abandoned: return Theme.faint
        }
    }

    private var ctxLabel: String {
        guard let pct = agent.metrics.ctxUsedPct else { return "—" }
        return "\(Int(pct.rounded()))%"
    }

    private var ctxColor: Color {
        guard let pct = agent.metrics.ctxUsedPct else { return Theme.faint }
        if agent.metrics.exceeds200k || pct >= 85 { return .red }
        if pct >= 75 { return .orange }
        return Theme.dim
    }
}

struct ProcessTableHeader: View {
    var body: some View {
        HStack(spacing: 6) {
            // Height as well as width: a `Color` is greedy in every direction
            // it is not pinned in, so a width-only frame let this heading grow
            // to the whole rail — the column titles floated in the middle of a
            // screen of empty air with the rows squeezed to the bottom edge.
            // 14pt is the face column the rows below draw a `PixelMark` in.
            Color.clear.frame(width: 14, height: 14)
            Color.clear.frame(width: ProcessRow.providerMarkSize, height: 14)
            Text("NAME").frame(width: 56, alignment: .leading)
            Text("STATE").frame(width: 36, alignment: .leading)
            Text("AGE").frame(width: FleetAge.width, alignment: .trailing)
            Text("CMD").frame(maxWidth: .infinity, alignment: .leading)
            // The two chip slots (`⌗`, `↗`) the rows reserve; unnamed, like
            // the provider mark's.
            Color.clear.frame(width: ProcessRow.chipSize, height: 14)
            Color.clear.frame(width: ProcessRow.chipSize, height: 14)
            Text("CTX").frame(width: 36, alignment: .trailing)
        }
        .font(Theme.mono(10, weight: .medium))
        // `Theme.faint` is 3.88:1 on the panel's background — under AA for
        // text this small, and these are the labels that say what each column
        // holds. `dim` is 8.11:1 and still reads as chrome next to the rows.
        .foregroundStyle(Theme.dim)
        .tracking(0.6)
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
    }
}

/// The selected process's last words and the verbs that apply to it.
/// Reply, Allow/Deny and Wrap up stay the same controls — this is only
/// where they sit: inside `AgentDetailPane`, in the board's place, under a
/// header that carries the `card ⌗` and `jump ↗` chips.
struct StdoutPane: View {
    let agent: Agent
    let category: Category
    let card: Notification?
    let prompt: PermissionPrompt?
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient
    var onReplyFocus: (Bool) -> Void = { _ in }
    /// When true, skip the `# stdout` header and the last-message dump —
    /// the hosted terminal already is that document. The action bars
    /// (permission, questions, reply, wrap-up) still draw.
    var messageHidden: Bool = false

    private var stopped: Bool { category == .waiting || category == .sleeping }

    /// How the offered answers to an `AskUserQuestion` are drawn in the pane.
    /// `buttons` is the one-question pick-one dialog's one-tap answer;
    /// `batch` is pick-then-send behind a single button that stays off until
    /// every question has a choice — the multi-question dialog (one press,
    /// one ordered burst, because the terminal's dialog advances on Enter and
    /// cannot take a half-answer) and any multi-select question (several
    /// boxes cannot be one tap).
    enum OptionShape: Equatable { case none, buttons, batch, captions }

    /// The dim line under unpressable answers. Says why there is no button and
    /// never "at the Mac" — the phone says that because it is not the Mac;
    /// this pane *is*, and the route from here is the detail header's `jump ↗`.
    static let cannotTypeNote =
        "Dark Army cannot type into this terminal — answer it in the terminal itself."

    /// Which answer shape the pane draws, and whether the free-text reply box
    /// survives beside it. Pure, static and table-tested
    /// (`AnswerLayoutTests.swift`) so it cannot quietly drift from the phone's
    /// ladder in `ios/BobPhone/AnswerBox.swift`.
    static func interactionExplanation(note: String, captions: Bool) -> String {
        note.isEmpty ? (captions ? cannotTypeNote : "") : note
    }

    static func answerLayout(hasOptions: Bool, questionCount: Int = 1,
                             multiSelect: Bool = false,
                             canType: Bool, promptUp: Bool,
                             stopped: Bool, channel: Bool)
        -> (options: OptionShape, reply: Bool) {
        // A permission dialog owns the input line: `BobDaemon.answer_question`
        // refuses with "answer it first" (`daemon.py:2290`), which is the same
        // reason `ReplyBar`/`WrapUpBar` are gated on `prompt == nil`.
        if promptUp { return (.none, false) }
        // Shape (a). No free-text field beneath live buttons — it would offer
        // two answers to one question (`AgentRowView.swift:316–320`). The
        // option shapes are deliberately *not* gated on `stopped`: a live
        // dialog is answerable regardless (`AnswerBox.swift:23–25`). Above
        // one question the one-tap buttons become pick-then-send, because
        // the terminal's dialog can only take all the answers at once; so
        // does a multi-select question, because several ticks are not one
        // tap.
        if hasOptions && canType {
            return ((questionCount > 1 || multiSelect) ? .batch : .buttons,
                    false)
        }
        // Shape (b). Captions plus the free-text route wherever the channel can
        // still carry it — then it is the only route (`AnswerBox.swift:45–48`).
        if hasOptions { return (.captions, stopped && channel) }
        // No question: the pane's existing `ReplyBar` rule, unchanged.
        return (.none, stopped && channel)
    }

    private var layout: (options: OptionShape, reply: Bool) {
        Self.answerLayout(hasOptions: !agent.question.options.isEmpty,
                          questionCount: agent.questionList.count,
                          multiSelect: agent.question.multiSelect,
                          canType: agent.canType,
                          promptUp: prompt != nil,
                          stopped: stopped,
                          channel: agent.channel)
    }

    var body: some View {
        let layout = self.layout
        return VStack(alignment: .leading, spacing: 0) {
            if !messageHidden {
                header
                ScrollView {
                    contents(layout)
                }
            } else {
                // Intrinsic height: sits above the terminal and must not
                // steal the GeometryReader's remaining space.
                contents(layout)
            }
        }
        .background(Theme.well)
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
    }

    @ViewBuilder
    private func contents(_ layout: (options: OptionShape, reply: Bool)) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !messageHidden {
                bodyText(layout)
                ForEach(Array(agent.reviewReports.enumerated()), id: \.offset) { _, report in
                    Text(report.label).font(Theme.mono(12, weight: .semibold))
                    CodexReviewOutput(source: report.text, truncated: report.truncated)
                }
                if agent.reviewReportsOmitted > 0 {
                    Text("\(agent.reviewReportsOmitted) older reviews omitted")
                        .font(Theme.mono(10)).foregroundStyle(Theme.faint)
                }
            }
            if layout.options != .none {
                if agent.questionList.count > 1 || agent.question.multiSelect {
                    // Every question with its own heading and
                    // choices; pressable means pick-per-question
                    // behind a single send, captions mean the same
                    // list read-only. A one-question multi-select
                    // takes this route too: `QuestionOptionRow`'s
                    // one tap cannot tick several boxes. Identity
                    // keyed on the dialog so a new dialog never
                    // inherits stale picks.
                    MultiQuestionAnswer(
                        agent: agent, questions: agent.questionList,
                        pressable: layout.options == .batch,
                        actions: actions, client: client)
                        .id("\(agent.sessionId):\(agent.question.id)")
                } else {
                    // `QuestionOptionRow` branches button/plain on
                    // `agent.canType` itself, so the pane adds no
                    // second branch — only the why-line the captions
                    // need.
                    ForEach(Array(agent.question.options.enumerated()),
                            id: \.offset) { index, label in
                        QuestionOptionRow(agent: agent, index: index,
                                          label: label,
                                          detail: agent.question.detail(at: index),
                                          actions: actions,
                                          client: client)
                    }
                }
            }
            let explanation = Self.interactionExplanation(
                note: agent.interactionNote, captions: layout.options == .captions)
            if !explanation.isEmpty {
                Text(explanation)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !agent.askNote.isEmpty && prompt == nil {
                Text(agent.askNote)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let prompt {
                PermissionBar(agent: agent, prompt: prompt,
                              actions: actions, client: client)
            }
            if layout.reply {
                ReplyBar(agent: agent, actions: actions, client: client,
                         onFocusChange: onReplyFocus)
            }
            // Stop and Delete are drawn, not only secret letters (S, R):
            // the same `RowActions` gate the keys arm, so the arm shows.
            // Stop sits in the Details pane; on the Terminal tab only
            // while armed, so a live screen does not wear a standing strip.
            if agent.canStop, !messageHidden || actions.armedStop == agent.id {
                StopBar(agent: agent, actions: actions, client: client)
            }
            if category == .abandoned {
                DeleteBar(agent: agent, actions: actions, client: client)
            }
            if agent.rebuildOffered, client.context.canRebuild {
                RebuildBar(agent: agent, client: client)
            }
            if prompt == nil, stopped, agent.canLowPriority {
                LowPriorityBar(agent: agent, actions: actions, client: client)
            }
            if prompt == nil, stopped, agent.canClose {
                WrapUpBar(agent: agent, actions: actions, client: client)
            }
            if agent.canHide {
                Button { actions.dismiss(agent, client: client) } label: {
                    Text(Verbs.hide.label)
                        .contentShape(Rectangle().inset(by: -6))
                }
                    .buttonStyle(.plain)
                    .clickable()
                    .reportsKeyboardFocus()
                    .font(Theme.mono(11))
                    .accessibilityHint("Hide this row until the session changes")
            }
            // Dark Army's own refusal sentence, verbatim. This slot is
            // written by `answerQuestion`, `reply` and
            // `answerPermission` alike, so surfacing it here also makes
            // reply and permission refusals visible from the pane for
            // the first time — deliberate: they were failing silently.
            if !actions.refusal(for: agent).isEmpty {
                Label(actions.refusal(for: agent),
                      systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
                    .labelStyle(.titleAndIcon)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("# stdout")
                .foregroundStyle(Theme.faint)
            Text("·")
                .foregroundStyle(Theme.faint)
            Text(agent.nickname.isEmpty ? String(agent.sessionId.prefix(8))
                                        : agent.nickname)
                .foregroundStyle(Theme.phosphorBright)
            Text("·")
                .foregroundStyle(Theme.faint)
            Text(category.rawValue)
                .foregroundStyle(category == .waiting ? Color.red : Theme.dim)
            Spacer(minLength: 4)
        }
        .font(Theme.mono(10))
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    /// What the agent last said, drawn as the document it is.
    ///
    /// Every one of these three strings is prose an agent wrote: a report ends
    /// in headings, bold, fenced code and lists, and as one monospaced block
    /// `**Done**` reads as punctuation and a fence is indistinguishable from
    /// the paragraph around it. `MarkdownText` is the renderer the board's
    /// document pane already uses — in `mono`, because this pane is a terminal
    /// readout and the block structure is what is being recovered here, not
    /// the typeface. Links come back as their label (`Markdown.inline`), which
    /// matters more here than there: this text arrives from the fleet.
    @ViewBuilder
    private func bodyText(_ layout: (options: OptionShape, reply: Bool))
        -> some View {
        if let card, !card.isGenericWait {
            markdown(card.message, color: .orange)
        } else if agent.questionList.count > 1 {
            // Every question's text, not just the first. Drawn here only
            // while a permission prompt suppresses the per-question section
            // below — everywhere else that section carries each heading and
            // text beside its own choices, and a second copy above it would
            // say everything twice.
            if layout.options == .none {
                markdown(agent.questionList.map(\.text)
                            .joined(separator: "\n\n"),
                         color: Theme.phosphorBright)
            }
        } else if !agent.question.text.isEmpty {
            markdown(agent.question.text, color: Theme.phosphorBright)
        } else if !agent.lastText.isEmpty || !agent.lastSummary.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                let latest = Self.latestToShow(
                    latest: agent.lastSummary.isEmpty ? agent.lastText : agent.lastSummary,
                    labelled: agent.workReport?.labelled == true)
                if !latest.isEmpty {
                    markdown(latest, color: Theme.dim)
                }
                workReport
            }
        } else if !Self.reportToShow(latest: "", report: agent.lastReport).isEmpty {
            // Nothing was said after the report, so the report is the message.
            workReport
        } else {
            Text("— no output —")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
        }
    }

    /// What a finished piece of work actually was, under the words that came
    /// after it. `lastText` is the *latest* message and nothing else, so a
    /// one-line follow-up — "that was the background job, nothing new" — was
    /// all this pane had of a session that had just reported a day's work, and
    /// there is no route from here back to the transcript.
    ///
    /// A report the daemon could split into its labelled parts is drawn as
    /// those parts (`WorkReportBlock`); the message line has already given
    /// up its copy of the report (`latestToShow`), so it is drawn whether or
    /// not the latest message was the report. Anything else keeps the raw
    /// block, under `reportToShow`'s two rules.
    @ViewBuilder
    private var workReport: some View {
        let report = Self.reportToShow(
            latest: agent.lastSummary.isEmpty ? agent.lastText : agent.lastSummary,
            report: agent.lastReport)
        if let parsed = agent.workReport, parsed.labelled,
           !agent.lastReport.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            WorkReportBlock(parsed: parsed)
        } else if !report.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Rectangle().fill(Theme.hair).frame(height: 1)
                Text("# work done")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                markdown(report, color: Theme.dim)
            }
        }
    }

    /// The report to draw beneath the latest words, or "" for none. Pure and
    /// static so the two rules are testable: an absent report draws nothing,
    /// and a report the latest message *is* is never drawn twice.
    static func reportToShow(latest: String, report: String) -> String {
        let trimmed = report.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return "" }
        if latest.contains(reportHeading) { return "" }
        return trimmed
    }

    /// The heading `WORK_REPORT_HINT` dictates, and the daemon slices on.
    static let reportHeading = WorkReport.reportHeading

    /// The latest words as the message line draws them: unchanged, unless
    /// the report is drawn as labelled sections — then a message that *is*
    /// the report keeps only the prose before its heading, so the report is
    /// never on screen twice.
    static func latestToShow(latest: String, labelled: Bool) -> String {
        labelled ? WorkReport.prose(before: latest) : latest
    }

    private func markdown(_ source: String, color: Color) -> some View {
        CodexReviewOutput(source: source)
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// A multi-question `AskUserQuestion`, every question with its own heading and
/// choices, answered in one press.
///
/// The terminal's dialog shows one question at a time and advances on Enter —
/// digits address the question *currently shown* — so a per-question "answer
/// just this one" cannot exist: the only meaningful act is all the answers, in
/// order, in one burst (`BobDaemon.answer_questions`). Hence pick-then-send
/// where the single-question pane is one-tap: each option is a selection
/// toggle, and the one send button stays off until every question has a
/// choice. Where the Mac cannot type (`pressable` false) the same list draws
/// read-only, the pane's `cannotTypeNote` beneath it saying why.
///
/// The send is a two-press: ANSWER ALL swaps the list for the picks read back
/// under "SEND THESE N ANSWERS?", and YES, SEND is the act. The dialog in the
/// terminal ends on its own summary screen for the same reason, and the
/// daemon's burst ends with the Enter that accepts it.
///
/// Picks are `@State` keyed by question position; the pane gives this view an
/// identity of session + tool_use id, so a new dialog starts clean rather than
/// inheriting the last one's picks.
struct MultiQuestionAnswer: View {
    let agent: Agent
    let questions: [AgentQuestion]
    let pressable: Bool
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient

    /// The picks per question position. A set on every question: a
    /// pick-one question holds at most one (a press replaces), a
    /// `multiSelect` question holds every ticked box (a press toggles) —
    /// one shape, because a parallel field would be two truths the send has
    /// to reconcile.
    @State private var choices: [Int: Set<Int>] = [:]
    /// The caption on a multi-select question's heading: the boxes are the
    /// same shape as the radio marks at a glance, and the words say what
    /// changed.
    static let multiSelectHint = "PICK ANY THAT APPLY"
    /// Set by the first press of ANSWER ALL: the picks are read back and the
    /// send waits to be confirmed. The terminal's own dialog ends on exactly
    /// such a summary screen — which is why the burst the daemon types ends
    /// with one more Enter — so this is the same beat rather than a second
    /// one, and it is the only send here that commits several answers at
    /// once. Cleared by CHANGE; a new dialog gives this view a new identity
    /// and so a fresh `false`.
    @State private var confirming = false

    private var complete: Bool {
        questions.indices.allSatisfy { !(choices[$0] ?? []).isEmpty }
    }

    /// "ANSWER" on a one-question (multi-select) dialog, "ANSWER ALL N" on
    /// several — the word "all" over one question reads as a mistake.
    private var answerTitle: String {
        questions.count == 1 ? "ANSWER" : "ANSWER ALL \(questions.count)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if confirming {
                confirmSummary
            } else {
                ForEach(Array(questions.enumerated()), id: \.offset) { qi, q in
                    VStack(alignment: .leading, spacing: 3) {
                        heading(qi, q)
                        MarkdownText(source: q.text, base: 11, mono: true)
                            .equatable()
                            .foregroundStyle(Theme.phosphorBright)
                            .fixedSize(horizontal: false, vertical: true)
                        ForEach(Array(q.options.enumerated()),
                                id: \.offset) { oi, label in
                            optionRow(question: qi, index: oi, label: label)
                                .padding(.leading, 6)
                        }
                    }
                }
                if pressable {
                    Button(answerTitle) {
                        confirming = true
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .clickable()
                    .disabled(!complete || actions.isSending(agent))
                    .help(complete
                          ? (questions.count == 1
                             ? "Read the answer back before sending"
                             : "Read back all \(questions.count) answers before sending")
                          : "Pick an answer for every question first")
                }
            }
        }
    }

    /// The picks read back, and the press that actually sends them.
    private var confirmSummary: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(questions.count == 1 ? "SEND THIS ANSWER?"
                                      : "SEND THESE \(questions.count) ANSWERS?")
                .font(Theme.mono(9))
                .foregroundStyle(Theme.faint)
                .tracking(0.6)
            ForEach(Array(questions.enumerated()), id: \.offset) { qi, q in
                VStack(alignment: .leading, spacing: 2) {
                    heading(qi, q)
                    Text(chosenLabel(qi, q))
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.phosphorBright)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack(spacing: 8) {
                Button("YES, SEND") {
                    // Ordered by question position, each group ascending —
                    // the daemon types them as one burst and validates the
                    // count, every index and every group's shape against
                    // the dialog it holds before the first keystroke.
                    let ordered: [[Int]] = (0..<questions.count).map {
                        (choices[$0] ?? []).sorted()
                    }
                    guard ordered.allSatisfy({ !$0.isEmpty }) else { return }
                    actions.answerQuestions(agent, choices: ordered,
                                            client: client)
                    confirming = false
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .clickable()
                .disabled(!complete || actions.isSending(agent))
                Button("CHANGE") { confirming = false }
                    .buttonStyle(.plain)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.dim)
                    .clickable()
            }
        }
    }

    private func heading(_ qi: Int, _ q: AgentQuestion) -> some View {
        let base = q.header.isEmpty ? "QUESTION \(qi + 1)"
                                    : "\(qi + 1) · \(q.header)"
        return Text(q.multiSelect ? "\(base) · \(Self.multiSelectHint)" : base)
            .font(Theme.mono(9))
            .foregroundStyle(Theme.faint)
            .tracking(0.6)
    }

    /// Every label the picks name, ascending and comma-joined, or a dash —
    /// unreachable while the send is gated on a complete set of picks, and
    /// saying so beats an index crash if that ever stops being true.
    private func chosenLabel(_ qi: Int, _ q: AgentQuestion) -> String {
        let picked = (choices[qi] ?? []).sorted()
            .filter { q.options.indices.contains($0) }
        guard !picked.isEmpty else { return "—" }
        return picked.map { "\($0 + 1). \(q.options[$0])" }
            .joined(separator: ", ")
    }

    @ViewBuilder
    private func optionRow(question: Int, index: Int, label: String)
        -> some View {
        let q = questions[question]
        let picked = choices[question]?.contains(index) == true
        let detail = q.detail(at: index)
        if pressable {
            Button {
                if q.multiSelect {
                    // A tick box: press again to untick.
                    var set = choices[question] ?? []
                    set.formSymmetricDifference([index])
                    choices[question] = set
                } else {
                    choices[question] = [index]
                }
            } label: {
                optionContent(index: index, label: label, detail: detail,
                              picked: picked, box: q.multiSelect)
            }
            .buttonStyle(.plain)
            .clickable()
        } else {
            optionContent(index: index, label: label, detail: detail,
                          picked: false, box: q.multiSelect)
        }
    }

    private func optionContent(index: Int, label: String, detail: String,
                               picked: Bool, box: Bool) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text(picked ? (box ? "☑" : "◉")
                            : (pressable ? (box ? "☐" : "○") : " "))
                    .font(Theme.mono(10))
                    .foregroundStyle(picked ? Theme.phosphorBright : Theme.faint)
                Text("\(index + 1).")
                    .font(.system(size: 10.5).monospacedDigit())
                    .foregroundStyle(picked ? Theme.phosphorBright : Theme.dim)
                Text(label)
                    .font(.system(size: 10.5))
                    .foregroundStyle(picked ? Theme.phosphorBright : Theme.dim)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !detail.isEmpty {
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.faint)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 28)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
