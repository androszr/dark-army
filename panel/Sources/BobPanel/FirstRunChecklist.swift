import SwiftUI

/// The three steps a newcomer sees where the panel first opens — **Enrol a
/// folder → Open it in VS Code → Start a session** — and every decision
/// about them, kept pure so the table in `FirstRunChecklistTests` is the
/// whole contract.
///
/// Three rules hold it together:
///
/// - **One root at a time, and only evidence for that root.** Step 1 is the
///   folder being in the enrolment ledger; step 2 is the daemon having seen
///   *that exact folder* open in a live VS Code window; step 3 is an admitted
///   main session inside it (`ChecklistRootFacts`, published per root). A
///   tick is never a button press and never inferred from a different
///   folder, a parent, a sibling or an installed extension.
/// - **Decided once per opening.** On the first authoritative snapshot after
///   the panel appears, the checklist is shown, skipped, or left to the
///   ordinary UI. A session that arrives *after* it appeared completes it in
///   place — all three ticks and "Setup complete" until the next opening —
///   rather than making it vanish under the reader.
/// - **Remembered, without authority.** Completion is one panel-local flag
///   (`PanelPlacement.checklistCompleted`). Un-enrolling still stops
///   observation the same instant whatever the flag says.
enum FirstRunChecklist {

    // MARK: - Steps

    /// The states a step can be in, spelled as the reader hears them.
    /// The raw value is the accessibility word; colour is never alone.
    /// `notNeeded` is step 2 alone, while "Dark Army's own terminal" is on:
    /// neither current nor next, and it counts toward completion.
    enum StepState: String, Equatable {
        case done = "Done"
        case current = "Current step"
        case next = "Next"
        case notNeeded = "Not needed"
    }

    struct Step: Equatable, Identifiable {
        let id: Int
        let title: String
        let state: StepState

        /// "Done: Enrol a folder" — what VoiceOver reads for the row.
        var accessibleLabel: String { "\(state.rawValue): \(title)" }
    }

    static let titles = ["Enrol a folder", "Open it in VS Code", "Start a session"]

    struct Progress: Equatable {
        let steps: [Step]
        /// The root the steps were computed for; nil when nothing is enrolled.
        let root: String?

        var complete: Bool {
            !steps.isEmpty && steps.allSatisfy { $0.state == .done || $0.state == .notNeeded }
        }

        var current: Step? { steps.first { $0.state == .current } }
    }

    /// Which enrolled root the checklist follows: the one it followed before
    /// while it is still enrolled, otherwise the first by canonical sort —
    /// deterministic, so two openings pick the same folder.
    static func selectRoot(previous: String?, enrolled: [String]) -> String? {
        if let previous, enrolled.contains(previous) { return previous }
        return enrolled.sorted().first
    }

    /// The folders the checklist may follow and the picker may offer: every
    /// enrolled folder except the ones the daemon marked as Dark Army's own
    /// checkout (`own_checkout`), which `enroll_self()` enrolled on a
    /// source build and the person never chose. Sorted by root, like the
    /// picker. With the checkout alone enrolled this is empty, so step 1
    /// stays current until a folder of the person's own is enrolled.
    static func candidates(enrollment: Enrollment) -> [EnrolledProject] {
        let own = enrollment.checklist?.ownCheckoutRoots ?? []
        return enrollment.enrolled
            .filter { !own.contains($0.root) }
            .sorted { $0.root < $1.root }
    }

    /// Each step's own truth for `root`, then the first not-done step is
    /// **current** and every later not-done one is **next**. Facts may
    /// arrive out of order (a session observed before its window is), and
    /// each is shown as it is — no prerequisite is ticked on a later step's
    /// evidence. With `ownTerminal` ("Dark Army's own terminal" on in
    /// Settings) an enrolled folder's unobserved step 2 is **not needed**:
    /// sessions started from the board run in Dark Army's own window, so no
    /// VS Code window has to appear. An observed editor still reads Done.
    static func progress(root: String?, enrollment: Enrollment,
                         ownTerminal: Bool = false) -> Progress {
        let enrolled = root.map { r in enrollment.enrolled.contains { $0.root == r } } ?? false
        let facts = root.flatMap { enrollment.checklist?.facts(for: $0) }
        let observed = [
            enrolled,
            enrolled && (facts?.editorObserved ?? false),
            enrolled && (facts?.sessionObserved ?? false),
        ]
        var seenCurrent = false
        let steps = titles.enumerated().map { index, title -> Step in
            if observed[index] { return Step(id: index, title: title, state: .done) }
            if index == 1, ownTerminal, enrolled {
                return Step(id: index, title: title, state: .notNeeded)
            }
            if seenCurrent { return Step(id: index, title: title, state: .next) }
            seenCurrent = true
            return Step(id: index, title: title, state: .current)
        }
        return Progress(steps: steps, root: root)
    }

