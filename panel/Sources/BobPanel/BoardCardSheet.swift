import AppKit
import CryptoKit
import SwiftUI
import UniformTypeIdentifiers

/// The whole of one card, on one screen: everything it holds, everything you can
/// change about it, the plan it points at, and the session doing it.
///
/// **One surface for reading and for editing**, not two. The card in the column
/// is a summary by construction — a title, a sentence and four clipped lines of
/// prompt — and before this the only thing that showed more was an editor that
/// replaced the card *in place* inside its column, in a 260pt-wide strip, with
/// the instructions in a 90pt text box. So the answer to "what does this card
/// actually say?" was to squint at a form. A sheet is the shape the platform
/// already uses for "this one thing, in full", and it has room for the three
/// things a card could never show: the prompt whole, the document the prompt
/// points at, and the assistant that is working on it.
///
/// **It is a window, not a sheet** (30 Aug 2026). The type and file keep the
/// old name — see the note on the frame below — but the presentation is
/// `CardWindowController`'s single reused `NSWindow`, which is what lets a
/// half-typed card be minimised to the Dock and left there while Dark Army is used
/// for something else.
///
/// The composer is the same view with `card == nil`. Two code paths for
/// "describe a piece of work" is how the two drift.
struct BoardCardSheet: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: BoardState
    /// `nil` in composer mode — `isComposer` says so explicitly rather than
    /// being inferred, because a card can also legitimately be `nil` for a
    /// moment when the snapshot that would carry it has not arrived.
    let card: BoardCard?
    let isComposer: Bool

    /// Which referenced document is open in the reader, and what it said.
    @State private var openedPath: String?
    @State private var openedText = ""
    /// The attached plan's text, loaded once per `planPath` (`.task(id:)`)
    /// rather than read on every body evaluation — the sheet redraws per
    /// snapshot and a 64 KB read per frame on the main actor is the habit
    /// this codebase keeps removing. nil while there is nothing to show;
    /// `planMissing` distinguishes "no plan" from "the card names one and it
    /// is not there", which must be words on screen, never a blank pane.
    @State private var planText: String?
    /// Whether the plan's technical half is unfolded. View state only —
    /// reset in `loadPlan()` so every open and every card swap in the
    /// reused window starts at the plain half.
    @State private var planDetailOpen = false
    /// Which folded sections the person has opened on *this* card. Screen
    /// state and nothing more — `CardSections` decides what is open by
    /// default per stage, this holds only the presses on top of it, and it
    /// is cleared on a card swap and on a stage change so every open starts
    /// back at the rule. Never banked, never sent.
    @State private var openedSections: Set<CardSections.Section> = []
    @State private var planMissing = false
    /// A scout's report, read once per path like `planText`, through the
    /// same containment and bounded read. `reportMissing` is the card
    /// naming a report that is not there — words on screen, never a blank.
    @State private var reportText: String?
    @State private var reportMissing = false
    /// The check file a card was flagged with, read once per path like
    /// `reportText`; `manualCheckMissing` is the card naming a file that is
    /// not there. `manualNote` is the Passed / Failed note being typed.
    @State private var manualCheckText: String?
    @State private var manualCheckMissing = false
    /// The daemon's words for a check file it would not serve (moved,
    /// edited out of shape, outside the project's folder). With it, Mark
    /// checked comes back so the badge always has a way off.
    @State private var manualCheckReason = ""
    /// The read itself failed (no answer, a refusal before the token
    /// loaded, a reply that would not decode) — not the daemon refusing the
    /// file, so Passed / Failed stay and the line says the read failed.
    @State private var manualCheckFetchFailed = false
    @State private var manualNote = ""
    @State private var manualRecording = false
    /// SHA-256 of the plan text drawn above, computed once beside the read.
    /// It is what an Approve press echoes to the daemon, so the approval is
    /// of the wording actually on screen — the panel never approves a file it
    /// has not itself read.
    @State private var planDigest = ""
    @State private var approving = false
    /// The message being typed at the session doing this card.
    @State private var message = ""
    @State private var sending = false
    @State private var sendNote = ""
    /// Whether the Send-a-message field is disclosed. The button *is* the
    /// deliberate gesture, which is why there is no arm-then-confirm here:
    /// arm-then-confirm in this app guards acts that destroy something —
    /// Stop, Retire, `/clear`, Delete, Clear Done — and a message is not one.
    @State private var messageOpen = false
    @State private var thread: [CardMessage] = []
    @State private var runReport: SessionRecordReport?
    @State private var runFetchFailed = false
    /// What Dark Army observed the last run do. Normally arrives with the card
    /// fetch the sheet already makes; the client route is the fallback for a
    /// reply that carried none.
    @State private var workRecord: WorkRecord?
    /// The card's timeline, fetched once per open and per stage change off
    /// the same single-card read the work record rides (`loadTimeline`).
    /// Nil until it lands and nil from an older daemon, which is what draws
    /// the TIMELINE row absent.
    @State private var timeline: CardTimelineReport?
    /// The file whose changes are open, by its index in the record's own
    /// list — an index, never a path, which is the whole containment story.
    @State private var openFile: Int?
    @State private var fileDiff: WorkRecordDiff?
    /// The CHANGES section's page: the card's branch against the main line,
    /// fetched when the section opens (never on a poll), and the one file
    /// whose changes are open, by its index in that page's own list.
    /// `changesFailed` is the read itself failing, not the daemon saying no.
    @State private var changes: CardChangesReport?
    @State private var changesFailed = false
    @State private var changeFile: Int?
    @State private var changeDiff: CardChangeDiff?
    /// The revision the store reported inside a `CARD_CHANGED_REFUSAL`, or
    /// nil. While it is set the sheet offers **Save anyway** at exactly that
    /// number and nothing else; cleared by a successful save or by Leave it.
    @State private var cardChangedAt: Int?
    @State private var deleteArmed = false
    @State private var preparing = false
    @State private var prepareNote = ""
    /// The root Prepare last suggested, or `""` for "no question on screen".
    /// View state and nothing more: it is on no card, no store column, no
    /// snapshot and in no banked draft, so a resumed composer never carries a
    /// stale opinion. Cleared the moment the question is answered — by
    /// accepting it, by keeping the current folder, or by the person picking
    /// for themselves.
    @State private var suggestedRoot = ""
    /// The root the picker held before Prepare's suggestion was applied,
    /// so the line under it can offer one way back. View state only, like
    /// `suggestedRoot`: on no card and in no banked draft.
    @State private var revertRoot = ""
    @State private var attachNote = ""
    @State private var revealReason: ComposerPhase.Reveal = .resumed
    @FocusState private var ideaFocused: Bool
    @FocusState private var titleFocused: Bool
    @FocusState private var summaryFocused: Bool
    @FocusState private var promptFocused: Bool
    @FocusState private var workflowFocused: Bool
    @FocusState private var messageFocused: Bool

    private var board: Board { client.snapshot.board }

    /// The saved-card map uses the main panel's complete reveal path, including
    /// closing an agent detail that would otherwise cover the board.
    func showCollaborationCard(_ target: BoardCard) {
        let current = board.cards.filter { $0.id == target.id && $0.root == target.root }
        guard current.count == 1 else { return }
        NotificationCenter.default.post(name: .panelShowCard, object: current[0])
        state.closeEditor()
    }

    /// The card as the *snapshot* currently has it, which is not the draft: the
    /// draft is what somebody is typing and this is what the daemon holds. The
    /// session state, the link and the dispatch error are all read from here.
    private var live: BoardCard? {
        guard let card else { return nil }
        return board.cards.first { $0.id == card.id } ?? card
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // The composer keeps its one unfolded form and reads no
                    // rule; a saved card is drawn through `CardSections`'
                    // order for its stage: the lead five — title, summary,
                    // the assistant row, where it stands, the one next
                    // action — then one MORE row with everything else
                    // behind it, Delete always last.
                    if isComposer {
                        fields
                    } else if live != nil {
                        identityLine
                        ForEach(CardSections.order(for: stage), id: \.self) { s in
                            foldedSection(s)
                        }
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
                .dropDestination(for: URL.self) { urls, _ in
                    guard isComposer, !state.composerSaving else { return false }
                    attach(urls: urls)
                    return true
                }
            }
            Divider()
            footer
        }
        // The type name is a leftover: this view lives in its own ordinary
        // window now (`CardWindow.swift`), not in a panel-attached sheet. A
        // rename would touch every call site and test for zero behaviour, so
        // this note stands in for one. The fixed frame became a floor with
        // the move — a window the user can resize must be honest about it.
        .frame(minWidth: CardWindowMetrics.minWidth,
               minHeight: CardWindowMetrics.minHeight)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        // Keyed on the path, not the card: an attach landing while this sheet
        // is open changes `planPath` and must reload the pane.
        .task(id: live?.planPath ?? "") { loadPlan() }
        // Keyed on the report path for the same reason: the scout's attach
        // lands while the sheet is open, and the REPORT section must fill.
        .task(id: live?.reportPath ?? "") { loadReport() }
        .task(id: live?.manualCheckPath ?? "") { await loadManualCheck() }
        .task(id: threadFetchKey) { await loadThread() }
        .task(id: live?.sessionId ?? "") { await loadRunRecord() }
        .task(id: workRecordFetchKey) { await loadWorkRecord() }
        // Keyed on the card, whether CHANGES is open, and the moments its
        // page can go stale (a merge landing, a verdict arriving): fetched
        // when the section opens and nowhere else.
        .task(id: changesFetchKey) { await loadChanges() }
        // Keyed on the card and its stage — never the snapshot's `revision`,
        // or every Save would refetch. One structured task, so a swap or a
        // column move cancels the request still in flight for the last card.
        .task(id: timelineFetchKey) { await loadTimeline() }
        // The card window is *reused*: `CardWindowController` follows the one
        // `BoardState.editing` slot and swaps this view's card rather than
        // opening a second window, so `@State` survives the swap. Left alone,
        // a half-typed message for card A stays in an open box on card B and
        // one click types it into B's terminal. Cleared here for the same
        // reason `threadFetchKey` exists.
        .onChange(of: live?.id ?? "") {
            messageOpen = false
            message = ""
            sendNote = ""
            openedSections = []
        }
        // A card that moves to another column while its window is up starts
        // back at the rule for where it is now.
        .onChange(of: stage) {
            openedSections = []
        }
        // `message` is shared between the two acts that occupy this corner —
        // typing at the session working the card, and asking a helper — and
        // `messagesTerminal` can flip under the person's hand: the session
        // ends, or a question opens on its input line. Text typed for the
        // terminal must not survive that flip into the Ask field, where the
        // next press would start a helper session with it. Same reasoning as
        // the card swap above, one rung in.
        .onChange(of: messagesTerminal) {
            messageOpen = false
            message = ""
            sendNote = ""
        }
        .onAppear {
            // New card opens with no first responder. Holding right Command
            // then hits MacWhisper's noActiveTextFieldFound and types nowhere.
            let s = client.context.settings
            DictationFocus.logGate(
                whisper: s.macwhisperInstalled,
                trusted: s.accessibilityTrusted,
                label: s.dictationShortcutLabel)
            guard isComposer else { return }
            summaryFocused = true
            DispatchQueue.main.async {
                summaryFocused = true
                DictationFocus.promote(reason: "new-card")
            }
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 10) {
            Text(isComposer ? "New card" : "Card")
                .font(Theme.mono(13, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            if let live, !isComposer {
                Text(columnLabel(live.column))
                    .font(Theme.mono(10, weight: .medium))
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
                if live.isAgentAuthored {
                    let name = live.authorName.isEmpty ? live.author : live.authorName
                    HStack(spacing: 6) {
                        AvatarView(character: Cast.character(forNickname: name),
                                   state: .sleep, size: 20)
                        Text("written by \(name)")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    private func columnLabel(_ raw: String) -> String {
        (BoardColumn(rawValue: raw) ?? .backlog).title
    }

    // MARK: - Which sections lead, and which fold

    /// Where this card is in its life, read off the snapshot's copy. The
    /// composer never asks; a card with no live copy yet reads as Backlog.
    private var stage: CardSections.Stage {
        guard let live else { return .backlog }
        return CardSections.stage(column: live.column,
                                  linkState: live.linkState,
                                  manualCheckDue: live.manualCheckDue,
                                  runActive: live.runActive)
    }

    /// The paths the prompt and summary point at, as `documents` resolves
    /// them — computed once here so the fold row's count and the section's
    /// presence read the same list.
    private var documentPaths: [String] {
        BoardDocuments.references(in: state.draft.prompt + "\n" + state.draft.summary
                                    + "\n" + (live?.reportPath ?? ""),
                                  root: state.draft.root)
    }

    /// What a closed row may say, all of it already on the card or already
    /// fetched by this view for the section behind the row.
    private var foldFacts: CardSections.Facts {
        var f = CardSections.Facts()
        guard let live else { return f }
        if let head = live.workRecord {
            f.files = head.files
            f.filesTotal = head.filesTotal
            f.filesAvailable = head.filesAvailable
            f.hasReport = head.report
        }
        f.planAttached = !live.planPath.isEmpty
        f.planApproved = !live.planApproved.isEmpty
        f.planMissing = planMissing
        f.reportAttached = !live.reportPath.isEmpty
        f.isScout = live.isScout
        f.messages = max(thread.count, live.threadCount)
        f.attachments = live.attachments.count
        f.documents = documentPaths.count
        f.manualNote = live.needsManualCheck
        f.crewStages = live.workflow.count
        f.dependencies = live.dependencies.count
        if let report = timeline, live.createdAt > 0 {
            f.age = CardTimeline.elapsed(report.generatedAt - live.createdAt)
        }
        f.hidden = CardSections.hidden(for: stage).filter { hasContent($0) }.count
        return f
    }

    /// Whether a section has anything to draw on this card — each existing
    /// section var's own presence gate, restated here so a fold row is never
    /// a chevron that opens nothing. The Mac's instructions and crew live
    /// inside `fields`, so those two sections have no body of their own.
    private func hasContent(_ s: CardSections.Section) -> Bool {
        guard let live else { return false }
        switch s {
        case .status: return true
        case .manualCheck: return live.needsManualCheck
        case .closeSignature: return live.isAgentClosed
        case .objective: return true
        case .verbs: return nextAction != .none
        case .otherVerbs:
            return canRefine(live) || (reach.canStart && nextAction != .start)
                || canPromote(live) || mergeControls(live)
        case .collaboration: return client.snapshot.collaboration != nil
        case .more: return CardSections.hidden(for: stage).contains { hasContent($0) }
        case .plan: return !live.planPath.isEmpty
        case .report: return live.isScout
        case .workRecord: return live.workRecord != nil
        case .changes: return showsChanges(live)
        case .run: return !live.sessionId.isEmpty
        case .editor: return true
        case .instructions, .crew: return false
        case .session:
            return !live.sessionId.isEmpty
                || (live.refineState == "live" && !live.refineSessionId.isEmpty)
                || (live.isAgentAuthored && agent(for: live.author) != nil)
        case .thread: return true
        case .queue: return live.isQueued
        // Something to show, or somewhere to add one: a finished card with
        // no links offers nothing, since nothing waits on a Done card.
        case .dependencies:
            return !live.dependencies.isEmpty || !live.dependents.isEmpty
                || live.column != "done"
        case .timeline: return timeline != nil
        case .documents: return !documentPaths.isEmpty
        case .attachments: return !live.attachments.isEmpty
        case .danger: return true
        }
    }

    /// The fold row and, when open, the section under it. A section the
    /// rule opens — pinned or a lead for this stage — draws its body with no
    /// row at all, under its own heading: a chevron is a promise that a
    /// press folds something, and `CardSections.isOpen` keeps those open
    /// whatever is pressed. Everything else is a labelled row behind the one
    /// MORE row (`CardSections.shown`), its body drawn beneath it only once
    /// the row has been pressed. MORE's own open state rides
    /// `openedSections` like any section's, so the card-swap and
    /// stage-change resets cover it.
    @ViewBuilder
    private func foldedSection(_ s: CardSections.Section) -> some View {
        if hasContent(s) {
            if CardSections.isOpen(s, stage: stage, opened: []) {
                section(s)
            } else if CardSections.shown(s, stage: stage, opened: openedSections) {
                let open = CardSections.isOpen(s, stage: stage, opened: openedSections)
                CardFoldRow(label: CardSections.rowText(s, facts: foldFacts), open: open) {
                    if open {
                        openedSections.remove(s)
                    } else {
                        openedSections.insert(s)
                    }
                }
                if open, s != .more {
                    section(s)
                }
            }
        }
    }

    /// One section's body. Every var keeps its own `if` gate, so a body that
    /// finds nothing to draw still draws nothing.
    @ViewBuilder
    private func section(_ s: CardSections.Section) -> some View {
        switch s {
        case .status: statusSection
        case .manualCheck: manualCheckSection
        case .closeSignature: closeSignature
        case .objective:
            if let live {
                OutcomeEditor(client: client, state: state, card: live)
                    .id(live.id)
            }
        case .verbs: verbsSection
        case .otherVerbs: otherVerbsSection
        case .plan: planSection
        case .report: reportSection
        case .workRecord: workRecordSection
        case .changes: changesSection
        case .run: runSection
        case .editor: fields
        case .instructions, .crew: EmptyView()
        case .session: sessionSection
        case .thread: threadSection
        case .queue: queueSection
        case .dependencies: dependenciesSection
        case .timeline: timelineSection
        case .documents: documents
        case .attachments: existingAttachments
        case .danger: dangerZone
        case .collaboration:
            if let saved = live {
                CollaborationView(client: client, focus: .card(saved.id),
                    initiallyExpanded: true,
                    onSession: { provider, sid, helper in
                        NotificationCenter.default.post(name: .panelShowSession,
                            object: sid, userInfo: ["helper": helper, "provider": provider])
                        state.closeEditor()
                    },
                    onCard: showCollaborationCard)
            }
        case .more: EmptyView()
        }
    }

    // MARK: - Timeline

    /// The moments in the card's life, in the daemon's order, three columns:
    /// when / what (and its note) / the time since the previous row. Words
    /// only — a moment the daemon never saw reads "not observed" in the
    /// time column and the gap after it says so too. Beneath a hairline,
    /// one line saying where the card is now, formatted once against the
    /// report's own clock so nothing here ticks.
    @ViewBuilder
    private var timelineSection: some View {
        if let report = timeline {
            VStack(alignment: .leading, spacing: 6) {
                if !report.available {
                    Text(report.reason.isEmpty ? "the timeline could not be read"
                                               : report.reason)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    let rows = CardTimeline.rows(report)
                    if rows.isEmpty {
                        Text("nothing recorded yet")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                    }
                    Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 3) {
                        ForEach(rows) { row in
                            GridRow {
                                Text(row.when)
                                    .font(Theme.mono(11))
                                    .foregroundStyle(row.when == CardTimeline.notObserved
                                                     ? Theme.dim : Theme.phosphor)
                                    .gridColumnAlignment(.leading)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(row.label)
                                        .font(Theme.mono(11, weight: row.durable ? .medium : .regular))
                                        .foregroundStyle(Theme.phosphor)
                                    if !row.note.isEmpty {
                                        Text(row.note)
                                            .font(Theme.mono(10))
                                            .foregroundStyle(Theme.dim)
                                    }
                                }
                                .fixedSize(horizontal: false, vertical: true)
                                Text(row.gap ?? "")
                                    .font(Theme.mono(11))
                                    .foregroundStyle(Theme.dim)
                                    .gridColumnAlignment(.trailing)
                            }
                            .accessibilityElement(children: .ignore)
                            .accessibilityLabel(timelineSpoken(row))
                        }
                    }
                    if report.truncated {
                        Text("older moments not listed")
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.dim)
                    }
                    if !report.recentOnlyNote.isEmpty {
                        Text(report.recentOnlyNote)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let line = CardTimeline.openLine(report, now: report.generatedAt) {
                        Rectangle().fill(Theme.hair).frame(height: 1)
                            .padding(.vertical, 2)
                        Text(line)
                            .font(Theme.mono(11, weight: .medium))
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func timelineSpoken(_ row: CardTimeline.Row) -> String {
        var parts = [row.label]
        if !row.note.isEmpty { parts.append(row.note) }
        parts.append(row.when)
        if let gap = row.gap, !gap.isEmpty { parts.append("after " + gap) }
        return parts.joined(separator: ", ")
    }

    private func loadTimeline() async {
        timeline = nil
        guard !isComposer, let live else { return }
        timeline = await client.boardCardReport(live.id)?.timeline
    }

    /// The card's own name and sentence, above every section on a saved
    /// card: with the editor folded, nothing else says which card this is.
    @ViewBuilder
    private var identityLine: some View {
        if let live {
            VStack(alignment: .leading, spacing: 4) {
                Text(live.title.isEmpty ? "untitled" : live.title)
                    .font(Theme.prose(17, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if !live.summary.isEmpty {
                    Text(live.summary)
                        .font(Theme.prose(14))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                // The assistant row is part of the lead on a saved card,
                // and it is the *tile's* row: it reads the live card and
                // writes at once through `setTool` (`boardUpdate`), never
                // the footer's Save — START and Refine below act on
                // `live.tool`, and a draft-only pick left a card with no
                // assistant showing the switcher and no START until Save.
                // Same opening line as the composer's block in `fields`
                // (search `field("Assistant"` there): two pins read the
                // two blocks by that line. Change both together.
                if canRetool(live) || !live.tool.isEmpty {
                    field("Assistant", "Who takes it when it starts.") {
                        if canRetool(live) {
                            if ProviderChoice.wordChipOnly(tools: board.tools,
                                                           selected: live.tool) {
                                toolRecordChip(live)
                            } else {
                                ProviderSwitch(tools: board.tools, installed: board.installed,
                                               selected: live.tool,
                                               interactive: true,
                                               showsNobody: true,
                                               pick: { setTool(live, $0) })
                            }
                        } else if let mark = ProviderSwitch.recordMark(selected: live.tool) {
                            // A record of what ran, not a setting — the
                            // tile's inert mark.
                            ProviderSwitch(tools: [mark],
                                           selected: mark,
                                           interactive: false,
                                           showsNobody: false)
                        } else {
                            toolRecordChip(live)
                        }
                    }
                    .padding(.top, 4)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// `BoardCardView.canRetool`: the assistant is still the person's to
    /// choose until a session is bound or being bound.
    private func canRetool(_ live: BoardCard) -> Bool {
        live.sessionId.isEmpty && !live.isDispatching
    }

    /// The inert word for the assistant where the marks cannot show the
    /// truth — nothing on offer, or a name no longer offered — and for a
    /// bound card whose assistant has no mark.
    private func toolRecordChip(_ live: BoardCard) -> some View {
        Text(live.tool.isEmpty ? ProviderChoice.nobody : live.tool)
            .font(Theme.mono(10))
            .foregroundStyle(Theme.faint)
            .padding(.horizontal, 4)
            .padding(.vertical, 2)
            .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
            .accessibilityLabel(ProviderChoice.groupLabel)
            .accessibilityValue(ProviderChoice.value(
                selected: live.tool,
                name: ProviderMark.displayName))
    }

    /// The tile's `setTool`, on the live card: `tool` alone — the store
    /// clears a now-wrong model itself — **guarded at the draft's own
    /// revision**, through the one client seam that parses the reply's
    /// `revision` (`outcomeWrite`; `post()` does not). Then the footer's
    /// draft is brought up to date from **the reply**, because Save sends
    /// `draft.tool` at `draft.revision` and this write moved both; without
    /// that a Save after a pick is refused as somebody else's change.
    ///
    /// Never the snapshot's `now.revision`: that is the card's *latest*
    /// number, whatever the phone wrote since the window opened, and
    /// adopting it would carry the open-time title past the daemon's
    /// `CARD_CHANGED_REFUSAL` on the next Save. A pick judged against a
    /// moved card is refused the same way a Save is — `cardChangedAt`
    /// arms **Save anyway**, the draft is left alone — and only a landed
    /// pick clears that arm.
    private func setTool(_ live: BoardCard, _ tool: String) {
        if tool == live.tool { return }
        state.disarm()
        Task { @MainActor in
            let result = await client.outcomeWrite(
                action: "board_update", cardId: live.id,
                fields: ["tool": tool,
                         "expected_revision": String(state.draft.revision)])
            await client.refresh()
            // The refusal lands only while this window still shows the
            // card: a Save that closed the editor mid-flight has nothing
            // left to answer it with.
            guard state.editing == live.id else { return }
            state.refusals[live.id] = result.ok ? "" : result.detail
            if result.isCardChangedRefusal {
                cardChangedAt = result.currentRevision
                return
            }
            guard result.ok else { return }
            state.draft.tool = tool
            state.draft.model = ""
            if let revision = result.cardRevision {
                state.draft.revision = revision
            }
            cardChangedAt = nil
        }
    }

    /// One line about where the card stands, always present: the rule's
    /// sentence for the stage — the shared check line on a card waiting on
    /// one — then the session's own state while it runs. The dispatch
    /// refusal stays in the footer, where it already is.
    @ViewBuilder
    private var statusSection: some View {
        if let live {
            Text(stage == .manualCheck ? CardSections.statusLine
                                       : CardSections.stateWords(
                                            for: stage, linked: !live.linkState.isEmpty))
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(stage == .manualCheck ? Theme.amber : Theme.phosphor)
                .fixedSize(horizontal: false, vertical: true)
            if stage == .running, let (row, bucket) = agent(for: live.sessionId) {
                Text((row.nickname.isEmpty
                      ? String(row.sessionId.prefix(8)) : row.nickname)
                     + " — " + bucketLabel(row, bucket))
                    .font(.system(size: 12))
                    .foregroundStyle(bucket == "waiting" ? Color.red : Color.secondary)
            }
            // What the last MERGE press came to and the review's verdict —
            // the daemon's words, verbatim, as the card face draws them.
            if !live.mergeLine.isEmpty {
                Text(live.mergeLine)
                    .font(.system(size: 12))
                    .foregroundStyle(["blocked", "conflict", "checks_failed"]
                        .contains(live.mergeState) ? Theme.amber : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let verdict = CardMerge.verdictLine(
                verdict: live.reviewVerdict, running: live.reviewRunning,
                current: changes?.review.current ?? true) {
                Text(verdict)
                    .font(.system(size: 12))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The card face's cost-and-time line, under the status here:
            // `RunFigures.line`, absent where the card has never run.
            if live.isScout {
                Text("SCOUT")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
                    .accessibilityLabel("scout card")
            }
            if let figures = RunFigures.line(live.runFigures) {
                Text(figures)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(figures)
            }
            // And the tile's run-health line, for every stage where the
            // card carries a reading — the same `RunHealthLine.text`, the
            // same figures, amber on the daemon's `attention` bit.
            if let health = live.runHealth {
                Text(RunHealthLine.text(health))
                    .font(Theme.mono(11))
                    .foregroundStyle(health.attention ? Theme.amber : Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(RunHealthLine.spoken(health))
            }
        }
    }

    // MARK: - The one next action

    /// What this window can reach from where it is, filled from the gates
    /// the sheet already computes; the rule turns it into the one verb the
    /// card leads with. `canStart` is `BoardCardView.canStart`'s terms.
    private var reach: CardSections.Reach {
        guard let live else { return CardSections.Reach() }
        return CardSections.Reach(
            canRefine: canRefine(live),
            canStart: board.dispatchEnabled
                && live.column != BoardColumn.done.rawValue
                && !live.tool.isEmpty
                && live.sessionId.isEmpty
                && !live.isDispatching
                && !live.isRefining,
            canReply: messagesTerminal,
            // A card flagged with a check file is settled by Passed / Failed
            // in its manual-check section, never by Mark checked.
            canMarkChecked: live.needsManualCheck
                && (live.manualCheckPath.isEmpty || manualCheckMissing),
            canMarkDone: live.column == BoardColumn.inProgress.rawValue,
            canReview: live.awaitsReview)
    }

    /// The one call into the rule; every guard below reads this.
    private var nextAction: CardSections.NextAction {
        CardSections.nextAction(stage: stage, reach: reach)
    }

    /// `BoardCardView.startsUnplanned`: `planPath` alone, no file stat —
    /// the daemon's gate and `startCard`'s fallback cover a plan file gone.
    /// A scout has no plan to skip, so it is never "unplanned".
    private var startsUnplanned: Bool {
        guard let live else { return true }
        return live.planPath.isEmpty && !live.isScout
    }

    /// `BoardCardView.doneClears`: Done reaches a live session and types
    /// `/clear` into it, so it arms; Codex never arms — Dark Army cannot type there.
    private var doneClears: Bool {
        guard let live, live.tool != "codex" else { return false }
        return live.isDispatching || (live.linkState == "live" && !live.sessionId.isEmpty)
    }

    /// The band: one verb, the same buttons the board tile draws, pressing
    /// `BoardState`'s one copy of each. Every verb drawn here is *not* drawn
    /// again in its own section (`nextAction != .x` guards there), so none
    /// appears twice and none is lost.
    @ViewBuilder
    private var verbsSection: some View {
        if let live {
            switch nextAction {
            case .refine:
                Button("Refine") {
                    state.disarm()
                    state.refineCard(live, client: client)
                }
                .buttonStyle(AlarmOutline())
                .disabled(state.refining.contains(live.id))
                .clickable(!state.refining.contains(live.id))
            case .start:
                startButton(live, ownTerminal: false)
            case .reply:
                messageBox
                if !sendNote.isEmpty {
                    Text(sendNote)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
            case .markChecked:
                HStack(spacing: 10) {
                    Button("Mark checked") { clearManual(live.id) }
                        .controlSize(.small)
                        .clickable()
                    Text("Clears the badge. The card does not move.")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            case .done:
                // `doneClears` arms (the press also types `/clear` into a
                // live session); otherwise one press moves the card.
                let doneArmed = state.doneArmed == live.id
                Button(doneClears && doneArmed ? "Done & clear?" : "Done") {
                    if doneClears && !doneArmed {
                        state.armDone(live.id)
                    } else {
                        state.disarm()
                        state.moveCard(live, to: .done, client: client)
                    }
                }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(doneArmed ? Color.orange : Theme.phosphor)
                .clickable()
            case .review:
                Button("Reviewed") { review(live) }
                    .buttonStyle(.link)
                    .font(.system(size: 11))
                    .clickable()
            case .none:
                EmptyView()
            }
        }
    }

    /// The tile's Start, armed then confirmed, with the armed label saying
    /// what the confirmed press will skip. The arm slot is `BoardState`'s so
    /// the tile and this window share one.
    private func startButton(_ live: BoardCard, ownTerminal: Bool) -> some View {
        let armed = state.armed == live.id
        let starting = state.starting.contains(live.id)
        return Button(armed ? (startsUnplanned ? "Start unplanned?" : "Really start?")
                            : "START") {
            if armed {
                state.disarm()
                // The confirmed press *is* the plan gate's confirmation on
                // this route, so it carries the flag.
                state.startCard(live, client: client,
                                skipPlanGate: startsUnplanned,
                                ownTerminal: ownTerminal)
            } else {
                state.arm(live.id)
            }
        }
        .buttonStyle(AlarmOutline())
        .disabled(starting)
        .clickable(!starting)
    }

    // MARK: - The fields

    private var phaseTwoVisible: Bool { !isComposer || state.composerExpanded }

    private var fields: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Composer-only, and first: the one box you can fill instead of
            // the four below it. Deliberately not on an existing card — this
            // is scratch for writing a card, not a field a card carries.
            if isComposer {
                field("Your idea",
                      "Type the whole thought here and press Prepare — Dark Army "
                      + "drafts the title, the plain words, the instructions "
                      + "and the specialists below, all still yours to "
                      + "correct. Or press fill in myself below.",
                      accessory: {
                    if DictateButton.available(client.context.settings) {
                        DictateButton(focus: { ideaFocused = true })
                    }
                }) {
                    TextEditor(text: $state.draft.idea)
                        .focused($ideaFocused)
                        .font(.system(size: 12))
                        .frame(height: 72)
                        .scrollContentBackground(.hidden)
                        .padding(4)
                        .overlay(Rectangle()
                            .strokeBorder(Theme.hair, lineWidth: 1))
                }
            }
            // Directly under the box it reads, so the fields it writes sit
            // below the press. The phone's composer already does this; the
            // button used to live under Assistant / Project, which hid the
            // flow the idea field's own hint describes.
            if isComposer {
                VStack(alignment: .leading, spacing: 3) {
                    prepareRow
                    if !prepareNote.isEmpty {
                        Text(prepareNote)
                            .font(.system(size: 10))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !preparing,
                       let missing = PrepareGate.missing(
                            summaryEmpty: prepareInputEmpty,
                            toolEmpty: state.draft.tool.isEmpty,
                            rootEmpty: state.draft.root.isEmpty) {
                        Text(missing)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !phaseTwoVisible {
                        Button(ComposerPhase.fillMyselfLabel) {
                            revealReason = .manual
                            state.revealPhaseTwo()
                        }
                        .buttonStyle(.plain)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .clickable()
                    }
                }
            }
            if isComposer {
                composerAttachments
            }
            // Composer only: a saved card draws this same block in its
            // lead (`identityLine`, search `field("Assistant"` there), so
            // the editor under MORE does not draw the row twice. Two call
            // sites on purpose, not one helper: two pins read them by
            // their opening line. Change both together.
            if isComposer {
            field("Assistant", "Who takes it when it starts.") {
                // The same mark row the card face draws, so the choice
                // looks identical in both places — plus the labelled route
                // back to unassigned, which lives here and only here: it
                // costs the model choice too, so it is a deliberate act
                // rather than something a stray click on a card can do.
                //
                // Never a menu: where the row cannot tell the truth — nothing
                // offered, or a card naming an assistant no longer offered —
                // an inert word chip says so and answers no press.
                if ProviderChoice.wordChipOnly(tools: board.tools,
                                               selected: state.draft.tool) {
                    Text(state.draft.tool.isEmpty ? ProviderChoice.nobody
                                                  : state.draft.tool)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 2)
                        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
                        .accessibilityLabel(ProviderChoice.groupLabel)
                        .accessibilityValue(ProviderChoice.value(
                            selected: state.draft.tool,
                            name: ProviderMark.displayName))
                } else {
                    ProviderSwitch(tools: board.tools, installed: board.installed,
                                   selected: state.draft.tool,
                                   interactive: true,
                                   showsNobody: true,
                                   pick: { state.draft.tool = $0 })
                }
            }
            field("Kind", "Build writes code; Scout investigates and writes a report.") {
                kindControl
            }
            }
            if phaseTwoVisible {
                if isComposer {
                    composerPhaseTwo
                } else {
                    savedCardEditor
                }
            }
        }
        // The view-side half of the store's clear-on-retool: switching
        // assistant re-narrows the menu and puts the choice back to
        // Default, so a model from the wrong provider can never be left
        // behind on a draft. The store does the same on its own, which is
        // why this is a mirror rather than the rule.
        .onChange(of: state.draft.tool) { state.draft.model = "" }
        // Every route that opens or replaces the composer bumps the
        // generation, and this view keeps its identity (and its `@State`)
        // across a + NEW CARD or a resumed draft while the card window is
        // already up. The slots are per press, so a new draft must never
        // inherit the last one's "Dark Army picked" line: the same seam the
        // in-flight guard keys on resets them.
        .onChange(of: state.prepareGeneration) {
            suggestedRoot = ""
            revertRoot = ""
            revealReason = .resumed
        }
    }

    /// The composer's second phase, in the order Prepare fills it: the
    /// project first (Prepare needs one), then what the card says, then
    /// the objective and the faces. Unchanged in substance from before the
    /// saved-card editor was regrouped; only lifted into a var of its own.
    @ViewBuilder
    private var composerPhaseTwo: some View {
        if let words = ComposerPhase.caption(for: revealReason) {
            Text(words)
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
        projectField
        titleField
        summaryField
        modelField
        areaPicker
        priorityField
        instructionsField
        // The objective, typed before the card exists. Composer only:
        // a saved card edits these in `OutcomeEditor` under the
        // revision guard, and a second box here would be a second
        // writer. Prepare fills an *empty* box and never a typed one
        // — the one place a suggestion is not allowed to win.
        field("Who benefits",
              "Who this work is for. Dark Army suggests one on "
              + "Prepare; a box you have filled keeps your words.") {
            TextField("", text: $state.draft.outcome.beneficiary)
                .textFieldStyle(.roundedBorder)
                .font(Theme.mono(12))
        }
        field("Intended benefit",
              "What good it should do for them, in a sentence or "
              + "three. Handed to the assistant under the instructions.") {
            TextField("", text: $state.draft.outcome.intendedBenefit,
                      axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .font(Theme.mono(12))
        }
        field("Success criterion",
              "One observable sentence a person could check. The plan "
              + "answers it, the checker reports against it, and only "
              + "you accept the outcome.") {
            TextField("", text: $state.draft.outcome.successCriterion,
                      axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .font(Theme.mono(12))
        }
        composerSpecialists
        specialistObservation
    }

    /// EDIT CARD on a saved card: the same fields, in four named groups a
    /// person can scan — what it is, where it runs, how important, what to
    /// do — each under a heading a screen reader announces as one, with
    /// every box labelled for VoiceOver by the words beside it
    /// (`field(_:_:)` does that for every field on both editors). The
    /// footer's Save is far below on a long card, so the group ends with
    /// its own line saying whether anything is unsaved, and one button
    /// that puts the card's own words back.
    @ViewBuilder
    private var savedCardEditor: some View {
        editorGroup("What it is") {
            titleField
            summaryField
            if live?.column == BoardColumn.prep.rawValue {
                field("Kind", "Build writes code; Scout investigates and writes a report.") {
                    kindControl
                }
            }
        }
        editorGroup("Where it runs") {
            projectField
            modelField
            areaPicker
        }
        editorGroup("How important") {
            priorityField
        }
        editorGroup("What to do") {
            instructionsField
            field("Expected specialists",
                  "One per line, in the order this card can still run them. "
                  + "This declares the plan; it does not claim anybody ran.",
                  accessory: {
                if DictateButton.available(client.context.settings) {
                    DictateButton(focus: { workflowFocused = true })
                }
            }) {
                TextEditor(text: $state.draft.workflow)
                    .focused($workflowFocused)
                    .font(.system(size: 12, design: .monospaced))
                    .frame(minHeight: 52)
                    .scrollContentBackground(.hidden)
                    .padding(4)
                    .overlay(Rectangle()
                        .strokeBorder(Theme.hair, lineWidth: 1))
            }
        }
        specialistObservation
        unsavedEditsRow
    }

    /// A titled group of fields: a heading VoiceOver reads as a heading,
    /// a hairline, the fields, and a little air after. The group is one
    /// accessibility container so a screen reader can skip it whole.
    private func editorGroup<Content: View>(_ title: String,
                                            @ViewBuilder _ content: () -> Content)
        -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(Theme.mono(10, weight: .semibold))
                .foregroundStyle(Theme.dim)
                .tracking(0.8)
                .accessibilityAddTraits(.isHeader)
            Rectangle().fill(Theme.hair).frame(height: 1)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(title)
    }

    /// Which of the editable fields differ from the card as Dark Army holds it.
    /// `BoardDraft(card)` is the one place a card becomes a draft, so the
    /// comparison is against exactly what the editor was opened with.
    private var editedFields: [String] {
        guard let live else { return [] }
        let opened = BoardDraft(live)
        let d = state.draft
        var out: [String] = []
        if d.title != opened.title { out.append("title") }
        if d.summary != opened.summary { out.append("plain words") }
        if d.root != opened.root { out.append("project") }
        if d.tool != opened.tool { out.append("assistant") }
        if d.kind != opened.kind { out.append("kind") }
        if d.model != opened.model { out.append("model") }
        if d.area != opened.area { out.append("area") }
        if d.priority != opened.priority { out.append("priority") }
        if d.prompt != opened.prompt { out.append("instructions") }
        if d.workflow != opened.workflow { out.append("specialists") }
        return out
    }

    /// One line under the editor saying what is unsaved, and the way back.
    /// Drawn only once something differs: a quiet editor says nothing.
    @ViewBuilder
    private var unsavedEditsRow: some View {
        let edited = editedFields
        if !edited.isEmpty {
            HStack(spacing: 10) {
                Text("Unsaved: " + edited.joined(separator: ", ")
                     + " — press Save below to keep them.")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button("Discard changes") { discardEdits() }
                    .controlSize(.small)
                    .clickable()
                    .accessibilityHint("Puts back the card's own words in every box")
            }
            .accessibilityElement(children: .contain)
        }
    }

    /// The card's own values back into every editable box. The objective
    /// and the revisions are left alone: they are the `OutcomeEditor`'s and
    /// the guard's, not this section's.
    private func discardEdits() {
        guard let live else { return }
        let opened = BoardDraft(live)
        state.draft.title = opened.title
        state.draft.summary = opened.summary
        state.draft.project = opened.project
        state.draft.root = opened.root
        state.draft.tool = opened.tool
        state.draft.model = opened.model
        state.draft.area = opened.area
        state.draft.priority = opened.priority
        state.draft.prompt = opened.prompt
        state.draft.workflow = opened.workflow
    }

    private var projectField: some View {
        field("Project", "Only folders Dark Army has an open window for.") {
            Picker("", selection: projectBinding) {
                // The dispatch guard refuses any other root, so offering
                // one here would be a card that cannot be started and
                // does not say why until it is dropped into In progress.
                Text("—").tag("")
                ForEach(board.projects) { project in
                    Text(project.name).tag(project.root)
                }
            }
            .labelsHidden()
            .font(.system(size: 12))
            // Gated on the revert slot, not on `offer`: while the
            // line is up `suggestedRoot` *is* the picker's root, so
            // the offer guard would read the live state as nothing.
            if !revertRoot.isEmpty { suggestionRow() }
        }
    }

    private var titleField: some View {
        field("Title", "One line naming the work.", accessory: {
            if DictateButton.available(client.context.settings) {
                DictateButton(focus: { titleFocused = true })
            }
        }) {
            TextField("", text: $state.draft.title)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 13))
                .focused($titleFocused)
        }
    }

    /// Deliberately above the instructions, and deliberately the wider
    /// box of the two on first read: this is the half of the card that
    /// gets shown in the column, on the board, to whoever walks past.
    private var summaryField: some View {
        field("In plain words",
              "What this is for, so somebody who will never read the "
              + "instructions can tell what the board is saying."
              + (DictateButton.available(client.context.settings)
                 ? " Hold, speak, release — the words land in this box." : ""),
              accessory: {
            if DictateButton.available(client.context.settings) {
                DictateButton(focus: { summaryFocused = true })
            }
        }) {
            TextEditor(text: $state.draft.summary)
                // Not editable while this is a preview. The board's live
                // payload carries only the first part of a long description,
                // and typing into that and saving would replace the whole of
                // somebody's paragraph with its opening — no warning, no
                // undo. Disabled rather than hidden, so the text that *is*
                // here can still be read while the rest arrives.
                .disabled(state.draft.summaryTruncated)
                .opacity(state.draft.summaryTruncated ? 0.55 : 1)
                .focused($summaryFocused)
                .font(.system(size: 12))
                .frame(height: 56)
                .scrollContentBackground(.hidden)
                .padding(4)
                .overlay(Rectangle()
                    .strokeBorder(Theme.hair, lineWidth: 1))
        }
    }

    /// A **full-width row of its own**, directly under the assistant it
    /// belongs to, rather than a third column beside it. A pinned model
    /// id is long (`claude-sonnet-5[1m]`), and a third of the sheet's
    /// width truncated exactly the part that distinguishes one entry
    /// from the next — which is the whole choice.
    ///
    /// Absent, not empty, when there is nothing to choose from: an
    /// older daemon sends no catalogue, no assistant is chosen yet, or
    /// the assistant has no models Dark Army ships. A picker whose only row
    /// is "Default" is a promise with nothing behind it.
    @ViewBuilder
    private var modelField: some View {
        if !Board.modelOptions(models: board.models,
                               tool: state.draft.tool).isEmpty {
            field("Model",
                  "Which model the assistant runs on. An alias like "
                  + "\"opus\" always means the newest of that family; a "
                  + "full name pins one exactly. Default lets the "
                  + "assistant decide.") {
                Picker("", selection: $state.draft.model) {
                    Text("Default").tag("")
                    ForEach(Board.modelOptions(models: board.models,
                                               tool: state.draft.tool),
                            id: \.self) { model in
                        Text(Board.modelLabel(model)).tag(model)
                    }
                }
                .labelsHidden()
                .font(.system(size: 12))
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    /// The importance number. **Absent, never inert**, against a
    /// daemon that does not know the column: it would drop the field
    /// and then 400 an update whose only named field was dropped.
    ///
    /// A plain number box rather than a stepper or bands: the scale is
    /// the helper's, a person correcting a 60 to a 75 is typing two
    /// characters, bands would quantise the helper's own answers, and
    /// a stepper needs thirty presses. A number outside 0–100 is said
    /// beside the box the moment it is typed, in the store's own words,
    /// rather than found out at Save.
    /// Drawn on the composer too: there it rides `boardCreate`'s
    /// named `priority` argument (sent only when typed), on the
    /// editor it rides `board_update`'s field dict.
    private var priorityField: some View {
        field("Priority",
              "How important this work is, 0–100. Dark Army suggests one "
              + "when the card is written; empty means no opinion, "
              + "and sorts with the zeroes at the bottom of the "
              + "column. The number decides the order, so dragging "
              + "only rearranges cards that share one.") {
            HStack(spacing: 8) {
                TextField("0–100", text: $state.draft.priority)
                    .textFieldStyle(.roundedBorder)
                    .font(Theme.mono(12))
                    .frame(width: 90, alignment: .leading)
                if let problem = PriorityInput.problem(state.draft.priority) {
                    Text(problem)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.amber)
                        .accessibilityLabel("Priority problem: " + problem)
                }
            }
        }
    }

    private var instructionsField: some View {
        field("Instructions",
              state.draft.promptTruncated
              ? "Loading the full text — Save is held until it arrives."
              : "Handed to the assistant verbatim, as one argument. No "
              + "shell sees it.",
              accessory: {
            if DictateButton.available(client.context.settings) {
                DictateButton(focus: { promptFocused = true })
            }
        }) {
            TextEditor(text: $state.draft.prompt)
                // Not editable while this is a preview. The board's live
                // payload carries only the first part of a long prompt, and
                // typing into that and saving would replace the whole of
                // somebody's instructions with its opening — no warning, no
                // undo. Disabled rather than hidden, so the text that *is*
                // here can still be read while the rest arrives.
                .disabled(state.draft.promptTruncated)
                .opacity(state.draft.promptTruncated ? 0.55 : 1)
                .focused($promptFocused)
                .font(.system(size: 12, design: .monospaced))
                .frame(minHeight: 150)
                .scrollContentBackground(.hidden)
                .padding(4)
                .overlay(Rectangle()
                    .strokeBorder(Theme.hair, lineWidth: 1))
        }
    }

    /// Area is card metadata and stays editable independently of the stages.
    @ViewBuilder private var areaPicker: some View {
        Text("Area").font(Theme.mono(11, weight: .semibold))
        AreaGrid(selected: $state.draft.area)
    }

    /// Composer only: on a real card the declared stages already have
    /// `specialistObservation` and `CrewBand` speaking for them, and a face
    /// there would read as a stage that ran. Nothing at all is drawn while no
    /// stage parses — the grid's own rule — so an unprepared composer never
    /// shows a lone caption over nothing.
    @ViewBuilder
    private var composerSpecialists: some View {
        if !Specialists.parse(state.draft.workflow).isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                Text("Expected specialists")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                SpecialistGrid(stages: Specialists.parse(state.draft.workflow))
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var specialistObservation: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("Observed by Dark Army")
                .font(Theme.mono(11, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            if isComposer {
                Text("Observed specialists appear only after the card starts.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            } else {
                let observed = live?.agentTrail ?? []
                let expected = live?.workflow ?? []
                if !observed.isEmpty || !(live?.area ?? "").isEmpty {
                    // The crew with names and room to breathe: face, the part
                    // of the job, who took it and whether it is finished,
                    // running or expected. Same view as the card face, one
                    // style along.
                    Text("CREW")
                        .font(Theme.mono(10, weight: .semibold))
                        .foregroundStyle(Theme.dim)
                    CrewBand(workflow: expected,
                             trail: observed,
                             live: [],
                             crew: live?.crew ?? [:],
                             lead: live?.area ?? "",
                             promised: live?.leadFace ?? "",
                             style: .grid)
                } else if expected.isEmpty {
                    Text("No specialists were declared or observed.")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                } else {
                    Text("None observed yet. Dark Army adds one only when it runs.")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func field<Content: View>(_ label: String, _ hint: String,
                                      @ViewBuilder _ content: () -> Content)
        -> some View {
        field(label, hint, accessory: { EmptyView() }) {
            content()
        }
    }

    private func field<Content: View, Accessory: View>(
        _ label: String, _ hint: String,
        @ViewBuilder accessory: () -> Accessory,
        @ViewBuilder _ content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Text(label).font(Theme.prose(13, weight: .semibold))
                    .foregroundStyle(Theme.text)
                Spacer(minLength: 0)
                accessory()
            }
            // The words beside the box are its name and its help for a
            // screen reader too: a bare `TextField("")` under a `Text`
            // label reads as "text field", which is every box on the
            // card sounding the same.
            content()
                .accessibilityLabel(label)
                .accessibilityHint(hint)
            Text(hint)
                .font(Theme.prose(11))
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityHidden(true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var prepareRow: some View {
        HStack(spacing: 8) {
            Button("Prepare") { prepare() }
                .buttonStyle(AlarmOutline(color: Theme.phosphor, size: 10))
                .disabled(preparing || !canPrepare || state.composerSaving)
                // The exact negation of the line above: `.disabled` stops
                // SwiftUI hit-testing but not the cursor overlay's tracking
                // area, so the hand has to be gated by hand.
                .clickable(!preparing && canPrepare && !state.composerSaving)
            if preparing {
                Text("Dark Army is writing…")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// There has to be something to prepare *from* — either box will do —
    /// and somewhere to prepare it for.
    private var canPrepare: Bool {
        !prepareInputEmpty
            && !state.draft.tool.isEmpty
            && !state.draft.root.isEmpty
    }

    private var prepareInputEmpty: Bool {
        state.draft.idea
            .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        && state.draft.summary
            .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    @ViewBuilder
    private var composerAttachments: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Attachments")
                .font(Theme.mono(11, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            ForEach(state.stagedAttachments, id: \.self) { rel in
                HStack(spacing: 8) {
                    Text(CardAttachments.display(rel))
                        .font(.system(size: 11, design: .monospaced))
                        .lineLimit(1)
                    Button {
                        state.unstageAttachment(rel)
                    } label: {
                        Text("×")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .buttonStyle(.plain)
                    .disabled(state.composerSaving)
                    .clickable(!state.composerSaving)
                    .accessibilityLabel("Remove \(CardAttachments.display(rel))")
                    Spacer()
                }
            }
            Button("Attach…") { pickAttachments() }
                .buttonStyle(AlarmOutline(color: Theme.phosphor, size: 10))
                .disabled(state.composerSaving)
                .clickable(!state.composerSaving)
            Text("Drop files here, or press Attach. Dark Army keeps its own copies.")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            if !attachNote.isEmpty {
                Text(attachNote)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var existingAttachments: some View {
        let rels = live?.attachments ?? []
        if !rels.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Attachments")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                ForEach(rels, id: \.self) { rel in
                    if let path = CardAttachments.resolve(rel) {
                        HStack(spacing: 8) {
                            Text(CardAttachments.display(rel))
                                .font(.system(size: 11, design: .monospaced))
                                .lineLimit(1)
                            Button("Open") { revealInEditor(path) }
                                .buttonStyle(.borderless)
                                .controlSize(.small)
                                .font(.system(size: 10))
                                .clickable()
                            Spacer()
                        }
                    } else {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Dark Army could not find this file.")
                                .font(.system(size: 11))
                                .foregroundStyle(.orange)
                            Text(rel)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func attach(urls: [URL]) {
        guard !state.composerSaving else { return }
        attachNote = ""
        let id = state.stagingId
        guard !id.isEmpty else { return }
        state.beginStaging(for: id)
        Task { @MainActor in
            defer { state.endStaging(for: id) }
            let (staged, refused) = await CardAttachments.stage(urls: urls,
                                                                into: id)
            // Still the composer *this* drop opened: every composer shares
            // `newCard`, so a close/reopen (or a second New card) while this
            // was in flight must not list the previous composer's paths.
            //
            // On mismatch do not discard the folder. A successful create
            // keeps it then clears stagingId, so a late discard would
            // delete copies already listed on the new card (drop-then-save,
            // or a second Attach still copying). Close-without-save already
            // discarded the folder; leftover files this call wrote after
            // that are what keepIfOwned removes, one path at a time.
            let owned = isComposer
                && state.editing == BoardState.newCard
                && state.stagingId == id
            let accepted = CardAttachments.keepIfOwned(staged, owned: owned)
            for rel in accepted where !state.stagedAttachments.contains(rel) {
                state.stagedAttachments.append(rel)
            }
            if owned, !refused.isEmpty {
                attachNote = refused.joined(separator: "\n")
            }
        }
    }

    private func pickAttachments() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = CardAttachments.contentTypes
        guard panel.runModal() == .OK else { return }
        attach(urls: panel.urls)
    }

    private func prepare() {
        guard !preparing, canPrepare, !state.composerSaving else { return }
        preparing = true
        prepareNote = ""
        suggestedRoot = ""
        revertRoot = ""
        let draft = state.draft
        let generation = state.prepareGeneration
        Task { @MainActor in
            // A drop that has not finished writing must not be omitted
            // from the list Prepare sends — join, then snapshot.
            await state.waitForStaging()
            guard isComposer,
                  state.editing == BoardState.newCard,
                  state.prepareGeneration == generation else {
                preparing = false
                return
            }
            let attached = state.stagedAttachments.joined(separator: "\n")
            let idea = draft.idea.trimmingCharacters(
                in: .whitespacesAndNewlines)
            let typedSummary = draft.summary.trimmingCharacters(
                in: .whitespacesAndNewlines)
            // The older-daemon degrade: a daemon that does not know `idea`
            // reads the description, so send the idea under that name too
            // when nothing was typed there. A newer daemon prefers `idea`
            // and ignores the duplicate.
            let result = await client.prepareCard(
                title: draft.title,
                summary: typedSummary.isEmpty ? idea : draft.summary,
                tool: draft.tool, project: draft.project, root: draft.root,
                attachments: attached,
                idea: idea)
            preparing = false
            // Still the composer *this* press opened: every composer shares
            // `newCard`, so a close/reopen (or a second New card) while this
            // was in flight must not drop generated text into the next draft.
            guard isComposer,
                  state.editing == BoardState.newCard,
                  state.prepareGeneration == generation else { return }
            revealReason = result.ok ? .prepared : .manual
            state.revealPhaseTwo()
            if result.ok {
                state.draft.prompt = result.prompt
                state.draft.workflow = result.workflow
                // Non-empty only. An older daemon answers without these two,
                // which arrives here as empty, and blanking a typed title on
                // the strength of a key that was never sent is the one way
                // this could lose somebody's words.
                if !result.title.isEmpty { state.draft.title = result.title }
                if !result.summary.isEmpty {
                    state.draft.summary = result.summary
                }
                // The objective is a suggestion, not a rewrite: each line
                // lands only in an *empty* box. Title and summary are
                // overwritten because in idea mode they are what the helper
                // was asked to write; these three are the person's own
                // whenever they were typed; a box holding only whitespace
                // counts as empty.
                if !result.beneficiary.isEmpty,
                   state.draft.outcome.beneficiary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    state.draft.outcome.beneficiary = result.beneficiary
                }
                if !result.intendedBenefit.isEmpty,
                   state.draft.outcome.intendedBenefit.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    state.draft.outcome.intendedBenefit = result.intendedBenefit
                }
                if !result.successCriterion.isEmpty,
                   state.draft.outcome.successCriterion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    state.draft.outcome.successCriterion = result.successCriterion
                }
                // Dark Army's pick wins unless the person says otherwise: apply
                // it through the picker's own path and remember the project
                // the picker held as the one way back. `selectProject`, not
                // the binding — its setter clears both slots. With `decide`
                // nil (same project, unlisted, no opinion) neither slot is
                // written and the picker keeps the person's choice.
                if let offered = AreaSuggestion.decide(offer: result.suggestedArea, current: state.draft.area) {
                    state.draft.area = offered
                }
                if let d = ProjectSuggestion.decide(
                    root: result.suggestedRoot, current: state.draft.root,
                    projects: board.projects) {
                    selectProject(d.apply.root)
                    suggestedRoot = d.apply.root
                    revertRoot = d.revertTo?.root ?? ""
                }
            } else {
                prepareNote = result.detail
            }
        }
    }

    // MARK: - Who said this was finished

    /// The assistant's own claim that the work is done, and the sentence it made
    /// it with.
    ///
    /// The sheet is where somebody goes to decide whether to press **Reopen**,
    /// and this note is the whole input to that decision — a card that reads
    /// "closed by a3f1c2d4" in the column and nothing more gives them nothing
    /// to weigh. Drawn only on a card an assistant closed; one a human dragged
    /// across shows nothing here, which is the difference worth seeing.
    ///
    /// Untrusted text, written by an agent through a socket that authenticates
    /// nothing: a plain `Text`, no link handling and no markdown, exactly the
    /// treatment the prompt preview already gets.
    @ViewBuilder
    private var closeSignature: some View {
        if let live, live.isAgentClosed {
            VStack(alignment: .leading, spacing: 3) {
                let name = live.closedByName.isEmpty ? live.closedBy : live.closedByName
                HStack(spacing: 6) {
                    AvatarView(character: Cast.character(forNickname: name),
                               state: .sleep, size: 20)
                    Text("Closed by \(name)")
                        .font(.system(size: 11, weight: .semibold))
                }
                Text(live.closeNote)
                    .font(.system(size: 12))
                    .fixedSize(horizontal: false, vertical: true)
                Text("The assistant that did this card said so itself. Dark Army "
                     + "checked nothing — Reopen sends it back and throws the "
                     + "claim away.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
                if live.awaitsReview, nextAction != .review {
                    // Reading and acting on a close are one surface, like
                    // Delete and Done: the same Reviewed press the card face
                    // offers, unarmed for the same reason — nothing is
                    // destroyed, the name and the note above stay drawn.
                    // On a Done card the band draws it instead, so this is
                    // the ended-stage copy under MORE.
                    Button("Reviewed") { review(live) }
                        .buttonStyle(.link)
                        .font(.system(size: 11))
                        .clickable()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Durable record of the bound session, fetched once per open. Absence
    /// is words, never a blank pane or a zero standing in for unknown.
    @ViewBuilder
    private var runSection: some View {
        if let live, !live.sessionId.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("WHAT RAN")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                if let line = SessionRecordFormat.absenceLine(
                    report: runReport, fetchFailed: runFetchFailed) {
                    Text(line)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                } else if let rec = runReport?.session {
                    runRecordRows(rec, live: runReport?.live == true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func runRecordRows(_ rec: SessionRecord, live: Bool) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            if live {
                Text("Still working — figures so far.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            runDetail("Duration", SessionRecordFormat.durationText(
                firstSeen: rec.firstSeen, endedAt: rec.endedAt,
                lastSeen: rec.lastSeen))
            runDetail("Steps", rec.turns > 0 ? "\(rec.turns)" : "—")
            runDetail("Cost", SessionRecordFormat.costText(
                usd: rec.costUsd, source: rec.costSource))
            runDetail("Tokens", rec.tokens.map(Format.count) ?? "—")
            if let model = runModelLine(rec) {
                runDetail("Model", model)
            }
            if rec.subagents > 0 {
                runDetail("Helpers", "\(rec.subagents)")
            }
            if !SessionRecordFormat.toolsLine(rec.tools).isEmpty {
                Text(SessionRecordFormat.toolsLine(rec.tools))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !rec.attribution.isEmpty {
                Text(rec.attribution.joined(separator: " · "))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !rec.title.isEmpty {
                Text(rec.title)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func runModelLine(_ rec: SessionRecord) -> String? {
        let bits = [rec.provider,
                    rec.primaryModel.isEmpty ? "" : Format.model(rec.primaryModel)]
            .filter { !$0.isEmpty }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }

    private func runDetail(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .frame(width: 72, alignment: .leading)
            Text(value)
                .font(.system(size: 12))
        }
    }

    /// Acknowledge the close from inside the sheet. Refusals land in the
    /// card's own refusal line, where the board draws them once the sheet is
    /// dismissed — the same channel every other card verb uses.
    private func review(_ live: BoardCard) {
        // Clear the slot before the press, like the card face does: a refusal
        // left over from an earlier failed press would otherwise outlive a
        // later success, since only a failure writes this slot.
        state.refusals[live.id] = ""
        Task { @MainActor in
            let result = await client.boardReview(live.id)
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
        }
    }

    // MARK: - The attached plan

    /// The plan a refinement attached, as a first-class section: the diagram
    /// (stages as tiles, files as a list) above the rendered document.
    ///
    /// Everything here reuses the documents reader's machinery wholesale —
    /// `BoardDocuments.resolve` for the containment test (the path is
    /// untrusted card state and must lie inside the card's own root),
    /// `BoardDocuments.read` for the bounded read, `MarkdownText` for the
    /// rendering — because a second copy of "read a file a card points at
    /// safely" is how the two drift. A plan that is not where the card says
    /// it is renders as one line saying so plus the path, never a blank pane:
    /// the daemon's plan gate treats that card as unplanned again, and this
    /// line is where a person learns why.
    /// The verbs that are not the one next action, behind MORE: Start where
    /// Refine leads (a Prep card), and "start the work by itself the moment
    /// the plan lands" — the same immediate, unguarded toggle the card face
    /// draws (`BoardCardView.startWhenPlannedTick`) and the same two gates
    /// read off `live`: it appears exactly where a Refine could still
    /// happen, and only where the daemon knows the column at all — absent
    /// rather than present and refused.
    @ViewBuilder
    private var otherVerbsSection: some View {
        if let live, reach.canStart, nextAction != .start {
            startButton(live, ownTerminal: false)
        }
        if let live, canPromote(live) {
            Button("Promote") { promote(live) }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(Theme.faint)
                .clickable()
        }
        if let live, mergeControls(live) {
            mergeVerbs(live)
        }
        if let live, canRefine(live) {
            VStack(alignment: .leading, spacing: 6) {
                Button(live.startWhenPlanned
                       ? "[x] Start it when the plan lands"
                       : "[ ] Start it when the plan lands") {
                    setStartWhenPlanned(live, on: !live.startWhenPlanned)
                }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(live.startWhenPlanned
                                 ? Theme.phosphor : Theme.faint)
                .clickable()
                Text("Dark Army takes the ordinary Start route, once, as soon as "
                     + "this card's plan is attached.")
                    .font(Theme.mono(9))
                    .foregroundStyle(Theme.faint)
            }
        }
    }

    /// `BoardCardView.canRefine`'s terms, against the sheet's own live copy.
    private func canRefine(_ live: BoardCard) -> Bool {
        board.dispatchEnabled
            && live.column == BoardColumn.prep.rawValue
            && live.planPath.isEmpty
            && !live.isScout
            && !live.isRefining
            && live.sessionId.isEmpty
            && !live.isDispatching
    }

    private func canPromote(_ live: BoardCard) -> Bool {
        live.isScout && !live.reportPath.isEmpty
            && live.column == BoardColumn.done.rawValue
    }

    private func promote(_ live: BoardCard) {
        state.disarm()
        state.refusals[live.id] = ""
        Task { @MainActor in
            let result = await client.boardPromote(live.id)
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
        }
    }

    // MARK: - Review and merge

    /// Whether this window draws any of the three merge controls on the
    /// card (`CardMerge`, the rule the tile and the phone read too). The
    /// daemon decides the rest at the press.
    private func mergeControls(_ live: BoardCard) -> Bool {
        mergeOffered(live)
            || CardMerge.fixOffered(mergeState: live.mergeState,
                                    daemonOffers: live.mergeOffered)
            || CardMerge.reviewOffered(column: live.column, branch: live.worktreeBranch,
                                       reviewRunning: live.reviewRunning,
                                       manualDue: live.needsManualCheck,
                                       daemonOffers: live.mergeOffered)
    }

    /// MERGE is offered where the rule says so, which includes the daemon's
    /// own fact on the card (`merge_offered`: a Failed hand-check shows
    /// nowhere else).
    private func mergeOffered(_ live: BoardCard) -> Bool {
        CardMerge.offered(column: live.column, branch: live.worktreeBranch,
                          mergeState: live.mergeState,
                          manualDue: live.needsManualCheck,
                          daemonOffers: live.mergeOffered)
    }

    /// The three buttons. MERGE and Fix arm and then confirm, each in its
    /// own `BoardState` slot; Run review is one press. Refusals land in the
    /// card's refusal line, which the footer draws.
    @ViewBuilder
    private func mergeVerbs(_ live: BoardCard) -> some View {
        HStack(spacing: 10) {
            if mergeOffered(live) {
                let armed = state.mergeArmed == live.id
                Button(CardMerge.mergeLabel(mergeState: live.mergeState, armed: armed)) {
                    if armed {
                        state.disarm()
                        merge(live)
                    } else {
                        state.armMerge(live.id)
                    }
                }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(armed ? Color.orange : Theme.faint)
                .clickable()
            } else if live.mergeState == "merging" {
                Text(CardMerge.mergeLabel(mergeState: "merging", armed: false))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
            }
            if CardMerge.fixOffered(mergeState: live.mergeState,
                                    daemonOffers: live.mergeOffered) {
                let armed = state.fixArmed == live.id
                Button(CardMerge.fixLabel(armed: armed)) {
                    if armed {
                        state.disarm()
                        fixMerge(live)
                    } else {
                        state.armFix(live.id)
                    }
                }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(armed ? Color.orange : Theme.faint)
                .clickable()
            }
            if CardMerge.reviewOffered(column: live.column, branch: live.worktreeBranch,
                                       reviewRunning: live.reviewRunning,
                                       manualDue: live.needsManualCheck,
                                       daemonOffers: live.mergeOffered) {
                Button("Run review") {
                    state.disarm()
                    runReview(live)
                }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundStyle(Theme.faint)
                .clickable()
            }
        }
        .font(Theme.mono(11))
    }

    private func merge(_ live: BoardCard) {
        state.refusals[live.id] = ""
        let tip = changes?.branchTip ?? ""
        Task { @MainActor in
            let result = await client.boardMerge(live.id, expectedTip: tip)
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
            await loadChanges()
        }
    }

    private func fixMerge(_ live: BoardCard) {
        state.refusals[live.id] = ""
        Task { @MainActor in
            let result = await client.boardMergeFix(live.id)
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
        }
    }

    private func runReview(_ live: BoardCard) {
        state.refusals[live.id] = ""
        Task { @MainActor in
            let result = await client.boardReviewRun(live.id)
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
        }
    }

    /// CHANGES is drawn on a Done or ended card that remembers a branch.
    private func showsChanges(_ live: BoardCard) -> Bool {
        !live.worktreeBranch.isEmpty && (stage == .done || stage == .ended)
    }

    private var changesOpen: Bool {
        openedSections.contains(.changes)
    }

    /// One request per open and per moment the page can go stale — never
    /// per frame, and never while the section is closed.
    private var changesFetchKey: String {
        guard changesOpen, let live else { return "closed" }
        return "\(live.id)-\(live.mergeState)-\(live.reviewVerdict)-\(live.reviewRunning)"
    }

    private func loadChanges() async {
        guard changesOpen, !isComposer, let live, showsChanges(live) else {
            changes = nil
            changesFailed = false
            changeFile = nil
            changeDiff = nil
            return
        }
        if let report = await client.cardChanges(live.id) {
            changes = report
            changesFailed = false
        } else {
            changesFailed = changes == nil
        }
        changeFile = nil
        changeDiff = nil
    }

    private func openChange(_ index: Int) {
        guard let live, let tip = changes?.branchTip, !tip.isEmpty else { return }
        if changeFile == index {
            changeFile = nil
            changeDiff = nil
            return
        }
        changeFile = index
        changeDiff = nil
        Task {
            let fetched = await client.cardChangeDiff(live.id, file: index, tip: tip)
            guard changeFile == index else { return }
            changeDiff = fetched ?? CardChangeDiff.unreadable
        }
    }

    /// The branch against the main line: commits, then files with their
    /// added and removed lines, a file's changes on a tap, the review's
    /// verdict and the three buttons. Every sentence is the daemon's or the
    /// rule's; this view composes none.
    @ViewBuilder
    private var changesSection: some View {
        if let live, showsChanges(live) {
            VStack(alignment: .leading, spacing: 6) {
                Text("CHANGES")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                if let report = changes {
                    changesBody(report)
                } else if changesFailed {
                    Text("Dark Army could not read this branch's changes.")
                        .font(.system(size: 12))
                        .foregroundStyle(.orange)
                } else {
                    Text("Reading the branch…")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
                if mergeControls(live) {
                    mergeVerbs(live)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func changesBody(_ report: CardChangesReport) -> some View {
        if !report.available {
            Text(report.reason)
                .font(.system(size: 12))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            Text("\(report.ahead) ahead of \(report.trunk), \(report.behind) behind")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            ForEach(report.commits) { commit in
                HStack(spacing: 8) {
                    Text(commit.sha8)
                        .font(Theme.mono(11))
                        .foregroundStyle(.secondary)
                    Text(commit.subject)
                        .font(.system(size: 12))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if report.commitsTruncated {
                Text("· more commits than are listed")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
            ForEach(Array(report.files.enumerated()), id: \.offset) { pair in
                changeFileRow(pair.offset, pair.element)
            }
            if report.filesTruncated {
                Text("· \(report.filesTotal) files changed, not all listed")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
            if !report.mergeOffered, !report.mergeRefusal.isEmpty {
                Text(report.mergeRefusal)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private func changeFileRow(_ index: Int, _ file: CardChangeFile) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Text(file.path)
                    .font(Theme.mono(11))
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(file.binary ? "binary" : "+\(file.added) \u{2212}\(file.removed)")
                    .font(Theme.mono(11))
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
            .onTapGesture { openChange(index) }
            .clickable()
            if changeFile == index {
                if let diff = changeDiff {
                    if diff.available {
                        ScrollView(.horizontal) {
                            Text(diff.text)
                                .font(Theme.mono(10))
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(maxHeight: 320)
                        if diff.truncated {
                            Text("· cut short")
                                .font(.system(size: 10))
                                .foregroundStyle(.tertiary)
                        }
                    } else {
                        Text(diff.reason)
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                    }
                } else {
                    Text("Reading that file…")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    /// Two-state segmented text control: Build (`""`) / Scout (`"scout"`).
    @ViewBuilder
    private var kindControl: some View {
        HStack(spacing: 0) {
            kindChip("Build", selected: state.draft.kind != "scout") {
                state.draft.kind = ""
            }
            kindChip("Scout", selected: state.draft.kind == "scout") {
                state.draft.kind = "scout"
            }
        }
        .font(Theme.mono(10))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Kind")
    }

    private func kindChip(_ title: String, selected: Bool,
                          _ action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(selected ? Theme.phosphorBright : Theme.faint)
            .overlay(Rectangle().strokeBorder(
                selected ? Theme.phosphor : Theme.hair, lineWidth: 1))
            .accessibilityAddTraits(selected ? .isSelected : [])
            .clickable()
    }

    private func setStartWhenPlanned(_ live: BoardCard, on: Bool) {
        state.refusals[live.id] = ""
        Task { @MainActor in
            let result = await client.boardStartWhenPlanned(live.id, on: on)
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
        }
    }

    @ViewBuilder
    private var planSection: some View {
        if let live, !live.planPath.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Plan")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                if let planText {
                    let split = PlanSplit.split(planText)
                    PlanDiagram(source: planText)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            MarkdownText(source: split.summary)
                                .equatable()
                            if let detail = split.detail {
                                Button(action: { planDetailOpen.toggle() }) {
                                    HStack(spacing: 6) {
                                        Image(systemName: "chevron.down")
                                            .font(.system(size: 8, weight: .semibold))
                                            .rotationEffect(.degrees(planDetailOpen ? 0 : -90))
                                            .foregroundStyle(Theme.faint)
                                        Text("TECHNICAL DETAIL")
                                            .font(Theme.mono(10, weight: .semibold))
                                            .tracking(0.8)
                                            .foregroundStyle(Theme.phosphor)
                                        Spacer()
                                    }
                                }
                                .buttonStyle(.plain)
                                .clickable()
                                .accessibilityLabel("Technical detail")
                                .accessibilityValue(planDetailOpen ? "shown" : "hidden")
                                if planDetailOpen {
                                    MarkdownText(source: detail)
                                        .equatable()
                                }
                            }
                        }
                        .padding(8)
                    }
                    .frame(height: 240)
                    .overlay(Rectangle()
                        .strokeBorder(Theme.hair, lineWidth: 1))
                    HStack(spacing: 10) {
                        Button("Open in editor") { revealInEditor(live.planPath) }
                            .buttonStyle(.borderless)
                            .controlSize(.small)
                            .font(.system(size: 10))
                            .clickable()
                        approveControl(live)
                    }
                } else if planMissing {
                    Text("The plan file is not where the card says it is.")
                        .font(.system(size: 11))
                        .foregroundStyle(.orange)
                    Text(live.planPath)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The scout's report, `planSection`'s twin and the reason a scout card
    /// exists: the rendered document, its path, Open in editor, and — on a
    /// Done scout — Promote, the one verb a report asks for. Before the
    /// scout has come back the section says so in a sentence, so the wait
    /// between Start and the attach reads as expected rather than as a miss
    /// (the 21 Sep 2026 "Before open report" complaint). Reads through
    /// `BoardDocuments.resolve` / `read` exactly as the plan does: one rule
    /// for "read a file a card points at safely".
    @ViewBuilder
    private var reportSection: some View {
        if let live, live.isScout {
            VStack(alignment: .leading, spacing: 6) {
                Text("Report")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                // The verdict the daemon stored at the attach, in full,
                // above the report — drawn even where the file has moved,
                // because the stored line is the point. `ScoutVerdictLine`
                // is the wording the tile and the phone share.
                if !live.reportPath.isEmpty,
                   let verdict = ScoutVerdictLine.text(
                       verdict: live.reportVerdict,
                       recommendation: live.reportRecommendation) {
                    Text(verdict)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                        .accessibilityLabel(ScoutVerdictLine.spoken(
                            verdict: live.reportVerdict,
                            recommendation: live.reportRecommendation))
                }
                if live.reportPath.isEmpty {
                    Text(live.column == BoardColumn.inProgress.rawValue
                         ? "Scouting — the report lands here when the investigation finishes."
                         : "No report yet — the scout writes one when it is started.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                } else if let reportText {
                    ScrollView {
                        MarkdownText(source: reportText)
                            .equatable()
                            .padding(8)
                    }
                    .frame(height: 240)
                    .overlay(Rectangle()
                        .strokeBorder(Theme.hair, lineWidth: 1))
                    HStack(spacing: 10) {
                        Button("Open in editor") { revealInEditor(live.reportPath) }
                            .buttonStyle(.borderless)
                            .controlSize(.small)
                            .font(.system(size: 10))
                            .clickable()
                        if canPromote(live) {
                            Button("Promote to a build card") { promote(live) }
                                .buttonStyle(.borderless)
                                .controlSize(.small)
                                .font(.system(size: 10))
                                .clickable()
                                .accessibilityLabel("Promote this report to a build card")
                        }
                        Text(BoardDocuments.display(live.reportPath, root: live.root))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                } else if reportMissing {
                    Text("The report file is not where the card says it is.")
                        .font(.system(size: 11))
                        .foregroundStyle(.orange)
                    Text(live.reportPath)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Through the daemon (`GET /api/manual-checks?path=`), never a file
    /// read on the main thread: the daemon's read is non-blocking and
    /// refuses a FIFO or a link, and it applies the same place and shape
    /// rule the press does, so "cannot be read" here is exactly "Passed /
    /// Failed would be refused".
    private func loadManualCheck() async {
        manualCheckText = nil
        manualCheckMissing = false
        manualCheckReason = ""
        manualCheckFetchFailed = false
        guard let live, !live.manualCheckPath.isEmpty else { return }
        let path = live.manualCheckPath
        let document = await client.manualCheckText(path: path)
        guard self.live?.manualCheckPath == path else { return }
        guard let document else {
            manualCheckFetchFailed = true
            return
        }
        if document.available {
            manualCheckText = document.text
        } else {
            manualCheckMissing = true
            manualCheckReason = document.reason
        }
    }

    private func loadReport() {
        reportText = nil
        reportMissing = false
        guard let live, !live.reportPath.isEmpty else { return }
        guard let resolved = BoardDocuments.resolve(live.reportPath,
                                                    root: live.root),
              let text = BoardDocuments.read(resolved) else {
            reportMissing = true
            return
        }
        reportText = text
    }

    private func loadPlan() {
        planDetailOpen = false
        planText = nil
        planMissing = false
        guard let live, !live.planPath.isEmpty else { return }
        guard let resolved = BoardDocuments.resolve(live.planPath,
                                                    root: live.root),
              let text = BoardDocuments.read(resolved) else {
            planMissing = true
            return
        }
        planText = text
        planDigest = Self.digest(text)
    }

    /// The approval control: absent on a daemon that does not take one (a
    /// version marker, never an inert button), and drawn as a statement
    /// rather than a button once this exact wording is the approved one.
    ///
    /// Approval is of a *version*, so it reads against the digest of the text
    /// on screen rather than against a flag — a plan edited after approval
    /// goes back to offering the button, which is the same fact the daemon's
    /// gate is about to state on the next Start.
    @ViewBuilder
    private func approveControl(_ live: BoardCard) -> some View {
        if !planDigest.isEmpty {
            if live.planApproved == planDigest {
                Text("approved")
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.phosphor)
            } else {
                Button(approving ? "Approving…" : "Approve this plan") {
                    approvePlan(live)
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .font(.system(size: 10))
                .disabled(approving)
                .clickable()
            }
        }
    }

    private func approvePlan(_ live: BoardCard) {
        guard !approving, !planDigest.isEmpty else { return }
        approving = true
        state.refusals[live.id] = ""
        let digest = planDigest
        Task { @MainActor in
            let result = await client.approvePlan(
                cardId: live.id, planPath: live.planPath, digest: digest)
            approving = false
            if !result.ok { state.refusals[live.id] = result.detail }
            await client.refresh()
        }
    }

    /// SHA-256 of a plan's text as the daemon hashes it: the UTF-8 bytes,
    /// lowercase hex. `CryptoKit` rather than a hand-rolled digest — the two
    /// sides must agree exactly or every approval is refused.
    static func digest(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8))
            .map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - The documents a card points at

    /// **Following the plan the card names.** Nearly every card an agent writes
    /// says "implement the accepted plan at plans/…", and until now that string
    /// was a dead end: the path was on the card, the file was on the disk, and
    /// there was no way from one to the other short of retyping it into an
    /// editor. `BoardDocuments` finds the paths, checks they exist inside the
    /// card's own project, and this shows them.
    ///
    /// The panel reads the file **itself** rather than asking the daemon for it,
    /// and that is not a shortcut: the panel is an ordinary process running as
    /// the user, with exactly the user's own access to their own files. Adding
    /// an API route would mean a loopback endpoint that serves arbitrary file
    /// contents, which is a strictly larger thing than a window reading a file
    /// the person looking at it can already open in any editor.
    @ViewBuilder
    private var documents: some View {
        let paths = BoardDocuments.references(in: state.draft.prompt + "\n"
                                              + state.draft.summary,
                                              root: state.draft.root)
        if !paths.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Referenced files")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                ForEach(paths, id: \.self) { path in
                    HStack(spacing: 8) {
                        Button {
                            openDocument(path)
                        } label: {
                            Text(BoardDocuments.display(path,
                                                        root: state.draft.root))
                                .font(.system(size: 11, design: .monospaced))
                        }
                        .buttonStyle(.link)
                        .clickable()
                        Button("Open in editor") { revealInEditor(path) }
                            .buttonStyle(.borderless)
                            .controlSize(.small)
                            .font(.system(size: 10))
                            .clickable()
                        Spacer()
                    }
                }
                if let openedPath {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(BoardDocuments.display(openedPath,
                                                        root: state.draft.root))
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Close") { self.openedPath = nil }
                                .buttonStyle(.borderless)
                                .controlSize(.small)
                                .font(.system(size: 10))
                                .clickable()
                        }
                        ScrollView {
                            // A plan is written to be read, and everything else
                            // a card points at is source. See `Markdown.swift`.
                            if BoardDocuments.isMarkdown(openedPath) {
                                MarkdownText(source: openedText)
                                    .equatable()
                                    .padding(8)
                            } else {
                                Text(openedText)
                                    .font(.system(size: 11, design: .monospaced))
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(8)
                            }
                        }
                        .frame(height: 240)
                        .overlay(Rectangle()
                            .strokeBorder(Theme.hair, lineWidth: 1))
                    }
                }
            }
        }
    }

    private func openDocument(_ path: String) {
        openedPath = path
        openedText = BoardDocuments.read(path)
            ?? "Dark Army could not read this file."
    }

    /// Hand the file to VS Code if it is installed, and to whatever the system
    /// would use otherwise. Not routed through the daemon's `vscode_reveal`:
    /// that machinery is about finding the *window that owns a session*, and
    /// this is a document with no session in it.
    ///
    /// It posts `.panelDidJump` on success, because this press *is* the user
    /// sending their attention to the editor, and a panel left standing in
    /// front of the file it just opened is covering the answer. Success-gated
    /// for the sheet's own Jump's reason: a yield with no editor coming
    /// forward leaves neither window, which reads as a dropped press.
    private func revealInEditor(_ path: String) {
        let url = URL(fileURLWithPath: path)
        let code = URL(fileURLWithPath: "/Applications/Visual Studio Code.app")
        if FileManager.default.fileExists(atPath: code.path) {
            NSWorkspace.shared.open([url], withApplicationAt: code,
                                    configuration: NSWorkspace.OpenConfiguration()) { _, error in
                guard error == nil else { return }
                // The completion handler arrives on an arbitrary queue; cross
                // to the main thread explicitly rather than leaning on the
                // observer's own `queue: .main`.
                DispatchQueue.main.async {
                    NotificationCenter.default.post(name: .panelDidJump, object: nil)
                }
            }
        } else if NSWorkspace.shared.open(url) {
            NotificationCenter.default.post(name: .panelDidJump, object: nil)
        }
    }

    // MARK: - The session doing this card

    /// The assistant, if one is bound — identity and Jump only. Asking a
    /// question about the card is the thread below, not a one-way send at
    /// the session.
    /// The subject ladder, matching the card face's one-chip rule: the session
    /// doing the work first, the one refining it second, and the one that
    /// filed the card only while neither of the others exists. The author
    /// variant is drawn only when the author still resolves in the fleet, so
    /// every old card whose author is long gone adds no noise.
    @ViewBuilder
    private var sessionSection: some View {
        if let live, !live.sessionId.isEmpty {
            sessionDetails(for: live.sessionId,
                           heading: "The assistant on this card")
        } else if let live, live.refineState == "live",
                  !live.refineSessionId.isEmpty {
            sessionDetails(for: live.refineSessionId,
                           heading: "The session refining this card")
        } else if let live, live.isAgentAuthored,
                  agent(for: live.author) != nil {
            sessionDetails(for: live.author,
                           heading: "The session that added this card")
        }
    }

    @ViewBuilder
    private func sessionDetails(for sessionId: String, heading: String) -> some View {
        let found = agent(for: sessionId)
        VStack(alignment: .leading, spacing: 8) {
            Text(heading)
                .font(Theme.mono(11, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            HStack(spacing: 8) {
                if let (row, bucket) = found {
                    Text(row.nickname.isEmpty
                         ? String(row.sessionId.prefix(8)) : row.nickname)
                        .font(.system(size: 12, weight: .medium))
                    Text(bucketLabel(row, bucket))
                        .font(.system(size: 11))
                        .foregroundStyle(bucket == "waiting"
                                         ? Color.red : Color.secondary)
                    Spacer()
                    if row.isJumpable {
                        Button("Jump to it") { jump(row) }
                            .controlSize(.small)
                            .clickable()
                    }
                } else {
                    Text("session \(String(sessionId.prefix(8))) — "
                         + "the fleet no longer lists it")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }
        }
    }

    /// One request per open and per stage change, not per frame: a card
    /// that moved has a new moment to list.
    private var timelineFetchKey: String {
        "\(live?.id ?? "")-\(stage)"
    }

    private var threadFetchKey: String {
        "\(live?.id ?? "")-\(live?.threadCount ?? 0)"
    }

    /// Re-fetched when the record changes, not on every frame: the headline
    /// on the snapshot carries the stamp of when Dark Army wrote it down, so the
    /// key moves exactly once per finished run.
    private var workRecordFetchKey: String {
        "\(live?.id ?? "")-\(live?.workRecord?.at ?? 0)"
    }

    private func loadWorkRecord() async {
        workRecord = nil
        openFile = nil
        fileDiff = nil
        guard !isComposer, let live else { return }
        // Absent on the card means there is nothing to fetch. Fetching anyway
        // would turn "no record" into a request per open for every card that
        // has never been run.
        guard live.workRecord != nil else { return }
        if let full = await client.boardCard(live.id)?.workRecordFull {
            workRecord = full
            return
        }
        // An older per-card reply carried no record; the dedicated route is
        // the fallback rather than a second normal path.
        workRecord = await client.workRecordReport(live.id)?.record
    }

    private func openDiff(_ index: Int) {
        guard let live else { return }
        if openFile == index {
            openFile = nil
            fileDiff = nil
            return
        }
        openFile = index
        fileDiff = nil
        Task {
            let fetched = await client.workRecordDiff(live.id, file: index)
            guard openFile == index else { return }
            fileDiff = fetched
        }
    }

    /// What Dark Army observed the run do, under WHAT RAN.
    ///
    /// Every sentence here is the daemon's, drawn verbatim: the caption, the
    /// verdict line and the reason a file list is missing. This view composes
    /// no wording of its own about what happened — that rule is what keeps
    /// the Mac and the phone saying the same thing about the same record.
    @ViewBuilder
    private var workRecordSection: some View {
        if let live, live.workRecord != nil {
            VStack(alignment: .leading, spacing: 6) {
                Text("WHAT CHANGED")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                Text("Dark Army watched this run and wrote this down; nothing here is the assistant's own claim. The files are what changed in this project since the work started, which can include another agent working in the same folder.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
                if let record = workRecord {
                    workRecordBody(record)
                } else {
                    Text("Dark Army is fetching what it wrote down…")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func workRecordBody(_ record: WorkRecord) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if !verdictWords(record.verdict).isEmpty {
                Text(verdictWords(record.verdict))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            if !record.report.isEmpty {
                // The report is somebody else's words: rendered through the
                // existing markdown copy with its link-stripping rule intact,
                // never parsed and never used to decide anything.
                MarkdownText(source: record.report)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(WorkRecordFormat.summary(record))
                .font(.system(size: 12))
                .foregroundStyle(record.filesAvailable
                                 ? Color.secondary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
            // What the shunt helper did — the daemon's own sentence
            // (`shuntWords`), drawn only where something was delegated.
            if record.shuntDelegations > 0 && !record.shuntWords.isEmpty {
                Text(record.shuntWords)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(record.files.enumerated()), id: \.offset) { pair in
                workRecordFileRow(pair.offset, pair.element)
            }
        }
    }

    @ViewBuilder
    private func workRecordFileRow(_ index: Int, _ file: WorkRecordFile) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Text(file.path)
                    .font(Theme.mono(11))
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(WorkRecordFormat.counts(file))
                    .font(Theme.mono(11))
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
            .onTapGesture { openDiff(index) }
            .clickable()
            if openFile == index {
                if let diff = fileDiff {
                    if diff.available {
                        // A diff is not markdown. Plain monospaced text, and
                        // its own horizontal scroll so a long line never makes
                        // the sheet scroll sideways.
                        ScrollView(.horizontal) {
                            Text(diff.text)
                                .font(Theme.mono(10))
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(maxHeight: 320)
                        if diff.truncated {
                            Text("· cut short")
                                .font(.system(size: 10))
                                .foregroundStyle(.tertiary)
                        }
                    } else {
                        Text(diff.reason)
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                    }
                } else {
                    Text("Reading that file…")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    /// The daemon's own sentence for a verdict. Kept here rather than fetched
    /// so an older daemon's record still reads as words; a verdict this build
    /// does not know draws **nothing** rather than an invented line.
    private func verdictWords(_ verdict: String) -> String {
        switch verdict {
        case "closed": return "The assistant said it had finished this card."
        case "manual": return "The assistant left a check for somebody to do by hand."
        case "quiet": return "The run ended without the assistant saying anything."
        default: return ""
        }
    }

    /// Direct when the bound row is a live claude channel; otherwise a helper.
    private var asksViaConsultant: Bool {
        guard let live, live.linkState == "live", !live.sessionId.isEmpty,
              let (row, _) = agent(for: live.sessionId), row.channel,
              row.provider.isEmpty || row.provider == "claude"
        else { return true }
        return false
    }

    private var askLabel: String {
        asksViaConsultant ? "Ask — starts a helper session" : "Ask"
    }

    /// Whether this corner of the sheet is the *message* box rather than the
    /// *ask* box: an assistant is working this card right now, and the daemon
    /// says it could type at it on this card's behalf.
    ///
    /// Every term is the daemon's own answer, re-checked at the press. The
    /// last is the version marker, so against an older daemon the button is
    /// absent rather than drawn and refused.
    private var messagesTerminal: Bool {
        guard let live, live.linkState == "live", !live.sessionId.isEmpty,
              let (row, _) = agent(for: live.sessionId), row.canMessage
        else { return false }
        return true
    }

    @ViewBuilder
    private var threadSection: some View {
        if live != nil {
            VStack(alignment: .leading, spacing: 8) {
                // The heading names what this row draws: the thread alone
                // while the band above owns the message box on a running
                // card, the box's own sentence where it is drawn here.
                Text(messagesTerminal
                     ? (nextAction == .reply
                        ? "Messages"
                        : "Send a message to the session working this card")
                     : "Ask about this card")
                    .font(Theme.mono(11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                ForEach(thread) { msg in
                    threadRow(msg)
                }
                if !sendNote.isEmpty, nextAction != .reply {
                    Text(sendNote)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                if messagesTerminal {
                    // The band draws the box while the card is being
                    // worked; here it stays for a stage the band leads
                    // with something else.
                    if nextAction != .reply {
                        messageBox
                    }
                } else if asksViaConsultant && !board.dispatchEnabled {
                    Text("Dark Army may not start sessions, so there is no way "
                         + "to ask this card right now.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                } else {
                    HStack(spacing: 8) {
                        TextField("Ask a question about this card",
                                  text: $message)
                            .textFieldStyle(.roundedBorder)
                            .font(.system(size: 12))
                            .focused($messageFocused)
                            .onSubmit { ask() }
                        if DictateButton.available(client.context.settings) {
                            DictateButton(focus: { messageFocused = true })
                        }
                        Button(askLabel) { ask() }
                            .controlSize(.small)
                            .disabled(sending || message.trimmingCharacters(
                                in: .whitespacesAndNewlines).isEmpty)
                    }
                    // The third of the three strings that have to move
                    // together — heading, button, caption. Two different acts
                    // share this corner of the sheet, so each says which one
                    // it is doing rather than leaving it to be inferred.
                    Text(asksViaConsultant
                         ? "Answered by a helper session, not by whoever is "
                           + "working this card."
                         : "Sent down this session's own channel, for it to "
                           + "read between turns.")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    /// The message box: a disclosure button, then a single-line field with
    /// Send and Cancel. Single-line on purpose — the daemon refuses a message
    /// containing a line break, because `sendText` ends in a newline and the
    /// tail of a two-line message would be typed at a prompt that had already
    /// moved on.
    @ViewBuilder
    private var messageBox: some View {
        if messageOpen {
            HStack(spacing: 8) {
                TextField("Say something to this assistant",
                          text: $message)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12))
                    .focused($messageFocused)
                    .onSubmit { sendToTerminal() }
                    // Asked for here rather than in the disclosure button's
                    // action: at the moment of the press the field does not
                    // exist yet, so AppKit drops the focus request and the box
                    // opens with no first responder.
                    .onAppear { messageFocused = true }
                if DictateButton.available(client.context.settings) {
                    DictateButton(focus: { messageFocused = true })
                }
                Button("Send") { sendToTerminal() }
                    .controlSize(.small)
                    .disabled(sending || message.trimmingCharacters(
                        in: .whitespacesAndNewlines).isEmpty)
                Button("Cancel") {
                    message = ""
                    sendNote = ""
                    messageOpen = false
                }
                .controlSize(.small)
            }
            Text("Typed straight onto that terminal's input line — the same "
                 + "as typing it there yourself.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        } else {
            Button("SEND A MESSAGE") {
                sendNote = ""
                messageOpen = true
            }
            .controlSize(.small)
            Text("Typed straight onto that terminal's input line — the same "
                 + "as typing it there yourself.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func threadRow(_ msg: CardMessage) -> some View {
        if msg.kind == "question" {
            HStack {
                Spacer(minLength: 40)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(msg.text)
                        .font(.system(size: 12))
                        .padding(8)
                        .background(Theme.hair.opacity(0.5))
                    Text("you")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                    // Which of the two acts this was. An older daemon writes
                    // no `terminal` via and falls straight past this.
                    if msg.via == "terminal" {
                        Text("typed into the session's terminal")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }
            }
        } else if msg.kind == "answer" {
            VStack(alignment: .leading, spacing: 2) {
                Text(msg.authorName.isEmpty ? msg.author : msg.authorName)
                    .font(Theme.mono(10, weight: .medium))
                    .foregroundStyle(Theme.phosphor)
                Text(msg.text)
                    .font(.system(size: 12))
                if msg.via == "consultant" {
                    Text(consultantCaption)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            Text(msg.text)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
    }

    private var consultantCaption: String {
        let hadSession = !(live?.sessionId ?? "").isEmpty
        return hadSession
            ? "answered by a new session — not the one that did the work"
            : "answered by a helper session"
    }

    private func bucketLabel(_ row: Agent, _ bucket: String) -> String {
        switch bucket {
        case "running": return "working"
        case "waiting": return "needs you"
        case "sleeping": return "quiet"
        default: return row.alive == true ? "quiet" : "finished"
        }
    }

    private func agent(for sessionId: String) -> (Agent, String)? {
        client.snapshot.agents.row(session: sessionId)
    }

    private func jump(_ row: Agent) {
        sendNote = ""
        if row.ownTerminal {
            NotificationCenter.default.post(
                name: .panelShowSession, object: row.sessionId)
            return
        }
        Task { @MainActor in
            let result = await client.act("reveal_session", session: row.sessionId)
            if result.ok {
                NotificationCenter.default.post(name: .panelDidJump, object: nil)
            } else {
                // Same surface Send already uses. Parking a 409 on the card
                // behind this sheet is a mute press — the sheet is what the
                // person is looking at.
                sendNote = result.detail.isEmpty
                    ? "No terminal to jump to."
                    : result.detail
            }
        }
    }

    private func ask() {
        let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let live, !text.isEmpty else { return }
        sending = true
        sendNote = ""
        Task { @MainActor in
            let result = await client.boardAsk(cardId: live.id, text: text)
            sending = false
            if result.ok {
                message = ""
                sendNote = asksViaConsultant
                    ? "Asked — a helper session is starting."
                    : "Asked."
            } else {
                sendNote = result.detail
            }
        }
    }

    /// `ask()`'s shape exactly, and a different verb. On a refusal the typed
    /// text is left alone — every one of the daemon's seven refusals is
    /// something the person can fix by editing what they wrote or by waiting,
    /// and clearing the field would make them type it again.
    private func sendToTerminal() {
        let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let live, !text.isEmpty else { return }
        let sentId = live.id
        sending = true
        sendNote = ""
        Task { @MainActor in
            let result = await client.boardMessage(cardId: sentId, text: text)
            sending = false
            // The window is reused, so the person may have swapped to another
            // card while this was on the wire — and the swap already cleared
            // the box (`.onChange(of: live?.id)`). Writing the outcome now
            // would draw "Sent." on card B for a message typed into card A's
            // terminal. `sending` is released above the guard: it gates card
            // B's Send button too, and this send really has finished.
            //
            // Asked of `state.editing` rather than of `live`: `state` is the
            // one shared reference this window follows, while `card` — and so
            // `live` — is a `let` on a struct the swap replaces, so the `self`
            // this closure captured would still name card A for ever.
            guard state.editing == sentId else { return }
            if result.ok {
                message = ""
                messageOpen = false
                sendNote = "Sent."
                await loadThread()
            } else {
                sendNote = result.detail
            }
        }
    }

    private func loadThread() async {
        guard let live, !isComposer else { return }
        if let full = await client.boardCard(live.id) {
            thread = full.messages
        }
    }

    private func loadRunRecord() async {
        runReport = nil
        runFetchFailed = false
        guard !isComposer, let sid = live?.sessionId, !sid.isEmpty else { return }
        if let report = await client.sessionRecord(sid) {
            runReport = report
        } else {
            runFetchFailed = true
        }
    }

    // MARK: - The queue

    /// Where this card sits in its project's pipeline, and the one way out.
    ///
    /// Unarmed, unlike Delete and Stop and Wrap up: taking a card out of the
    /// queue destroys nothing and the undo is pressing Start again. An arm on
    /// a reversible verb teaches people to press twice, which is what makes
    /// the arms on the irreversible ones stop working.
    /// What a queued card is waiting for, in words. One axis: a free place
    /// in this project, which is the whole waiting rule — whether Dark Army takes
    /// it by itself is `autostartEnabled`.
    private var queuedExplanation: String {
        board.autostartEnabled
            ? "Waiting for a free launch slot. Dark Army starts it in a moment."
            : "Waiting for a free launch slot. Dark Army will not start it on "
              + "its own — press Start."
    }

    @ViewBuilder
    private var queueSection: some View {
        if let live, live.isQueued {
            VStack(alignment: .leading, spacing: 6) {
                Divider()
                Text("QUEUED")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                    .tracking(0.6)
                HStack(spacing: 10) {
                    Text(queuedExplanation)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Button("Take out of the queue") { unqueue(live.id) }
                        .controlSize(.small)
                        .clickable()
                }
            }
        }
    }

    /// The cards this one waits on, one row each — the title, the daemon's
    /// met bit as a word, and ✕ to stop waiting on it — then the daemon's
    /// Unblocks line verbatim, then **Add…**: this project's other cards
    /// that are not Done and not already listed. Every change writes the
    /// whole list through `setDependencies`; the daemon refuses a loop, a
    /// self-wait and another project in words, drawn on the refusal line.
    @ViewBuilder
    private var dependenciesSection: some View {
        if let live {
            VStack(alignment: .leading, spacing: 6) {
                Divider()
                Text(CardSections.Section.dependencies.rawValue)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                    .tracking(0.6)
                ForEach(live.dependencies) { dep in
                    HStack(spacing: 8) {
                        Text(dep.title.isEmpty ? "untitled" : dep.title)
                            .font(.system(size: 11))
                            .fixedSize(horizontal: false, vertical: true)
                        Text(dependencyWord(dep))
                            .font(.system(size: 10))
                            .foregroundStyle(dep.met ? Theme.phosphor : Theme.dim)
                        Spacer(minLength: 8)
                        Button("\u{2715}") {
                            setDependencies(live, live.linkedIds.filter { $0 != dep.id })
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("Stop waiting on \(dep.title)")
                        .clickable()
                    }
                }
                if !live.dependentsLine.isEmpty {
                    Text(live.dependentsLine)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                let choices = dependencyChoices(live)
                if live.column != "done", !choices.isEmpty {
                    Menu("Add\u{2026}") {
                        ForEach(choices) { other in
                            Button(other.title.isEmpty ? "untitled" : other.title) {
                                setDependencies(live, live.linkedIds + [other.id])
                            }
                        }
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .controlSize(.small)
                    .accessibilityLabel("Add a card this one waits on")
                    .clickable()
                } else if live.dependencies.isEmpty, live.dependents.isEmpty {
                    Text("Waits on nothing.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    /// The daemon's met bit in words — the same three `dependency_line`
    /// uses. `met` is never re-derived here; the column only says *why*.
    private func dependencyWord(_ dep: CardDependency) -> String {
        if dep.column == "done" { return "done" }
        return dep.met ? "check pending" : "not yet"
    }

    /// What **Add…** offers: this project's other cards, none in Done, none
    /// already listed, and nothing once the list holds the store's eight.
    private func dependencyChoices(_ live: BoardCard) -> [BoardCard] {
        BoardCard.dependencyChoices(for: live, in: board.cards)
    }

    /// Write this card's whole dependency list — `setTool`'s route exactly:
    /// `board_update` through `outcomeWrite`, guarded at the draft's own
    /// revision, the draft then brought up to date from the reply so the
    /// next Save is not refused as somebody else's change. The ids ride as
    /// one newline-joined string, the store's own shape.
    private func setDependencies(_ live: BoardCard, _ ids: [String]) {
        state.disarm()
        Task { @MainActor in
            let result = await client.outcomeWrite(
                action: "board_update", cardId: live.id,
                fields: ["blocked_by": ids.joined(separator: "\n"),
                         "expected_revision": String(state.draft.revision)])
            await client.refresh()
            guard state.editing == live.id else { return }
            state.refusals[live.id] = result.ok ? "" : result.detail
            if result.isCardChangedRefusal {
                cardChangedAt = result.currentRevision
                return
            }
            guard result.ok else { return }
            if let revision = result.cardRevision {
                state.draft.revision = revision
            }
            cardChangedAt = nil
        }
    }

    private func unqueue(_ id: String) {
        Task { @MainActor in
            let result = await client.boardUnqueue(id)
            if !result.ok { state.refusals[id] = result.detail }
            await client.refresh()
        }
    }

    /// The hand-check the session that did the work handed back, and the one
    /// press that ends it.
    ///
    /// Unarmed, `queueSection`'s reason: the person pressing this has just
    /// done the chore, and an arm on a verb that follows a chore teaches
    /// people to press twice — which is what makes the arms on Delete and Stop
    /// stop working. It does destroy a note, and the undo is that the session
    /// can say it again.
    ///
    /// Above the danger zone rather than below it: this is the reason the
    /// sheet was opened on a flagged card, and Delete stays last on every
    /// surface that offers it.
    /// The unconditional flag deliberately, never the card's `manualCheckDue`:
    /// the card and the rail hide the line while the assistant is working,
    /// but a person who opened the detail may read and clear a stale note at
    /// any time. The undo is the same one the button already has — the
    /// session can say it again.
    @ViewBuilder
    private var manualCheckSection: some View {
        if let live, live.needsManualCheck {
            VStack(alignment: .leading, spacing: 6) {
                Divider()
                Text("MANUAL CHECK NEEDED")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(Theme.amber)
                    .tracking(0.6)
                // As written, verbatim and unparsed. The steps are somebody's
                // sentences about their own work — numbering them again here,
                // or reflowing them into a list, would be this view inventing
                // structure it cannot know is there.
                Text(live.manualSteps)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if !live.manualCheckPath.isEmpty && !manualCheckMissing {
                    manualCheckFile(live)
                } else {
                    if !live.manualCheckPath.isEmpty {
                        // The file the card names cannot be served any
                        // more, so Passed / Failed would be refused: say
                        // why, and offer Mark checked instead.
                        Text(manualCheckReason.isEmpty
                             ? "The check file cannot be read."
                             : "The check file cannot be read — \(manualCheckReason).")
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                            .textSelection(.enabled)
                    }
                    manualClearRow(live)
                }
            }
        }
    }

    @ViewBuilder
    private func manualClearRow(_ live: BoardCard) -> some View {
        if nextAction != .markChecked {
                    // The band draws the press on the manual-check stage;
                    // this copy is for a stale note read at any other stage.
                    HStack(spacing: 10) {
                        Button("Mark checked") { clearManual(live.id) }
                            .controlSize(.small)
                            .clickable()
                        Text("Clears the badge. The card does not move.")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                        Spacer()
                    }
        }
    }

    /// The check file under the card's manual-check section: the document
    /// (`reportSection`'s frame), Open in editor, Open in Checks, and
    /// unarmed **Passed** / **Failed** with a one-line note. The daemon
    /// writes the outcome into the file and clears this card's steps.
    @ViewBuilder
    private func manualCheckFile(_ live: BoardCard) -> some View {
        if let manualCheckText {
            ScrollView {
                MarkdownText(source: manualCheckText)
                    .equatable()
                    .padding(8)
            }
            .frame(height: 200)
            .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
        } else if manualCheckFetchFailed {
            Text(ManualCheckRules.fetchFailedLine)
                .font(.system(size: 11))
                .foregroundStyle(.orange)
        }
        HStack(spacing: 10) {
            ManualCheckButton(action: { revealInEditor(live.manualCheckPath) }) {
                Text("Open in editor").font(.system(size: 10))
            }
            ManualCheckButton(action: {
                state.onOpenManualCheck?(live.manualCheckPath)
            }) {
                Text("Open in Checks").font(.system(size: 10))
            }
            Text(BoardDocuments.display(live.manualCheckPath, root: live.root))
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
        HStack(spacing: 10) {
            TextField("note (optional)", text: $manualNote)
                .textFieldStyle(.roundedBorder)
                .controlSize(.small)
                .font(.system(size: 11))
            ManualCheckButton(action: { recordManual(live, status: "passed") }) {
                Text("Passed")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
            }
            .disabled(manualRecording || manualCheckLoading)
            ManualCheckButton(action: { recordManual(live, status: "failed") }) {
                Text("Failed")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.alarm)
            }
            .disabled(manualRecording || manualCheckLoading)
        }
    }

    /// The check file is still on its way: an outcome must not be recorded
    /// against steps the person has not been shown yet.
    private var manualCheckLoading: Bool {
        manualCheckText == nil && !manualCheckFetchFailed
    }

    private func recordManual(_ live: BoardCard, status: String) {
        let path = live.manualCheckPath
        let note = ManualCheckRules.clampedNote(manualNote)
        manualRecording = true
        Task { @MainActor in
            let result = await client.boardManualOutcome(
                path: path, status: status, note: note)
            manualRecording = false
            if result.ok {
                manualNote = ""
            } else {
                state.refusals[live.id] = result.detail
            }
            await client.refresh()
            await loadManualCheck()
        }
    }

    private func clearManual(_ id: String) {
        Task { @MainActor in
            let result = await client.boardManualClear(id)
            if !result.ok { state.refusals[id] = result.detail }
            await client.refresh()
        }
    }

    // MARK: - Deleting

    /// Armed, then confirmed, in the sheet as well as on the card. Two surfaces
    /// for one destructive verb both need the gate — the arm is per-view state
    /// here rather than `BoardState.deleteArmed` on purpose: this sheet is
    /// modal, the card behind it cannot be pressed while it is up, and sharing
    /// the slot would leave the board's own Delete armed after the sheet closed.
    private var dangerZone: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider()
            HStack(spacing: 10) {
                // `live`, not `card`: the sheet opens on a frozen copy, and a
                // dispatching card can bind while Details is up. The armed
                // label must follow the snapshot or it hides a close the
                // confirmed press will still do.
                Button(deleteArmed
                       ? (live?.deleteClosesTerminal == true
                          ? "Really delete? Its terminal closes too"
                          : "Really delete this card?")
                       : "Delete card") {
                    if deleteArmed {
                        deleteCard()
                    } else {
                        deleteArmed = true
                    }
                }
                .controlSize(.small)
                .foregroundStyle(Color.red)
                .clickable()
                if deleteArmed {
                    Button("Keep it") { deleteArmed = false }
                        .buttonStyle(.borderless)
                        .controlSize(.small)
                        .clickable()
                    Text(live?.deleteClosesTerminal == true
                         ? "The card and instructions go. Outcome evidence and measurements remain in history. The assistant's terminal closes with it."
                         : "The card and instructions go. Outcome evidence and measurements remain in history.")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
        }
        .padding(.top, 4)
    }

    private func deleteCard() {
        guard let card else { return }
        let id = card.id
        state.closeEditor()
        // The sheet is gone before the answer arrives, so the card behind it is
        // the only thing left to say the delete is in flight — same fade and
        // spinner the board's own Delete raises.
        state.deleting.insert(id)
        state.refusals[id] = ""
        Task { @MainActor in
            let result = await client.boardDelete(id)
            if !result.ok {
                state.deleting.remove(id)
                state.refusals[id] = result.detail
            }
            await client.refresh()
        }
    }

    // MARK: - Footer

    private var footer: some View {
        HStack(spacing: 10) {
            if isComposer,
               let note = state.refusals[BoardState.newCard], !note.isEmpty {
                Text(note)
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            } else if let live, let note = state.refusals[live.id], !note.isEmpty {
                Text(note)
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            } else if let live, !live.dispatchError.isEmpty {
                Text(live.dispatchError)
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            }
            // The one press out of a refused Save, beside the words that
            // refused it.
            cardChangedRow
            Spacer()
            // Two pieces of news, both of which can be true at once, so they
            // take opposite ends rather than one branch. The orange text on
            // the leading edge is the daemon's report about the *last* press;
            // this is a local statement about *this* one — a refused create
            // leaves the composer open with the refusal drawn, and clearing
            // the title then holds the button again. `layoutPriority` makes
            // the two-line refusal give way first: this line is short and
            // names the control six points to its right.
            if let reason = saveHoldReason {
                Text(reason)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(1)
                    .layoutPriority(1)
            }
            Button("Close") { state.closeEditor() }
                .keyboardShortcut(.cancelAction)
                .disabled(state.composerSaving)
            // The composer's second exit: the same create, with the
            // daemon's Refine on the end of it. Absent rather than inert
            // when Dark Army may not start sessions — `BoardCardView.canRefine`'s
            // first term. One click and no arm, `refineButton`'s argument
            // restated: the session it opens is an interview, not a
            // destruction. Held by exactly the conditions that hold Add to
            // Prep; Enter stays on that button.
            if isComposer, board.dispatchEnabled {
                // A scout has no plan to refine, so neither the tick nor
                // Add & Refine — Start is its Prep verb.
                if state.draft.kind != "scout" {
                    // A draft tick rather than an immediate write — the card does
                    // not exist yet, so it rides the create.
                    Button(state.draft.startWhenPlanned
                           ? "[x] start when planned"
                           : "[ ] start when planned") {
                        state.draft.startWhenPlanned.toggle()
                    }
                    .buttonStyle(.plain)
                    .controlSize(.small)
                    .foregroundStyle(state.draft.startWhenPlanned
                                     ? Theme.phosphor : Theme.faint)
                    .clickable()
                    Button("Add & Refine") { saveAndRefine() }
                        .buttonStyle(AlarmOutline(color: Theme.phosphor))
                        .disabled(saveHeld)
                        .clickable(!saveHeld)
                }
            }
            Button(isComposer ? "Add to Prep" : "Save") { save() }
                .buttonStyle(AlarmOutline(color: Theme.phosphor))
                .keyboardShortcut(.defaultAction)
                .disabled(saveHeld)
                // One condition, two consumers: the gate and the hand cannot
                // drift apart. `.disabled` does not stop the cursor overlay's
                // tracking area, so the hand needs its own say.
                .clickable(!saveHeld)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    /// Held while the prompt on screen is still only the preview, while
    /// Prepare is in flight, while a dropped file is still copying, or
    /// while a composer create is on the wire. `preparing` is its own
    /// term on `BoardState.saveHeld`, because a finished prepare must
    /// not unhold a truncated prompt.
    private var saveHeld: Bool {
        state.saveHeld(preparing: preparing)
    }

    /// The same computation, in words for the person looking at the held
    /// button. `nil` when nothing holds it.
    private var saveHoldReason: String? { state.saveHoldReason(preparing: preparing) }

    /// Prepare's pick, already applied, with one way back: plain faint text
    /// and a single button naming the project the picker held, not orange,
    /// because nothing has gone wrong. Both projects are re-resolved against
    /// the snapshot at draw time, so a window closed between the press and
    /// now draws no button rather than a bad one.
    @ViewBuilder
    private func suggestionRow() -> some View {
        if let picked = board.projects.first(where: { $0.root == suggestedRoot }),
           let previous = board.projects.first(where: { $0.root == revertRoot }) {
            HStack(spacing: 8) {
                Text("Dark Army picked \(picked.name.isEmpty ? picked.root : picked.name)")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Use \(previous.name.isEmpty ? previous.root : previous.name) instead") {
                    // Through the binding, so `draft.project` and `draft.root`
                    // stay in step exactly as the picker keeps them — and its
                    // setter clears both slots, which takes the line down.
                    projectBinding.wrappedValue = previous.root
                }
                .font(Theme.mono(10))
                .clickable(true)
            }
        }
    }

    /// The picker chooses a *root*, and the name rides along with it: the two
    /// have to agree, and a project typed by hand is exactly the thing that gets
    /// refused at dispatch.
    private var projectBinding: Binding<String> {
        Binding(
            get: { state.draft.root },
            set: { root in
                selectProject(root)
                // Choosing for yourself answers the question too.
                suggestedRoot = ""
                revertRoot = ""
            })
    }

    /// The write half of `projectBinding`, on its own so Prepare can apply
    /// its pick without going through the setter that clears the slots:
    /// `draft.root` plus the `draft.project` lookup, nothing else.
    private func selectProject(_ root: String) {
        // The composer's faces were computed against the project the picker
        // held when Prepare ran, so a move makes them another project's staff
        // — the Mac drops them at the write anyway, and faces that will not
        // survive the save must not sit on screen looking like a promise.
        // Composer only: a saved card's box is typed by a person and the
        // daemon refuses it in words, which is a correction, not a loss.
        if isComposer, !state.draft.root.isEmpty, root != state.draft.root {
            state.draft.workflow = ""
        }
        state.draft.root = root
        state.draft.project = board.projects
            .first(where: { $0.root == root })?.name ?? ""
    }

    /// Save a card that already exists.
    ///
    /// `expected_revision` is the change number of the copy this sheet
    /// loaded, so a card somebody changed underneath — from the phone, or
    /// from another window — is **refused in the daemon's words** rather
    /// than overwritten. The Mac gets the guard for the same reason the
    /// phone does: a Mac that silently overwrote a phone's edit would make
    /// the phone's guard theatre.
    ///
    /// `expecting` is the **Save anyway** press: the same fields again at
    /// the revision the refusal reported.
    private func save(expecting: Int? = nil) {
        if isComposer {
            saveComposer()
            return
        }
        let draft = state.draft
        let existing = card
        guard !state.outcomeSaving else { return }
        state.outcomeSaving = true
        Task { @MainActor in
            defer { state.outcomeSaving = false }
            guard let existing else { return }
            var fields = [
                "title": draft.title, "summary": draft.summary,
                "prompt": draft.prompt, "project": draft.project,
                "root": draft.root, "tool": draft.tool,
                "model": draft.model,
                // Always a string, never a number: `_board_fields` does
                // `str(payload.get(key) or "")`, so a JSON `0` would store
                // `""` — unscored rather than the lowest score.
                "priority": draft.priority,
                "workflow": draft.workflow,
            ]
            // The objective rides only where the person saw it: the
            // "Benefit and success" fold is closed by default on every
            // stage but Done, and `OutcomeEditor` — the one thing that
            // fetches the objective and marks the draft loaded — mounts
            // only once that fold is opened. A Save that waited on that
            // load could never be satisfied without opening the fold, and
            // sending the draft's empty defaults would wipe the objective.
            // The daemon judges the objective only when its fields are
            // named, so a save of the card's own fields leaves it alone.
            fields["area"] = draft.area
            fields["kind"] = draft.kind
            fields.merge(draft.objectiveFields()) { _, new in new }
            fields["expected_revision"] = String(expecting ?? draft.revision)
            let result = await client.outcomeWrite(action: "board_update", cardId: existing.id, fields: fields)
            if state.editing == existing.id {
                if result.isCardChangedRefusal {
                    // Every typed field is kept. The daemon's own words go
                    // on the card, and `cardChangedAt` arms the one press
                    // out of it.
                    state.refusals[existing.id] = result.detail
                    cardChangedAt = result.currentRevision
                } else if result.ok, let revision = result.revision {
                    state.draft.outcomeRevision = revision
                    if let card = result.cardRevision { state.draft.revision = card }
                    cardChangedAt = nil
                    state.closeEditor()
                } else {
                    state.refusals[existing.id] = result.detail
                }
            }
            await client.refresh()
        }
    }

    /// The one press out of a refused Save, drawn only where the daemon
    /// actually raised that refusal. It re-sends the same fields at the
    /// revision the refusal reported — deliberately not an automatic
    /// retry, and deliberately not a silent overwrite.
    @ViewBuilder
    private var cardChangedRow: some View {
        if let at = cardChangedAt {
            HStack(spacing: 10) {
                Button("Save anyway") { save(expecting: at) }
                    .controlSize(.small)
                    .clickable()
                Button("Leave it") { cardChangedAt = nil }
                    .buttonStyle(.borderless)
                    .controlSize(.small)
                    .clickable()
            }
        }
    }

    /// Composer create keeps the sheet open until the daemon answers.
    /// `keepStagedAttachments` used to run *before* `boardCreate`, so a
    /// refusal had already closed the form and skipped discard — copies
    /// sat unreferenced, and the sentence landed on `__new__` which no
    /// card draws. Join in-flight copies first so the list we send is
    /// the list on screen.
    /// `refine` rides the create as the envelope flag; the refusal, if any,
    /// arrives on the new card's `dispatch_error` in the next frame.
    private func saveAndRefine() {
        state.disarm()
        saveComposer(refine: true)
    }

    private func saveComposer(refine: Bool = false) {
        guard !state.composerSaving else { return }
        state.composerSaving = true
        state.refusals[BoardState.newCard] = ""
        let draft = state.draft
        let generation = state.prepareGeneration
        let id = state.stagingId
        Task { @MainActor in
            await state.waitForStaging()
            guard isComposer,
                  state.editing == BoardState.newCard,
                  state.prepareGeneration == generation,
                  state.stagingId == id else {
                state.composerSaving = false
                return
            }
            let attached = state.stagedAttachments.joined(separator: "\n")
            // Always Prep — a newly written card is an idea, not a plan.
            // In progress is a dispatch, and a dispatch is a gesture
            // somebody makes on the board, not a dropdown in a form —
            // see `BoardView.drop(_:into:)`.
            let result = await client.boardCreate(
                title: draft.title, summary: draft.summary,
                prompt: draft.prompt, project: draft.project,
                root: draft.root, tool: draft.tool,
                column: BoardColumn.prep.rawValue,
                workflow: draft.workflow,
                attachments: attached,
                model: draft.model,
                createToken: id,
                refine: refine,
                beneficiary: draft.outcome.beneficiary,
                intendedBenefit: draft.outcome.intendedBenefit,
                successCriterion: draft.outcome.successCriterion,
                priority: draft.priority,
                area: draft.area,
                kind: draft.kind,
                startWhenPlanned: draft.startWhenPlanned)
            let stillComposer = state.editing == BoardState.newCard
                && state.prepareGeneration == generation
            // Success keeps the folder even if the sheet moved: the
            // card owns it. Leaving composerSaving true, or discarding
            // after a keep-skip, is the worse pair of failures.
            state.finishComposerCreate(ok: result.ok, detail: result.detail,
                                       stillComposer: stillComposer)
            await client.refresh()
        }
    }
}

// MARK: - The fold row

/// One folded section's closed row: a chevron, the label and, where the card
/// already knows it, a count or a word (`CardSections.rowText`). The plan's
/// `TECHNICAL DETAIL` idiom, lifted to a type so every section draws it the
/// same. A header for VoiceOver, with the open state as its value.
private struct CardFoldRow: View {
    let label: String
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .semibold))
                    .rotationEffect(.degrees(open ? 0 : -90))
                    .foregroundStyle(Theme.faint)
                    .accessibilityHidden(true)
                Text(label)
                    .font(Theme.mono(11, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.phosphor)
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityLabel(label)
        .accessibilityValue(open ? "shown" : "hidden")
        .accessibilityAddTraits(.isHeader)
    }
}

// MARK: - Finding the documents a card points at

/// Pulling file paths out of prose, and reading them back safely.
///
/// Separate from the view because the interesting part is not the drawing: it is
/// **which strings count as a path and which files may be read**. A card's
/// instructions can have been written by an agent through the channel, so the
/// text here is untrusted, and a naive "open whatever looks like a path" would
/// be a window that reads any file on the machine on somebody else's say-so.
///
/// Two rules do the work. A candidate must contain a separator and end in a
/// short extension — that is what a document reference looks like and it turns
/// away ordinary prose. And the resolved file must lie **inside the card's own
/// project root**, standardized first, so neither an absolute path nor a walk of
/// `../` escapes it. A card with no root offers no documents at all, which is
/// the right answer: there is nothing to be inside of.
enum BoardDocuments {
    /// The cap exists because this is drawn in a fixed-height reader inside a
    /// sheet. A plan is a few dozen kilobytes; anything past this is not a
    /// document somebody meant to point at.
    static let maxBytes = 512 * 1024

    private static let pattern = try? NSRegularExpression(
        pattern: #"[A-Za-z0-9._~-]*(?:/[A-Za-z0-9._-]+)+\.[A-Za-z]{1,6}"#)

    /// Every distinct file the text names that really exists inside `root`, in
    /// the order they first appear — which is the order somebody wrote them, and
    /// therefore the order they meant them to be read.
    static func references(in text: String, root: String) -> [String] {
        guard !root.isEmpty, let pattern, !text.isEmpty else { return [] }
        let ns = text as NSString
        var out: [String] = []
        var seen = Set<String>()
        for match in pattern.matches(in: text, range: NSRange(location: 0,
                                                              length: ns.length)) {
            let raw = ns.substring(with: match.range)
            guard let resolved = resolve(raw, root: root), !seen.contains(resolved)
            else { continue }
            seen.insert(resolved)
            out.append(resolved)
            if out.count >= 8 { break }
        }
        return out
    }

    /// A candidate as an absolute path, or nil if it is not a readable file
    /// inside `root`. The containment test is on the standardized strings with a
    /// trailing separator, so `/tmp/bob-evil` is not inside `/tmp/bob`.
    static func resolve(_ candidate: String, root: String) -> String? {
        // Symlinks are resolved on **both** sides, and both halves matter.
        // `standardizedFileURL` is purely lexical — it removes `..` and `.` and
        // knows nothing about the file system — so a symlink written inside the
        // project root walks straight out of it while still passing a prefix
        // test. Resolving only the candidate would then break the honest case
        // instead: `/tmp` is itself a symlink to `/private/tmp` on macOS, so a
        // project under it would have every one of its own files judged
        // outside. Resolve both, compare the results.
        let base = URL(fileURLWithPath: root)
            .standardizedFileURL.resolvingSymlinksInPath()
        let raw = candidate.hasPrefix("/")
            ? URL(fileURLWithPath: candidate)
            : base.appendingPathComponent(candidate)
        let url = raw.standardizedFileURL.resolvingSymlinksInPath()
        let prefix = base.path.hasSuffix("/") ? base.path : base.path + "/"
        guard url.path.hasPrefix(prefix) else { return nil }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path,
                                             isDirectory: &isDirectory),
              !isDirectory.boolValue else { return nil }
        return url.path
    }

    /// Whether to draw this as a document or as its own bytes. Extension only,
    /// and only the two Markdown ones: source drawn as Markdown is mangled
    /// (`# comment` becomes a headline), so the test has to be one that never
    /// says yes by accident — sniffing the content for `#` would.
    static func isMarkdown(_ path: String) -> Bool {
        let ext = (path as NSString).pathExtension.lowercased()
        return ext == "md" || ext == "markdown"
    }

    /// The file's text, or nil. Never throws into a view: an unreadable file is
    /// a line saying so, not a blank pane.
    static func read(_ path: String) -> String? {
        guard let size = try? FileManager.default
            .attributesOfItem(atPath: path)[.size] as? Int else { return nil }
        // The cap is applied **before** the read, not after. This ran
        // `String(contentsOfFile:)` on the whole file first and trimmed the
        // result, so `maxBytes` bounded what was displayed and nothing at all
        // about what was loaded — and this is called straight from a button on
        // the main actor, so a card naming a large text file (a log, a long
        // plan) froze the window for as long as it took to read and decode it.
        // The size is already in hand a line above; using it costs nothing.
        if size > maxBytes {
            guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
            defer { try? handle.close() }
            guard let head = try? handle.read(upToCount: maxBytes),
                  // A prefix can cut a multi-byte character in half, which
                  // fails strict UTF-8 decoding — so drop trailing bytes until
                  // it decodes rather than reporting an unreadable file.
                  let text = Self.decode(head)
            else { return nil }
            return text + "\n\n… (truncated — open it in an editor for the rest)"
        }
        return try? String(contentsOfFile: path, encoding: .utf8)
    }

    /// UTF-8 from a byte range that may end mid-character. Backs off at most
    /// three bytes, which is the most a truncated scalar can leave dangling.
    private static func decode(_ data: Data) -> String? {
        for drop in 0...3 where data.count >= drop {
            if let text = String(data: data.dropLast(drop), encoding: .utf8) {
                return text
            }
        }
        return nil
    }

    /// What to call it on screen: relative to the project, since that is how the
    /// card wrote it and how anybody working in the repo says it.
    static func display(_ path: String, root: String) -> String {
        let prefix = root.hasSuffix("/") ? root : root + "/"
        return path.hasPrefix(prefix) ? String(path.dropFirst(prefix.count)) : path
    }
}

/// File-local copy of the grid `composerSpecialists` draws. Concurrent work
/// removed the shared type from `Specialists.swift`; this file still needs it.
private struct SpecialistGrid: View {
    let stages: [String]

    private var unique: [String] {
        var seen = Set<String>()
        return stages.filter { seen.insert($0.lowercased()).inserted }
    }

    var body: some View {
        if !unique.isEmpty {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 132), spacing: 8)],
                      alignment: .leading, spacing: 8) {
                ForEach(unique, id: \.self) { stage in
                    tile(stage)
                }
            }
        }
    }

    @ViewBuilder
    private func tile(_ stage: String) -> some View {
        let role = Specialists.role(for: stage)
        let face = Specialists.face(for: stage)
        HStack(alignment: .top, spacing: 6) {
            if face.isEmpty {
                StageMark(name: stage, active: false)
            } else {
                PixelMark(character: face, state: .sleep, size: 22)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(stage)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
                if let role {
                    Text(role)
                        .font(Theme.mono(9))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(stage + (role.map { ", \($0)" } ?? ""))
    }
}
