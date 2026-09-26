import SwiftUI

/// The resolution step between the project a person picked and the projects
/// actually on screen — the phone's answer to the Mac's `fallbackTab`.
///
/// Pure, and kept in this file on purpose: `ios/BobPhone.xcodeproj` has no
/// `PBXFileSystemSynchronizedRootGroup`, so a new `.swift` file would parse
/// under the floor test and never reach the app target.
enum FleetProjects {
    /// Today's `projects` body, lifted: unique, non-empty, sorted.
    static func names(_ projects: [String]) -> [String] {
        Array(Set(projects.filter { !$0.isEmpty })).sorted()
    }

    /// The project to actually show. `""` is `ALL` and always valid; a name
    /// still listed is kept; anything else falls back to `ALL`, so the lists
    /// come back and the highlighted chip always matches what is drawn.
    ///
    /// `heard` is the offline keep, and it is **not** `names.isEmpty`: an
    /// empty `names` is also a real published state — every row carrying an
    /// empty `project`, which the daemon keeps and the Mac draws under
    /// `Other` — and a stale choice must still be forgotten there. What the
    /// keep actually asks is whether the phone has heard anything at all
    /// (`!allRows.isEmpty`); offline, a home/away transport switch and the
    /// first launch before the first snapshot all answer no, and none of them
    /// may erase a choice somebody made.
    static func resolve(selected: String, in names: [String], heard: Bool) -> String {
        if selected.isEmpty { return selected }
        if !heard { return selected }
        return names.contains(selected) ? selected : ""
    }
}

/// The host Mac's battery in words — the one rule the Fleet line and its
/// spoken label share.
///
/// Pure, and kept in this file for `FleetProjects`' reason: a new `.swift`
/// file would never reach the app target. `nil` means draw nothing: an
/// older Mac (`available` false), a desktop (`present` false) or a battery
/// line with no percent.
enum MacPower {
    static func line(available: Bool, present: Bool, percent: Int?,
                     source: String, charge: String) -> String? {
        guard available, present, let percent else { return nil }
        let head = "mac battery \(percent)%"
        guard let word = word(source: source, charge: charge) else { return head }
        return "\(head) · \(word)"
    }

    /// The `charge` word first; where the Mac named none, the source.
    static func word(source: String, charge: String) -> String? {
        switch charge {
        case "charging": return "charging"
        case "discharging": return "on battery"
        case "charged": return "charged"
        case "not_charging": return "on power, not charging"
        case "finishing": return "finishing charge"
        default: break
        }
        switch source {
        case "ac": return "on power"
        case "battery": return "on battery"
        default: return nil
        }
    }
}

