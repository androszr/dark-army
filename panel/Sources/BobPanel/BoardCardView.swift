import SwiftUI

/// One card, and the verbs that apply to the column it is in.
///
/// **The card leads with the summary, not the prompt.** A board is read by
/// whoever walks past it, and what a card used to show under its title was the
/// first three lines of an agent instruction — "Implement the accepted plan at
/// plans/2026-08-22-agent-stage-track.md. Read it in full first…" — which tells
/// that reader nothing at all about what the work is *for*. The prompt is still
/// here, below and quieter, and in full in the detail sheet; it is simply no
/// longer the thing the card is about.
///
/// **Start is absent, not disabled**, when the card names no assistant, already
/// has one, is waiting on another card, or Dark Army is not allowed to start sessions.
/// That is the rule `WrapUpBar` already follows for `can_type`, and it is worth
/// restating: a button that is present and inert is a promise the app cannot
/// keep, and the user has to press it to find out.
struct BoardCardView: View {
    /// Held for the verbs, **not observed**: a card redraws when its own
    /// inputs below differ, never because the client published a frame about
    /// somebody else.
    let client: DaemonClient
    @ObservedObject var state: BoardState
    let card: BoardCard
    /// The board-wide facts this face reads (`BoardChrome`), as a value.
    let chrome: BoardChrome
    /// The session doing this card and which bucket it is in, straight from the
    /// agents snapshot. `nil` when the card has no session, or has one the fleet
    /// no longer lists.
    let agent: FleetRow?
    /// The session that wrote this card, if the fleet still lists it. `nil` on a
    /// card a person typed (`author == "user"`), and on one whose author is gone.
    let authorAgent: FleetRow?
    /// The session *refining* this card, if the refinement is live and the fleet
    /// still lists it. `nil` while a refinement is merely dispatching — no
    /// session exists yet, so there is nothing a chip could aim at.
    let refinerAgent: FleetRow?

    private var isArmed: Bool { state.armed == card.id }
    private var isArmedHere: Bool { state.armedHere == card.id }
    private var isDeleteArmed: Bool { state.deleteArmed == card.id }
    private var isDoneArmed: Bool { state.doneArmed == card.id }
    private var isMergeArmed: Bool { state.mergeArmed == card.id }
    private var isFixArmed: Bool { state.fixArmed == card.id }
    private var isStarting: Bool { state.starting.contains(card.id) }
    /// Confirmed Delete, not yet gone from a snapshot. Same rule as
    /// `RowActions.stopping`: the press has to change the screen immediately or
    /// it reads as dropped, and the second or so before the board agrees is
    /// exactly when a third press arrives.
    private var isDeleting: Bool { state.deleting.contains(card.id) }
    /// Refine pressed (optimistic) or the snapshot says a refinement is
    /// underway. `isStarting`'s twin, drawn with the same spinner treatment.
    private var isRefining: Bool {
        state.refining.contains(card.id) || card.isRefining
    }
    @State private var jumpHover = false
    /// Which jump chip holds the keyboard focus, keyed by its row and its
    /// words: a focused chip brightens as a hovered one does.
    @FocusState private var jumpFocus: String?
    /// Under an agent's detail the board is covered: its tile leaves the
    /// focus chain (`keyboardClaimsSuppressed`).
    @Environment(\.keyboardClaimsSuppressed) private var keyboardSuppressed
    /// Reduce Motion, for the reveal ring and the delete fade.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// True while a reverse jump from VS Code has just brought this card into
    /// view. A brief phosphor ring, not a selection: the board has no card
    /// selection and this must not invent one, so it fades on the applier's
    /// own timer and leaves nothing behind.
    private var isRevealed: Bool { state.revealedCard == card.id }

    /// Every condition, in one place. The button is drawn only if all of them
    /// hold; the daemon re-checks all of them again at the moment of dispatch,
    /// against the database rather than against this view.
    private var canStart: Bool {
        chrome.dispatchEnabled
            && card.column != BoardColumn.done.rawValue
            && !card.tool.isEmpty
            && card.sessionId.isEmpty
            && !card.isDispatching
            && !card.isRefining
            // A card waiting its turn in a batch is bound by that batch's
            // own session; the daemon refuses a second Start on it in
            // words, so the button is absent rather than inert.
            && !card.holdsBatchMark
    }

    /// Refine's own gate, `canStart`'s shape: absent, never disabled. Prep
    /// only, no plan yet, nothing already refining or running it, and Dark Army
    /// allowed to start sessions at all — Refine *is* Dark Army starting a session,
    /// so it sits behind the same `dispatchEnabled` the daemon checks.
    ///
    /// The terms live in `RowSelection.tickable(.prep, …)`, so the Prep row's
    /// batch tick and this button can never disagree about a card.
    private var canRefine: Bool {
        RowSelection.tickable(.prep, card: card, chrome: chrome)
    }

    /// Promote is offered on a Done scout that already has a report. The
    /// daemon re-checks; this only hides a button that cannot succeed.
    private var canPromote: Bool {
        card.isScout && !card.reportPath.isEmpty
            && card.column == BoardColumn.done.rawValue
    }

    /// Review and merge on a Done card (`CardMerge`, one rule on the Mac and
    /// the phone). The daemon re-checks the hand-check, the folder and the
    /// main checkout at the press; these only hide a control that cannot
    /// succeed.
    private var mergeOffered: Bool {
        CardMerge.offered(column: card.column, branch: card.worktreeBranch,
                          mergeState: card.mergeState,
                          manualDue: card.needsManualCheck,
                          daemonOffers: card.mergeOffered)
    }
    private var fixOffered: Bool {
        CardMerge.fixOffered(mergeState: card.mergeState,
                             daemonOffers: card.mergeOffered)
    }
    private var reviewOffered: Bool {
        CardMerge.reviewOffered(column: card.column, branch: card.worktreeBranch,
                                reviewRunning: card.reviewRunning,
                                manualDue: card.needsManualCheck,
                                daemonOffers: card.mergeOffered)
    }
    private var isMerging: Bool {
        card.column == BoardColumn.done.rawValue && card.mergeState == "merging"
    }
    /// The merge line is amber where the person is wanted ("Merge needs
    /// you"), dim otherwise.
    private var mergeNeedsYou: Bool {
        ["blocked", "conflict", "checks_failed"].contains(card.mergeState)
    }

    /// Whether a confirmed Start would skip the plan gate — the armed label
    /// says so ("Start unplanned?") and the confirmed press carries the flag.
    /// The arm *is* the confirmation; a popup stacked on an arm is ceremony,
    /// not safety.
    ///
    /// `planPath` alone, deliberately — no file stat here, this is read on
    /// every redraw. It can therefore say "planned" about a card whose plan
    /// *file* is gone; the daemon's gate catches that case and `start`'s
    /// plan-gate fallback raises the confirmation dialog instead, so the
    /// route through still exists — it just takes the daemon's word for it
    /// rather than keeping a second copy of the `isfile` check that could
    /// drift. A scout has no plan to skip, so it is never "unplanned".
    private var startsUnplanned: Bool { card.planPath.isEmpty && !card.isScout }