    // MARK: - The opening

    /// What the first authoritative snapshot of an opening decides.
    enum Decision: Equatable {
        /// No checklist facts on the wire (an older daemon, or no enrolment
        /// section at all): the ordinary enrolment/quiet UI, untouched.
        case unsupported
        /// Setup was completed on an earlier opening: ordinary content.
        case alreadyDone
        /// A main session is already running somewhere: this install is
        /// plainly monitoring, so the tutorial is skipped and completion
        /// recorded — with no invented ticks drawn.
        case skipAndComplete
        /// Draw the checklist.
        case show
    }

    static func decide(enrollment: Enrollment, completed: Bool) -> Decision {
        guard enrollment.available, let checklist = enrollment.checklist,
              checklist.available else { return .unsupported }
        if completed { return .alreadyDone }
        if checklist.roots.contains(where: \.sessionObserved) { return .skipAndComplete }
        return .show
    }

    /// Where the checklist stands on the screen right now.
    enum Placement: Equatable {
        case none
        case checklist
        case completed
    }

    /// The per-opening state machine, advanced by every snapshot.
    ///
    /// An opening ends when the panel is **put away** (`FocusRouter.hidden`),
    /// never when it is merely covered: the newcomer's own flow is checklist
    /// showing → go to VS Code (panel covered) → open the folder, start a
    /// session → come back, and a re-decision at that point would find the
    /// session and *skip* the tutorial instead of ticking it. The same rule
    /// holds across a real put-away: a checklist that has appeared is
    /// **sticky** (`resumeChecklist`), so the next opening resumes it and a
    /// session seen meanwhile completes it — `.completed`, never
    /// `.skipAndComplete`. Only a completed or never-shown checklist lets
    /// `decide` run.
    struct Opening: Equatable {
        var decided = false
        var placement: Placement = .none
        var selectedRoot: String?
        /// The checklist was showing, unfinished, when the last opening
        /// ended. Carried across `reset()`; spent by the next decision.
        var resumeChecklist = false

        /// The opening is over: decide afresh on the next snapshot, keeping
        /// only whether an unfinished checklist was on screen — and, when it
        /// was, which folder it followed, so a resumed checklist does not
        /// silently re-pick the first root of several. `selectRoot` still
        /// drops a root that has left the ledger meanwhile.
        mutating func reset() {
            let sticky = placement == .checklist || resumeChecklist
            let followed = sticky ? selectedRoot : nil
            self = Opening()
            resumeChecklist = sticky
            selectedRoot = followed
        }

        /// Whether a snapshot is one this opening may decide on: a real
        /// frame (`generatedAt` stamped) whose enrolment section the daemon
        /// stated.
        static func authoritative(_ snapshot: Snapshot) -> Bool {
            snapshot.generatedAt > 0 && snapshot.enrollment.available
        }

        /// Feed one snapshot. Returns `true` when completion should be
        /// **persisted** — the skip on an already-monitoring install, or all
        /// three steps observed (or step 2 not needed) for the followed root.
        /// `ownTerminal` must be the same flag the view draws with, or the
        /// ticks and the "Setup complete" moment disagree. The followed
        /// root is chosen among `candidates`, never Dark Army's own checkout.
        @discardableResult
        mutating func advance(snapshot: Snapshot, completed: Bool,
                              ownTerminal: Bool = false) -> Bool {
            guard Opening.authoritative(snapshot) else { return false }
            let enrollment = snapshot.enrollment
            var persist = false
            if !decided {
                decided = true
                let sticky = resumeChecklist
                resumeChecklist = false
                if sticky, !completed, enrollment.checklist != nil {
                    // Shown before and not finished: resume rather than
                    // re-decide, so a session that arrived meanwhile ticks
                    // step 3 below instead of skipping the tutorial.
                    placement = .checklist
                } else {
                    switch FirstRunChecklist.decide(enrollment: enrollment,
                                                    completed: completed) {
                    case .show:
                        placement = .checklist
                    case .skipAndComplete:
                        placement = .none
                        persist = true
                    case .unsupported, .alreadyDone:
                        placement = .none
                    }
                }
            }
            guard placement == .checklist else { return persist }
            selectedRoot = FirstRunChecklist.selectRoot(
                previous: selectedRoot,
                enrolled: FirstRunChecklist.candidates(enrollment: enrollment).map(\.root))
            let progress = FirstRunChecklist.progress(root: selectedRoot,
                                                      enrollment: enrollment,
                                                      ownTerminal: ownTerminal)
            if progress.complete {
                placement = .completed
                persist = true
            }
            return persist
        }

