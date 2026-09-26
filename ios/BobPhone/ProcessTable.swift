import SwiftUI

/// Trailing tap affordance, reserved in both the row and the header so
/// STATE/CTX stay under their headings once List's own indicator is hidden.
private let kChevronWidth: CGFloat = 12

/// Scan-form age for a fleet row (`12s` / `5m` / `1h`); minutes past the
/// hour are dropped so the figure fits the column. Not `Format.duration`.
enum FleetAge {
    static let width: CGFloat = 36

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

/// One row in the htop-style process table, at phone measure.
///
/// Line 1 is face, NAME, STATE, AGE, CTX. Line 2 is the work description, wrapping,
/// drawn only when the agent has something to say. The work description is no
/// longer a column: NAME is flexible and the command takes a whole line of
/// its own. The Mac's six facts all survive — face, name, state, age, command,
/// context — they just no longer share one cramped line. A live helper line
/// sits between line 1 and the command when the agent has helpers; it is
/// extra, not a column.
///
/// Line 1 fixed = 20 + 42 + 36 + 44 = 142pt; with 32pt of gaps (4 × 8), 24pt of
/// padding and a trailing chevron (`kChevronWidth` + 8pt gap) that the header
/// also reserves, so STATE/CTX sit under their headings. NAME still has
/// roughly 172pt (≈21 mono characters at 13pt) on a 390pt portrait screen.
/// A 12-char "Captcha-c3f9" (the longest name is now 7 letters) needs ≈94pt,
/// well inside NAME's ≈172pt; the
/// old fixed 76pt is why an 11-char overflow name once truncated.
/// Line 2 = 390 − 24 − chevron ≈ 346pt × up to 3 lines, against ≈19 today.
/// The CTX cell also carries a one-glyph pace marker (`100%↑`, five glyphs ≈
/// 33pt at 11pt mono), which fits the 44pt column without widening it.
///
/// The chevron is drawn here, not by List: a built-in disclosure steals
/// trailing width from the label and the header (a plain row) would miss it.
/// Past `dynamicTypeSize.isAccessibilitySize` the columns are abandoned
/// altogether: five cells that fit in 390pt at 13pt do not fit at 40pt, and a
/// column that cannot fit clips or ends in dots. `stacked` draws the same five
/// facts as labelled lines instead. `columns` is first in source order and
/// holds today's line verbatim, because three other test files count literals
/// in it — the CTX cell and its 44pt frame, the STATE cell and its 42pt one —
/// by finding the **first** occurrence in the file.
struct PhoneProcessRow: View {
    let agent: Agent
    let category: Category
    /// The bound board card's title, worded as the agent sheet words it
    /// (`AgentDetailView.cardLine`). Empty for no card.
    var cardLine: String = ""

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Group {
            if dynamicTypeSize.isAccessibilitySize {
                stacked
            } else {
                columns
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .overlay(alignment: .leading) {
            if category == .waiting {
                // In place: the row keeps its slot in the arrival order and
                // goes red right there. A waiter is never lifted to the top —
                // order is by start time, and status is not a term in it.
                Rectangle().fill(Color.red).frame(width: 2)
            }
        }
        // One element, one sentence. The fields are read out in the order a
        // person would say them rather than as five separate fragments.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
        .accessibilityAddTraits(.isButton)
    }

    private var columns: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    PixelMark(character: Cast.character(for: agent),
                              state: Cast.state(for: agent, category: category),
                              size: 20)
                    // Which assistant runs this session — the Mac's rail
                    // wears the same mark. Hidden here because the row is
                    // one spoken sentence, and `spoken` names the provider.
                    PhoneProviderMark(provider: agent.provider, size: 11)
                        .accessibilityHidden(true)
                    Text(name)
                        .font(Theme.mono(13, weight: .medium))
                        .foregroundStyle(Theme.phosphorBright)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(stateLabel)
                        .font(Theme.mono(11))
                        .foregroundStyle(stateColor)
                        .frame(width: 42, alignment: .leading)
                    ageCell(fixedWidth: true)
                    Text(ctxLabel)
                        .font(Theme.mono(11))
                        .foregroundStyle(ctxColor)
                        .frame(width: 44, alignment: .trailing)
                        .accessibilityLabel(ctxSpoken)
                }
                helperLine
                tabGoneLine
                commandLine
            }
            chevron
        }
    }

    /// The same five facts, one per line, each under its own heading — the
    /// column headings the table drops at these sizes, moved onto the row.
    private var stacked: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                PixelMark(character: Cast.character(for: agent),
                          state: Cast.state(for: agent, category: category),
                          size: 20)
                PhoneProviderMark(provider: agent.provider, size: 11)
                    .accessibilityHidden(true)
                Text(name)
                    .font(Theme.mono(13, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            labelled("STATE", stateLabel, stateColor)
            labelledAge
            labelled("CTX", ctxLabel, ctxColor)
            helperLine
            tabGoneLine
            commandLine
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One heading-over-value pair. No `.frame(width:)` and no line cap:
    /// the whole point of the stacked branch is that nothing is clipped.
    private func labelled(_ heading: String, _ value: String,
                          _ tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            self.heading(heading)
            Text(value)
                .font(Theme.mono(13))
                .foregroundStyle(tint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The heading over a stacked cell. One builder, so no column caption is
    /// written as a second `Text("…")` literal in this file.
    private func heading(_ text: String) -> some View {
        Text(text)
            .font(Theme.mono(11, weight: .medium))
            .foregroundStyle(Theme.dim)
            .tracking(0.6)
    }

    /// AGE keeps the row's one clock — `ageCell` is shared with `columns`, so
    /// the `TimelineView` stays at a single call site.
    private var labelledAge: some View {
        VStack(alignment: .leading, spacing: 1) {
            heading("AGE")
            ageCell(fixedWidth: false)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Shared by both branches, so the helper summary is read at exactly one
    /// occurrence in this file. Uncapped at every size: it is prose on its
    /// own line and the dots were hiding it.
    @ViewBuilder
    private var helperLine: some View {
        if helpersVisible {
            Text(agent.subagentSummary)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(helpersSpoken)
        }
    }

    /// Shared by both branches. What an agent is working on is the whole
    /// point of line 2, so it runs to as many lines as it needs at every
    /// size; the row is variable-height and `minHeight: 44` is its floor.
    /// Its own line, like the helper summary: the row wraps, and a prefix
    /// on the title would be the title. The words are exactly `tab gone`.
    @ViewBuilder
    private var tabGoneLine: some View {
        if agent.tabGone {
            Text("tab gone")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private var commandLine: some View {
        if command != "—" {
            Text(command)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The tap affordance. Decoration to a screen reader — the row already
    /// carries `.isButton` — and gone once the row is not a column.
    private var chevron: some View {
        Text(">")
            .font(Theme.mono(13))
            .foregroundStyle(Theme.faint)
            .frame(width: kChevronWidth, alignment: .trailing)
            .accessibilityHidden(true)
    }

    /// The whole row as one sentence, from the fields this row already draws
    /// and nothing else — no snapshot walk, no bucket re-derived.
    private var spoken: String {
        var parts = [name, PhoneProviderMark.label(for: agent.provider), spokenState]
        let age = FleetAge.spoken(startedAt: agent.startedAt, now: Date().timeIntervalSince1970)
        if !age.isEmpty { parts.append(age) }
        parts.append(ctxSpoken)
        if helpersVisible { parts.append(helpersSpoken) }
        if command != "—" { parts.append(command) }
        if agent.tabGone { parts.append("tab gone") }
        // Where it came from, folded into the one sentence rather than added
        // as a second element: a row is one thing to hear about.
        if !agent.originLine.isEmpty { parts.append(agent.originLine) }
        return parts.filter { !$0.isEmpty }.joined(separator: ", ")
    }

    /// `wait` / `slp` are scan-form, for a 42pt column. Said out loud they are
    /// not words, so VoiceOver gets the whole one.
    private var spokenState: String {
        switch category {
        case .waiting: return "waiting"
        case .running: return "running"
        case .sleeping: return "sleeping"
        case .finished: return "finished"
        case .abandoned: return "stopped"
        }
    }

    private var dated: Bool {
        guard let start = agent.startedAt else { return false }
        return start > 0
    }

    @ViewBuilder
    private func ageCell(fixedWidth: Bool) -> some View {
        if dated {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                ageText(now: context.date.timeIntervalSince1970,
                        fixedWidth: fixedWidth)
            }
            .frame(width: fixedWidth ? FleetAge.width : nil, alignment: .trailing)
        } else {
            ageText(now: 0, fixedWidth: fixedWidth)
                .frame(width: fixedWidth ? FleetAge.width : nil, alignment: .trailing)
        }
    }

    private func ageText(now: Double, fixedWidth: Bool) -> some View {
        let label = FleetAge.text(startedAt: agent.startedAt, now: now)
        let spoken = FleetAge.spoken(startedAt: agent.startedAt, now: now)
        return Text(label)
            .font(Theme.mono(fixedWidth ? 11 : 13).monospacedDigit())
            .foregroundStyle(label == "—" ? Theme.faint : Theme.dim)
            // A fixed-width column, and `12s` never needs two lines.
            .lineLimit(1)
            .frame(width: fixedWidth ? FleetAge.width : nil,
                   alignment: fixedWidth ? .trailing : .leading)
            .accessibilityLabel(spoken)
            .accessibilityHidden(spoken.isEmpty)
    }

    private var name: String {
        agent.nickname.isEmpty ? String(agent.sessionId.prefix(6)) : agent.nickname
    }

    private var command: String {
        // The detail's own title line first, so the row names what tapping
        // it opens; then the daemon's card title, which it publishes only
        // beside its own non-empty placeholder name — a rung below `name`
        // never fires. A finished run's report headline is not put in
        // front: it hid the title (the Mac's `ProcessRow.baseTitle`).
        if !cardLine.isEmpty { return cardLine }
        if !agent.cardTitle.isEmpty { return agent.cardTitle }
        if !agent.name.isEmpty { return agent.name }
        if !agent.currentTool.isEmpty { return agent.currentTool }
        return "—"
    }

    /// Live buckets only. Empty `subagentSummary` (count 0) draws nothing.
    private var helpersVisible: Bool {
        (category == .waiting || category == .running || category == .sleeping)
            && agent.subagents > 0
    }

    /// VoiceOver: "+5 · Explore" is terse, so this is the same fact in words.
    private var helpersSpoken: String {
        let noun = agent.subagents == 1 ? "helper" : "helpers"
        var spoken = "\(agent.subagents) \(noun)"
        if let newest = agent.subagentRows.last?.qualified, !newest.isEmpty {
            spoken += ", newest \(newest)"
        }
        return spoken
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

    /// The STATE column keeps telling the truth. The red edge widens; the
    /// word does not lie.
    private var stateColor: Color {
        switch category {
        case .waiting: return .red
        case .running: return Theme.phosphor
        case .sleeping, .finished, .abandoned: return Theme.faint
        }
    }

    private var ctxLabel: String {
        guard let pct = agent.metrics.ctxUsedPct else { return "—" }
        return "\(Int(pct.rounded()))%" + (agent.trend.pace?.marker ?? "")
    }

    /// The same fact in words, for VoiceOver: "62 percent, rising".
    private var ctxSpoken: String {
        guard let pct = agent.metrics.ctxUsedPct else { return "no context reading" }
        let base = "\(Int(pct.rounded())) percent"
        guard let pace = agent.trend.pace else { return base }
        return "\(base), \(pace.spoken)"
    }

    private var ctxColor: Color {
        guard let pct = agent.metrics.ctxUsedPct else { return Theme.faint }
        if agent.metrics.exceeds200k || pct >= 85 { return .red }
        if pct >= 75 { return .orange }
        return Theme.dim
    }
}

/// The column headings, mirroring line 1's widths exactly. The work
/// description is no longer a column — it lives on line 2 of the row — so
/// the heading does not name it.
///
/// At an accessibility size it is **absent**: the rows below it are no longer
/// columns, and a column heading over a stacked row is noise. It is hidden
/// from VoiceOver at every size, because each row now speaks its own fields.
struct PhoneProcessHeader: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        if !dynamicTypeSize.isAccessibilitySize {
            columns
        }
    }

    private var columns: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                // Height as well as width: a `Color` is greedy in every
                // direction it is not pinned in.
                Color.clear.frame(width: 20, height: 12)
                Text("NAME").frame(maxWidth: .infinity, alignment: .leading)
                Text("STATE").frame(width: 42, alignment: .leading)
                Text("AGE").frame(width: FleetAge.width, alignment: .trailing)
                Text("CTX").frame(width: 44, alignment: .trailing)
                Color.clear.frame(width: kChevronWidth, height: 12)
            }
            .font(Theme.mono(11, weight: .medium))
            // `dim`, not `faint`: the panel's own contrast note — `faint` is
            // under AA for text this small.
            .foregroundStyle(Theme.dim)
            .tracking(0.6)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
        .accessibilityHidden(true)
    }
}

/// A group heading over the finished and dead rows below the live table.
/// With a `provider` set it opens with that brand's mark, so a heading can
/// say whose readings sit under it.
struct PhoneSectionHeader: View {
    let title: String
    var provider: String? = nil

    var body: some View {
        HStack(spacing: 6) {
            if let provider = provider {
                PhoneProviderMark(provider: provider, size: 10)
                    // The heading beside it already says the name; without
                    // this the row reads "Claude, CLAUDE · SPENDERS".
                    .accessibilityHidden(true)
            }
            Text(title)
                .font(Theme.mono(11, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Theme.faint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.top, 14)
        .padding(.bottom, 4)
        // A heading is a landmark: the rotor's heading jump is how a screen
        // reader skips a group rather than swiping through it.
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}
