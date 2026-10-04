import SwiftUI

/// The Menu's Worktrees screen: every card's side folder across the watched
/// projects, each with the Mac's own status word and line, grouped by
/// project. A finished card whose hand-check is settled can be ticked
/// (`WorktreeRows.tickable`, the card screen's MERGE rule); ticking several
/// and pressing MERGE, then confirming, lands them on main one after
/// another through the Mac's single-card merge.
///
/// The list is asked for when the screen appears, on pull and when a merge
/// on it moves (`WorktreeRows.fetchKey`) — never on a timer, the poll, the
/// background refresh or the widget. The batch is sent by `post`, never
/// banked: it moves main up to eight times, so it happens at the press or
/// not at all (`board_start_batch`'s rule).
struct WorktreesView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    @StateObject private var arm = Arm()

    @State private var page = WorktreesPage()
    @State private var loaded = false
    @State private var loading = false
    @State private var failed = false
    @State private var ticked: Set<String> = []
    @State private var sending = false
    @State private var report = ""
    @State private var refusal = ""
    /// A read asked for while one was in flight: one more runs when it ends.
    @State private var reloadPending = false
    /// The read in flight, an unstructured task the screen owns: neither
    /// `.task(id:)` restarting nor a pull ending cancels it, so a change of
    /// `fetchKey` mid-read can never lose both reads.
    @State private var readTask: Task<Void, Never>?
    /// The finished group (merged, gone, no card) is folded by default: it
    /// holds nothing to do.
    @State private var showFinished = false

    private var board: Board { client.snapshot.board }

    private var rows: [WorktreeRow] { page.rows }

    private func card(for row: WorktreeRow) -> BoardCard? {
        guard !row.cardId.isEmpty else { return nil }
        return board.cards.first(where: { $0.id == row.cardId })
    }

    private func isTickable(_ row: WorktreeRow) -> Bool {
        let live = card(for: row)
        let facts = live.map {
            WorktreeRows.CardFacts(column: $0.column, branch: $0.worktreeBranch,
                                   mergeState: $0.mergeState,
                                   manualDue: !$0.manualSteps.isEmpty,
                                   mergeOffered: $0.mergeOffered)
        }
        return WorktreeRows.tickable(rowMergeable: row.mergeable,
                                     branchTip: row.branchTip, card: facts,
                                     mergeWritable: board.mergeWritable)
    }

    private var entries: [WorktreeRows.Entry] {
        rows.map { WorktreeRows.Entry(id: $0.id, branchTip: $0.branchTip,
                                      mergeable: $0.mergeable) }
    }

    private var orderedTicked: [String] {
        WorktreeRows.orderedIds(ticked: ticked, listed: rows.map(\.id))
    }

    private var fetchKey: String {
        WorktreeRows.fetchKey(
            cards: board.cards.map { (id: $0.id, column: $0.column, mergeState: $0.mergeState,
                                      mergeLine: $0.mergeLine) },
            listed: rows.map(\.cardId).filter { !$0.isEmpty })
    }

    private var projects: [String] {
        var seen: [String] = []
        for row in rows where !seen.contains(row.project) { seen.append(row.project) }
        return seen
    }

    /// What a person can do with a row, which is how the screen groups it.
    private enum Bucket: CaseIterable {
        case ready, attention, notReady, finished

        var heading: String {
            switch self {
            case .ready: return "READY TO MERGE"
            case .attention: return "NEEDS A FIX"
            case .notReady: return "NOT READY YET"
            case .finished: return "ALREADY MERGED OR GONE"
            }
        }

        var blurb: String {
            switch self {
            case .ready: return "Tick the ones you want on main."
            case .attention: return "A merge stopped. Fix it, then merge again."
            case .notReady: return "Still being worked on, or waiting on something."
            case .finished: return "Nothing to do here."
            }
        }
    }

    private func bucket(_ row: WorktreeRow) -> Bucket {
        if isTickable(row) { return .ready }
        switch row.status {
        case "conflict", "checks_failed", "blocked", "unreadable": return .attention
        case "merged", "gone", "no_card": return .finished
        default: return .notReady
        }
    }

    private func rows(in bucket: Bucket) -> [WorktreeRow] {
        rows.filter { self.bucket($0) == bucket }
    }

    /// The one sentence under the prompt that says what this screen is for
    /// right now.
    private var guide: String {
        let ready = rows(in: .ready).count
        if ready > 0 {
            return "Tick the finished cards you want on main, then press MERGE. "
                + "They land one after another; a conflict stops only that card."
        }
        if !rows(in: .attention).isEmpty {
            return "Nothing is ready to merge. A merge below stopped: "
                + "press Fix to send a helper, then merge it again."
        }
        return "Nothing is ready to merge yet. A card can merge once it is in "
            + "Done, its work is committed and its hand-check is settled."
    }

    /// What to do next with one row, in the person's words.
    private func nextStep(_ row: WorktreeRow, canTick: Bool, isTicked: Bool) -> String {
        if canTick {
            return isTicked ? "→ Ticked. Press MERGE above." : "→ Tap to tick it for the merge."
        }
        switch row.status {
        case "working":
            return "→ Finish it on the board first. It can merge once it is in Done."
        case "waiting":
            return "→ Settle what the line above says, then come back."
        case "merging", "queued":
            return "→ Nothing to do. It is on its way to main."
        case "conflict", "checks_failed", "blocked":
            if let live = card(for: row), showsFix(live) {
                return "→ Press Fix to send a helper. Once fixed, merge it again."
            }
            return "→ Open the card to see what stopped it."
        case "unreadable":
            return "→ Pull down to read again. If it stays, open the card."
        case "done", "nothing":
            return "→ Open the card. Something must be settled first."
        default:
            return ""
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                PromptLine(path: "~/worktrees")
                if loading && rows.isEmpty {
                    AgentChatterView(.line, wait: .refreshing, seed: "worktrees",
                                     spoken: "Checking with the Mac")
                        .id("worktrees")
                } else if failed && rows.isEmpty {
                    failure
                } else if loaded && !page.available {
                    CommentLine(text: page.reason.isEmpty
                                ? "The Mac could not list its worktrees."
                                : page.reason)
                } else if loaded && rows.isEmpty {
                    CommentLine(text: "no worktrees yet")
                }
                if failed && !rows.isEmpty {
                    CommentLine(text: "Could not reach the Mac — this list may be out of date.")
                }
                if loaded && !rows.isEmpty {
                    Text(guide)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if showsMerge { mergeBar }
                pressReply
                ForEach(Bucket.allCases, id: \.self) { bucket in
                    bucketSection(bucket)
                }
                if page.truncated {
                    CommentLine(text: "The list is cut short; some rows are not shown.")
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .tint(Theme.phosphor)
        .decryptSurface("WorktreesView")
        .refreshable { await load() }
        .task(id: fetchKey) { request() }
        .onChange(of: client.snapshot.board.cards) { _, _ in prune() }
        .onDisappear {
            arm.disarm()
            readTask?.cancel()
            readTask = nil
            loading = false
            reloadPending = false
        }
    }

    private var failure: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("The Mac did not answer, so the list is not shown.")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.amber)
                .fixedSize(horizontal: false, vertical: true)
            DecryptButton("Retry") { Task { await load() } }
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                .buttonStyle(.plain)
                .accessibilityLabel("Retry reading the worktrees")
        }
    }

    // MARK: rows

    @ViewBuilder
    private func bucketSection(_ bucket: Bucket) -> some View {
        let members = rows(in: bucket)
        if !members.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("\(bucket.heading) · \(members.count)")
                    .font(Theme.mono(12, weight: .semibold))
                    .foregroundStyle(bucket == .attention ? Theme.amber
                                     : bucket == .finished ? Theme.dim
                                     : Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                Text(bucket.blurb)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                if bucket == .finished && !showFinished {
                    DecryptButton("SHOW \(members.count)") { showFinished = true }
                        .font(Theme.mono(11, weight: .medium))
                        .foregroundStyle(Theme.phosphor)
                        .frame(minWidth: 44, minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                        .accessibilityLabel("Show \(members.count) finished worktrees")
                } else {
                    ForEach(members) { row in
                        rowView(row)
                    }
                }
            }
            .padding(.top, 6)
        }
    }

    private func rowView(_ row: WorktreeRow) -> some View {
        let canTick = isTickable(row)
        let isTicked = ticked.contains(row.id)
        let box = canTick ? (isTicked ? "[x]" : "[ ]") : "[-]"
        let spoken = spokenRow(row, canTick: canTick, isTicked: isTicked)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                DecryptButton(action: { toggle(row, canTick: canTick) }) {
                    HStack(alignment: .top, spacing: 8) {
                        Text(box)
                            .font(Theme.mono(13, weight: .semibold))
                            .foregroundStyle(canTick ? Theme.phosphor : Theme.faint)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(row.title.isEmpty ? row.folder : row.title)
                                .font(Theme.mono(12, weight: .semibold))
                                .foregroundStyle(canTick ? Theme.phosphorBright : Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                            Text(row.word)
                                .font(Theme.mono(12, weight: .medium))
                                .foregroundStyle(statusColour(row))
                                .fixedSize(horizontal: false, vertical: true)
                            if !row.line.isEmpty {
                                Text(row.line)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.dim)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            let step = nextStep(row, canTick: canTick, isTicked: isTicked)
                            if !step.isEmpty {
                                Text(step)
                                    .font(Theme.mono(11, weight: .medium))
                                    .foregroundStyle(canTick ? Theme.phosphor : Theme.dim)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            if !detailLine(row).isEmpty {
                                Text(detailLine(row))
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.faint)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .frame(minHeight: 44, alignment: .topLeading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
                .accessibilityLabel(spoken)
                .accessibilityHint(canTick ? "Ticks this card for the merge" : "")
            }
            HStack(spacing: 14) {
                if let live = card(for: row) {
                    DecryptButton("OPEN CARD") { sheets.show(.card(live)) }
                        .font(Theme.mono(11, weight: .medium))
                        .foregroundStyle(Theme.phosphor)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                        .accessibilityLabel("Open the card \(live.title)")
                    if showsFix(live) { fixButton(live) }
                }
            }
            if let live = card(for: row) { fixNote(live) }
        }
        .padding(.vertical, 4)
    }

    private func detailLine(_ row: WorktreeRow) -> String {
        var parts: [String] = []
        if projects.count > 1 && !row.project.isEmpty { parts.append(row.project) }
        if !row.branch.isEmpty { parts.append(row.branch) }
        if !row.branch.isEmpty && row.ahead >= 0
            && (row.ahead > 0 || row.status == "nothing") {
            parts.append(row.ahead == 1 ? "1 commit" : "\(row.ahead) commits")
        }
        if row.uncommitted > 0 { parts.append("\(row.uncommitted) not committed") }
        if row.branch.isEmpty && !row.folder.isEmpty { parts.append(row.folder) }
        return parts.joined(separator: " · ")
    }

    private func spokenRow(_ row: WorktreeRow, canTick: Bool, isTicked: Bool) -> String {
        var words = (row.title.isEmpty ? row.folder : row.title) + ". " + row.word
        if !row.line.isEmpty { words += ". " + row.line }
        let step = nextStep(row, canTick: canTick, isTicked: isTicked)
        if !step.isEmpty { words += ". " + step.replacingOccurrences(of: "→ ", with: "") }
        let detail = detailLine(row)
        if !detail.isEmpty { words += ". " + detail }
        words += canTick ? (isTicked ? ". Ticked" : ". Not ticked")
                         : ". Cannot be ticked"
        return words
    }

    private func statusColour(_ row: WorktreeRow) -> Color {
        switch row.status {
        case "conflict", "checks_failed", "blocked", "unreadable": return Theme.amber
        case "done", "nothing": return Theme.phosphorBright
        default: return Theme.dim
        }
    }

    private func toggle(_ row: WorktreeRow, canTick: Bool) {
        guard canTick, !sending else { return }
        arm.disarm()
        refusal = ""
        if ticked.contains(row.id) {
            ticked.remove(row.id)
        } else if ticked.count < WorktreeRows.maximum {
            ticked.insert(row.id)
        }
    }

    /// Ticks follow the live snapshot: a card that stopped being mergeable
    /// loses its tick.
    private func prune() {
        let keep = ticked.filter { id in
            rows.first(where: { $0.id == id }).map(isTickable) ?? false
        }
        if keep != ticked {
            ticked = keep
            arm.disarm()
        }
    }

    // MARK: Fix (the card screen's own route)

    private func showsFix(_ live: BoardCard) -> Bool {
        board.mergeWritable
            && CardMerge.fixOffered(mergeState: live.mergeState,
                                    daemonOffers: live.mergeOffered)
    }

    private func fixButton(_ live: BoardCard) -> some View {
        let armed = arm.mergeFix == live.id
        let mark = client.queueMark(for: live.id)
        let words = mark.map { Receipt.spoken(mark: $0) } ?? CardMerge.fixLabel(armed: armed)
        return DecryptButton(mark ?? CardMerge.fixLabel(armed: armed)) { pressFix(live) }
            .font(Theme.mono(11, weight: .semibold))
            .foregroundStyle(armed ? Theme.alarm : Theme.phosphor)
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
            .buttonStyle(.plain)
            .disabled(mark != nil)
            .accessibilityLabel(words)
    }

    @ViewBuilder
    private func fixNote(_ live: BoardCard) -> some View {
        if let note = client.queueNote(for: live.id) {
            Text(note.text)
                .font(Theme.mono(11))
                .foregroundStyle(note.refused ? Theme.amber : Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func pressFix(_ live: BoardCard) {
        if arm.confirm(.mergeFix, id: live.id) {
            Task {
                _ = await client.enqueue(action: PhoneActions.boardMergeFix,
                                         fields: ["card_id": live.id],
                                         scope: live.id)
            }
        } else {
            arm.arm(.mergeFix, id: live.id)
        }
    }

    // MARK: MERGE

    private var showsMerge: Bool {
        board.mergeBatchWritable && board.mergeWritable
            && rows.contains(where: isTickable)
    }

    private var mergeBar: some View {
        let count = ticked.count
        let armed = arm.mergeBatch != nil
        let tooMany = count > WorktreeRows.maximum
        let words = sending ? "MERGING…"
            : (tooMany ? WorktreeRows.tooManyLine
               : (armed ? WorktreeRows.armedVerb(count: count)
                        : count == 0 ? "MERGE · tick a card first"
                        : WorktreeRows.verb(count: count)))
        return VStack(alignment: .leading, spacing: 6) {
            DecryptButton(words) { pressMerge() }
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(armed ? Theme.alarm : Theme.phosphorBright)
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                .buttonStyle(.plain)
                .disabled(count == 0 || tooMany || sending)
                .accessibilityLabel(words)
        }
    }

    /// The Mac's report or refusal, drawn whenever there is one — after a
    /// batch every finished card may have left the tickable set, and the
    /// button with it, but the words stay.
    @ViewBuilder
    private var pressReply: some View {
        if !refusal.isEmpty {
            Text(refusal)
                .font(Theme.mono(11))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
        } else if !report.isEmpty {
            Text(report)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Armed on the first press, sent on the second, keyed on the joined
    /// ordered list — a tick changed between the two disarms.
    private func pressMerge() {
        let ids = orderedTicked
        let key = ids.joined(separator: ",")
        if arm.confirm(.mergeBatch, id: key) {
            Task { @MainActor in await sendMerge(ids) }
        } else {
            refusal = ""
            report = ""
            arm.arm(.mergeBatch, id: key)
        }
    }

    /// Sent synchronously and never banked. The reply's `detail` is the
    /// Mac's report, drawn verbatim: amber with the ticks kept on a refusal,
    /// plain with the ticks cleared on success.
    private func sendMerge(_ ids: [String]) async {
        arm.disarm()
        refusal = ""
        report = ""
        sending = true
        let tips = WorktreeRows.tips(for: ids, entries: entries)
        let result = await client.post(
            action: PhoneActions.boardMergeBatch,
            fields: ["card_ids": ids.joined(separator: ","),
                     "expected_tips": tips.joined(separator: ",")],
            scope: "batch:merge")
        sending = false
        if result.ok {
            report = result.detail
            ticked = []
            await load()
        } else {
            refusal = result.detail
        }
    }

    // MARK: the read

    /// Start a read, or mark one more as wanted when one is in flight. Never
    /// cancels the running read.
    private func request() {
        if readTask != nil {
            reloadPending = true
            return
        }
        loading = true
        readTask = Task { @MainActor in
            let fetched = await client.fetchWorktrees()
            // A cancelled read (the screen went away) is neither a page nor
            // a failure.
            guard !Task.isCancelled else { return }
            readTask = nil
            loading = false
            if let fetched {
                page = fetched
                failed = false
            } else {
                failed = true
            }
            loaded = true
            prune()
            if reloadPending {
                reloadPending = false
                request()
            }
        }
    }

    /// The pull's read: starts or joins the running one and waits for it.
    private func load() async {
        request()
        await readTask?.value
    }
}