        /// The reader picked another folder: the ticks are recomputed from
        /// that root alone on the next snapshot. Ignored once complete.
        mutating func choose(root: String) {
            guard placement == .checklist else { return }
            selectedRoot = root
        }
    }

    // MARK: - Words

    /// What to do next, under the current step. Step 2 and 3 name the
    /// folder; step 1 explains the private key, because enrolling writes one.
    /// Step 2 names VS Code's trust question, the one reason a window can be
    /// open and still not connect. Step 3 names the terminal route only, with
    /// or without Dark Army's own terminal: the board files a card only under
    /// a folder open in a VS Code window, so "write a card and press START"
    /// would send a newcomer with no window to a composer that cannot file
    /// under their folder.
    static func instruction(for step: Step, folderLabel: String) -> String {
        switch step.id {
        case 0:
            return "Dark Army watches only the folders you enrol. Enrolling "
                + "one puts a private key file inside it; everything else stays quiet."
        case 1:
            return "Open the folder \(folderLabel) in VS Code. Waiting for VS "
                + "Code to connect — if VS Code is asking whether you trust the "
                + "folder's authors, answer that first; if it is already open "
                + "there, install or reload the Dark Army extension from Settings."
        default:
            return "Start a Claude Code, Codex or Grok session in a terminal inside \(folderLabel). "
                + "It appears here as soon as it starts; you do not have to send a prompt."
        }
    }

    /// The one line under a step 2 that is not needed, saying why.
    static let notNeededNote = "Not needed while \"Dark Army's own terminal\" "
        + "is on in Settings: sessions you start from the board run in Dark "
        + "Army's own window."

    static let heading = "Set up Dark Army"
    static let completeHeading = "Setup complete"
    static let completeLine = "Dark Army is watching your first session. "
        + "Next: write your first card on the board — refine it in Prep, then "
        + "press START in Backlog to hand it to an agent. "
        + "Next time the panel opens as usual."
    /// The button under "Setup complete": opens a blank card, filed under
    /// the folder that was just set up where the board can file there.
    static let firstCardAction = "Write your first card"

    /// What the first card's composer is pre-filled with: the project and
    /// root the board itself offers for the followed folder, or nothing.
    struct FirstCardFiling: Equatable {
        let project: String
        let root: String
    }

    /// The followed root pre-fills the composer **only** when the board
    /// offers it (`board.projects`, the folders open in a VS Code window —
    /// the composer's own picker list, and the only roots `board_create`
    /// accepts), under the board's name for it. Otherwise the composer opens
    /// blank, so the person picks from what the board can actually file
    /// under rather than meeting a refusal on save.
    static func firstCardFiling(selectedRoot: String?,
                                boardProjects: [BoardProject]) -> FirstCardFiling {
        guard let root = selectedRoot, !root.isEmpty,
              let offered = boardProjects.first(where: { $0.root == root }) else {
            return FirstCardFiling(project: "", root: "")
        }
        return FirstCardFiling(project: offered.name, root: offered.root)
    }

    // MARK: - The launch line

    /// "This launch: hooks current; editor extension installed; login item
    /// enabled." One clause per installer, from its actual result. A failure
    /// is named first and carries its detail; a pending result reads as
    /// checking; nothing here ever calls an attempt a success.
    static func launchLine(_ report: DaemonClient.LaunchReport) -> String {
        let clauses = [
            clause("hooks", report.hooks, changed: "installed", unchanged: "current"),
            clause("editor extension", report.editorExtension,
                   changed: "installed", unchanged: "current"),
            clause("login item", report.loginItem,
                   changed: "enabled", unchanged: "unchanged"),
        ]
        let lead = report.anyFailed ? "This launch had a problem: " : "This launch: "
        return lead + clauses.joined(separator: "; ") + "."
    }

    private static func clause(_ label: String, _ outcome: DaemonClient.LaunchOutcome,
                               changed: String, unchanged: String) -> String {
        let said = outcome.detail.trimmingCharacters(in: .whitespacesAndNewlines)
        switch outcome.status {
        // A change that waits for the next login (a login item written but
        // deliberately not loaded mid-launch) says so in the daemon's words
        // rather than claiming "enabled".
        case "changed" where said.hasPrefix("\(label) ") && said.contains("next login"):
            return said
        case "changed": return "\(label) \(changed)"
        case "unchanged": return "\(label) \(unchanged)"
        case "skipped": return "\(label) skipped"
        case "pending": return "\(label) checking…"
        case "failed":
            let detail = outcome.detail.trimmingCharacters(in: .whitespacesAndNewlines)
            return detail.isEmpty ? "\(label) failed" : "\(label) failed — \(detail)"
        default: return "\(label) unknown"
        }
    }

    // MARK: - Notifications

