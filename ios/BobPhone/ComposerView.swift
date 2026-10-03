import PhotosUI
import SwiftUI

/// Small new-card form. Lands in Prep. Tool required. Project is a pick
/// from the Mac's list — no typed path. Closing discards.
struct ComposerView: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @ObservedObject var client: PhoneClient
    @ObservedObject var outbox: OutboxStore
    /// The tab hosting this composer, stamped onto every stash so a resumed
    /// draft reopens where it was last written.
    let tab: PhoneTab
    /// What the profile screen needs, handed through from the tab root so
    /// the "Add a key under Profile" link under PREPARE can push it.
    let pairing: PairingStore?
    let onForget: (() -> Void)?
    @Environment(\.dismiss) private var dismiss

    @State private var title = ""
    @State private var summary = ""
    @State private var prompt = ""
    @State private var tool = ""
    /// `""` means Default — let the assistant decide. Unsent draft state, so
    /// the `.onChange` reset below is the sheet's own precedent rather than a
    /// second copy of the store's clear-on-retool rule.
    @State private var model = ""
    @State private var projectRoot = ""
    /// The card's expected stages. Written by Prepare, drawn as faces under
    /// a caption rather than as a typed box, and restored from the draft.
    /// Sent only when non-empty, so a card saved without it carries no
    /// `workflow` field at all.
    @State private var workflow = ""
    /// "Start the work by itself the moment the plan lands", ticked before
    /// the card exists. Rides `board_create` on the live path only —
    /// `refine`'s stated rule, for the same reason: a banked draft carries no
    /// intent about starting anything. View state: on no card, no store
    /// column, no snapshot and in no banked draft.
    @State private var startWhenPlanned = false
    @State private var area = ""
    /// The cards this draft waits on, newline-joined ids — the store's own
    /// shape. Drawn and sent only against a Mac that said it takes the link
    /// (`dependencies_supported`); Prepare fills it only while empty.
    @State private var blockedBy = ""
    /// Build (`""`) or Scout (`"scout"`) — the Mac composer's Kind chips.
    /// A scout investigates and writes a report instead of code; it has no
    /// plan to refine, so Save & Refine and start-when-planned are absent
    /// for it. Sent on create only when scout, so an older Mac's door
    /// never meets the key; drawn only against a Mac that said it takes it.
    @State private var kind = ""
    /// The whole thought in one box, which Prepare writes the other four
    /// fields from. Composer scratch: a card has no idea field, so this is
    /// never sent with `save()` and lives only in the draft slot.
    @State private var idea = ""
    /// The objective, typed before the card exists: who benefits, the
    /// intended benefit, the success criterion. Drawn only against a Mac
    /// that takes them on create (`objectiveOnCreateSupported`), banked
    /// with the draft, and filled by Prepare only where a box is empty.
    @State private var beneficiary = ""
    @State private var intendedBenefit = ""
    @State private var successCriterion = ""
    /// The importance number typed before the card exists, `""` for no
    /// opinion. A string all the way to the wire (the Mac's `_board_fields`
    /// turns a JSON `0` into `""`); drawn only against a Mac that knows the
    /// column (`prioritySupported`), banked with the draft.
    @State private var draftPriority = ""
    @State private var expanded = false
    @State private var revealReason: ComposerPhase.Reveal = .resumed
    @AppStorage("composer.lastRoot") private var lastRoot = ""
    /// The root Prepare last suggested, or `""` for "no question on screen".
    /// `@State`, deliberately not a draft field: it is on no card, no store
    /// column, no snapshot and not in the banked draft, so a resumed draft
    /// never carries a stale opinion.
    @State private var suggestedRoot = ""
    /// The root the picker held before the Mac's pick was applied, so the
    /// line under it can offer one way back. View state only, like
    /// `suggestedRoot`: on no card and in no banked draft.
    @State private var revertRoot = ""
    @State private var note = ""
    @State private var saving = false
    /// Which of the two save buttons reads "SAVING…" — `CardDetailView`'s
    /// `Pressed` marker, reduced to the one pair this form has.
    @State private var refinePressed = false
    /// SAVE & REFINE arms then confirms, the phone's own Refine restated;
    /// the id is `stagingId` so a confirm cannot match a slot armed for
    /// nothing in particular.
    @StateObject private var arm = Arm()
    @State private var preparing = false
    /// Which side wrote the fields last, drawn under PREPARE. Cleared on
    /// every press and when the composer is cleared.
    @State private var preparedVia: PrepareRoute?
    /// The staging folder this composer owns, minted once and lowercased —
    /// the desktop's contract, and inside `[a-z0-9-]{8,40}`. Nothing removes
    /// it: a composer abandoned mid-compose ages out of the Mac's 24 h
    /// orphan sweep, which is exactly what the desktop relies on too.
    @State private var stagingId = UUID().uuidString.lowercased()
    /// Stored relative paths the Mac wrote back, `<folder>/<name>`. The
    /// *returned* path, never the name we asked for — the Mac dedupes.
    @State private var staged: [String] = []
    @State private var picked: [PhotosPickerItem] = []
    @State private var showingPhotos = false
    @State private var uploading = false
    /// Photo names copied into this card's folder on the phone, still to be
    /// uploaded. A different wait from `uploading`, and it says so.
    @State private var localPhotos: [String] = []
    @State private var staging = false
    /// The three-answer question, up only when leaving would lose words.
    @State private var leaving = false
    @FocusState private var focusedField: String?
    @ObservedObject private var dictation = DictationEngine.shared

    /// Seeded in `init`, never `.onAppear`: `State(initialValue:)` is applied
    /// once per view identity, while an `.onAppear` assignment refires on
    /// every re-appear and would clobber what has been typed since.
    ///
    /// One slot for the whole phone, so the + button on **any** tab resumes
    /// the banked draft rather than opening a blank form; the draft's own
    /// `tab` is simply rewritten by the next stash.
    init(client: PhoneClient, outbox: OutboxStore, tab: PhoneTab,
         pairing: PairingStore? = nil, onForget: (() -> Void)? = nil) {
        self.client = client
        self.outbox = outbox
        self.tab = tab
        self.pairing = pairing
        self.onForget = onForget
        let d = outbox.draft
        _title = State(initialValue: d?.title ?? "")
        _summary = State(initialValue: d?.summary ?? "")
        _prompt = State(initialValue: d?.prompt ?? "")
        _tool = State(initialValue: d?.tool ?? "")
        _model = State(initialValue: d?.model ?? "")
        _projectRoot = State(initialValue: d?.projectRoot ?? "")
        _workflow = State(initialValue: d?.workflow ?? "")
        _idea = State(initialValue: d?.idea ?? "")
        _beneficiary = State(initialValue: d?.beneficiary ?? "")
        _intendedBenefit = State(initialValue: d?.intendedBenefit ?? "")
        _successCriterion = State(initialValue: d?.successCriterion ?? "")
        _draftPriority = State(initialValue: d?.priority ?? "")
        _area = State(initialValue: d?.area ?? "")
        _blockedBy = State(initialValue: d?.blockedBy ?? "")
        _kind = State(initialValue: d?.kind ?? "")
        _expanded = State(initialValue: d?.expanded ?? false)
        // The staging id travels with the draft: a restored composer must
        // keep uploading into the folder the first attempt opened, and it
        // still owns the phone-side folder its offline photos went to.
        _stagingId = State(initialValue: d?.id ?? UUID().uuidString.lowercased())
        _staged = State(initialValue: d?.staged ?? [])
        _localPhotos = State(initialValue: d?.localPhotos ?? [])
    }

    /// The Mac's board when it is answering, the remembered lists when it is
    /// not — one property, so every picker below is offline-capable without
    /// knowing it. A phone that has never seen a board has no lists to
    /// remember and the pickers stay empty.
    private var board: Board {
        let live = client.snapshot.board
        return live.available ? live : outbox.catalogueBoard()
    }

    /// The Mac is not in reach right now. Everything the Mac itself has to do
    /// — Prepare, and the upload leg of a photo — is held; everything the
    /// phone can do alone still works.
    private var offline: Bool { client.status != .live }

    /// Prepare can run on this phone: the Mac is out of reach and a key is
    /// saved. Decided per press from the poll loop's own verdict; nothing
    /// new reads `via`.
    private var phoneRoute: Bool { offline && AnthropicKeyStore.hasKey }

    /// Somebody can write the card's fields — the Mac, or this phone.
    private var prepareRouteAvailable: Bool { !offline || phoneRoute }

    private var selectedProject: BoardProject? {
        board.projects.first { $0.root == projectRoot }
    }

    private var phaseTwoVisible: Bool {
        ComposerPhase.expanded(flag: expanded, fields: [
            title, summary, prompt, workflow, beneficiary,
            intendedBenefit, successCriterion, draftPriority, model, area,
            blockedBy])
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                field("your idea", text: $idea, axis: .vertical,
                      hint: "the whole thought — PREPARE writes the rest",
                      multiline: true, dictation: "composer.idea")
                prepareBlock
                if !phaseTwoVisible {
                    DecryptButton(ComposerPhase.fillMyselfLabel) {
                        revealReason = .manual
                        expanded = true
                    }
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.faint)
                    .accessibilityLabel("Fill the card in yourself")
                }
                photosRow
                VStack(alignment: .leading, spacing: 4) {
                    Text("assistant")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        // The group below is named Assistant; a caption that
                        // is its own element makes VoiceOver say it twice.
                        .accessibilityHidden(true)
                    // `fills`: the tiles share the width equally, so the
                    // usage columns beneath sit under their own tile.
                    PhoneProviderSwitch(tools: board.tools, installed: board.installed,
                                        selected: tool,
                                        fills: true,
                                        pick: { tool = $0 })
                    if !board.tools.isEmpty {
                        ComposerUsageRow(tools: board.tools, selected: tool, bars: client.usage)
                    }
                }
                if phaseTwoVisible {
                if let words = ComposerPhase.caption(for: revealReason) {
                    Text(words)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                projectPicker
                if board.scoutSupported {
                    kindControl
                }
                field("title", text: $title, hint: "one line naming the card",
                      dictation: "composer.title")
                field("summary", text: $summary, axis: .vertical,
                      hint: "a sentence or two on what this is",
                      multiline: true, dictation: "composer.summary")
                if !board.modelOptions(for: tool).isEmpty {
                    modelPicker
                }
                field("notes", text: $prompt, axis: .vertical,
                      hint: "what the assistant should do",
                      multiline: true, dictation: "composer.notes")
                if !Specialists.parse(workflow).isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("expected specialists")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 132),
                                                     spacing: 8)],
                                  alignment: .leading, spacing: 8) {
                            ForEach(Specialists.parse(workflow), id: \.self) { stage in
                                HStack(alignment: .top, spacing: 6) {
                                    if Specialists.face(for: stage).isEmpty {
                                        StageMark(name: stage, active: false)
                                    } else {
                                        PixelMark(character: Specialists.face(for: stage),
                                                  state: .sleep, size: 22)
                                    }
                                    Text(stage)
                                        .font(Theme.mono(11))
                                        .foregroundStyle(Theme.phosphor)
                                        .fixedSize(horizontal: false, vertical: true)
                                    Spacer(minLength: 0)
                                }
                            }
                        }
                    }
                }
                // The objective stays independent of the chosen delivery area.
                if board.objectiveOnCreateSupported {
                    field("who benefits", text: $beneficiary,
                          hint: "who this work is for",
                          dictation: "composer.beneficiary")
                    field("intended benefit", text: $intendedBenefit,
                          axis: .vertical,
                          hint: "what good it should do for them",
                          multiline: true, dictation: "composer.benefit")
                    field("success criterion", text: $successCriterion,
                          axis: .vertical,
                          hint: "one sentence you could check",
                          multiline: true, dictation: "composer.criterion")
                }
                // The importance number. **Absent, never inert** against an
                // older Mac (the editor's rule): a number typed here is
                // stored from the start and the Mac's scorer leaves the
                // card alone. Not `field(...)`: that helper capitalises
                // sentences and this is a number.
                if board.areasSupported {
                    Text("Area").font(Theme.mono(11)).foregroundStyle(Theme.faint)
                    AreaGrid(selected: $area)
                }
                // The waits-on box, only against a Mac that takes the link
                // and only once a project is chosen: the choices are that
                // project's unfinished cards.
                if board.dependenciesSupported, !projectRoot.isEmpty {
                    Text("waits on").font(Theme.mono(11)).foregroundStyle(Theme.faint)
                    ComposerDependencyPicker(selected: $blockedBy,
                                             root: projectRoot, cards: board.cards)
                }
                if board.prioritySupported {
                    Text("priority (0–100, empty for no opinion)")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                    TextField("", text: $draftPriority)
                        .font(Theme.mono(13))
                        .foregroundStyle(Theme.phosphorBright)
                        .textFieldStyle(.plain)
                        .keyboardType(.numberPad)
                        // The number pad has no return key, so without a focus
                        // binding the background tap has nothing to clear and
                        // this is the one keyboard with no way down.
                        .focused($focusedField, equals: "composer.priority")
                        .hidesKeyboard(when: focusedField == "composer.priority")
                        .padding(6)
                        .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                        .accessibilityLabel("Priority, 0 to 100, empty for no opinion")
                }
                if !note.isEmpty {
                    Text(note)
                        .font(Theme.mono(12))
                        .foregroundStyle(.orange)
                }
                // A photo still on the wire is not yet a stored path, so
                // both verbs are held until the batch lands — and the hold
                // says so, because a button that is merely dim reads as a
                // missing field rather than as a wait.
                if uploading {
                    Text("photos are still sending…")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                } else if staging {
                    Text("photos are being saved on this phone…")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                }
                DecryptButton(saving && !refinePressed ? "SAVING…" : "SAVE") {
                    // An armed refine must not outlive a plain save.
                    arm.disarm()
                    Task { await save() }
                }
                .disabled(tool.isEmpty || saving || preparing || uploading || staging)
                .buttonStyle(AlarmOutline())
                // The same create with the Mac's Refine on the end of it.
                // Absent while the Mac is out of reach: SAVE keeps banking
                // the card, and the outbox carries no refine intent. A Mac
                // that vanishes between this draw and the tap lands in
                // `save`'s "Could not reach the Mac." branch, which banks
                // the card without the flag — the person gets the card and
                // not the refinement, and nothing is queued to refine later
                // without a press. Absent too when Dark Army may not start
                // sessions, like the board's own Refine.
                // A scout has no plan to refine, so neither Save & Refine
                // nor the start-when-planned tick — Start is its Prep verb.
                if board.dispatchEnabled && !offline && kind != "scout" {
                    DecryptButton(saveRefineLabel) {
                        if arm.confirm(.refine, id: stagingId) {
                            Task { await save(refine: true) }
                        } else {
                            arm.arm(.refine, id: stagingId)
                        }
                    }
                    .disabled(tool.isEmpty || saving || preparing || uploading || staging)
                    .buttonStyle(AlarmOutline())
                    // Absent, never present and refused, against a Mac that
                    // predates the column — the marker's own argument.
                    if board.startWhenPlannedSupported {
                        DecryptButton(startWhenPlanned
                               ? "[x] START WHEN PLANNED"
                               : "[ ] START WHEN PLANNED") {
                            startWhenPlanned.toggle()
                        }
                        .buttonStyle(AlarmOutline())
                        .accessibilityLabel(startWhenPlanned
                            ? "Start the work when the plan lands, on. Press to turn off."
                            : "Start the work when the plan lands, off. Press to turn on.")
                    }
                }
                // Greyed is not an explanation: the card itself is not lost,
                // which is the half a dim button never says. Outside the
                // helper's switch, because Save is there either way.
                if offline {
                    Text("This card will be saved on your phone and sent to the board when the Mac is back in reach.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            // Behind the form, never over it: `.background` hit-tests only
            // where no foreground view covers the point, so the wells' own
            // taps, the mic, the pickers' menus, PREPARE, the photo controls
            // and the save buttons all keep their first press. The gaps, the
            // margins and the space beside a short label are what is left,
            // and that is exactly "outside any input field".
            //
            // Not the two obvious alternatives:
            // - A plain tap gesture on the `ScrollView` puts a tap region
            //   *over* every control in the form: the swallowed-first-tap bug.
            // - A simultaneous tap gesture on the `ScrollView` is non-blocking
            //   but still fires on taps that landed on a control, racing
            //   `FieldWell`'s own `onTap` (which sets the focus to the well's
            //   key while this clears it, with no defined ordering), so
            //   tapping a well's padding ring would intermittently fail to
            //   focus it.
            // The background layer arbitrates with nothing at all, and the
            // greps in `test_phone_composer_attach_prepare.py` keep it here.
            //
            // One accepted limit: `.background` is exactly as tall as the
            // `VStack`, so a form shorter than the screen has no target in the
            // empty space below the last control. Not worth a
            // `GeometryReader`-driven `minHeight` — with the keyboard up the
            // form always overflows, which is the only case this is about.
            .background(
                Color.clear
                    .contentShape(Rectangle())
                    .onTapGesture { focusedField = nil }
                    .accessibilityHidden(true)
            )
        }
        // A drag lowers it straight away; `.interactively` would tie the
        // keyboard to the finger and read as sluggish on a flick.
        .scrollDismissesKeyboard(.immediately)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        .navigationTitle("new card")
        .decryptSurface("ComposerView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .onChange(of: tool) { model = "" }
        // The faces Prepare drew were computed against the project the picker
        // held when it ran — including the moment Prepare's own folder
        // suggestion moves it. A move makes them another project's staff, and
        // the Mac drops them at the write, so they must not sit here looking
        // like a promise. Only a *change away from a chosen* project clears:
        // `onAppear`'s first fill of an empty picker keeps a resumed draft's
        // faces.
        .onChange(of: projectRoot) { previous, current in
            if !previous.isEmpty && previous != current {
                workflow = ""
                // The waits-on choices were the old project's cards.
                blockedBy = ""
            }
            if !current.isEmpty { lastRoot = current }
        }
        // Every persisted field, quietly banked as it is typed.
        .onChange(of: draftKey) { stash() }
        // Deliberately **not** `.onDisappear`: the lock's teardown fires it
        // after the flush, so lowering `open` there would overwrite the
        // saved `open: true` and kill the auto-reopen.
        //
        // The hold is a different matter and does belong on the pair: it says
        // "a form is on screen filling this staging folder", so Discard in a
        // second tab's composer cannot delete the photos out from under this
        // one. `open` is about the draft; this is about the folder.
        .onAppear { outbox.holdComposer(stagingId) }
        .onDisappear {
            dictation.stop()
            arm.disarm()
            outbox.releaseComposer(stagingId)
        }
        .navigationBarBackButtonHidden(true)
        .toolbar { backItem }
        .confirmationDialog("This card has not been saved.",
                            isPresented: $leaving, titleVisibility: .visible) {
            leaveAnswers
        }
        .onChange(of: focusedField) { _, field in
            // Thumbs and voice never share a sentence: taking the keyboard
            // commits what has been said so far and stops listening.
            if field != nil { dictation.stop() }
        }
        .onAppear { adoptDefaults() }
        // The lists arrive whenever the Mac does — the first poll of a fresh
        // install, or a relay round trip seconds after the form opened — so
        // the defaults are re-applied when they land, not only on appear.
        // Before this, a composer opened ahead of the first snapshot kept an
        // empty project for good: the picker is hidden until Prepare has
        // run, and PREPARE is held while no project is chosen, so the form
        // was stuck with nothing on screen saying why.
        .onChange(of: board.projects.map(\.root)) { _, _ in adoptDefaults() }
        .onChange(of: board.tools) { _, _ in adoptDefaults() }
    }

    /// Fill the assistant and the project where nothing has been chosen
    /// yet. Never overwrites a choice: `ComposerDefaults` answers `nil`
    /// for a field that already holds one.
    private func adoptDefaults() {
        if let pick = ComposerDefaults.tool(current: tool, tools: board.tools) {
            tool = pick
        }
        if let pick = ComposerDefaults.projectRoot(
            current: projectRoot, roots: board.projects.map(\.root),
            lastRoot: lastRoot) {
            projectRoot = pick
        }
    }

    /// Hiding the system back item also disarms the interactive edge-swipe
    /// pop — the gesture belongs to that button — so there is no way out of
    /// the form that dodges the question.
    @ToolbarContentBuilder
    private var backItem: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            DecryptButton {
                attemptLeave()
            } label: {
                HStack(spacing: 3) {
                    Image(systemName: "chevron.left")
                    Text("back").font(Theme.mono(13))
                }
                .foregroundStyle(Theme.phosphor)
            }
            .accessibilityLabel("Back")
        }
    }

    @ViewBuilder
    private var leaveAnswers: some View {
        DecryptButton("Keep draft") {
            outbox.stashDraft(currentDraft(open: false))
            outbox.flushDraftNow()
            leave()
        }
        DecryptButton("Discard", role: .destructive) {
            outbox.clearDraft(deletingPhotos: true)
            leave()
        }
        DecryptButton("Cancel", role: .cancel) {}
    }

    // --- the draft slot -------------------------------------------------

    /// This composer's state as a draft. `open` says the form was on screen,
    /// and is lowered only by the four deliberate exits — Keep draft,
    /// Discard, a successful `save()`, and `bank()`.
    private func currentDraft(open: Bool) -> ComposerDraft {
        ComposerDraft(id: stagingId, tab: tab.rawValue, title: title,
                      summary: summary, prompt: prompt, tool: tool,
                      model: model, projectRoot: projectRoot,
                      workflow: workflow, idea: idea, staged: staged,
                      localPhotos: localPhotos, open: open,
                      beneficiary: beneficiary,
                      intendedBenefit: intendedBenefit,
                      successCriterion: successCriterion,
                      priority: draftPriority, area: area, kind: kind,
                      expanded: expanded, blockedBy: blockedBy)
    }

    /// Everything persisted, as one comparable value. `updatedAt` moves on
    /// every stash, so `ComposerDraft` itself cannot be the change key.
    /// `stagingId` is absent: it is minted once and never changes.
    private var draftKey: [String] {
        [title, summary, prompt, tool, model, projectRoot, workflow, idea,
         draftPriority, area, kind, blockedBy,
         beneficiary, intendedBenefit, successCriterion, expanded ? "1" : "", "\u{0}"]
            + staged + ["\u{0}"] + localPhotos
    }

    private func stash() {
        outbox.stashDraft(currentDraft(open: true))
    }

    /// Leaving with nothing worth keeping goes silently; leaving with words
    /// in the form asks the one three-answer question.
    private func attemptLeave() {
        if currentDraft(open: false).worthKeeping {
            leaving = true
            return
        }
        outbox.clearDraft(deletingPhotos: false)
        leave()
    }

    private func leave() {
        dictation.stop()
        dismiss()
    }

    /// One labelled input. `multiline` is the only difference between a title
    /// and a notes box: it opens the well with five lines of room and lets it
    /// grow. The `MicButton` stays in the *label* row, so a growing well never
    /// pushes it down the screen.
    private func field(_ label: String, text: Binding<String>,
                       axis: Axis = .horizontal,
                       hint: String = "",
                       multiline: Bool = false,
                       dictation id: String? = nil) -> some View {
        let key = id ?? label
        let input = TextField("", text: text,
                              prompt: Text(hint).foregroundStyle(Theme.faint),
                              axis: axis)
            .font(Theme.prose(17))
            .foregroundStyle(Theme.text)
            // Prose, so it types like prose: the keyboard fixes typos and
            // starts a sentence with a capital. The pairing screen's host,
            // port and code are identifiers and deliberately keep neither.
            .textInputAutocapitalization(.sentences)
            .focused($focusedField, equals: key)

        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(label)
                    .font(Theme.prose(14, weight: .medium))
                    .foregroundStyle(Theme.muted)
                Spacer(minLength: 0)
                if let id {
                    MicButton(id: id, text: text)
                }
            }
            Group {
                if multiline {
                    input.lineLimit(5...)
                } else {
                    input.lineLimit(1)
                }
            }
            .fieldWell(focused: focusedField == key, multiline: multiline,
                       onTap: { focusedField = key })
            if let id, dictation.noteField == id, !dictation.note.isEmpty {
                Text(dictation.note)
                    .font(Theme.mono(12))
                    .foregroundStyle(.orange)
            }
        }
    }

    /// Default first, then whatever the chosen assistant offers. Absent
    /// entirely when it offers none — a chooser whose only row is Default is
    /// a promise with nothing behind it.
    /// Two-state text control, the Mac composer's `kindControl`: Build
    /// (`""`) / Scout (`"scout"`). Selection is never colour-alone — the
    /// chosen chip carries the selected trait and a brighter border.
    private var kindControl: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("kind")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .accessibilityHidden(true)
            HStack(spacing: 0) {
                kindChip("Build", selected: kind != "scout") { kind = "" }
                kindChip("Scout", selected: kind == "scout") { kind = "scout" }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Kind")
            .accessibilityValue(kind == "scout" ? "Scout" : "Build")
            Text(kind == "scout"
                 ? "Scout investigates and writes a report; nothing is built."
                 : "Build writes code; Scout investigates and writes a report.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func kindChip(_ title: String, selected: Bool,
                          _ action: @escaping () -> Void) -> some View {
        DecryptButton(action: action) {
            Text(title)
                .font(Theme.mono(13))
                .foregroundStyle(selected ? Theme.phosphorBright : Theme.faint)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .overlay(Rectangle().stroke(selected ? Theme.phosphor : Theme.hair,
                                            lineWidth: 1))
                .contentShape(Rectangle())
        }
        .accessibilityLabel(title)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var modelPicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("model")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            Menu {
                DecryptButton("Default") { model = "" }
                ForEach(board.modelOptions(for: tool), id: \.self) { name in
                    DecryptButton(Board.modelLabel(name)) { model = name }
                }
            } label: {
                Text(model.isEmpty ? "Default" : Board.modelLabel(model))
                    .font(Theme.mono(13))
                    .foregroundStyle(Theme.phosphor)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 6)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
            }
        }
    }

    private var projectPicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("project")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            Menu {
                ForEach(board.projects) { project in
                    DecryptButton(project.name.isEmpty ? project.root : project.name) {
                        projectRoot = project.root
                        // Choosing for yourself answers the question too.
                        suggestedRoot = ""
                        revertRoot = ""
                    }
                }
            } label: {
                let name = selectedProject?.name ?? ""
                Text(name.isEmpty ? (projectRoot.isEmpty ? "choose…" : projectRoot) : name)
                    .font(Theme.mono(13))
                    .foregroundStyle(Theme.phosphor)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 6)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
            }
            if let previous = revertProject { suggestionRow(previous) }
        }
    }

    /// The project to offer going back to, once the Mac's pick has been
    /// applied: only while both the pick and the previous root still name a
    /// project this phone can select, re-resolved at draw time so a window
    /// closed on the Mac since the press draws no button rather than a bad one.
    private var revertProject: BoardProject? {
        guard !revertRoot.isEmpty, !suggestedRoot.isEmpty,
              board.projects.contains(where: { $0.root == suggestedRoot })
        else { return nil }
        return board.projects.first { $0.root == revertRoot }
    }

    /// The Mac's pick, already applied, with one way back: plain faint text
    /// and a single button naming the project the picker held. Pressing it
    /// puts that project back and the line goes away.
    private func suggestionRow(_ previous: BoardProject) -> some View {
        let picked = board.projects.first { $0.root == suggestedRoot }
        let pickedName = picked.map { $0.name.isEmpty ? $0.root : $0.name }
            ?? suggestedRoot
        return HStack(spacing: 10) {
            Text("Dark Army picked \(pickedName)")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            DecryptButton("Use \(previous.name.isEmpty ? previous.root : previous.name) instead") {
                projectRoot = previous.root
                suggestedRoot = ""
                revertRoot = ""
            }
            .font(Theme.mono(11))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// How many more photos this card may take. Eight is the Mac's own
    /// ceiling; the picker is capped at what is left rather than refusing
    /// after the fact.
    private var remainingSlots: Int {
        max(0, 8 - staged.count - localPhotos.count)
    }

    /// The desktop composer's `canPrepare`: there has to be something to
    /// prepare *from*, and somewhere to prepare it for.
    private var canPrepare: Bool {
        !prepareInputEmpty && !tool.isEmpty && !projectRoot.isEmpty
    }

    /// Either box will do — the idea, or the description typed the old way.
    private var prepareInputEmpty: Bool {
        idea.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// PREPARE and everything that is about PREPARE, drawn directly under
    /// the box it reads. Absent, never inert: with the Mac's card-writing
    /// helper switched off there is nothing behind this button, so nothing
    /// here is drawn at all.
    @ViewBuilder
    private var prepareBlock: some View {
        if board.prepareEnabled {
            VStack(alignment: .leading, spacing: 6) {
                DecryptButton(preparing ? "WRITING…" : "PREPARE") {
                    Task { await prepare() }
                }
                .disabled(!canPrepare || preparing || saving || uploading || staging || !prepareRouteAvailable)
                .buttonStyle(AlarmOutline())
                // WRITING… from away is a two-minute wait by design, not a
                // hang: the relay's own pickup rides on top of the helper's
                // patience. Gated on `client.via`, never on `offline` —
                // `offline` is false from away whenever the relay is up.
                // A held PREPARE says why. The project picker only appears
                // once the form is open, so a missing project was the one
                // hold with no words on screen.
                if let reason = ComposerDefaults.holdReason(
                    projectRoot: projectRoot, tool: tool) {
                    Text(reason)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if preparing && !offline && client.via == .relay {
                    Text("Writing from away — this can take up to two minutes.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // Out of reach: the phone's own key steps in, or the line
                // says why nothing is behind the button and where the key
                // goes.
                if phoneRoute {
                    Text("The Mac is out of reach — Prepare will run on this phone.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                } else if offline {
                    Text("Prepare needs the Mac, which is out of reach right now.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                    if let pairing, let onForget {
                        NavigationLink {
                            ProfileView(client: client, pairing: pairing,
                                        receipts: client.receipts, onForget: onForget)
                        } label: {
                            Text("Add a key under Profile")
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.phosphor)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .accessibilityLabel("Add an Anthropic key under Profile")
                    }
                }
                if let preparedVia, !preparing {
                    Text(preparedVia.sentence)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    @ViewBuilder
    private var photosRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("photos")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            if remainingSlots > 0 {
                DecryptButton { showingPhotos = true } label: {
                    Text(uploading ? "SENDING…" : (staging ? "SAVING…" : "ADD PHOTOS"))
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 6)
                        .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                }
                .disabled(uploading || staging)
                // Mid-press the words on it are "SENDING…" alone.
                .accessibilityLabel(uploading ? "Sending"
                                    : (staging ? "Saving" : "Add photos"))
            }
            ForEach(localPhotos, id: \.self) { name in
                HStack(spacing: 8) {
                    Text("\(name) · on this phone, sent from home Wi-Fi")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    DecryptButton("REMOVE") {
                        localPhotos.removeAll { $0 == name }
                        outbox.dropPhoto(stagingId: stagingId, name: name)
                    }
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    // Names its file: a list of attachments is a list of
                    // identical REMOVE buttons otherwise.
                    .accessibilityLabel("Remove \(name)")
                    // A 44pt target under 11pt ink: this row's only control
                    // is a discard, and it sat at its own glyph height.
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                }
            }
            ForEach(staged, id: \.self) { path in
                HStack(spacing: 8) {
                    Text(path.split(separator: "/").last.map(String.init) ?? path)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    DecryptButton("REMOVE") {
                        // List-local. The Mac's copy is referenced by no card
                        // and ages out of the orphan sweep; there is
                        // deliberately no LAN verb for deleting a staged file.
                        staged.removeAll { $0 == path }
                    }
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .accessibilityLabel(
                        "Remove \(path.split(separator: "/").last.map(String.init) ?? path)")
                    // A 44pt target under 11pt ink: this row's only control
                    // is a discard, and it sat at its own glyph height.
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                }
            }
        }
        .photosPicker(isPresented: $showingPhotos, selection: $picked,
                      maxSelectionCount: remainingSlots, matching: .images)
        .onChange(of: showingPhotos) { _, presented in
            if !presented { decryptFeedback?.returnedFromPresentation() }
        }
        .onChange(of: picked) { _, items in
            guard !items.isEmpty else { return }
            Task { await sendPicked(items) }
        }
    }

    /// Each photo travels the moment it is picked, into the same staging
    /// folder the Mac's own composer would use. A failure names itself and
    /// the rest of the batch still goes.
    private func sendPicked(_ items: [PhotosPickerItem]) async {
        guard !uploading, !staging else { return }
        // Out of reach the photo is copied onto the phone instead, under the
        // same two bounds — the card must arrive whole or not at all, and
        // that is decided here rather than at sync time.
        // Away is the same: the upload leg is home-only
        // (`docs/transport-contract.md`), so a photo picked through the
        // relay is kept on the phone and the card is banked with it —
        // sent when the phone is next home — rather than refused.
        let local = offline || client.knowsItIsAway
        if local { staging = true } else { uploading = true }
        defer {
            uploading = false
            staging = false
            picked = []
        }
        // Cleared once, for the whole batch: a refusal on the first photo
        // must survive a success on the second, or the card saves with one
        // file and nothing on screen ever said so.
        note = ""
        var index = staged.count + localPhotos.count
        for item in items {
            if staged.count + localPhotos.count >= 8 {
                note = "a card can have at most 8 attachments"
                break
            }
            guard let data = try? await item.loadTransferable(type: Data.self),
                  !data.isEmpty else {
                note = "that photo could not be read"
                continue
            }
            if data.count > 20 * 1024 * 1024 {
                note = "that file is larger than 20 MB"
                continue
            }
            index += 1
            let ext = item.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
            let name = "photo-\(index).\(ext)"
            if local {
                if await outbox.stagePhoto(stagingId: stagingId, name: name,
                                           data: data) {
                    localPhotos.append(name)
                } else {
                    note = "that photo could not be saved on this phone"
                }
                continue
            }
            let result = await client.upload(stagingId: stagingId,
                                             name: name,
                                             data: data)
            if result.ok, !result.detail.isEmpty {
                staged.append(result.detail)
            } else if !result.detail.isEmpty {
                note = result.detail
            } else {
                // A refusal with no sentence — a dropped connection, a
                // cancelled task — still has to say something: the list
                // below is the only other evidence, and an absent row is
                // exactly what the person is not looking at.
                note = "that photo did not reach the Mac"
            }
        }
    }

    /// Ask the Mac to write the instructions. Single-flight — a second press
    /// while one is in the air is refused by the daemon in its own words, and
    /// the button is disabled for the same reason.
    private func prepare() async {
        dictation.stop()
        guard canPrepare, !preparing, !saving, !uploading, !staging,
              prepareRouteAvailable else { return }
        preparing = true
        defer { preparing = false }
        preparedVia = nil
        suggestedRoot = ""
        revertRoot = ""
        let thought = idea.trimmingCharacters(in: .whitespacesAndNewlines)
        let typedSummary = summary.trimmingCharacters(
            in: .whitespacesAndNewlines)
        let result: PhonePrepareResult
        if phoneRoute {
            // The Mac's own prompt and readers, run from here against
            // Anthropic with the person's own key. The roster is the helpers
            // Dark Army ships; the FOLDER menu is the last-known project list.
            result = await PhonePreparer.prepare(
                idea: thought.isEmpty ? typedSummary : thought,
                tool: tool,
                projectName: selectedProject?.name ?? "",
                roots: board.projects.map(\.root),
                roster: PhonePreparer.roster(from: Array(Specialists.table.keys)),
                areas: Areas.all.map { (slug: $0.slug, name: $0.name, concept: $0.concept) },
                candidates: board.dependenciesSupported
                    ? CardPrepareRules.candidates(from: board.cards.map {
                        (id: $0.id, title: $0.title, root: $0.root, column: $0.column)
                    }, root: projectRoot)
                    : [],
                hasPhotos: !staged.isEmpty || !localPhotos.isEmpty)
        } else {
            // The older-Mac degrade: a Mac that does not know `idea` reads
            // the description, so the idea rides under that name too when
            // nothing was typed there. A newer Mac prefers `idea` and
            // ignores the copy.
            result = await client.prepareCard(
                title: title,
                summary: typedSummary.isEmpty ? thought : summary,
                tool: tool,
                project: selectedProject?.name ?? "",
                root: projectRoot,
                attachments: staged.joined(separator: "\n"),
                idea: thought)
        }
        revealReason = result.ok ? .prepared : .manual
        expanded = true
        if result.ok {
            preparedVia = result.preparedVia
            prompt = result.prompt
            workflow = result.workflow
            // Non-empty only: an older Mac answers without these two, and
            // blanking a typed title on a key that was never sent is the one
            // way this could lose somebody's words.
            if !result.title.isEmpty { title = result.title }
            if !result.summary.isEmpty { summary = result.summary }
            // The objective is a suggestion, never a rewrite: a line lands
            // only in an empty box, because these are the person's own
            // words whenever they were typed; a box holding only whitespace
            // counts as empty.
            if !result.beneficiary.isEmpty && beneficiary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                beneficiary = result.beneficiary
            }
            if !result.intendedBenefit.isEmpty && intendedBenefit.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                intendedBenefit = result.intendedBenefit
            }
            if !result.successCriterion.isEmpty && successCriterion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                successCriterion = result.successCriterion
            }
            // The Mac's pick wins unless the person says otherwise: a listed
            // project other than the one chosen moves the picker, and the
            // project it held is kept only where the picker can still name
            // it. Same project, unlisted or no opinion: nothing is written.
            if board.areasSupported, let offered = AreaSuggestion.decide(offer: result.suggestedArea, current: area) { area = offered }
            // The waits-on box, the area's rule: only an empty box, only ids
            // this project still lists, only against a Mac that takes the
            // link. Before the project move below, which clears the box.
            let boxBeforePrepare = blockedBy
            if board.dependenciesSupported,
               let offered = DependencySuggestion.decide(
                offer: result.suggestedDependencies, current: blockedBy,
                listed: Set(board.cards.filter {
                    $0.root == projectRoot && $0.column != "done"
                }.map(\.id))) {
                blockedBy = offered
            }
            let wanted = result.suggestedRoot.trimmingCharacters(
                in: .whitespacesAndNewlines)
            if DependencySuggestion.allowsProjectMove(boxBeforePrepare: boxBeforePrepare),
               let pick = board.projects.first(where: { $0.root == wanted }),
               pick.root != projectRoot {
                revertRoot = board.projects.contains { $0.root == projectRoot }
                    ? projectRoot : ""
                projectRoot = pick.root
                suggestedRoot = pick.root
            }
            note = ""
        } else if !result.detail.isEmpty {
            note = result.detail
        }
    }

    private var saveRefineLabel: String {
        if saving && refinePressed { return "SAVING…" }
        return arm.refine != nil ? "Really refine?" : "SAVE & REFINE"
    }

    /// `refine` asks the Mac to open a planning session on the card it has
    /// just written — the envelope flag on `board_create`. It is sent only
    /// on the live path: every banked card is a plain save.
    private func save(refine: Bool = false) async {
        // The microphone is never left running behind a sent card.
        dictation.stop()
        // `uploading` is load-bearing, not belt-and-braces: a card created
        // while a photo is still climbing the wire is written with an empty
        // `attachments` field, and the copy that lands a second later is
        // referenced by nothing and ages out of the Mac's orphan sweep. The
        // photo is not lost late — it was never on the card.
        guard !tool.isEmpty, !saving, !uploading, !staging else { return }
        saving = true
        refinePressed = refine
        defer {
            saving = false
            refinePressed = false
        }
        // A bad number is refused here, on the form, in the store's own
        // sentence — before either bank() below can carry it into the
        // outbox, whose row can retry or discard an entry but never
        // correct it.
        if !priorityAcceptable(draftPriority) {
            note = "priority must be a whole number from 0 to 100, or empty"
            return
        }
        // Offline, the flag is dropped: the outbox gains no refine intent.
        // A card holding a photo on this phone is banked too, whatever the
        // route: the live create carries `attachments` the Mac already has,
        // and a local photo would otherwise be left behind.
        if offline || !localPhotos.isEmpty {
            bank()
            return
        }
        var fields = [
            "title": title,
            "summary": summary,
            "prompt": prompt,
            "tool": tool,
            "model": model,
            "column_name": "prep",
        ]
        if !staged.isEmpty {
            fields["attachments"] = staged.joined(separator: "\n")
        }
        if !workflow.isEmpty {
            fields["workflow"] = workflow
        }
        // Only when typed, so an unfilled composer's payload is byte-
        // identical and an older Mac never sees a key its door refuses.
        if !beneficiary.isEmpty {
            fields["beneficiary"] = beneficiary
        }
        if !intendedBenefit.isEmpty {
            fields["intended_benefit"] = intendedBenefit
        }
        if !successCriterion.isEmpty {
            fields["success_criterion"] = successCriterion
        }
        if !draftPriority.isEmpty {
            fields["priority"] = draftPriority
        }
        if let project = selectedProject {
            fields["project"] = project.name
            fields["root"] = project.root
        }
        if !stagingId.isEmpty {
            fields["create_token"] = stagingId
        }
        if refine {
            fields["refine"] = "true"
        }
        // Live path only, `refine`'s own rule and the same reason.
        if board.areasSupported && !area.isEmpty { fields["area"] = area }
        // Same live-path rule, and only against a Mac that takes the link.
        if board.dependenciesSupported && !blockedBy.isEmpty {
            fields["blocked_by"] = blockedBy
        }
        // A scout only, and only against a Mac that takes the key: a build
        // card's kind is the store's default.
        if board.scoutSupported && kind == "scout" { fields["kind"] = kind }
        if startWhenPlanned && kind != "scout" {
            fields["start_when_planned"] = "1"
        }
        let result = await client.post(action: PhoneActions.boardCreate, fields: fields)
        if result.ok {
            // The Mac owns the card now. The phone-side folder, if any,
            // becomes an orphan and the next `load()` sweep takes it.
            outbox.clearDraft(deletingPhotos: false)
            dismiss()
        } else if result.detail == "Could not reach the Mac." {
            // The Mac vanished between the last poll and this press. That is
            // the offline case one frame late, and the answer is the same
            // one: keep the card, send it silently later — without the
            // refine flag, which `bank()` never carries.
            bank()
        } else if !result.detail.isEmpty {
            note = result.detail
        }
    }

    /// The store's `normalise_priority` rule, mirrored so the offline path
    /// refuses what the Mac would: empty is fine, else ASCII digits only,
    /// at most 100. Mirrored, not authoritative — the Mac still judges.
    private func priorityAcceptable(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return true }
        guard trimmed.allSatisfy({ $0.isASCII && $0.isNumber }),
              let number = Int(trimmed) else { return false }
        return number <= 100
    }

    /// Keep the card on the phone and let the sweep take it. The staging id
    /// travels with it, so a photo already stored on the Mac and one still on
    /// the phone end up in the same folder.
    private func bank() {
        outbox.enqueue(id: stagingId, title: title, summary: summary,
                       prompt: prompt, tool: tool, model: model,
                       project: selectedProject?.name ?? "",
                       root: projectRoot, workflow: workflow,
                       remotePaths: staged, localPhotos: localPhotos,
                       beneficiary: beneficiary,
                       intendedBenefit: intendedBenefit,
                       successCriterion: successCriterion,
                       priority: draftPriority, area: area, kind: kind)
        // `enqueue` first: the entry now owns the folder and the id, and
        // `load()`'s entry-wins rule covers a crash between the two.
        outbox.clearDraft(deletingPhotos: false)
        dismiss()
        // A race where reach returned mid-compose resolves itself.
        Task { await outbox.sync(using: client) }
    }
}

// Copied byte-equal from
// panel/Sources/BobPanel/ComposerPhase.swift — test_composer_phase.py pins
// the pair.
/// What the composer fills in on its own before a person has chosen — the
/// assistant and the project — and the sentence PREPARE shows while it is
/// held for want of one. Pure, so `ComposerDefaultsTests` can pin the three
/// rules: a choice is never overwritten, the last-used project wins where it
/// is still offered, and an empty list is "waiting", not "nothing".
enum ComposerDefaults {
    /// The assistant to pre-select, or `nil` to leave `current` alone.
    static func tool(current: String, tools: [String]) -> String? {
        guard current.isEmpty, let first = tools.first, !first.isEmpty else { return nil }
        return first
    }

    /// The project to pre-select, or `nil` to leave `current` alone: the
    /// one used last where the Mac still offers it, else the first offered.
    static func projectRoot(current: String, roots: [String],
                            lastRoot: String) -> String? {
        guard current.isEmpty else { return nil }
        if !lastRoot.isEmpty, roots.contains(lastRoot) { return lastRoot }
        guard let first = roots.first, !first.isEmpty else { return nil }
        return first
    }

    static let waitingForProjects =
        "Waiting for the Mac's list of projects — PREPARE opens once one is chosen."
    static let noAssistant = "Pick an assistant above to PREPARE."

    /// Why PREPARE is held, in words, or `nil` when it is not held for one
    /// of these two reasons. The other holds (a photo in flight, a save)
    /// already read as words on their own buttons.
    static func holdReason(projectRoot: String, tool: String) -> String? {
        if projectRoot.isEmpty { return waitingForProjects }
        if tool.isEmpty { return noAssistant }
        return nil
    }
}

enum ComposerPhase {
    /// Why the second half of the form was opened; picks the caption.
    enum Reveal { case prepared, manual, resumed }
    /// Phase two is drawn when the flag was set — by Prepare or the link —
    /// or when any phase-two field already holds text (an older draft has
    /// no flag). Whitespace-only text counts as empty.
    static func expanded(flag: Bool, fields: [String]) -> Bool {
        flag || fields.contains {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }
    /// The caption above the opened form, `nil` where none is drawn.
    static func caption(for reveal: Reveal) -> String? {
        switch reveal {
        case .prepared:
            return "Dark Army drafted these from your idea — every box is yours to correct"
        case .manual:
            return "Every box is yours to fill — Prepare can still draft them from your idea"
        case .resumed:
            return nil
        }
    }
    static let fillMyselfLabel = "fill in myself"
    /// The words beside a held Save while the form is short.
    static let holdReason = "Prepare first, or fill it in yourself"
}

/// Who takes the card, as a row of brand marks you tap — the panel's
/// `ProviderSwitch` on the phone, drawn with the phone's own `PhoneProviderMark`
/// silhouettes and the same words. It replaced the last two assistant menus
/// the phone had (the card screen and the composer): one tap chooses, no menu
/// to open first, and a screen reader hears one group named Assistant whose
/// value is the chosen name (or "Sending" while a pick is on its way), then
/// one button per tile by name, the chosen one selected. The tiles reflow
/// into a column at the accessibility text sizes (`AdaptiveStack`), and an
/// assistant with no silhouette draws its name alone — the mark is already
/// `Color.clear` for it. The rule deciding the tiles and the value is
/// `ProviderChoice`, byte-equal with the panel's.
struct PhoneProviderSwitch: View {
    let tools: [String]
    var installed: [String: Bool] = [:]
    let selected: String
    var sending = false
    /// Equal-width tiles across the row (the composer, whose usage columns
    /// line up under them); the card screen keeps content-sized tiles.
    var fills = false
    let pick: (String) -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize, spacing: 8) {
            ForEach(ProviderChoice.tiles(tools: tools, installed: installed,
                                         selected: selected,
                                         showsNobody: false),
                    id: \.provider) { tile in
                let name = PhoneProviderMark.label(for: tile.provider)
                DecryptButton {
                    if tile.installed { pick(tile.provider) }
                } label: {
                    VStack(spacing: 2) {
                        HStack(spacing: 6) {
                            PhoneProviderMark(provider: tile.provider, size: 12)
                                // The silhouette is decoration; the name is
                                // the fact and the tile speaks it once.
                                .accessibilityHidden(true)
                            Text(name)
                                .font(Theme.mono(13))
                                .foregroundStyle(tile.selected ? Theme.phosphorBright : Theme.phosphor)
                        }
                        if !tile.installed {
                            Text(ProviderChoice.missingNote)
                                .font(Theme.mono(9))
                                .foregroundStyle(Theme.faint)
                        }
                        // Reserved under every tile, so choosing another one
                        // moves nothing. Selection is never colour-alone.
                        Rectangle()
                            .fill(tile.selected ? Theme.phosphor : Color.clear)
                            .frame(height: 2)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .frame(maxWidth: fills ? .infinity : nil)
                    .overlay(Rectangle().stroke(tile.selected ? Theme.phosphor : Theme.hair, lineWidth: 1))
                    .contentShape(Rectangle())
                }
                .opacity(tile.installed ? 1 : 0.45)
                // Inert, like the panel's: no press animation or haptic for
                // a tile that cannot be chosen.
                .disabled(!tile.installed)
                .accessibilityLabel(tile.installed ? name
                                    : "\(name), \(ProviderChoice.missingNote)")
                .accessibilityAddTraits(tile.selected ? .isSelected : [])
            }
        }
        .disabled(sending)
        // `.disabled` alone changes nothing a plain-styled tile draws, so
        // a row waiting on its pick looked exactly like one open to a tap
        // — and was tapped again. Dimmed, and never colour-alone: the
        // group's spoken value says "Sending" for the same stretch.
        .opacity(sending ? 0.45 : 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(ProviderChoice.groupLabel)
        .accessibilityValue(sending ? "Sending"
                            : ProviderChoice.value(selected: selected,
                                                   name: PhoneProviderMark.label(for:)))
    }
}

/// The figures under the assistant tiles: each offered assistant's
/// seven-day usage and Claude's five-hour window, off the bars the
/// phone already holds. Read-only; no poll of its own. **One column per
/// assistant, under its own tile** — the tiles are drawn `fills` so both
/// rows share the width equally — and each window on its own line, never
/// the figures run together on one line.
struct ComposerUsageRow: View {
    let tools: [String]
    let selected: String
    let bars: [UsageBar]
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var lines: [ComposerUsageRule.Row] {
        ComposerUsageRule.rows(tools: tools, bars: bars)
    }

    var body: some View {
        if !lines.isEmpty {
            AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize, spacing: 8,
                          alignment: .leading, rowAlignment: .top) {
                ForEach(lines, id: \.provider) { row in
                    ComposerUsageLine(row: row, selected: selected)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    /// One assistant's name and figures. Selection tints the name only.
    // Column-0 so test_phone_accessibility's owner walk (re.match, no
    // leading whitespace) names this line, not the enclosing row.
private struct ComposerUsageLine: View {
        let row: ComposerUsageRule.Row
        let selected: String

        private var spoken: String {
            ComposerUsageRule.spoken(row: row,
                                     name: PhoneProviderMark.label(for: row.provider))
        }

        /// One window per line: "7d 42%" over "5h 18%".
        private func figure(_ item: ComposerUsageRule.Figure) -> Text {
            let drawn = UsageChipText.figure(percent: item.percent, stale: item.stale)
            let fresh = item.percent != nil && !item.stale
            return Text(item.window + " ")
                .foregroundStyle(Theme.faint)
                + Text(drawn)
                .foregroundStyle(fresh ? Theme.phosphor : Theme.faint)
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 1) {
                Text(PhoneProviderMark.label(for: row.provider))
                    .font(Theme.mono(11))
                    .foregroundStyle(row.provider == selected ? Theme.phosphorBright : Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(row.figures, id: \.window) { item in
                    figure(item)
                        .font(Theme.mono(11))
                        .monospacedDigit()
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(spoken)
        }
    }
}

enum ProviderChoice {
    /// What a screen reader calls the group.
    static let groupLabel = "Assistant"
    /// The group's value while no assistant is chosen.
    static let unchosen = "not chosen"
    /// The one tile that is a route back to unassigned, drawn by the card
    /// window alone.
    static let nobody = "Nobody yet"
    static let missingNote = "not installed on this Mac"
    /// The three the artwork actually has. A fourth entry in
    /// `dispatch._EXECUTABLES` draws as its name on a tile rather than
    /// borrowing somebody else's logo for it.
    static let knownMarks: Set<String> = ["claude", "codex", "grok"]

    /// One entry of the row. `provider` is `""` for the "Nobody yet" tile.
    struct Tile: Equatable {
        let provider: String
        let hasMark: Bool
        let selected: Bool
        let installed: Bool
    }

    /// One tile per offered tool, in the daemon's order, then the "Nobody
    /// yet" tile where the caller offers the route back to unassigned.
    static func tiles(tools: [String], installed: [String: Bool] = [:],
                      selected: String, showsNobody: Bool) -> [Tile] {
        var out = tools.map {
            Tile(provider: $0,
                 hasMark: knownMarks.contains($0),
                 selected: !selected.isEmpty && $0 == selected,
                 installed: installed[$0] ?? true)
        }
        if showsNobody {
            out.append(Tile(provider: "", hasMark: false, selected: selected.isEmpty,
                            installed: true))
        }
        return out
    }

    /// True only where the row cannot tell the truth and the caller draws an
    /// inert word chip instead: nothing offered (an older daemon, or dispatch
    /// switched off), or a chosen assistant that is not among the offered ones
    /// — a selection the row could only render by leaving every tile dim,
    /// which reads as *unassigned* and is a lie about a card that names
    /// somebody. An assistant with no artwork is **not** a reason: it draws
    /// as its name.
    static func wordChipOnly(tools: [String], selected: String) -> Bool {
        if tools.isEmpty { return true }
        if !selected.isEmpty && !tools.contains(selected) { return true }
        return false
    }

    /// The group's spoken value: the chosen assistant's name, or `unchosen`.
    static func value(selected: String, name: (String) -> String) -> String {
        selected.isEmpty ? unchosen : name(selected)
    }

    /// The cursor after one arrow press. Clamped, never wrapped: a macOS
    /// radio group stops at its ends.
    static func moved(cursor: Int, count: Int, step: Int) -> Int {
        guard count > 0 else { return 0 }
        return min(max(cursor + step, 0), count - 1)
    }

    /// Where the cursor starts: on the chosen tile, else the first.
    static func startCursor(tiles: [Tile]) -> Int {
        tiles.firstIndex(where: \.selected) ?? 0
    }
}

/// The composer's waits-on box: one row per chosen card with ✕, and **Add…**
/// over this project's other unfinished cards not already chosen, capped at
/// the store's eight (`BoardCard.dependencyChoices`' rule, the saved card's
/// picker in `CardDetailView`). Bound to newline-joined ids; the Mac still
/// refuses a loop or another project in words at create.
private struct ComposerDependencyPicker: View {
    @Binding var selected: String
    let root: String
    let cards: [BoardCard]

    private var chosen: [BoardCard] {
        let wanted = selected.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return wanted.compactMap { id in cards.first { $0.id == id } }
    }

    private var choices: [BoardCard] {
        let listed = Set(chosen.map(\.id))
        guard listed.count < 8 else { return [] }
        return cards.filter {
            $0.root == root && !listed.contains($0.id) && $0.column != "done"
        }
    }

    private func write(_ ids: [String]) {
        selected = ids.joined(separator: "\n")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(chosen) { card in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(card.title.isEmpty ? "untitled" : card.title)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                    Spacer(minLength: 8)
                    DecryptButton {
                        write(chosen.map(\.id).filter { $0 != card.id })
                    } label: {
                        Text("\u{2715}")
                            .font(Theme.mono(13))
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.phosphor)
                    .accessibilityLabel("Stop waiting on \(card.title.isEmpty ? "untitled" : card.title)")
                }
            }
            if !choices.isEmpty {
                Menu {
                    ForEach(choices) { other in
                        DecryptButton(other.title.isEmpty ? "untitled" : other.title) {
                            write(chosen.map(\.id) + [other.id])
                        }
                    }
                } label: {
                    Text("WAITS ON \u{00b7} Add\u{2026}")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphor)
                        .frame(minHeight: 44, alignment: .leading)
                }
                .accessibilityLabel("Add a card this one waits on")
            }
        }
    }
}