    /// START HERE: spawn Dark Army's own terminal for a card that has none
    /// connected. Absent when Start already does that (the machine-wide
    /// preference is on), so the two buttons cannot do the same thing.
    private var canSpawnHere: Bool {
        canStart && !chrome.ownTerminalEnabled
    }

    /// What a queued card says about its wait — the daemon's own sentence,
    /// shown verbatim, else a plain queued promise for a daemon that predates
    /// `queue_reason`. Nothing is composed here. The preference order lives
    /// on `BoardCard` so it is a testable function rather than a private view
    /// property. See `BoardCard.queuedLine(autostart:)`.
    private var queuedLine: String {
        card.queuedLine(autostart: chrome.autostartEnabled)
    }

    private var isDropTarget: Bool {
        if case .card(let id) = state.dropTarget { return id == card.id }
        return false
    }

    /// Whether the assistant is still somebody's choice to make. Once a session
    /// is bound the field is a record of what ran, not a setting — offering a
    /// menu there would let a click rewrite history the board is reporting.
    private var canRetool: Bool {
        card.sessionId.isEmpty && !card.isDispatching
    }

    /// The tick box leading the title while this card's row is in select
    /// mode. Phosphor when ticked; faint and inert where the card could not
    /// join the selection (`RowSelection.admits`: not refinable right now, or
    /// a different project from the first card ticked). The Refine button
    /// stays where it is — the tick is an addition, never a replacement.
    private func rowTick(_ row: BoardColumn) -> some View {
        let ticked = state.rowSelection.contains(card.id)
        let admitted = ticked || RowSelection.admits(
            row, card: card, given: state.rowSelectionCards, chrome: chrome)
        return Button {
            state.toggleRowSelection(card, chrome: chrome)
        } label: {
            Image(systemName: ticked ? "checkmark.square" : "square")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(ticked ? Theme.phosphor : Theme.faint)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!admitted)
        .clickable(admitted)
        // Backlog's batch verb starts sessions; Prep's refines.
        .accessibilityLabel(row == .backlog ? "Select for batch start"
                                            : "Select for batch refine")
        .accessibilityValue(ticked ? "selected" : "not selected")
        .accessibilityHint(!admitted ? "This card cannot join this batch"
                           : row == .backlog ? "Ticks this card to start with the others"
                           : "Ticks this card to refine with the others")
    }

    #if DEBUG
    /// How many times any tile's `body` ran — the redraw tests' probe
    /// (`BoardScrollRedrawTests`). Debug builds only; a release panel
    /// counts nothing.
    @MainActor static var bodyEvaluations = 0
    #endif