    /// Only an explicit **denied** draws the warning. `unknown`,
    /// `not_determined`, `unavailable` and `error` say nothing: the first
    /// three are not a decision and the last is not the person's.
    static func notificationsDenied(_ status: String) -> Bool {
        status == "denied"
    }

    static let deniedLine = "Notifications are off for Dark Army"
    static let deniedAction = "Notification Settings…"
}

// MARK: - The view

/// The checklist as the rail draws it, above the tab's own content. Every
/// word comes from `FirstRunChecklist`; this file arranges them.
struct FirstRunChecklistView: View {
    let progress: FirstRunChecklist.Progress
    let completed: Bool
    /// The folders the checklist may follow (`FirstRunChecklist.candidates`:
    /// enrolled, minus Dark Army's own checkout), for the picker drawn only
    /// when there are two or more.
    let folders: [EnrolledProject]
    let selectedRoot: String?
    let launchLine: String
    let notificationsDenied: Bool
    /// Enrolling is in flight (the chooser was answered, the daemon has not).
    let enrolling: Bool
    /// The daemon's own refusal from the last Enrol press, or empty.
    let enrolError: String
    let onChooseRoot: (String) -> Void
    let onEnrol: () -> Void
    let onNotificationSettings: () -> Void
    /// "Write your first card", under "Setup complete".
    var onFirstCard: () -> Void = {}

    private var folderLabel: String {
        folders.first { $0.root == selectedRoot }?.label
            ?? selectedRoot.map { ($0 as NSString).lastPathComponent }
            ?? "your project"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(completed ? FirstRunChecklist.completeHeading : FirstRunChecklist.heading)
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(Theme.phosphorBright)
                .accessibilityAddTraits(.isHeader)
            if folders.count > 1, !completed {
                Picker("Folder being set up", selection: Binding(
                    get: { selectedRoot ?? "" },
                    set: { onChooseRoot($0) })) {
                    ForEach(folders.sorted { $0.root < $1.root }) { folder in
                        Text(folder.label).tag(folder.root)
                    }
                }
                .pickerStyle(.menu)
                .font(Theme.mono(11))
            }
            ForEach(progress.steps) { step in
                stepRow(step)
            }
            if completed {
                Text(FirstRunChecklist.completeLine)
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button(FirstRunChecklist.firstCardAction, action: onFirstCard)
                    .font(.caption)
                    .clickable()
            }
            Text(launchLine)
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(launchLine)
            if notificationsDenied {
                HStack(spacing: 8) {
                    Image(systemName: "bell.slash")
                        .font(.system(size: 10))
                        .accessibilityHidden(true)
                    Text(FirstRunChecklist.deniedLine)
                        .font(.caption2)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button(FirstRunChecklist.deniedAction, action: onNotificationSettings)
                        .font(.caption2)
                        .clickable()
                }
                .foregroundStyle(Theme.amber)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Theme.bg)
        Divider()
    }

    @ViewBuilder
    private func stepRow(_ step: FirstRunChecklist.Step) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(marker(step.state))
                    .font(Theme.mono(11, weight: .bold))
                    .foregroundStyle(tint(step.state))
                    .frame(width: 14, alignment: .leading)
                    .accessibilityHidden(true)
                Text("\(step.id + 1). \(step.title)")
                    .font(Theme.mono(11, weight: step.state == .current ? .semibold : .regular))
                    .foregroundStyle(step.state == .next || step.state == .notNeeded
                                     ? .secondary : Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Text(step.state.rawValue)
                    .font(.caption2)
                    .foregroundStyle(tint(step.state))
                    .accessibilityHidden(true)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(step.accessibleLabel)
            if step.state == .notNeeded {
                Text(FirstRunChecklist.notNeededNote)
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 20)
            }
            if step.state == .current {
                Text(FirstRunChecklist.instruction(for: step, folderLabel: folderLabel))
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 20)
                if step.id == 0 {
                    HStack(spacing: 8) {
                        Button(enrolling ? "Enrolling…" : "Enrol a folder…", action: onEnrol)
                            .disabled(enrolling)
                            .font(.caption)
                            .clickable(!enrolling)
                    }
                    .padding(.leading, 20)
                    if !enrolError.isEmpty {
                        Text(enrolError)
                            .font(.caption2).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.leading, 20)
                    }
                }
            }
        }
    }

    private func marker(_ state: FirstRunChecklist.StepState) -> String {
        switch state {
        case .done: return "✓"
        case .current: return "›"
        case .next: return "·"
        case .notNeeded: return "–"
        }
    }

    private func tint(_ state: FirstRunChecklist.StepState) -> Color {
        switch state {
        case .done: return Theme.phosphor
        case .current: return Theme.amber
        case .next, .notNeeded: return .secondary
        }
    }
}
