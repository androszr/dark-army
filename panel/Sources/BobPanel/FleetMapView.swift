import SwiftUI

// The fleet rail's Map: the selected project's live agents as portraits in the
// middle of a ring of stations, every tool call flying to its station in that
// agent's colour. Everything on it is a fact the daemon already stated (bucket,
// context figure, card ties, stations, calls); the pane only draws. Read-only
// apart from selecting an agent. `docs/fleet-map-contract.md`.

/// The two-word Table | Map control under the project tabs.
struct FleetRailSwitch: View {
    @Binding var selection: FleetRailView

    var body: some View {
        HStack(spacing: 10) {
            ForEach(FleetRailView.allCases, id: \.self) { view in
                Button { selection = view } label: {
                    Text(view == .table ? "Table" : "Map")
                        .font(Theme.mono(10, weight: selection == view ? .medium : .regular))
                        .foregroundStyle(selection == view ? Theme.phosphorBright : Theme.dim)
                        .underline(selection == view)
                }
                .buttonStyle(.plain)
                .clickable()
                .accessibilityAddTraits(selection == view ? .isSelected : [])
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Fleet view")
    }
}

/// The map and the feed beneath it. `agents` are the tab's live rows in
/// `byStart` order; the colour slot is the index in that order.
struct FleetMapPane: View {
    let agents: [SectionRow]
    let activity: FleetActivitySection
    let board: Board
    let visible: Bool
    let reduceMotion: Bool
    let selected: String?
    let onSelect: (SectionRow) -> Void

    /// `seq` -> the wall-clock second this pane first saw it. A call seen
    /// while the pane was not watching is stamped 0, which reads as landed.
    @State private var arrivals: [Int: Double] = [:]
    @State private var watchingSince: Double = 0
    @State private var settle = 0
    @State private var previousCategory: [String: Category] = [:]
    @State private var endedAt: [String: Double] = [:]

    private static let palette: [Color] = [
        Color(red: 0.30, green: 0.80, blue: 0.95),
        Color(red: 0.62, green: 0.50, blue: 0.95),
        Color(red: 0.90, green: 0.40, blue: 0.80),
        Color(red: 0.60, green: 0.90, blue: 0.35),
        Color(red: 0.25, green: 0.85, blue: 0.65),
        Color(red: 0.35, green: 0.55, blue: 1.00),
        Color(red: 1.00, green: 0.60, blue: 0.75),
        Color(red: 0.85, green: 0.88, blue: 0.90),
    ]

    private var side: Double { Double(PanelMetrics.width - 24) }
    private var ids: [String] { agents.map(\.agent.sessionId) }
    private var stations: [FleetStation] { activity.stations }

    private func colour(_ sid: String) -> Color {
        Self.palette[FleetMap.colorIndex(sessionId: sid, in: ids)]
    }

    private var callsBySession: [String: [FleetCall]] {
        activity.sessions.mapValues(\.calls)
    }

    private func now() -> Double { Date().timeIntervalSince1970 }

    private var anyInFlight: Bool {
        let t = now()
        return arrivals.contains { _, seen in
            seen > 0 && FleetMap.pulseProgress(seenAt: seen, now: t) < 1
        } || endedAt.values.contains { t - $0 < 0.8 }
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                if !activity.available || stations.isEmpty {
                    Text("No activity yet")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .frame(maxWidth: .infinity, minHeight: 80)
                } else {
                    mapBody
                }
                FleetFeed(rows: FleetMap.feedRows(sessions: callsBySession,
                                                  names: nameMap, in: ids),
                          labels: Dictionary(stations.map { ($0.id, $0.label) },
                                             uniquingKeysWith: { first, _ in first }))
            }
            .padding(.vertical, 6)
        }
        .onAppear {
            watchingSince = now()
            mint()
            noteCategories()
        }
        .onChange(of: activity) { _, _ in mint(); noteCategories() }
        .onChange(of: agents.map(\.category)) { _, _ in noteCategories() }
        .onChange(of: visible) { _, nowVisible in
            if nowVisible { watchingSince = now() }
        }
    }

