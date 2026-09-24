import SwiftUI

/// What each project is running, out of how many it may, and what is lined up
/// behind it — the desk's pipeline band and its two dials, on the phone.
///
/// **It states what the Mac sends and works nothing out.** The RUN numerator
/// is `Board.running(in:)` over the daemon's own `run_active`; the denominator
/// is `Board.resolvedLimit`'s rule over the per-card `parallel_limit` with the
/// board scalar as the older-Mac fallback; the queue order is the coalesced
/// `queueRank ?? queuedAt ?? 0` then id. All three are copied from the panel,
/// so the two surfaces cannot disagree about what runs next — a second count
/// of what is running is the failure this rule exists to prevent.
///
/// **Absent, never inert.** Every control here is drawn only where the Mac has
/// said it will honour it: the queue's × and ▲/▼ behind `queueWritable`, the
/// switch and the capacity picker behind `preferencesWritable`, and the picker
/// additionally behind a project having a `root` to aim at, and START PROJECT
/// behind `startProjectWritable` *and* `dispatchEnabled` *and* a root. An
/// older Mac 404s those verbs inside the sealed reply, so the gate is the
/// published flag rather than the attempt.
///
/// **A press is judged by the Mac, against what it holds now.** Nothing here
/// is optimistic about the *queue*: a move or a removal is shown when the next
/// snapshot shows it. The two preferences are the one exception, and only
/// because the 200 genuinely means accepted-not-applied — the Mac stores on
/// its own thread — so the press is held (`settlingPreferences`) until a board
/// reports it, and admits in one line if it never does.
struct PipelineView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var board: Board { client.snapshot.board }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if client.refreshing {
                    AgentChatterView(.line, wait: .refreshing, seed: "refresh",
                                     spoken: "Checking with the Mac")
                        .id("refresh")
                }
                PromptLine(path: "~/pipeline")
                if board.preferencesWritable {
                    autostartSwitch
                }
                let projects = board.pipelineProjects
                if projects.isEmpty {
                    CommentLine(text: "nothing running and nothing waiting")
                } else {
                    ForEach(projects, id: \.self) { name in
                        section(for: name)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .background(Theme.bg)
        .navigationTitle("PIPELINE")
        .decryptSurface("PipelineView")
        .navigationBarTitleDisplayMode(.inline)
        .tint(Theme.phosphor)
        .refreshable { await client.refreshNow() }
    }

    // MARK: - Autostart

    /// The Mac's own words, verbatim from the panel's `⋯` menu. A
    /// phone-specific rewording would be a second string to keep in step for
    /// no gain, and `test_phone_pipeline_controls.py` pins that it is this
    /// one.
    private static let autostartLabel = "Start queued cards automatically"

    /// What the switch shows: the value asked for while a press is settling,
    /// else the Mac's own. The held value is the whole reason the switch does
    /// not visibly flick back for one poll after a 200.
    private var autostartShown: Bool {
        if let want = client.settlingPreferences[PhoneClient.autostartSettlingKey] {
            return want == "on"
        }
        return board.autostartEnabled
    }

    /// The switch's press is in play — queued, on its way, or landed and
    /// waiting for a board that reports it. Read for the label alone: a
    /// press is queued, not awaited, and a second identical press is turned
    /// away by the queue itself.
    private var autostartMark: String? {
        client.queueMark(for: PhoneClient.autostartSettlingKey)
    }

    private var autostartSwitch: some View {
        VStack(alignment: .leading, spacing: 4) {
            DecryptButton {
                setAutostart(!autostartShown)
            } label: {
                autostartFace
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(Self.autostartLabel)
            .accessibilityValue(autostartShown ? "on" : "off")
            .accessibilityAddTraits(autostartShown ? .isSelected : [])
            notice(for: PhoneClient.autostartSettlingKey)
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
    }

    /// The tick and its sentence. Side by side while they fit; stacked past
    /// the point where a whole sentence beside a `[x]` cannot.
    @ViewBuilder
    private var autostartFace: some View {
        let mark = Text(autostartShown ? "[x]" : "[ ]")
            .font(Theme.mono(13))
            .foregroundStyle(Theme.phosphor)
        let words = Text(autostartMark ?? Self.autostartLabel)
            .font(Theme.mono(13))
            .foregroundStyle(Theme.phosphorBright)
            .fixedSize(horizontal: false, vertical: true)
        if dynamicTypeSize.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 6) {
                mark
                words.frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            HStack(spacing: 10) {
                mark
                words.frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func setAutostart(_ on: Bool) {
        let key = PhoneClient.autostartSettlingKey
        let want = on ? "on" : "off"
        Task { @MainActor in
            let result = await client.enqueue(
                action: PhoneActions.setBoardAutostart,
                fields: ["enabled": want], scope: key)
            if result.ok {
                client.beginSettlingPreference(key, want: want)
            } else if !result.detail.isEmpty {
                client.notePipeline(key, result.detail)
            }
        }
    }

    // MARK: - One project

    @ViewBuilder
    private func section(for project: String) -> some View {
        let running = board.running(in: project)
        let refining = board.refining(in: project)
        let queued = board.queued(in: project)
        let finished = board.finishedRun(in: project)
        let ready = board.backlog(in: project)
        // The Backlog is in the input on purpose: a project with work banked
        // and nothing running has no running or queued card to read a `root`
        // off, and that is exactly the project the press exists for. Stated
        // consequence: the capacity picker appears one case earlier too.
        let rows = running + queued + ready
        let limit = Board.resolvedLimit(cards: rows,
                                        fallback: board.parallelLimit)
        let root = Board.pipelineRoot(cards: rows)

        VStack(alignment: .leading, spacing: 6) {
            PhoneSectionHeader(title: project.uppercased())
            if !refining.isEmpty {
                groupHeading("REFINE")
                ForEach(refining) { card in row(card, kind: .refine) }
            }
            if Board.showsRunHeading(running: running.count,
                                     queued: queued.count) {
                groupHeading("RUN \(running.count)/\(max(1, limit))")
            }
            ForEach(running) { card in row(card, kind: .run) }
            if board.preferencesWritable && !root.isEmpty {
                capacityPicker(root: root, limit: limit)
            }
            if !queued.isEmpty {
                groupHeading("QUEUED (\(queued.count))")
                ForEach(Array(queued.enumerated()), id: \.element.id) { index, card in
                    row(card, kind: .queued(index: index, of: queued))
                }
            }
            // Two headings off one list, split by the word each card wears:
            // ENDED is a stopped run still in In progress, DONE is the Done
            // column — the panel's rule, so the two surfaces never disagree
            // about what "done" means.
            let ended = finished.filter { Board.finishedState($0) == "ENDED" }
            let done = finished.filter { Board.finishedState($0) == "DONE" }
            if !ended.isEmpty {
                groupHeading("ENDED (\(ended.count))")
                ForEach(ended) { card in row(card, kind: .finished) }
            }
            if !done.isEmpty {
                groupHeading("DONE (\(done.count))")
                ForEach(done) { card in row(card, kind: .finished) }
            }
            if !ready.isEmpty {
                groupHeading("BACKLOG (\(ready.count))")
                // Absent, never inert: launching switched off, a Mac too old
                // to carry the verb, or no folder to aim at each remove the
                // button rather than dimming it.
                if board.dispatchEnabled && board.startProjectWritable
                    && !root.isEmpty {
                    startProjectButton(root: root)
                }
                ForEach(ready) { card in row(card, kind: .ready) }
            }
        }
    }

    /// One press for the whole project. The reply's `detail` is a **report**
    /// — what started or joined the line, what was left alone and why — and
    /// it is shown whether or not the press succeeded, verbatim: the
    /// sentences are composed once on the Mac (`_start_project_report`), so
    /// the phone and the desk cannot phrase one press two ways.
    ///
    /// **Stays on the synchronous `post`**: this screen reads the reply
    /// body — the 200's `detail` is the report — and a queue cannot carry
    /// that back. The receipt `post` opens still wears the mark
    /// (`SENT` while the press is out), and `post`'s own per-scope lock
    /// turns a double-tap away in `stillSendingRefusal`'s words.
    @ViewBuilder
    private func startProjectButton(root: String) -> some View {
        let mark = client.queueMark(for: root)
        VStack(alignment: .leading, spacing: 4) {
            DecryptButton {
                Task { @MainActor in
                    let result = await client.post(
                        action: PhoneActions.boardStartProject,
                        fields: ["root": root], scope: root)
                    if !result.detail.isEmpty {
                        client.notePipeline(root, result.detail)
                    }
                }
            } label: {
                Text(mark ?? "START PROJECT")
                    .font(Theme.mono(13, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.phosphor)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(mark.map(Receipt.spoken(mark:))
                                ?? "Start every banked card in this project")
            notice(for: root)
        }
    }

    private func groupHeading(_ text: String) -> some View {
        Text(text)
            .font(Theme.mono(11, weight: .semibold))
            .tracking(dynamicTypeSize.isAccessibilitySize ? 0 : 0.8)
            .foregroundStyle(Theme.dim)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 4)
            .accessibilityAddTraits(.isHeader)
    }

    // MARK: - The capacity dial

    /// `Default · 1 · 2 · 3 · 4`, ticked from the override map — which is read
    /// for the tick alone and is **never** the denominator: the number a
    /// project actually runs on arrives per card, already resolved.
    @ViewBuilder
    private func capacityPicker(root: String, limit: Int) -> some View {
        let key = PhoneClient.parallelSettlingKey(root)
        let want = client.settlingPreferences[key]
        let stored = board.parallelOverrides[root]
        let shown = want ?? String(stored ?? 0)
        let mark = client.queueMark(for: key)

        VStack(alignment: .leading, spacing: 4) {
            AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize,
                          spacing: 8) {
                Text("at once")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    // The picker beside it carries this name in its own
                    // label; a caption element would say it twice.
                    .accessibilityHidden(true)
                Menu {
                    DecryptButton("Default (\(max(1, board.parallelLimit)))") {
                        setLimit(root: root, to: 0)
                    }
                    ForEach(1...4, id: \.self) { n in
                        DecryptButton("\(n)") { setLimit(root: root, to: n) }
                    }
                } label: {
                    Text(mark ?? (shown == "0" ? "Default (\(max(1, board.parallelLimit)))"
                                  : shown))
                        .font(Theme.mono(13))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(minWidth: 120, minHeight: 44, alignment: .leading)
                        .padding(.horizontal, 8)
                        .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                }
                .accessibilityLabel("How many run at once in this project")
            }
            notice(for: key)
        }
    }

    private func setLimit(root: String, to n: Int) {
        let key = PhoneClient.parallelSettlingKey(root)
        let want = String(n)
        Task { @MainActor in
            let result = await client.enqueue(
                action: PhoneActions.setBoardParallelRoot,
                // The snapshot's own `root`, verbatim: the Mac keys its
                // stored map by the string it is sent, so a normalised
                // variant would grow a second row meaning this project.
                fields: ["root": root, "limit": want], scope: key)
            if result.ok {
                client.beginSettlingPreference(key, want: want)
            } else if !result.detail.isEmpty {
                client.notePipeline(key, result.detail)
            }
        }
    }

    // MARK: - Rows

    private enum RowKind {
        case refine
        case run
        case queued(index: Int, of: [BoardCard])
        case finished
        /// Banked and not started. No controls: the press above the group is
        /// the whole gesture, and which of these cards will actually start is
        /// the Mac's judgment, reported in the Mac's own words afterwards.
        case ready
    }

    @ViewBuilder
    private func row(_ card: BoardCard, kind: RowKind) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .top, spacing: 8) {
                DecryptButton(action: { sheets.show(rowSheet(card)) }) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        // Which assistant the card names — the mark the
                        // fleet rows wear. Kept in the reader's path with
                        // its name: this row is not one folded sentence.
                        PhoneProviderMark(provider: card.tool, size: 11)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(card.title.isEmpty ? "untitled" : card.title)
                                .font(Theme.mono(13))
                                .foregroundStyle(Theme.phosphorBright)
                                .multilineTextAlignment(.leading)
                            if card.manualCheckDue {
                                Text("MANUAL CHECK NEEDED")
                                    .font(Theme.mono(10))
                                    .tracking(0.8)
                                    .foregroundStyle(Theme.amber)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if case .queued(let index, let all) = kind, board.queueWritable {
                    queueControls(card, index: index, of: all)
                }
            }
            if let mark = client.queueMark(for: card.id) {
                Text(mark)
                    .font(Theme.mono(9, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.faint)
            }
            // The Mac's words about the last queued press on this card,
            // else the queue's own immediate refusal (`notePipeline`).
            notice(for: card.id)
            if case .queued = kind, !card.queueReason.isEmpty {
                // The daemon's own sentence, drawn verbatim: what a queued
                // card waits on is composed once, on the Mac, per frame.
                Text(card.queueReason)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            if let notice = client.boardNotices[card.id], !notice.isEmpty {
                Text(notice)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 2)
    }

    /// A Dark Army-hosted session's live screen is the agent, not the card.
    /// Editor-run work, queued cards and forgotten sessions keep the card.
    private func rowSheet(_ card: BoardCard) -> PhoneSheet {
        if let (agent, category) = client.snapshot.agents.hostedAgent(for: card) {
            return .agent(agent, category)
        }
        return .card(card)
    }

    /// `×` takes the card out of the line; `▲`/`▼` place it before its new
    /// neighbour. Unarmed, the panel's rule: clearing a slot destroys nothing
    /// and the undo is pressing Start again. Nothing here dims on a press in
    /// play — a press is queued, not awaited, and the row wears the mark.
    @ViewBuilder
    private func queueControls(_ card: BoardCard, index: Int,
                               of all: [BoardCard]) -> some View {
        // Every label names its card. Three verbs on each of N queued rows
        // is 3N controls that otherwise read identically, and one of them
        // takes a card out of the line.
        let subject = card.title.isEmpty ? "untitled" : card.title
        HStack(spacing: 4) {
            controlButton("▲", label: "Move \(subject) up",
                          enabled: index > 0) {
                // Before the card currently above it.
                move(card, beforeId: all[index - 1].id)
            }
            controlButton("▼", label: "Move \(subject) down",
                          enabled: index < all.count - 1) {
                // Before the card two places down, or — past the last row —
                // an empty `before_id`, which appends.
                let target = index + 2
                move(card, beforeId: target < all.count ? all[target].id : "")
            }
            controlButton("×", label: "Take \(subject) out of the queue",
                          enabled: true) {
                Task { @MainActor in
                    let result = await client.enqueue(
                        action: PhoneActions.boardUnqueue,
                        fields: ["card_id": card.id], scope: card.id)
                    client.notePipeline(card.id, result.detail)
                }
            }
        }
    }

    /// The queue's immediate answer — a duplicate press, a declined Face
    /// ID sheet — is drawn under the row (`notice(for:)`), never dropped;
    /// a press written down clears the last one.
    private func move(_ card: BoardCard, beforeId: String) {
        Task { @MainActor in
            let result = await client.enqueue(
                action: PhoneActions.boardQueueMove,
                fields: ["card_id": card.id, "before_id": beforeId],
                scope: card.id)
            client.notePipeline(card.id, result.detail)
        }
    }

    /// `label` is not optional on purpose: ▲, ▼ and × are silent symbols, and
    /// a screen reader reading three unnamed buttons in a row is exactly what
    /// this parameter exists to stop.
    private func controlButton(_ glyph: String, label: String, enabled: Bool,
                               action: @escaping () -> Void) -> some View {
        DecryptButton(action: action) {
            Text(glyph)
                .font(Theme.mono(13))
                .foregroundStyle(enabled ? Theme.phosphor : Theme.rule)
                .frame(minWidth: 44, minHeight: 44)
                .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel(label)
    }

    /// The Mac's own refusal about one control: the one the queued press
    /// came back with (`queueNote`) first — the Mac's words outrank the
    /// phone's "accepted, but…" backstop and a stale immediate note — else
    /// the one the screen noted at the press (`notePipeline`).
    @ViewBuilder
    private func notice(for key: String) -> some View {
        if let text = client.queueNote(for: key)?.text ?? client.pipelineNotices[key],
           !text.isEmpty {
            Text(text)
                .font(Theme.mono(11))
                .foregroundStyle(.orange)
                // Drawn is read: a queued press's refusal ages off the
                // QUEUE list as finished business.
                .onAppear { client.readQueueNote(for: key) }
                .onChange(of: text) { _, _ in client.readQueueNote(for: key) }
        }
    }
}