/// Fleet, drawn as the Mac's process table.
///
/// One merged table rather than three hardcoded sections. Order is `started_at`
/// descending, undated rows last, session id breaking ties — the panel's
/// `byStart` rule, and the reason "a waiter is never moved to the top" is
/// structural here rather than promised: status is not a term in the sort, so a
/// row that starts waiting goes red in its own slot and stays there.
struct FleetView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    private var snapshot: Snapshot { client.snapshot }
    @SceneStorage("fleet.project") private var project = ""

    private struct Row: Identifiable {
        let agent: Agent
        let category: Category
        var id: String { "\(category.rawValue)/\(agent.sessionId)" }
    }

    var body: some View {
        List {
            if client.refreshing {
                AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                 spoken: "Checking with the Mac")
                    .id("refresh")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            // The host Mac's battery: a label, never a control. Absent on
            // a desktop and on an older Mac.
            if let line = MacPower.line(available: snapshot.power.available,
                                        present: snapshot.power.present,
                                        percent: snapshot.power.percent,
                                        source: snapshot.power.source,
                                        charge: snapshot.power.charge) {
                CommentLine(text: line)
                    .accessibilityLabel(line)
                    .id("power")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            // The historical episode list lives beside the fleet, not above
            // the decisions: Needs you is what blocks you now, and a button
            // over that list was one more thing between you and it.
            DecryptButton(action: { sheets.show(.catchUp()) }) { Text("Catch up across projects") }
            .buttonStyle(.plain)
            // `ALL` must always be pressable while anything else is selected,
            // or a stale choice with no projects listed would leave no route
            // back to every agent.
            if !projects.isEmpty || !activeProject.isEmpty {
                projectStrip
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            PhoneProcessHeader()
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            if liveRows.isEmpty {
                CommentLine(text: "no agents here")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            ForEach(liveRows) { row in
                DecryptButton(action: { sheets.show(.agent(row.agent, row.category)) }) {
                    PhoneProcessRow(agent: row.agent, category: row.category,
                                    cardLine: cardLine(for: row.agent))
                }
                .buttonStyle(.plain)
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            }
            group("FINISHED", rows: finishedRows)
            RecentlySection(client: client)
            group("DEAD", rows: abandonedRows)
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .tint(Theme.phosphor)
        .refreshable { await client.refreshNow() }
        // Forget a project that has gone, rather than carrying it across a
        // relaunch. The offline keep lives inside `resolve`: an empty list
        // says nothing, so losing signal never rewrites the choice.
        .onChange(of: projects) { _, names in
            let next = FleetProjects.resolve(selected: project, in: names,
                                             heard: heardSomething)
            if next != project { project = next }
        }
        // The diary is asked for the moment the tab is on screen, not on the
        // next 30 s tick — this is the tab a person opens to catch up.
        .task { await client.fetchLog(force: true) }
    }

    /// The agent sheet's title line for this row's session.
    private func cardLine(for agent: Agent) -> String {
        AgentDetailView.cardLine(card: snapshot.board.card(forSession: agent.sessionId),
                                 sessionId: agent.sessionId)
    }

    @ViewBuilder
    private func group(_ title: String, rows: [Row]) -> some View {
        if !rows.isEmpty {
            PhoneSectionHeader(title: title)
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            ForEach(rows) { row in
                DecryptButton(action: { sheets.show(.agent(row.agent, row.category)) }) {
                    PhoneProcessRow(agent: row.agent, category: row.category,
                                    cardLine: cardLine(for: row.agent))
                }
                .buttonStyle(.plain)
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            }
        }
    }

    /// The shared strip (`PhoneProjectStrip`, below), fed Fleet's own facts:
    /// live rows per project, and the red dot for a project with a waiter.
    private var projectStrip: some View {
        PhoneProjectStrip(names: projects, active: activeProject,
                          total: allLive.count, counts: liveCounts,
                          needsYou: waitingProjects,
                          noun: (one: "agent", many: "agents")) { project = $0 }
    }

    // MARK: - Rows

    /// Every bucket's rows, **less Mission Control's**: its session lives
    /// in Dark Army's own checkout and would otherwise be listed as work on
    /// that project; its home is the Comm tab (`CommRules.isMissionRow`).
    private var allRows: [Row] {
        let a = snapshot.agents
        let mission = snapshot.mission.sessionId
        let rows = a.waiting.map { Row(agent: $0, category: .waiting) }
            + a.running.map { Row(agent: $0, category: .running) }
            + a.sleeping.map { Row(agent: $0, category: .sleeping) }
            + a.finished.map { Row(agent: $0, category: .finished) }
            + a.abandoned.map { Row(agent: $0, category: .abandoned) }
        return rows.filter {
            !CommRules.isMissionRow(sessionId: $0.agent.sessionId,
                                    originBy: $0.agent.originBy,
                                    missionSessionId: mission)
        }
    }

    /// Live rows before the project filter — the one predicate both the
    /// list and every chip's count read.
    private var allLive: [Row] {
        allRows.filter {
            $0.category == .waiting || $0.category == .running || $0.category == .sleeping
        }
    }

    private var liveRows: [Row] {
        sorted(filtered(allLive))
    }

    /// Live rows per project, from `allLive` — never a second walk over the
    /// buckets. A project in `projects` with no live row reads 0.
    private var liveCounts: [String: Int] {
        allLive.reduce(into: [:]) { $0[$1.agent.project, default: 0] += 1 }
    }

    private var finishedRows: [Row] {
        sorted(filtered(allRows.filter { $0.category == .finished }))
    }

    private var abandonedRows: [Row] {
        sorted(filtered(allRows.filter { $0.category == .abandoned }))
    }

    private func filtered(_ rows: [Row]) -> [Row] {
        activeProject.isEmpty ? rows : rows.filter { $0.agent.project == activeProject }
    }

    /// Newest first, undated to the bottom, session id breaking ties — Swift's
    /// sort promises no stability, so the tiebreak is not optional.
    private func sorted(_ rows: [Row]) -> [Row] {
        rows.sorted { lhs, rhs in
            let l = lhs.agent.startedAt
            let r = rhs.agent.startedAt
            switch (l, r) {
            case let (l?, r?) where l != r: return l > r
            case (nil, _?): return false
            case (_?, nil): return true
            default: return lhs.agent.sessionId < rhs.agent.sessionId
            }
        }
    }

    private var projects: [String] {
        FleetProjects.names(allRows.map(\.agent.project))
    }

    /// What is actually being shown — the stored choice, resolved against the
    /// projects on screen. Resolved on *read* as well as on the write-back
    /// below, because `@SceneStorage` survives a relaunch and the very first
    /// body evaluation happens before any change is delivered.
    private var activeProject: String {
        FleetProjects.resolve(selected: project, in: projects, heard: heardSomething)
    }

    /// The evidence that actually means "the phone has heard from the Mac":
    /// any row at all. `projects` being empty does not answer it — every row
    /// may carry an empty `project`.
    private var heardSomething: Bool { !allRows.isEmpty }

    private var waitingProjects: Set<String> {
        Set(allRows.filter { $0.category == .waiting }
                .map(\.agent.project).filter { !$0.isEmpty })
    }
}

/// The project chip strip, shared by Fleet and Board so the two tabs filter
/// the same way: `ALL` pinned outside the sideways scroll, one chip per name,
/// the highlighted chip always the one resolved by `FleetProjects.resolve`.
/// It owns no selection — each tab stores its own and passes the resolved
/// name in as `active`; a press hands the chosen name back (`""` is `ALL`).
struct PhoneProjectStrip: View {
    let names: [String]
    /// What is actually shown, never the stored name.
    let active: String
    /// The `ALL` chip's number: every item before the filter.
    let total: Int
    let counts: [String: Int]
    /// Projects that get the red "needs you" dot.
    var needsYou: Set<String> = []
    /// What a count counts, for the spoken label ("3 agents", "1 open card").
    let noun: (one: String, many: String)
    let select: (String) -> Void

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    /// `ALL` pinned outside the scroll, so the way back from a selection that
    /// hides everything is never off screen.
    var body: some View {
        HStack(spacing: 8) {
            chip(title: "ALL", name: "", dot: false, count: total)
            Rectangle().fill(Theme.rule).frame(width: 1, height: 18)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(names, id: \.self) { name in
                        chip(title: name, name: name, dot: needsYou.contains(name),
                             count: counts[name, default: 0])
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    private func chip(title: String, name: String, dot: Bool, count: Int) -> some View {
        // Against what is *shown*, not what is stored: a stale stored name
        // would otherwise leave every chip unlit, `ALL` included.
        let on = active == name
        return DecryptButton {
            select(name)
        } label: {
            HStack(spacing: 5) {
                Text(title)
                    .font(Theme.prose(14, weight: on ? .semibold : .regular))
                    // The strip scrolls sideways; a cap here hides nothing.
                    .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 1)
                    .fixedSize(horizontal: false, vertical: true)
                Text("\(count)")
                    .font(Theme.mono(10).monospacedDigit())
                    .foregroundStyle(Theme.faint)
                if dot {
                    // Never colour alone: this dot always coexists with the
                    // row's own `wait` word and the bar's "n need you".
                    Circle().fill(Color.red).frame(width: 6, height: 6)
                        .accessibilityHidden(true)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            // A floor, never a cap: the chip's own text grows past it.
            .frame(minHeight: 32)
            .background(Rectangle().fill(on ? Theme.card : Color.clear))
            .overlay(Rectangle().stroke(on ? Theme.hair : Theme.rule))
            .foregroundStyle(on ? Theme.phosphor : Theme.faint)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // On the `Button`, so `.combine`: `.ignore` would drop the press.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(chipSpoken(title: title, dot: dot, count: count))
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
    }

    /// "dark-army, 3 agents, needs you" — the chip's own three facts, and
    /// nothing walked out of the snapshot a second time.
    private func chipSpoken(title: String, dot: Bool, count: Int) -> String {
        var parts = [title, count == 1 ? "1 \(noun.one)" : "\(count) \(noun.many)"]
        if dot { parts.append("needs you") }
        return parts.joined(separator: ", ")
    }
}