    private var nameMap: [String: String] {
        Dictionary(agents.map {
            ($0.agent.sessionId,
             $0.agent.nickname.isEmpty ? String($0.agent.sessionId.prefix(6))
                                       : $0.agent.nickname)
        }, uniquingKeysWith: { first, _ in first })
    }

    /// Stamp unseen calls. The first second after the pane starts watching
    /// stamps 0, so showing a hidden panel never plays catch-up motion.
    private func mint() {
        let t = now()
        let settling = t - watchingSince < 1.0
        var changed = false
        for session in activity.sessions.values {
            for call in session.calls where arrivals[call.seq] == nil {
                arrivals[call.seq] = (settling || !visible) ? 0 : t
                if !settling && visible { changed = true }
            }
        }
        // Every pass, not only past a size: the daemon's seq restarts at 1
        // with the daemon, and a stale stamp would swallow a new call's pulse.
        let live = Set(activity.sessions.values.flatMap { $0.calls.map(\.seq) })
        if arrivals.count != live.count {
            arrivals = arrivals.filter { live.contains($0.key) }
        }
        if changed { scheduleSettle() }
    }

    private func noteCategories() {
        let t = now()
        for row in agents {
            let sid = row.agent.sessionId
            if FleetMap.turnEndedRing(previous: previousCategory[sid],
                                      current: row.category), visible {
                endedAt[sid] = t
                scheduleSettle()
            }
            previousCategory[sid] = row.category
        }
    }

    /// One state change after the flight, so `anyInFlight` is re-read and the
    /// clock pauses; the TimelineView would otherwise keep ticking.
    private func scheduleSettle() {
        DispatchQueue.main.asyncAfter(deadline: .now() + FleetMap.flight + 0.3) {
            settle += 1
        }
    }

    // MARK: - The map

    private var mapBody: some View {
        let centers = FleetMap.portraitCenters(count: agents.count, side: side)
        let size = FleetMap.portraitSize(count: agents.count)
        // Reading `settle` makes its bump re-run this body (so `anyInFlight` is
        // re-read and the clock pauses) without replacing a single view.
        _ = settle
        let paused = !visible || reduceMotion || !anyInFlight
        // Folded once per body, not once per frame: the canvas redraws at
        // 30fps while anything is in flight and none of these move with time.
        let counts = FleetMap.stationCounts(calls: callsBySession, in: ids)
        let failed = FleetMap.failedCounts(sessions: activity.sessions, in: ids)
        let ties = FleetMap.ties(rows: agents.map(\.agent), board: board)
        return ZStack {
            // `AnimationTimelineSchedule` is the type behind `.animation(...)`; spelled
            // out so the reduced-motion sweep sees no bare modifier.
            TimelineView(AnimationTimelineSchedule(minimumInterval: 1.0 / 30.0,
                                                   paused: paused)) { context in
                Canvas { gc, _ in
                    draw(&gc, centers: centers, size: size,
                         counts: counts, failed: failed, ties: ties,
                         t: context.date.timeIntervalSince1970)
                }
            }
            .frame(width: side, height: side)
            .accessibilityHidden(true)
            ForEach(Array(agents.enumerated()), id: \.element.id) { i, row in
                if i < centers.count {
                    portrait(row, size: size)
                        .position(centers[i])
                }
            }
        }
        .frame(width: side, height: side)
    }

    private func stationIndex(_ id: String) -> Int {
        stations.firstIndex { $0.id == id } ?? (stations.count - 1)
    }

    private func stationPoint(_ id: String) -> CGPoint {
        FleetMap.stationPoint(index: stationIndex(id), count: stations.count, side: side)
    }

