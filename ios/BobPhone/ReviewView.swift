import SwiftUI

/// The Menu's Review screen: pick a project, read in one line what will be
/// reviewed, pick the provider (Claude is pre-selected) and the after-steps,
/// press Start — and below it the runs, a row pushing the run's own screen.
///
/// Source of truth: `snapshot.review` for the runs, the Mac's offer for the
/// scope sentence and the steps, `snapshot.board` for the providers. Nothing
/// is re-derived; the words are `ReviewRules`', byte-equal with the Mac's.
/// Start rides `enqueue` (queued, not awaited), and the pressed control wears
/// the queue's mark until the run row appears.
struct ReviewView: View {
    @ObservedObject var client: PhoneClient
    /// Watched for its `signal`: a Lock Screen link can land while this
    /// screen is already in front, with no fresh `onAppear` to take the run.
    @ObservedObject private var router = PhoneRouter.shared

    @State private var root = ""
    @State private var tool = ReviewRules.defaultProvider
    /// The ticked after-steps. Every step starts unticked: the tick is the
    /// permission, and a default must never tick one.
    @State private var ticked: Set<String> = []
    @State private var offer: ReviewOffer?
    @State private var checking = false
    @State private var note = ""
    @State private var openRun: String?
    /// A run a link named, waiting for the first review data to decide on.
    @State private var heldRun = ""
    /// When the run was taken: "not listed" is only decided from a picture
    /// that arrived after this, never from the one already on screen.
    @State private var heldAt = Date()