    var body: some View {
        #if DEBUG
        Self.bodyEvaluations += 1
        #endif
        return VStack(alignment: .leading, spacing: 6) {
            statusBanner
            if card.manualCheckDue {
                // Above the title, because it is the first thing about this
                // card that is true: the work is finished and it is now
                // somebody's turn — which is why the flag is `manualCheckDue`
                // and not the bare note. While the assistant is still typing
                // it is *not* somebody's turn, and the line stays away; it
                // returns by itself the moment that session stops, with
                // nothing cleared and nothing pressed. The **title is never rewritten** to say so
                // — that would edit the person's own words and would not
                // survive being cleared. Amber rather than the orange
                // `dispatchError` wears below: orange means Dark Army refused
                // something, this means your turn, and a reader who cannot
                // tell those apart has two different problems and one colour.
                Text("MANUAL CHECK NEEDED")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .tracking(0.8)
                    .accessibilityLabel("Manual check needed")
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if let row = state.selectingRow, row.rawValue == card.column {
                    rowTick(row)
                }
                Text(card.title)
                    .font(Theme.prose(14, weight: .semibold))
                    .foregroundStyle(Theme.text)
                    // Four lines, in a column twice as wide as it was: a
                    // title is the one thing on the face a person reads to
                    // decide, and a clipped one sends them into the card.
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(card.title)
                if card.threadCount > 0 {
                    let label = card.threadCount == 1
                        ? "1 message" : "\(card.threadCount) messages"
                    chip(label)
                        .accessibilityLabel(label)
                }
                // The importance number, beside the assistant chip where the
                // card's other short facts already sit. Drawn only when the
                // card has one: `""` is "nobody has scored this" and draws
                // nothing, so the face gains nothing on an unscored board,
                // while `"0"` is a real score and draws `P 0`.
                if card.isScout {
                    chip("SCOUT")
                        .accessibilityLabel("scout card")
                }
                if !card.area.isEmpty { chip(Areas.name(card.area)) }
                if !card.priority.isEmpty {
                    chip("P \(card.priority)")
                        .accessibilityLabel("priority \(card.priority)")
                }
                Spacer(minLength: 4)
                toolControl
            }
            // The card face never names its lane: every card is drawn under a
            // band heading that has just said it — including Unfiled — which
            // is the same rule the project chip below follows for the filter.
            // The chip shows whenever the filter has *not* just said the
            // project — `singleProject == nil`, which covers both "all" and
            // "several ticked". Under two or more ticks the projects mix, so
            // hiding the chip there would make them indistinguishable.
            if state.singleProject == nil && !card.project.isEmpty {
                Text(card.project)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            }
            if !card.summary.isEmpty {
                Text(card.summary)
                    .font(Theme.prose(12))
                    .foregroundStyle(Theme.muted)
                    // Six, for the same reason as the title's four: the
                    // summary is the field written to be read on the face.
                    .lineLimit(6)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !card.prompt.isEmpty {
                // No summary written, so the instructions are all there is. Two
                // lines rather than three: they are a fallback, and a card that
                // is mostly clipped machine input is the thing to make people
                // notice the summary field, not to make comfortable.
                Text(card.prompt)
                    .font(Theme.prose(11))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // What a finished scout found — the verdict and the scout's
            // recommendation as the daemon stored them at the attach,
            // through `ScoutVerdictLine`, the wording shared with the phone.
            // One line on the tile, cut short at the end; the card window
            // draws it in full. Absent where the report had no answer block.
            if card.isScout,
               let verdict = ScoutVerdictLine.text(
                   verdict: card.reportVerdict,
                   recommendation: card.reportRecommendation) {
                Text(verdict)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .accessibilityLabel(ScoutVerdictLine.spoken(
                        verdict: card.reportVerdict,
                        recommendation: card.reportRecommendation))
            }
            // What the card has cost so far and how long its assistant has
            // worked — `RunFigures.line`, the wording shared with the phone.
            // Drawn only where the daemon sent figures: a card nobody has
            // run shows nothing here, not a zero and not a dash. No clock:
            // the daemon buys the frame when a minute, a cent or a percent
            // moves, and the line is drawn as it arrived.
            if let figures = RunFigures.line(card.runFigures) {
                Text(figures)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(figures)
            }
            // How the run is going — `RunHealthLine.text`, the daemon's own
            // reading drawn verbatim, the size word first so it reads
            // without colour. Amber on the daemon's `attention` bit, never
            // colour alone. Drawn only where the card carries a reading: a
            // card nobody has started shows nothing here.
            if let health = card.runHealth {
                Text(RunHealthLine.text(health))
                    .font(Theme.mono(9))
                    .foregroundStyle(health.attention ? Theme.amber : Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(RunHealthLine.spoken(health))
            }
            if card.isAgentAuthored {
                // Attribution matters here in a way it does not for a card
                // somebody typed: this one arrived through a socket that
                // authenticates nothing, and the daemon refused to file it at
                // all unless it could name the session that asked.
                // The author chip exists only while nothing is running the
                // card: once a worker or a refiner resolves, that session is
                // the card's subject and owns the card's one jump chip.
                reporterRow(name: card.authorName.isEmpty ? card.author : card.authorName,
                            prefix: "added by",
                            jumpTarget: (agent == nil && refinerAgent == nil)
                                ? authorAgent?.agent : nil,
                            jumpHelp: "Jump to the session that added this card")
            }
            if card.isAgentClosed {
                // Why a reader can tell a *checked* close from a drag. Dark Army
                // decided nothing here: the assistant that was doing the work
                // said it was finished, and this is its name and its sentence.
                // A card a human dragged into Done shows neither, which is the
                // whole difference the two fields exist to draw.
                // The chip targets the *bound* session, not a second resolve
                // of `closedBy`: `board.py`'s `declare_done` refuses a card
                // that is not the declaring session's, so on every
                // agent-closed card `closed_by == session_id` and the bound
                // row *is* the closer — resolving `closedBy` separately would
                // be a second copy of the same fact. If that guard is ever
                // loosened to accept a card id, this aim stops being right.
                reporterRow(name: card.closedByName.isEmpty ? card.closedBy : card.closedByName,
                            prefix: "closed by",
                            jumpTarget: agent?.agent,
                            jumpHelp: "Jump to the session that closed this card")
                Text(card.closeNote)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            liveState
            // The crew, under the status they belong to. Draws nothing on a
            // card that has neither run anything nor declared anything, which
            // is most of the backlog. The spoken caption lives here rather
            // than inside the band, which is byte-shared with the phone.
            CrewBand(workflow: card.workflow,
                     trail: card.agentTrail,
                     live: liveStageNames,
                     crew: card.crew,
                     lead: card.area,
                     promised: card.leadFace,
                     style: .strip)
                .accessibilityLabel(CrewBand.caption(workflow: card.workflow,
                                                     trail: card.agentTrail,
                                                     live: liveStageNames,
                                                     crew: card.crew, lead: card.area,
                                                     promised: card.leadFace))
            if let refusal = state.refusals[card.id], !refusal.isEmpty {
                Text(refusal)
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.batchLine.isEmpty {
                // Where this card stands in a batch session — the daemon's
                // three fields, composed into one line. Phosphor for the
                // card being worked, faint for one waiting its turn.
                Text(card.batchLine)
                    .font(.system(size: 10))
                    .foregroundStyle(card.isBatchWaiting ? Theme.faint
                                                         : Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.worktreeBranch.isEmpty {
                // The card's own branch (`docs/card-worktrees.md`), dim:
                // where its work lands, for the person who will merge it.
                Text("\u{2387} \(card.worktreeBranch)")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.worktreeNote.isEmpty {
                // The daemon's own words about the folder — preparing, or
                // kept because it holds unsaved work. Verbatim.
                Text(card.worktreeNote)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.mergeLine.isEmpty {
                // What the last MERGE press came to, the daemon's words
                // verbatim: dim while it works or once it landed, amber
                // where it needs you.
                Text(card.mergeLine)
                    .font(.system(size: 10))
                    .foregroundStyle(mergeNeedsYou ? Theme.amber : Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let verdict = CardMerge.verdictLine(
                verdict: card.reviewVerdict, running: card.reviewRunning,
                current: true) {
                Text(verdict)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if card.isQueued {
                // Secondary ink, deliberately **not** the orange
                // `dispatchError` style: queued is not an error, it is the
                // gesture working. And the wording is keyed off the daemon's
                // own flag rather than assumed — with the drain switched off
                // nobody is coming, and a card that says otherwise is the app
                // promising something it will not do.
                // Naming the holder is the difference between a queue a
                // person can read and one they have to reconstruct from the
                // daemon log: four cards once sat here marked Queued for an
                // hour with nothing on screen saying which session held them.
                Text(queuedLine)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The cards this one waits on and the ones it unblocks — the
            // daemon's two sentences, verbatim, each drawn only where it sent
            // one. Secondary ink for the queued line's reason: a link is a
            // fact about order, not an error.
            if !card.dependencyLine.isEmpty {
                Text(card.dependencyLine)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.dependentsLine.isEmpty {
                Text(card.dependentsLine)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.dispatchError.isEmpty {
                HStack(spacing: 4) {
                    Text(card.dispatchError)
                        .font(.system(size: 10))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Clear") { clearError() }
                        .buttonStyle(.link)
                        .font(.system(size: 10))
                        .clickable()
                        .reportsKeyboardFocus()
                }
            }
            actions
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Theme.cardRadius).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).strokeBorder(stroke, lineWidth: 1))
        // Above the ordinary border rather than instead of it: the card's own
        // stroke still says what state it is in, and this only says "here".
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius)
            .strokeBorder(Theme.phosphor, lineWidth: 2)
            .opacity(isRevealed ? 1 : 0))
        .shadow(color: isRevealed ? Theme.phosphor.opacity(0.45)
                                  : (isLive ? Theme.phosphor.opacity(0.12) : .clear),
                radius: isRevealed ? 16 : (isLive ? 12 : 0))
        .animation(Motion.animation(.easeOut(duration: 0.25), reduced: reduceMotion),
                   value: isRevealed)
        .opacity(isDeleting ? 0.45 : 1)
        // Nothing on a card that is going anywhere is worth pressing, and a
        // Delete pressed twice is a refusal on a card that no longer exists.
        .allowsHitTesting(!isDeleting)
        .overlay {
            if isDeleting {
                // An overlay, so the line cannot change the card's size; applied
                // after the hit-testing gate so the skip tap still lands
                // while the card underneath ignores clicks.
                AgentChatterView(.line, wait: .deleting, seed: card.id,
                                 spoken: "Deleting this card")
                    .id(card.id)
                    .padding(.horizontal, 10)
            }
        }
        .animation(Motion.animation(.easeOut(duration: 0.15), reduced: reduceMotion),
                   value: isDeleting)
        .contentShape(Rectangle())
        // A double click opens the card, which is what a double click on a
        // document-shaped thing means everywhere else on this platform. The
        // single tap keeps its old job: anything that is not pressing the armed
        // button is moving on, and moving on disarms. The gates are one object,
        // so this is the only place that has to say so.
        .onTapGesture(count: 2) { openDetail() }
        .onTapGesture { state.disarm() }
        // The hand states the two gestures the card itself answers: pick it up
        // and drag it into a column, or double click it open. It goes dark with
        // `allowsHitTesting(!isDeleting)` above, so a card mid-deletion does not
        // promise a press it is already ignoring.
        .clickable(!isDeleting)
        // VoiceOver's way in, beside the pointer's double click: the tile
        // reads as a button and opens the card on its default action, and
        // "Open card" is listed among its actions. The verbs inside stay
        // reachable as their own elements.
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { openDetail() }
        .accessibilityAction(named: "Open card") { openDetail() }
        // And the keyboard's: Tabbed onto (Full Keyboard Access), the tile
        // takes the focus like a button and opens on Return or Space, which
        // the key monitor hands to a focused control — only while the tile
        // itself is focused (never a verb inside it), never on a covered
        // board, and never on a card already being deleted.
        .focusable(!keyboardSuppressed, interactions: .activate)
        .reportsKeyboardFocus(keys: [.return, .space],
                              onPress: isDeleting ? nil : { openDetail() })
    }

    /// The card's loud header, above everything else on it, because it is the
    /// first thing about the card that is true: the work stopped, and what
    /// happens next is a person's move. Two bands, both derived from fields
    /// the daemon already states on the frame — a second flag saying what
    /// `closed_by` and `link_state` already say would be the "computes a count
    /// a second way" failure — and both carry their meaning in words: the
    /// tints (green / red) distinguish, but they are never the sole carrier,
    /// and each band's words are a unique string beside the amber manual-check
    /// line and the orange `dispatchError` text — distinguishable even in
    /// monochrome.
    /// Whether the "SESSION ENDED · NO REPORT" band is drawn on this card.
    ///
    /// One name for the condition rather than two copies of it: `liveState`
    /// asks the same question to decide whether it still has to state the
    /// fact, and two spellings of one gate is how the band and the line drift
    /// apart.
    private var endedBannerDrawn: Bool {
        card.column == BoardColumn.inProgress.rawValue
            && card.sessionEnded && !card.isAgentClosed
    }

    @ViewBuilder
    private var statusBanner: some View {
        if card.column == BoardColumn.done.rawValue && card.awaitsReview {
            // An assistant said this work is finished and no human has looked
            // yet. The banner stays up — overnight, across restarts — until
            // the Reviewed press below drops it; nothing else can. One click,
            // no arm: nothing is destroyed (the name and the note stay on the
            // card), and a gate here would dilute the gates that guard real
            // losses. No fourth `BoardState` arm slot.
            HStack(spacing: 6) {
                Text("FINISHED · REVIEW")
                    .font(Theme.mono(10, weight: .bold))
                    .foregroundStyle(Theme.phosphorBright)
                    .tracking(0.8)
                Spacer(minLength: 4)
                Button("Reviewed") { review() }
                    .buttonStyle(.link)
                    .font(Theme.mono(10))
                    .clickable()
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .frame(maxWidth: .infinity)
            .background(Rectangle().fill(Color.green.opacity(0.18)))
            .accessibilityLabel("Finished — waiting for your review")
        } else if endedBannerDrawn {
            // The session went away without declaring anything. Marked in
            // place, never moved — the daemon still never infers Done — and
            // no button: the verbs are the ones the card already has (Done,
            // Back, Start). The `liveState` row below keeps the instruction
            // and the jump chip; this band states the fact.
            Text("SESSION ENDED · NO REPORT")
                .font(Theme.mono(10, weight: .bold))
                .foregroundStyle(Theme.phosphorBright)
                .tracking(0.8)
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Rectangle().fill(Color.red.opacity(0.18)))
                .accessibilityLabel("Session ended with no report")
        }
    }

    /// The subagents running under this card's session right now, by name.
    ///
    /// Read off the live agents snapshot rather than out of the card, for the
    /// same reason the card's status is: `agent_trail` records that a stage
    /// *ran*, and only the fleet knows whether it is still running. A card
    /// whose session has ended resolves to no agent and therefore to an empty
    /// set, which leaves every stage it reached drawn as done — correct, and
    /// the reason this is not persisted anywhere.
    private var liveStageNames: Set<String> {
        guard let agent = agent?.agent else { return [] }
        return Set(agent.subagentRows.map(\.label).filter { !$0.isEmpty })
    }

    /// Bound session still listed, and this card is not Done. Done must not
    /// assert anything outstanding — same gate `liveState` hoists.
    private var isLive: Bool {
        agent != nil && card.column != BoardColumn.done.rawValue
    }

    /// Drop-target phosphor, Delete red, Start- or Done-armed orange, live
    /// phosphor, else the idle hairline. Distinct from each other on purpose.
    /// Start and Done share the orange because they share the meaning — an
    /// armed press that will do something on the far end of a terminal — and
    /// the button's own label is what says which.
    private var stroke: Color {
        if isDropTarget { return Theme.phosphor }
        if isDeleteArmed { return .red }
        if isArmed || isArmedHere || isDoneArmed { return .orange }
        if isLive { return Theme.phosphor }
        return Theme.hair
    }

    private func reporterRow(name: String, prefix: String,
                             jumpTarget: Agent? = nil,
                             jumpHelp: String = "") -> some View {
        HStack(spacing: 6) {
            PixelMark(character: Cast.character(forNickname: name),
                      state: .sleep, size: 16)
            Text("\(prefix) \(name)")
                .font(Theme.mono(9))
                .foregroundStyle(Theme.faint)
            if let target = jumpTarget, target.isJumpable {
                Spacer(minLength: 4)
                jumpChip(to: target, help: jumpHelp)
            }
        }
    }

    /// Compact Jump, copied from the attention tile rather than the row rail —
    /// a 38pt pill does not fit a 220pt column. Absent, never disabled:
    /// `isJumpable` is the daemon's `canJump` or a terminal Dark Army itself hosts,
    /// and every draw site checks it before drawing this.
    private func jumpChip(to row: Agent, help: String) -> some View {
        let key = "\(row.id)|\(help)"
        let lit = jumpHover || jumpFocus == key
        return Button {
            jump(to: row)
        } label: {
            Image(systemName: "arrow.up.forward")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(lit ? Theme.phosphorBright : Theme.faint)
                .frame(width: 16, height: 16)
                .background(Theme.phosphor.opacity(lit ? 0.16 : 0.08))
                .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .clickable()
        .focused($jumpFocus, equals: key)
        .modifier(ClaimsKeyboardFocus(focused: jumpFocus == key))
        .onHover { jumpHover = $0 }
        .accessibilityLabel(help)
    }

    // MARK: - Choosing the assistant

    /// The assistant, chosen on the card itself rather than only inside the
    /// editor. It is the one field that decides whether the card can be started
    /// at all, and a person deciding "codex should take this one" should not
    /// have to open a form to say so — especially now that the drop into In
    /// progress reads it.
    @ViewBuilder
    private var toolControl: some View {
        let chipOnly = ProviderChoice.wordChipOnly(tools: chrome.tools,
                                                   selected: card.tool)
        if canRetool {
            if chipOnly {
                // A row that cannot show the truth hands back to the word
                // that can — and only for the two dishonest cases: nothing
                // on offer, or a card naming an assistant no longer offered.
                // Inert rather than a menu: a menu kept alive for a
                // hypothetical fourth assistant is dead code the day it ships.
                chip(toolChipLabel, muted: card.tool.isEmpty)
                    .help("Dark Army offers no assistant to switch to, or this "
                          + "card names one it no longer offers — choose again "
                          + "in the card's detail window.")
            } else {
                ProviderSwitch(tools: chrome.tools, installed: chrome.installed,
                               selected: card.tool,
                               interactive: true,
                               showsNobody: false,
                               pick: setTool,
                               // The panel's key monitor stands aside while
                               // the row holds the keyboard. Reported with
                               // this card's identity: another tile's
                               // disappearance must not release this one.
                               onFocusChange: { now in
                                   state.noteSwitcherFocus(card: card.id,
                                                           focused: now)
                               })
                    // An image-only view has no text baseline, so it sinks in
                    // the title row's `.firstTextBaseline` HStack unless it
                    // says where its own baseline is.
                    .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
            }
        } else if !card.tool.isEmpty {
            // A record of what ran, not a setting. The model left the card
            // face with the menu; the hover text still names it.
            if let mark = ProviderSwitch.recordMark(selected: card.tool) {
                ProviderSwitch(tools: [mark],
                               selected: mark,
                               interactive: false,
                               showsNobody: false)
                    .help(toolChipLabel)
                    .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
            } else {
                chip(toolChipLabel)
            }
        }
    }

    /// `claude`, or `claude · opus` once the card names a model. Both branches
    /// of `toolControl` read it, so the badge says the same thing before and
    /// after a session binds — the menu is what goes away, not the record of
    /// what ran.
    private var toolChipLabel: String {
        BoardCardView.toolChipLabel(tool: card.tool, model: card.model)
    }

    /// The label rule, pure so a test can hold it. `""` for the tool is the
    /// unassigned placeholder and a model beside it would be a claim about
    /// nobody.
    ///
    /// The model's own `<tool>-` prefix is dropped, because the chip has just
    /// said it: a pinned id on a 260pt card came out as
    /// `claude · claude-opus-5[1m]`, whose first two thirds are the same word
    /// twice. Display only — the stored value and everything sent to the
    /// daemon is the full id.
    ///
    /// **Only when a family name survives the cut**, which is why the rest is
    /// required to start with a letter: `claude-opus-5` → `opus-5` still says
    /// which model it is, while `grok-4.5` → `4.5` is a bare version number
    /// that names nothing, and losing the word is worse than repeating it.
    static func toolChipLabel(tool: String, model: String) -> String {
        if tool.isEmpty { return "assign…" }
        guard !model.isEmpty else { return tool }
        var shown = model
        if shown.hasPrefix(tool + "-") {
            let rest = shown.dropFirst(tool.count + 1)
            if rest.first?.isLetter == true { shown = String(rest) }
        }
        return tool + " · " + shown
    }

    /// What the *fleet* says the session is doing. Read off the agents snapshot
    /// and never re-derived, so the board and the menu-bar strip cannot disagree.
    ///
    /// **Nothing here is drawn on a Done card**, and the gate is hoisted to one
    /// `isDone` rather than repeated per arm so the next status line added
    /// inherits it. Both arms need it and only one of them used to have it. The
    /// ended arm *offers a choice* — "mark done or send it back" — which is
    /// nonsense under a card already in Done. The live arm is the worse half
    /// and it is the steady state, not an edge: `/ship` calls `dark_army_close_card`
    /// while the session goes on running for the rest of the turn, so a closed
    /// card would sit in Done reading "closed by …", the note, and
    /// `Gil — working`; and the moment that session ends a turn, `Gil — needs
    /// you` in red beneath a card the board has just said is finished. A Done
    /// card must not assert that anything is outstanding. What the session is
    /// doing *now* is a fact about the session, not about the card, and the
    /// fleet view is where it belongs once the card is closed.
    @ViewBuilder
    private var liveState: some View {
        let isDone = card.column == BoardColumn.done.rawValue
        if isStarting || card.isDispatching {
            HStack(spacing: 5) {
                AgentChatterView(.caret, wait: .starting, seed: card.id,
                                 spoken: "Starting this card")
                    .id(card.id)
                Text("starting — waiting for the session to appear")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            }
        } else if isRefining {
            // The card stays in Prep for the whole refinement — the plan
            // arriving is what moves it — so this line is the only thing that
            // says a session is at work on it. Drawn from `refineState`,
            // never derived: the daemon owns the refinement's liveness.
            HStack(spacing: 5) {
                AgentChatterView(.caret, wait: .refining, seed: card.id,
                                 spoken: "Refining this card")
                    .id(card.id)
                Text(card.refineState == "live"
                     ? "refining — answer the interview in the terminal"
                     : "refining — waiting for the session to appear")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                // Keyed off the *resolved* refiner, never off `isRefining`:
                // the optimistic/dispatching phase has no session yet, so a
                // chip drawn from the phase would target nothing.
                if let r = refinerAgent?.agent, r.isJumpable {
                    Spacer(minLength: 4)
                    jumpChip(to: r,
                             help: "Jump to the session refining this card")
                }
            }
        } else if isDone {
            // Deliberately nothing. See the note above.
            EmptyView()
        } else if card.sessionEnded {
            // Never moved to Done by Dark Army. A session ending says nothing about
            // whether the work was finished. Where the "SESSION ENDED · NO
            // REPORT" band is drawn above, it states the fact and this line
            // keeps only the instruction and the jump chip. The band is gated
            // to In progress, so everywhere else — a card hand-dragged back to
            // Backlog still carries its dead link, since a plain drag never
            // clears it, only `board_reset` does — this line keeps the old
            // prefix. The instruction must never be the only thing on a card
            // whose stated fact lives nowhere.
            HStack(spacing: 4) {
                Text(endedBannerDrawn
                     ? "mark done or send it back"
                     : "session ended — mark done or send it back")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                // The daemon's `can_jump` is the authority — a tombstoned
                // session it still says is jumpable is jumpable.
                if let row = agent?.agent, row.isJumpable {
                    Spacer(minLength: 4)
                    jumpChip(to: row,
                             help: "Jump to the session working on this card")
                }
            }
        } else if let row = agent?.agent, let bucket = agent?.bucket {
            HStack(spacing: 4) {
                PixelMark(character: Cast.character(for: row),
                          state: liveMarkState(bucket),
                          size: 16)
                Text(liveLabel(row, bucket))
                    .font(Theme.mono(10))
                    .foregroundStyle(bucket == "waiting" ? Color.red : Theme.faint)
                if row.isJumpable {
                    Spacer(minLength: 4)
                    jumpChip(to: row,
                             help: "Jump to the session working on this card")
                }
            }
        }
    }

    /// **A row in the `finished` bucket has not necessarily finished.** That
    /// bucket also holds sessions merely quiet past `FINISHED_IDLE_GRACE_SECONDS`,
    /// flagged `alive` — which is exactly why `_reconcile_board` consults the
    /// flag before calling a card's session ended. Reading the bucket name alone
    /// here would have the daemon correctly keeping the card live while the card
    /// told the user its session had finished. `AgentRowView.endReason` already
    /// gates on `alive != true`; this is the same test, not a third convention.
    private func liveLabel(_ row: Agent, _ bucket: String) -> String {
        let who = row.nickname.isEmpty ? String(row.sessionId.prefix(8)) : row.nickname
        switch bucket {
        case "running": return "\(who) — working"
        case "waiting": return "\(who) — needs you"
        case "sleeping": return "\(who) — quiet"
        default: return row.alive == true ? "\(who) — quiet" : "\(who) — finished"
        }
    }

    private func liveMarkState(_ bucket: String) -> Cast.State {
        Cast.state(forBucket: bucket)
    }

    @ViewBuilder
    private var actions: some View {
        if (BoardColumn(rawValue: card.column) ?? .backlog) == .prep {
            prepActions
        } else if mergeOffered || fixOffered || reviewOffered || isMerging {
            // A Done card with a branch to land: the merge verbs take a
            // line of their own beneath the usual ones, or the row wraps
            // mid-word (`prepActions`' reason).
            VStack(alignment: .leading, spacing: 6) {
                otherActions
                mergeActions
            }
        } else {
            otherActions
        }
    }

    /// MERGE (armed, then confirmed), Fix (armed, then confirmed) and Run
    /// review (one press), all dim words: `CardActionWeight` names no verb
    /// for Done, and an absent primary promotes nothing.
    @ViewBuilder
    private var mergeActions: some View {
        HStack(spacing: 8) {
            if mergeOffered {
                dimVerb(CardMerge.mergeLabel(mergeState: card.mergeState,
                                             armed: isMergeArmed),
                        armed: isMergeArmed) {
                    if isMergeArmed {
                        state.disarm()
                        merge()
                    } else {
                        state.armMerge(card.id)
                    }
                }
            } else if isMerging {
                Text(CardMerge.mergeLabel(mergeState: "merging", armed: false))
                    .foregroundStyle(Theme.dim)
            }
            if fixOffered {
                dimVerb(CardMerge.fixLabel(armed: isFixArmed), armed: isFixArmed) {
                    if isFixArmed {
                        state.disarm()
                        fixMerge()
                    } else {
                        state.armFix(card.id)
                    }
                }
            }
            if reviewOffered {
                dimVerb("Review", armed: false) {
                    state.disarm()
                    runReview()
                }
            }
            Spacer(minLength: 0)
        }
        .font(Theme.mono(10))
    }

    /// A dim word that is really a button, `weighted`'s dim branch for a
    /// verb `CardActionWeight` does not name. Not built on `small`, which
    /// disarms before it acts.
    private func dimVerb(_ title: String, armed: Bool,
                         _ action: @escaping () -> Void) -> some View {
        DimVerbFocus { hovered, focused in
            Button(title, action: action)
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(armed ? Color.orange : Theme.dim)
                .underline(hovered || focused,
                           color: armed ? Color.orange : Theme.dim)
                .clickable()
        }
    }

    /// Prep carries the widest pair of buttons on the board — **Refine**,
    /// outlined, beside **START** — and a card is only a column wide. Crammed
    /// onto one line beside Details and Delete they wrapped mid-word ("Ref /
    /// ine"), so here the two verbs take a line of their own and the small
    /// neutral controls sit under them, the auto tick beneath the Refine it
    /// belongs to.
    @ViewBuilder
    private var prepActions: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                // Refine leads: it is the column's own verb, and the one
                // `CardActionWeight` outlines here. Start and HERE survive
                // beside it as dim words because the gate is a confirmation,
                // not a wall — the armed label says what a press would skip.
                if canRefine { refineButton }
                if canStart { startButton }
                if canSpawnHere { startHereButton }
                Spacer(minLength: 0)
            }
            HStack(spacing: 8) {
                small("Details") { openDetail() }
                // Gated on `canRefine` as well as the marker, so the tick
                // appears exactly where a Refine could still happen and
                // disappears with it: there is nothing to arm on a card whose
                // plan has already landed.
                if canRefine {
                    startWhenPlannedTick
                }
                if card.holdsBatchMark { small("LEAVE BATCH") { leaveBatch() } }
                Spacer(minLength: 0)
                deleteButton
            }
        }
        .font(Theme.mono(10))
    }

    @ViewBuilder
    private var otherActions: some View {
        HStack(spacing: 8) {
            small("Details") { openDetail() }
            Spacer(minLength: 0)
            switch BoardColumn(rawValue: card.column) ?? .backlog {
            case .prep:
                EmptyView()
            case .backlog:
                if canStart { startButton }
                if canSpawnHere { startHereButton }
                // LEAVE BATCH is drawn below the switch, for every column.
            case .inProgress:
                // Done is the row's outlined verb, so it leads; Back keeps
                // its place beside it and nothing takes a second line.
                doneButton
                small("Back") { move(to: .backlog) }
                // A card can sit here with nothing progressing it — Dark Army's
                // launcher was off when it was dropped, or the run it had was
                // sent back. Without this the only way to start it is to drag
                // it out and drag it in again, which reads as the board having
                // forgotten how.
                if canStart { startButton }
                if canSpawnHere { startHereButton }
            case .done:
                small("Reopen") { move(to: .backlog) }
                if canPromote { small("Promote") { promote() } }
            }
            // A card still carrying a batch mark with no session of its
            // own, in whatever column a drag left it: its one way to a
            // Start of its own is leaving the batch (`board_reset`, which
            // clears the mark) — the words the daemon's refusal names.
            if card.holdsBatchMark { small("LEAVE BATCH") { leaveBatch() } }
            deleteButton
        }
        .font(Theme.mono(10))
    }

    /// Whether pressing **Done** would also clear a session. Moving a card into
    /// Done wraps up the session bound to it, which types `/clear` — so this is
    /// the same pair the daemon's own gate reads off the card (`link_state ==
    /// "live"` with a session id), and nothing more: a card with no live
    /// session has nothing to destroy and keeps its plain single click.
    ///
    /// Read off the card rather than off the agents snapshot on purpose. The
    /// daemon re-checks this at the moment of the write, against the row, and a
    /// second source here would be a gate that disagrees with the real one.
    ///
    /// `dispatching` arms too, and that is the whole reason this is not simply
    /// the daemon's own test. A card is stamped `live` by the bind and the panel
    /// learns it one frame later, so a gate that waited for `live` would draw an
    /// *unarmed* Done for the frame in between — and the daemon, re-reading the
    /// row at the write, would find `live` and clear a session on a single
    /// click. The safe direction for a gate that lags its subject is to
    /// over-arm: an arm on a card that turns out to have nothing to clear costs
    /// one extra press, the other way costs a conversation.
    ///
    /// Codex never arms, because the daemon's matching rung returns silently
    /// there — Dark Army cannot type into a Codex session. A confirm step warning
    /// about an act that cannot happen is the inverse of the rule against a
    /// button that is present and inert.
    private var doneClears: Bool {
        guard card.tool != "codex" else { return false }
        return card.isDispatching || (card.linkState == "live" && !card.sessionId.isEmpty)
    }

    /// **Done is a destructive verb now**, on the cards where it reaches a live
    /// session: it moves the card *and* types `/clear` into that session, which
    /// has no undo. Unarmed, it was the one press on this board that could
    /// destroy a conversation by a slip — while `START` and `Delete` either
    /// side of it both arm first. In progress it wears the red outline as the
    /// row's primary (`CardActionWeight`), which is weight, not a gate: the
    /// arm below is still what stands between a click and `/clear`.
    ///
    /// The drag into Done is deliberately still unarmed, and that is not an
    /// inconsistency: `startButton` states the argument and it is unchanged
    /// here — carrying a card across the window is a deliberate gesture, where
    /// a single click is the one thing a slipped hand produces. This arms the
    /// *click*, which is the gesture that argument does not cover.
    ///
    /// The armed label says what it will do rather than "Really?", because the
    /// half a person needs warning about is the half the button does not name.
    private var doneButton: some View {
        weighted(.done, armed: isDoneArmed) {
            if doneClears {
                Button(isDoneArmed ? "Done & clear?" : "Done") {
                    if isDoneArmed {
                        state.disarm()
                        move(to: .done)
                    } else {
                        state.armDone(card.id)
                    }
                }
            } else {
                Button("Done") {
                    state.disarm()
                    move(to: .done)
                }
            }
        }
    }

    /// Armed, then confirmed — the gate Stop, Retire and Wrap up already use, and
    /// for their reason plus one: this spends money and opens a window on the
    /// screen. The armed state lives in `BoardState`, not here, so there is one
    /// copy of it.
    ///
    /// The drag into In progress does the same thing without arming, and that is
    /// not an inconsistency: carrying a card across the window *is* a deliberate
    /// gesture, where a single click is the one thing a slipped hand produces.
    private var startButton: some View {
        weighted(.start, armed: isArmed, enabled: !isStarting) {
            Button(isArmed ? (startsUnplanned ? "Start unplanned?" : "Really start?")
                           : "START") {
                if isArmed {
                    state.disarm()
                    // The confirmed press *is* the plan gate's confirmation on
                    // this route, so it carries the flag — no popup stacked on
                    // an arm; two gates in a row is ceremony, not safety.
                    start(skipPlanGate: startsUnplanned)
                } else {
                    state.arm(card.id)
                }
            }
        }
    }

    /// Spawn a terminal Dark Army itself opens, on a card that has none
    /// connected. Same arm-then-confirm as Start — it spends money and
    /// opens a window — and a separate slot so arming Start cannot fire
    /// this, or the other way around.
    private var startHereButton: some View {
        weighted(.startHere, armed: isArmedHere, enabled: !isStarting) {
            Button(isArmedHere
                   ? (startsUnplanned ? "Start unplanned here?" : "Really here?")
                   : "HERE") {
                if isArmedHere {
                    state.disarm()
                    start(skipPlanGate: startsUnplanned, ownTerminal: true)
                } else {
                    state.armHere(card.id)
                }
            }
            .help("Start this assistant on a terminal Dark Army itself opens")
            .accessibilityLabel("Start on Dark Army's own terminal")
        }
    }

    /// The Prep column's verb: open a planning session on this card. It is
    /// the column's outlined primary (`CardActionWeight`) — it spends money
    /// and opens a window — but no arm: the session it opens is an
    /// *interview*, which asks before it does anything, so a slipped click
    /// costs a terminal and a question rather than anything irreversible.
    private var refineButton: some View {
        weighted(.refine, enabled: !isRefining) {
            Button("Refine") {
                state.disarm()
                refine()
            }
        }
    }

    /// "Start the work by itself the moment the plan lands." Unarmed: setting
    /// it destroys nothing and the undo is pressing it again. It arms Dark Army to
    /// take the *ordinary* Start route later — every guard that route runs
    /// still runs — and it is spent on that attempt, so it clears itself once
    /// Dark Army has acted.
    private var startWhenPlannedTick: some View {
        small(card.startWhenPlanned ? "[x] auto" : "[ ] auto") {
            setStartWhenPlanned(!card.startWhenPlanned)
        }
    }

    private func setStartWhenPlanned(_ on: Bool) {
        state.refusals[card.id] = ""
        Task { @MainActor in
            let result = await client.boardStartWhenPlanned(card.id, on: on)
            if !result.ok { state.refusals[card.id] = result.detail }
            await client.refresh()
        }
    }

    /// The only irreversible thing on this board: the store archives nothing, so
    /// a deleted card is gone and whatever somebody typed into it is gone with
    /// it. Hence the same arm-then-confirm the destructive session verbs use,
    /// and hence red rather than the neutral row of small buttons beside it.
    private var deleteButton: some View {
        Button(isDeleteArmed
               ? (card.deleteClosesTerminal ? "Delete & close terminal?" : "Really delete?")
               : "Delete") {
            if isDeleteArmed {
                state.disarm()
                delete()
            } else {
                state.armDelete(card.id)
            }
        }
        .buttonStyle(.plain)
        .controlSize(.small)
        .foregroundStyle(isDeleteArmed ? Color.red : Theme.dim)
        .clickable()
        .reportsKeyboardFocus()
    }

    private func chip(_ text: String, muted: Bool = false) -> some View {
        Text(text)
            .font(Theme.mono(9, weight: .medium))
            .foregroundStyle(muted ? Theme.faint : Theme.phosphor)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
    }

    private func small(_ title: String, _ action: @escaping () -> Void) -> some View {
        Button(title) {
            state.disarm()
            action()
        }
        .buttonStyle(.plain)
        .controlSize(.small)
        .foregroundStyle(Theme.faint)
        .clickable()
        .reportsKeyboardFocus()
    }

    /// One primary per card, chosen by column (`CardActionWeight`): the
    /// column's verb wears the red outline, every other verb the dim words
    /// `small` uses. This is the one `AlarmOutline` on a card face. Not
    /// built on `small`, which disarms before it acts: an arm-then-confirm
    /// verb drawn through it would disarm and re-arm forever. Each branch
    /// carries its own `.clickable`, so a call site adds none.
    @ViewBuilder
    private func weighted<Content: View>(_ verb: CardActionWeight.Verb,
                                         armed: Bool = false,
                                         enabled: Bool = true,
                                         @ViewBuilder _ button: () -> Content) -> some View {
        if CardActionWeight.isPrimary(verb, column: card.column, kind: card.kind) {
            button()
                .buttonStyle(AlarmOutline(filled: true))
                .disabled(!enabled)
                .clickable(enabled)
                .reportsKeyboardFocus()
        } else {
            // A dim word that is really a button says so: the readable dim
            // ink, and an underline the moment the pointer or the keyboard
            // focus lands on it.
            // Built once here: `button` does not escape, the focus
            // wrapper's closure does.
            let verb = button()
            DimVerbFocus { hovered, focused in
                verb
                    .buttonStyle(.plain)
                    .controlSize(.small)
                    .foregroundStyle(armed ? Color.orange : Theme.dim)
                    .underline(hovered || focused, color: armed ? Color.orange : Theme.dim)
                    .disabled(!enabled)
                    .clickable(enabled)
            }
        }
    }

    // MARK: - Verbs

    private func openDetail() {
        state.openEditor(card, client: client)
    }

    /// Press-time re-check on purpose: `row` is a snapshot copy and the fleet
    /// moves between draw and press. The daemon's reveal refuses safely
    /// anyway; the guard keeps the silent-looking no-op path out.
    private func jump(to row: Agent) {
        guard row.isJumpable else { return }
        state.disarm()
        state.refusals[card.id] = ""
        if row.ownTerminal {
            // Same destination `Triage.jump` takes: the agent's full pane,
            // which is the live terminal. Never `reveal_session` — a 200
            // there used to post `.panelDidJump` and hide the panel that
            // holds the screen.
            NotificationCenter.default.post(
                name: .panelShowSession, object: row.sessionId)
            return
        }
        Task { @MainActor in
            let result = await client.act("reveal_session", session: row.sessionId)
            // A failed Jump leaves the screen exactly as it was, which is what a
            // dead button looks like — `Triage.jump`'s reasoning, and the reason
            // this does not discard `detail` the way `BoardCardSheet.jump` does.
            if result.ok {
                NotificationCenter.default.post(name: .panelDidJump, object: nil)
            } else {
                state.refusals[card.id] = result.detail
            }
        }
    }

    private func setTool(_ tool: String) {
        state.disarm()
        Task { @MainActor in
            // Only `tool` — the store clears a now-wrong model itself, so no
            // route can miss the clear and the panel keeps no bookkeeping of
            // its own about which model belongs to which assistant.
            let result = await client.boardUpdate(card.id, fields: ["tool": tool])
            state.refusals[card.id] = result.ok ? "" : result.detail
            await client.refresh()
        }
    }


    /// The three verbs are `BoardState`'s now, so the card window presses
    /// the same code as this tile (`startCard` / `refineCard` / `moveCard`);
    /// what stays here is the arm, the label and the layout.
    private func start(skipPlanGate: Bool = false, ownTerminal: Bool = false) {
        state.startCard(card, client: client, skipPlanGate: skipPlanGate,
                        ownTerminal: ownTerminal)
    }

    private func refine() {
        state.refineCard(card, client: client)
    }

    /// Every route back into a startable column clears the card's link —
    /// `BoardState.moveCard` states the rule and keeps it.
    private func move(to column: BoardColumn) {
        state.moveCard(card, to: column, client: client)
    }

    /// The Reviewed press. Unarmed on purpose — the codebase's stated test is
    /// reversibility-of-consequence, and this destroys nothing: the close
    /// signature stays drawn, the card stays in Done, and a slipped click
    /// costs a dropped highlight. Refusals (a card reopened under the press,
    /// an old daemon without the verb) land in the card's refusal line.
    private func review() {
        state.disarm()
        state.refusals[card.id] = ""
        Task { @MainActor in
            let result = await client.boardReview(card.id)
            if !result.ok { state.refusals[card.id] = result.detail }
            await client.refresh()
        }
    }

    /// Promote a finished scout into a Prep build card. Unarmed — it
    /// creates a card and starts nothing, and a second press is refused
    /// in words. No reveal: `post` discards a 200 body, so the new id
    /// is not in hand; the new card is the newest in Prep.
    private func promote() {
        state.disarm()
        state.refusals[card.id] = ""
        Task { @MainActor in
            let result = await client.boardPromote(card.id)
            if !result.ok { state.refusals[card.id] = result.detail }
            await client.refresh()
        }
    }

    /// MERGE, confirmed: land the card's branch on the local main line. The
    /// daemon answers at once and works in the background, so a refusal in
    /// words is the only thing that comes back here; the progress and the
    /// result ride the card's `merge_line`.
    private func merge() {
        state.refusals[card.id] = ""
        Task { @MainActor in
            let result = await client.boardMerge(card.id)
            if !result.ok { state.refusals[card.id] = result.detail }
            await client.refresh()
        }
    }

    /// Fix, confirmed: start the card's assistant in the card's folder.
    private func fixMerge() {
        state.refusals[card.id] = ""
        Task { @MainActor in
            let result = await client.boardMergeFix(card.id)
            if !result.ok { state.refusals[card.id] = result.detail }
            await client.refresh()
        }
    }

    /// Run review. Unarmed, `promote`'s shape: it changes no files, and a
    /// second press is refused in words while one is running.
    private func runReview() {
        state.refusals[card.id] = ""
        Task { @MainActor in
            let result = await client.boardReviewRun(card.id)
            if !result.ok { state.refusals[card.id] = result.detail }
            await client.refresh()
        }
    }

    /// Take a waiting card out of its batch: the existing reset, which
    /// clears the batch mark with the link (the daemon's `reset_card`).
    private func leaveBatch() {
        clearError()
    }

    private func clearError() {
        Task { @MainActor in
            let result = await client.boardReset(card.id)
            state.refusals[card.id] = result.ok ? "" : result.detail
            await client.refresh()
        }
    }

    private func delete() {
        let id = card.id
        state.deleting.insert(id)
        state.refusals[id] = ""
        Task { @MainActor in
            let result = await client.boardDelete(id)
            if !result.ok {
                // The card is staying, so the fade and the spinner have to go
                // with it, or a refusal reads as a delete that is still running.
                state.deleting.remove(id)
                state.refusals[id] = result.detail
            }
            await client.refresh()
        }
    }
}

/// Hover and keyboard focus for one dim card verb, handed to the closure
/// that draws it, so `weighted` can underline a dim word the moment the
/// pointer or the focus lands on it. One per verb: a shared flag would
/// underline every verb on the card at once.
private struct DimVerbFocus<Content: View>: View {
    @ViewBuilder let content: (_ hovered: Bool, _ focused: Bool) -> Content
    @State private var hovered = false
    @FocusState private var focused: Bool

    var body: some View {
        content(hovered, focused)
            .focused($focused)
            .onHover { hovered = $0 }
            // The claim, from this wrapper's own focus: a second `.focused`
            // on one view would split it.
            .modifier(ClaimsKeyboardFocus(focused: focused))
    }
}