    private func draw(_ gc: inout GraphicsContext, centers: [CGPoint],
                      size: Double, counts: [String: Int], failed: [String: Int],
                      ties: [[String]], t: Double) {
        let mid = CGPoint(x: side / 2, y: side / 2)

        // Spokes.
        for station in stations {
            var spoke = Path()
            spoke.move(to: mid)
            spoke.addLine(to: stationPoint(station.id))
            gc.stroke(spoke, with: .color(Theme.hair), lineWidth: 1)
        }

        // Ties between sessions bound to one card.
        for group in ties {
            let points = group.compactMap { sid in
                ids.firstIndex(of: sid).flatMap { $0 < centers.count ? centers[$0] : nil }
            }
            guard let first = points.first else { continue }
            var tie = Path()
            tie.move(to: first)
            for p in points.dropFirst() { tie.addLine(to: p) }
            gc.stroke(tie, with: .color(Theme.phosphor.opacity(0.7)),
                      style: StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
        }

        // Paths, pulses, parked waiters.
        for (i, row) in agents.enumerated() where i < centers.count {
            let sid = row.agent.sessionId
            let from = centers[i]
            let ink = colour(sid)
            let activitySession = activity.sessions[sid]
            // Helpers circle their parent as small still dots.
            let helpers = min(8, row.agent.subagentRows.count)
            for h in 0..<helpers {
                let a = 2 * Double.pi * Double(h) / Double(max(1, helpers))
                let r = size / 2 + 9
                gc.fill(Path(ellipseIn: CGRect(x: from.x + r * cos(a) - 2.5,
                                               y: from.y + r * sin(a) - 2.5,
                                               width: 5, height: 5)),
                        with: .color(ink.opacity(0.8)))
            }
            if FleetMap.waitingLine(category: row.category) {
                var park = Path()
                park.move(to: from)
                park.addLine(to: stationPoint("you"))
                gc.stroke(park, with: .color(Theme.amber), lineWidth: 2)
                continue
            }
            for (station, n) in (activitySession?.stationCounts ?? [:]) {
                var path = Path()
                path.move(to: from)
                path.addLine(to: stationPoint(station))
                gc.stroke(path, with: .color(ink.opacity(0.35)),
                          lineWidth: FleetMap.thickness(count: n))
            }
            let calls = FleetMap.pulsingCalls(category: row.category,
                                              calls: activitySession?.calls ?? [])
            for call in calls {
                let to = stationPoint(call.station)
                if reduceMotion {
                    // Still mark: the running call's dot sits at its station.
                    if call.end == .running {
                        let dot = CGRect(x: to.x - 4 + Double(i) * 3, y: to.y + 12,
                                         width: 8, height: 8)
                        gc.fill(Path(ellipseIn: dot), with: .color(ink))
                    }
                    continue
                }
                let p = FleetMap.pulseProgress(seenAt: arrivals[call.seq] ?? 0, now: t)
                guard p < 1 else { continue }
                let at = CGPoint(x: from.x + (to.x - from.x) * p,
                                 y: from.y + (to.y - from.y) * p)
                let tint: Color = call.end == .failed ? Theme.alarm : ink
                gc.fill(Path(ellipseIn: CGRect(x: at.x - 4, y: at.y - 4,
                                               width: 8, height: 8)),
                        with: .color(tint))
            }
            // A turn ending: one expanding ring, never under Reduce Motion.
            if !reduceMotion, let ended = endedAt[sid], t - ended < 0.8 {
                let grow = (t - ended) / 0.8
                let r = size / 2 + 4 + grow * 18
                gc.stroke(Path(ellipseIn: CGRect(x: from.x - r, y: from.y - r,
                                                 width: r * 2, height: r * 2)),
                          with: .color(ink.opacity(1 - grow)), lineWidth: 1.5)
            }
        }

        // Stations: node, label, count, failed count.
        for station in stations {
            let p = stationPoint(station.id)
            let n = counts[station.id] ?? 0
            let node = CGRect(x: p.x - 9, y: p.y - 9, width: 18, height: 18)
            if n > 0 {
                gc.fill(Path(ellipseIn: node), with: .color(Theme.phosphor.opacity(0.25)))
            }
            gc.stroke(Path(ellipseIn: node),
                      with: .color(n > 0 ? Theme.phosphor : Theme.hair), lineWidth: 1)
            gc.draw(Text(station.label).font(Theme.mono(10)).foregroundStyle(Theme.dim),
                    at: CGPoint(x: p.x, y: p.y - 18))
            if n > 0 {
                gc.draw(Text("\(n)").font(Theme.mono(10, weight: .medium))
                            .foregroundStyle(Theme.phosphorBright), at: p)
            }
            if let bad = failed[station.id], bad > 0 {
                gc.draw(Text("✕\(bad)").font(Theme.mono(10, weight: .medium))
                            .foregroundStyle(Theme.alarm),
                        at: CGPoint(x: p.x, y: p.y + 18))
            }
        }
    }

    // MARK: - A portrait

    private func portrait(_ row: SectionRow, size: Double) -> some View {
        let agent = row.agent
        let state = Cast.state(for: agent, category: row.category)
        let pct = max(0, min(100, agent.metrics.ctxUsedPct ?? 0))
        let name = agent.nickname.isEmpty ? String(agent.sessionId.prefix(6)) : agent.nickname
        let ctx = agent.metrics.ctxUsedPct.map { "context \(Int($0.rounded()))%" }
            ?? "context not reported"
        let isWaiting = row.category == .waiting
        return Button { onSelect(row) } label: {
            VStack(spacing: 2) {
                ZStack {
                    if isWaiting {
                        Circle()
                            .stroke(Theme.amber.opacity(0.55), lineWidth: 5)
                            .frame(width: size + 10, height: size + 10)
                    }
                    Circle()
                        .stroke(Theme.hair, lineWidth: 2)
                        .frame(width: size + 4, height: size + 4)
                    if agent.metrics.ctxUsedPct != nil {
                        Circle()
                            .trim(from: 0, to: pct / 100)
                            .stroke(ProcessRow.ctxColor(agent.metrics),
                                    style: StrokeStyle(lineWidth: 2, lineCap: .butt))
                            .rotationEffect(.degrees(-90))
                            .frame(width: size + 4, height: size + 4)
                    }
                    portraitFace(agent, size: size)
                }
                StateRule(state: state, width: size * 0.7)
                Text(name)
                    .font(Theme.mono(9))
                    .foregroundStyle(selected == row.id ? Theme.phosphorBright : Theme.dim)
                    .lineLimit(1)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityLabel("\(name), \(stateWord(row.category))"
                            + (isWaiting ? ", needs you" : ""))
        .accessibilityValue(ctx)
    }

    @ViewBuilder
    private func portraitFace(_ agent: Agent, size: Double) -> some View {
        let slug = Cast.character(for: agent)
        if let image = Cast.portrait(slug) {
            Image(nsImage: image)
                .interpolation(.high)
                .resizable()
                .scaledToFill()
                .frame(width: size, height: size)
                .clipShape(Circle())
                .opacity(AvatarView.portraitDim)
        } else {
            Text(AvatarView.initial(of: slug))
                .font(Theme.mono(size * 0.4, weight: .medium))
                .foregroundStyle(Theme.faint)
                .frame(width: size, height: size)
                .background(Circle().fill(Theme.bg))
        }
    }

    private func stateWord(_ category: Category) -> String {
        switch category {
        case .waiting: return "waiting"
        case .running: return "running"
        case .sleeping: return "sleeping"
        case .finished: return "finished"
        case .abandoned: return "abandoned"
        }
    }
}

/// The few lines under the map: who did what, newest first. Plain text, so a
/// screen reader reads each row as it is.
struct FleetFeed: View {
    let rows: [FleetFeedRow]
    let labels: [String: String]

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                Text(line(row))
                    .font(Theme.mono(10))
                    .foregroundStyle(row.end == .failed ? Theme.alarm
                                     : row.end == .running ? Theme.phosphorBright
                                     : Theme.dim)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
    }

    private func line(_ row: FleetFeedRow) -> String {
        let station = labels[row.station] ?? row.station
        let more = row.extra > 0 ? " +\(row.extra)" : ""
        return "\(row.symbol) \(row.name)  \(station)  \(row.tool)\(more)"
    }
}