    private var review: ReviewSection { client.snapshot.review }
    private var board: Board { client.snapshot.board }
    private var startScope: String { "review:start:\(root)" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                PromptLine(path: "~/review")
                if !board.reviewSupported || !review.available {
                    Text(ReviewRules.olderMacNote)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    form
                    runs
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .decryptSurface("ReviewView")
        .navigationDestination(item: $openRun) { id in
            ReviewRunView(client: client, runId: id)
                .navigationTitle("review run")
        }
        .onAppear { openHeldRun() }
        // A held run is pushed as soon as `snapshot.review` lists it; it is
        // dropped (the section stays as it is) only once a picture newer than
        // the hold has arrived and still does not list it. Until then the id
        // waits here, memory only, and is dropped when the screen goes.
        .onChange(of: router.signal) { _, _ in openHeldRun() }
        .onChange(of: review) { _, _ in settleHeldRun() }
        .onChange(of: client.pictureAsOf) { _, _ in settleHeldRun() }
        .onDisappear { heldRun = "" }
        .onChange(of: root) { _, _ in refreshOffer() }
    }

    private func openHeldRun() {
        let taken = PhoneRouter.shared.takeReviewRun()
        if !taken.isEmpty { heldRun = taken; heldAt = Date() }
        settleHeldRun()
    }

    private func settleHeldRun() {
        guard !heldRun.isEmpty else { return }
        switch HeldReviewRun.decide(listed: review.run(id: heldRun) != nil,
                                    pictureAsOf: client.pictureAsOf, heldAt: heldAt) {
        case .open: openRun = heldRun; heldRun = ""
        case .drop: heldRun = ""
        case .wait: break
        }
    }

    // MARK: the form

    private var form: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Project")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            Picker("Project", selection: $root) {
                if root.isEmpty { Text("Choose a project…").tag("") }
                ForEach(client.snapshot.enrollment.enrolled) { project in
                    Text(project.label.isEmpty ? project.root : project.label)
                        .tag(project.root)
                }
            }
            .pickerStyle(.menu)
            .font(Theme.mono(13))
            scopeLine
            Text("Assistant")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            PhoneProviderSwitch(tools: board.tools, installed: board.installed,
                                selected: tool, pick: { tool = $0 })
            stepsList
            startButton
            if !note.isEmpty {
                Text(note)
                    .font(Theme.mono(12))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("Dark Army says: \(note)")
            }
        }
    }

    @ViewBuilder private var scopeLine: some View {
        if root.isEmpty {
            EmptyView()
        } else if checking {
            Text("checking…")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.faint)
        } else if let offer {
            Text(offer.available ? offer.scopeLine
                 : (offer.reason.isEmpty ? "Dark Army could not read this project." : offer.reason))
                .font(Theme.mono(12))
                .foregroundStyle(offer.available ? Theme.dim : .orange)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private var stepsList: some View {
        if let offer, offer.available, !offer.steps.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("After the review")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                ForEach(offer.steps) { step in
                    let on = ticked.contains(step.id)
                    DecryptButton(action: {
                        if on { ticked.remove(step.id) } else { ticked.insert(step.id) }
                    }) {
                        HStack(spacing: 8) {
                            Image(systemName: on ? "checkmark.square" : "square")
                                .accessibilityHidden(true)
                            Text(step.label)
                                .font(Theme.mono(13))
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                        .foregroundStyle(on ? Theme.phosphorBright : Theme.dim)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Step: \(step.label)")
                    .accessibilityValue(on ? "ticked" : "not ticked")
                    .accessibilityAddTraits(.isButton)
                }
            }
        }
    }

    private var startButton: some View {
        let canStart = ReviewRules.canStart(
            root: root, tool: tool, supported: board.reviewSupported,
            dispatchEnabled: board.dispatchEnabled) && board.reviewSectionWritable
        return VStack(alignment: .leading, spacing: 4) {
            DecryptButton(action: start) {
                Text(client.queueMark(for: startScope) ?? "Start")
                    .font(Theme.mono(13))
                    .frame(minHeight: 44)
            }
            .disabled(!canStart)
            .accessibilityLabel("Start the review")
            if !board.dispatchEnabled {
                Text("Dark Army is not allowed to start sessions (see the ⋯ menu)")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let queued = client.queueNote(for: startScope)?.text, !queued.isEmpty {
                Text(queued)
                    .font(Theme.mono(11))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    // Drawn is read: a refused Start closes as refused.
                    .onAppear { client.readQueueNote(for: startScope) }
                    .onChange(of: queued) { _, _ in client.readQueueNote(for: startScope) }
            }
        }
    }

    // MARK: the runs

    private var runs: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !review.runs.isEmpty {
                Text("Runs")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            ForEach(review.runs.sorted { $0.startedAt > $1.startedAt }) { run in
                let name = run.project.isEmpty ? run.root : run.project
                DecryptButton(action: { openRun = run.id }) {
                    HStack(spacing: 8) {
                        Text(name)
                            .font(Theme.mono(13, weight: .medium))
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(ReviewRules.stateWord(run.state))
                            .font(Theme.mono(11))
                            .foregroundStyle(run.state == "picks" ? .orange : Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(name), \(ReviewRules.stateWord(run.state))")
                .accessibilityAddTraits(.isButton)
            }
        }
    }

    // MARK: the reads and the press

    /// The offer for the chosen project; a stale answer (the choice moved
    /// while it was in flight) is dropped.
    private func refreshOffer() {
        let wanted = root
        ticked = []
        offer = nil
        guard !wanted.isEmpty else { checking = false; return }
        checking = true
        Task { @MainActor in
            let answer = await client.reviewOffer(root: wanted)
            guard root == wanted else { return }
            checking = false
            offer = answer ?? ReviewOffer()
        }
    }

    private func start() {
        let steps = (offer?.steps ?? []).map(\.id).filter { ticked.contains($0) }
        note = ""
        Task { @MainActor in
            let result = await client.enqueue(
                action: PhoneActions.reviewStart,
                fields: ["root": root, "tool": tool,
                         "steps": steps.joined(separator: ",")],
                scope: startScope)
            if !result.ok { note = result.detail }
        }
    }
}

/// What to do with a run a link named, given the picture on screen. Pure.
enum HeldReviewRun {
    enum Decision: Equatable { case open, drop, wait }

    /// Listed: open now. Not listed: drop only when the picture is newer than
    /// the hold (a review started while the phone was locked is not on the
    /// picture the phone woke with); otherwise wait for one.
    static func decide(listed: Bool, pictureAsOf: Date?, heldAt: Date) -> Decision {
        if listed { return .open }
        if let pictureAsOf, pictureAsOf > heldAt { return .drop }
        return .wait
    }
}
