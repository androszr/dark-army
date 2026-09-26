import SwiftUI

/// The one place that decides how a waiting agent is answered.
///
/// Two screens draw it — the agent detail and the "Needs you" tab — so the
/// shapes cannot drift apart. Exactly one of five is drawn, in this order:
///
/// 1. A one-question `AskUserQuestion` dialog **and** a terminal the Mac can
///    type into (`can_type`): the offered answers as numbered buttons,
///    posting the singular answer verb. Not armed — arming is for
///    destructive verbs, and a two-press answer is friction on the one path
///    this exists to make fast.
/// 2. A dialog of **several** questions with `can_type`: one group per
///    question down the screen, each option a selection toggle, and a single
///    send that stays off until every question has a choice — the terminal's
///    dialog advances on Enter and digits address the question currently
///    shown, so the only meaningful act is every answer, in order, in one
///    burst (the plural answer verb). The question texts themselves are the
///    screen's, above this box; the groups carry the headers and choices.
///    The send is a **two-press**, unlike shape (1): the first press swaps
///    the toggles for the picks read back and a "send these N answers?"
///    line, the second sends. The terminal's own dialog ends on exactly such
///    a screen, so this is the same beat rather than a second one, and it is
///    the one place here where a single thumb commits several answers at
///    once.
/// 3. Either dialog with `can_type` false: every question's answers as
///    caption text and one dim line saying why there is no button. A button
///    that would be refused every time is worse than no button.
/// 4. A turn that ended offering `reply_options` (`bob-actions`): those
///    labels as buttons, sent through `reply`. A different mechanism from
///    the dialog options entirely — a channel push with its own caps — and
///    deliberately kept so.
/// 5. Otherwise the free-text `reply` field.
///
/// With `terminalWins` the free-text reply (shape 5) is hidden — the
/// terminal's own box is the one box on that screen. The buttons of shapes
/// 1, 2 and 4 stay where they are.
///
/// The refusal note is the Mac's own sentence, verbatim: the refusals in
/// both of `BobDaemon`'s answer verbs are written for a human to read.
/// `@MainActor` — `PhoneCardDetailView`'s reason: the mark is drawn from
/// `@State` a task writes.
@MainActor
struct AnswerBox: View {
    let agent: Agent
    /// Whether this session is at the end of a turn in the *current* snapshot.
    /// Gates the reply shapes only — a live dialog is answerable regardless.
    let stopped: Bool
    @ObservedObject var client: PhoneClient
    /// The dialog picks, shared across every box drawing this question — the
    /// "Needs you" row and the agent detail are two view identities, and
    /// picks held as local `@State` never crossed between them.
    @ObservedObject private var drafts: AnswerDrafts
    /// True where the screen already draws a terminal send box for this
    /// agent: the free-text reply steps aside so there is one place to type.
    let terminalWins: Bool

    /// Which part of the box this instance draws. The conversation screen
    /// splits it in two (26 Sep 2026): the answer buttons ride the
    /// scrolling page under the turns, and the message field is pinned
    /// beneath the page, drawn whether the agent is working or stopped, so
    /// a person can always say something to it. Every other screen draws
    /// the whole box, gated as it always was.
    enum Part { case whole, choices, composer }
    let part: Part

    init(agent: Agent, stopped: Bool, client: PhoneClient,
         terminalWins: Bool = false, retainedReply: PhoneReplyDraft? = nil,
         part: Part = .whole) {
        self.agent = agent
        self.stopped = stopped
        self.client = client
        self.terminalWins = terminalWins
        self.part = part
        self.drafts = client.answerDrafts
        _replyDraft = StateObject(wrappedValue: retainedReply ?? PhoneReplyDraft())
    }

    @StateObject private var replyDraft: PhoneReplyDraft
    private var replyText: String {
        get { replyDraft.text }
        nonmutating set { replyDraft.text = newValue }
    }
    @State private var note = ""
    /// Which of this box's controls made the press that is in play, so
    /// exactly one button wears the queue's mark (QUEUED, SENDING…, SENT)
    /// while every control stays usable. Held until the mark goes — the
    /// press landed or was refused — never cleared by the send returning,
    /// which is immediate now that a press is queued rather than awaited.
    @State private var sendingKey: String?
    @FocusState private var replyFocused: Bool
    @ObservedObject private var dictation = DictationEngine.shared

    /// This box's slot in the shared store: session + question, so a new
    /// dialog starts blank by construction and the same one keeps its picks.
    private var draftKey: String {
        AnswerDrafts.key(sessionId: agent.sessionId,
                         questionId: agent.question.id)
    }

    /// The picks per question position. A set on every question: a pick-one
    /// question holds at most one, a multi-select question holds every
    /// ticked box — the same shape `AnswerDrafts.Entry.choices` stores.
    /// Typed here so a drift from `[Int: Set<Int>]` is a compile error on
    /// the phone, not a TestFlight archive failure.
    private var choices: [Int: Set<Int>] { drafts.entry(draftKey).choices }

    /// True once every question has a pick and the send has been asked for
    /// once: the box then draws the picks back as a summary and waits to be
    /// confirmed. Claude Code's own multi-question dialog ends on exactly
    /// such a screen, and a thumb on a phone deserves it more than a
    /// keyboard does. Cleared by a new dialog, by CHANGE, and by the send
    /// that lands.
    private var confirming: Bool { drafts.entry(draftKey).confirming }

    /// One microphone slot per session, so two boxes on the "Needs you" list
    /// never share a transcription.
    private var micField: String { "reply:\(agent.sessionId)" }

    private var options: [String] { agent.question.options }
    private var questions: [AgentQuestion] { agent.questionList }
    /// A stopped turn on a session with a channel: the `bob-actions`
    /// buttons (shape 4) are drawn on this alone.
    private var canAnswer: Bool { stopped && agent.channel }
    /// The free-text reply (shape 5) additionally yields to a terminal box.
    /// Never on the `.choices` part: its field is the pinned composer's.
    private var canReply: Bool { stopped && agent.channel && !terminalWins && part == .whole }

    /// A press for *this agent* is in play — queued, on its way, or landed
    /// and waiting for the snapshot to agree. Read for the **label swap
    /// only**: nothing here dims on it. A press is queued, not awaited, so
    /// the control is the person's again the moment it is written down; a
    /// second identical press is turned away by the queue's own dedupe
    /// (`PhoneClient.queuedTwiceRefusal`) and said so in the note, and the
    /// answer hold inside `post` is a row-leaving visual the agent screen
    /// reads, not a lock on this box.
    private var busy: Bool { client.queueMark(for: agent.sessionId) != nil }

    /// The word the pressed control wears while its press is in play.
    private var mark: String { client.queueMark(for: agent.sessionId) ?? "SENDING…" }

    /// The same word as a screen reader says it — "Queued", "Sending",
    /// "Sent" — never "Sending" for a control drawn QUEUED.
    private var spokenMark: String { Receipt.spoken(mark: mark) }

    private func sending(_ key: String) -> Bool { sendingKey == key && busy }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if part == .composer {
                composerParts
            } else {
                answerParts
            }
            if !note.isEmpty {
                Text(note)
                    .font(Theme.mono(12))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onChange(of: replyFocused) { _, focused in
            // Thumbs and voice never share a sentence.
            if focused { dictation.stop() }
        }
        .onChange(of: agent.question.id) { _, _ in
            // The new id's slot is blank by construction; this only sweeps
            // the replaced dialog's picks out of the store.
            drafts.reset(sessionId: agent.sessionId,
                         keepingQuestionId: agent.question.id)
        }
        .onChange(of: busy) { _, inPlay in
            // The press left the queue — landed or refused — so the pressed
            // control goes back to its own words, and the Mac's words about
            // it, if any, are the note.
            if !inPlay { sendingKey = nil }
            takeNote()
        }
        .onAppear {
            // A refusal that landed while this box was not on screen is
            // still the Mac's last word about this agent: read it on
            // arrival, not only on a change seen while drawn — and drawn
            // is read, so the record ages off the QUEUE list.
            takeNote()
        }
    }

    /// The pinned composer: the message field, always, and — only where the
    /// Mac says it has no route to this agent — one dim line saying the
    /// message may be turned down, so SEND is never a silent dead end. The
    /// Mac's own refusal, when one comes back, is the note under it.
    @ViewBuilder private var composerParts: some View {
        freeText
        if !agent.channel {
            Text(agent.interactionNote.isEmpty
                 ? "The Mac has no route to this agent right now, so a message may be turned down."
                 : agent.interactionNote)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private var answerParts: some View {
            if !options.isEmpty {
                if agent.canType {
                    if questions.count > 1 || agent.question.multiSelect {
                        multiQuestionGroups
                    } else {
                        optionButtons
                    }
                } else {
                    optionCaptions
                    if canReply { freeText }
                }
            } else if canAnswer {
                if !agent.replyOptions.isEmpty {
                    replyButtons
                } else if canReply {
                    freeText
                }
            }
            // On the Conversation tab the pinned composer draws this line
            // when the Mac has no route (`composerParts`); drawn here too,
            // it said the same sentence twice on one screen.
            if !agent.interactionNote.isEmpty, part == .whole || agent.channel {
                Text(agent.interactionNote)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !agent.askNote.isEmpty {
                Text(agent.askNote)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
    }

    /// The session's note is one per scope and this box is one of two
    /// readers on the agent screen: it takes the refusal of an **answer or
    /// reply** — the verbs it sends (`QueueNote.isAnswer`) — and leaves
    /// every other verb's to the screen around it, so a refused Stop is
    /// never drawn under the answer buttons as well as under the actions.
    /// Reading is drawing: only the reader that draws it reads it.
    /// On the conversation screen the pinned composer is the one reader: it
    /// is always in view, where the scrolling `.choices` half could not be.
    private func takeNote() {
        guard part != .choices,
              let queued = client.queueNote(for: agent.sessionId),
              queued.isAnswer else { return }
        note = queued.text
        client.readQueueNote(for: agent.sessionId)
    }

    // MARK: - Shape (1)

    /// `option_index` is an index into the options the *daemon* holds, so the
    /// question's own id travels with it — that match is what catches a stale
    /// snapshot pointing the index at a different answer.
    private var optionButtons: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(options.enumerated()), id: \.offset) { index, label in
                let detail = agent.question.detail(at: index)
                DecryptButton {
                    Task {
                        await send(key: "opt:\(index)",
                                   action: PhoneActions.answerQuestion,
                                   fields: [
                                       "session_id": agent.sessionId,
                                       "question_id": agent.question.id,
                                       "option_index": String(index),
                                   ])
                    }
                } label: {
                    optionCaption(index: index, label: label, detail: detail)
                }
                .buttonStyle(AlarmOutline())
                .accessibilityLabel(sending("opt:\(index)")
                                    ? spokenMark
                                    : (detail.isEmpty
                                       ? "Answer: \(label)"
                                       : "Answer: \(label). \(detail)"))
            }
        }
    }

    /// Label plus the option's description, unclipped. Empty detail is
    /// omitted rather than drawn as a blank line.
    private func optionCaption(index: Int, label: String,
                               detail: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(sending("opt:\(index)") ? mark : "\(index + 1). \(label)")
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
            if !detail.isEmpty && !sending("opt:\(index)") {
                Text(detail)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Shape (2) — several questions or several answers, one send

    /// The caption on a multi-select question's heading: the boxes are the
    /// same shape as the radio marks at a glance, and the words say what
    /// changed. `NeedsYouView` draws the same words above the question text.
    static let multiSelectHint = "pick any that apply"

    /// Every question has at least one pick.
    private var complete: Bool {
        questions.indices.allSatisfy { !(choices[$0] ?? []).isEmpty }
    }

    /// "ANSWER" on a one-question (multi-select) dialog, "ANSWER ALL N" on
    /// several — the word "all" over one question reads as a mistake.
    private var answerTitle: String {
        questions.count == 1 ? "ANSWER" : "ANSWER ALL \(questions.count)"
    }

    private func groupHeading(_ qi: Int, _ q: AgentQuestion) -> String {
        let base = q.header.isEmpty ? "question \(qi + 1)"
                                    : "\(qi + 1) · \(q.header)"
        return q.multiSelect ? "\(base) · \(Self.multiSelectHint)" : base
    }

    /// Pick per question, then send once. The question texts are drawn by
    /// the screen above this box; each group here is the header (or its
    /// number) plus that question's choices — radio marks on a pick-one
    /// question, tick boxes that toggle on a multi-select one. The send posts
    /// the picks in dialog order, comma-joined, a multi-select question's
    /// picks `+`-joined, and the Mac re-checks the count, every index and
    /// every group's shape against the dialog it holds before typing.
    private var multiQuestionGroups: some View {
        VStack(alignment: .leading, spacing: 12) {
            if confirming {
                confirmSummary
            } else {
                ForEach(Array(questions.enumerated()), id: \.offset) { qi, q in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(groupHeading(qi, q))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                        ForEach(Array(q.options.enumerated()),
                                id: \.offset) { index, label in
                            let detail = q.detail(at: index)
                            DecryptButton {
                                drafts.pick(draftKey, question: qi,
                                            option: index, multi: q.multiSelect)
                            } label: {
                                HStack(alignment: .firstTextBaseline,
                                       spacing: 6) {
                                    Text(q.multiSelect
                                         ? (choices[qi]?.contains(index) == true
                                            ? "☑" : "☐")
                                         : (choices[qi]?.contains(index) == true
                                            ? "◉" : "○"))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("\(index + 1). \(label)")
                                            .multilineTextAlignment(.leading)
                                            .fixedSize(horizontal: false,
                                                       vertical: true)
                                        if !detail.isEmpty {
                                            Text(detail)
                                                .font(Theme.mono(11))
                                                .foregroundStyle(Theme.faint)
                                                .multilineTextAlignment(.leading)
                                                .fixedSize(horizontal: false,
                                                           vertical: true)
                                        }
                                    }
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .font(Theme.mono(13))
                            .foregroundStyle(choices[qi]?.contains(index) == true
                                             ? Theme.phosphorBright : Theme.dim)
                            // The ◉/○ and ☑/☐ glyphs are colour-and-shape
                            // only; VoiceOver needs the trait to say
                            // "selected", the option's own words to say what
                            // it is, and `.isButton` to say it can be picked.
                            // The selected trait keeps its own line: it is
                            // pinned verbatim by `test_phone_needs_you.py`.
                            //
                            // `.combine`, never `.ignore`: this modifier is
                            // on the `Button` itself, and `.ignore` mints a
                            // fresh element that keeps neither the button's
                            // activation action nor its enabled state.
                            // `.ignore` is right only over a subtree whose
                            // parent supplies the action — a DecryptButton's
                            // label, which is where the row versions sit.
                            .accessibilityElement(children: .combine)
                            .accessibilityLabel(detail.isEmpty
                                                ? label
                                                : "\(label). \(detail)")
                            .accessibilityAddTraits(.isButton)
                            .accessibilityAddTraits(
                                choices[qi]?.contains(index) == true ? .isSelected : [])
                        }
                    }
                }
                DecryptButton(answerTitle) {
                    drafts.setConfirming(draftKey, true)
                }
                .buttonStyle(AlarmOutline())
                .disabled(!complete)
                // The step, said out loud: this one reads the picks back
                // rather than sending them.
                .accessibilityLabel("\(answerTitle) — read the picks back first")
            }
        }
    }

    /// The picks read back, and the press that actually sends them.
    ///
    /// The terminal's own dialog ends on a summary that waits to be
    /// confirmed, so the answers are not in until that screen is accepted —
    /// which is why the burst the Mac types ends with one more Enter. This
    /// is the same beat on the phone, and it is what stops a thumb sending
    /// three answers it never read back.
    private var confirmSummary: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(questions.count == 1 ? "send this answer?"
                                      : "send these \(questions.count) answers?")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
            ForEach(Array(questions.enumerated()), id: \.offset) { qi, q in
                VStack(alignment: .leading, spacing: 2) {
                    Text(groupHeading(qi, q))
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                    Text(chosenLabel(qi, q))
                        .font(Theme.mono(13))
                        .foregroundStyle(Theme.phosphorBright)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            DecryptButton(sending("all") ? mark : "YES, SEND") {
                // One group per question in dialog order, each ascending; a
                // pick-one question's group is one index, so a dialog with
                // no multi-select question posts exactly what it always did.
                let ordered: [[Int]] = (0..<questions.count).map {
                    (choices[$0] ?? []).sorted()
                }
                guard ordered.allSatisfy({ !$0.isEmpty }) else { return }
                Task {
                    let ok = await send(key: "all",
                                        action: PhoneActions.answerQuestions,
                                        fields: [
                                            "session_id": agent.sessionId,
                                            "question_id": agent.question.id,
                                            "option_indexes": ordered
                                                .map { $0.map(String.init)
                                                         .joined(separator: "+") }
                                                .joined(separator: ","),
                                        ])
                    if ok {
                        drafts.clear(draftKey)
                    }
                }
            }
            .buttonStyle(AlarmOutline())
            .accessibilityLabel(sending("all") ? spokenMark : "Yes, send these answers")
            DecryptButton("CHANGE") { drafts.setConfirming(draftKey, false) }
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .accessibilityLabel("Change the picks")
        }
    }

    /// Every label the picks name, ascending and comma-joined, or a dash —
    /// the guard is unreachable while `confirming` is only ever set on a
    /// complete set of picks, and saying so beats an index crash if that
    /// ever stops being true.
    private func chosenLabel(_ qi: Int, _ q: AgentQuestion) -> String {
        let picked = (choices[qi] ?? []).sorted()
            .filter { q.options.indices.contains($0) }
        guard !picked.isEmpty else { return "—" }
        return picked.map { "\($0 + 1). \(q.options[$0])" }
            .joined(separator: ", ")
    }

    // MARK: - Shape (3)

    private var optionCaptions: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(questions.enumerated()), id: \.offset) { qi, q in
                if questions.count > 1 || q.multiSelect {
                    Text(groupHeading(qi, q))
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                }
                ForEach(Array(q.options.enumerated()),
                        id: \.offset) { index, label in
                    let detail = q.detail(at: index)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(index + 1). \(label)")
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                        if !detail.isEmpty {
                            Text(detail)
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.faint)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            if agent.interactionNote.isEmpty {
                Text("Dark Army cannot type into this terminal, so answer it at the Mac.")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Shape (4)

    private var replyButtons: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(agent.replyOptions, id: \.self) { label in
                DecryptButton(sending("reply:\(label)") ? mark : label) {
                    Task {
                        await send(key: "reply:\(label)",
                                   action: PhoneActions.reply,
                                   fields: [
                                       "session_id": agent.sessionId,
                                       "text": label,
                                   ])
                    }
                }
                .buttonStyle(AlarmOutline())
                .accessibilityLabel(sending("reply:\(label)")
                                    ? spokenMark : "Reply: \(label)")
            }
        }
    }

    // MARK: - Shape (5)

    /// The placeholder names the route the Mac says the reply takes, read
    /// verbatim off `reply_via`; anything but `"typed"` reads as the channel.
    private var replyPlaceholder: String {
        agent.replyVia == "typed"
            ? "message to this agent — typed into its terminal"
            : "message to this agent"
    }

    private var replyField: some View {
        TextField("", text: $replyDraft.text,
                  prompt: Text(replyPlaceholder)
                    .foregroundStyle(Theme.faint),
                  axis: .vertical)
            .font(Theme.mono(13))
            .foregroundStyle(Theme.phosphor)
            // Prose, so it types like prose. The pairing screen's
            // identifiers are the one place that stays verbatim.
            .textInputAutocapitalization(.sentences)
            .focused($replyFocused)
            .fieldWell(focused: replyFocused, multiline: true,
                       onTap: { replyFocused = true })
    }

    private var freeText: some View {
        VStack(alignment: .leading, spacing: 8) {
            // `.top` alignment is what pins the microphone to the well's top
            // corner: the well grows downward as the message does, and a
            // centred mic would drift away from the thumb that started it.
            HStack(alignment: .top, spacing: 8) {
                // The pinned composer starts at one line and grows with the
                // message, so an empty one never takes the page it sits
                // under; the whole box keeps its five-line well.
                if part == .composer {
                    replyField
                } else {
                    replyField.lineLimit(5...)
                }
                MicButton(id: micField, text: $replyDraft.text)
            }
            if dictation.noteField == micField, !dictation.note.isEmpty {
                Text(dictation.note)
                    .font(Theme.mono(12))
                    .foregroundStyle(.orange)
            }
            HStack {
                Spacer(minLength: 0)
                if sending("send") {
                    AgentChatterView(.caret, wait: .sending, seed: agent.sessionId,
                                     spoken: "Sending your reply")
                        .id(agent.sessionId)
                }
                DecryptButton(sending("send") ? mark : "SEND") {
                    // The microphone is never left running behind a sent reply.
                    dictation.stop()
                    let text = replyText.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { return }
                    Task {
                        let ok = await send(key: "send",
                                            action: PhoneActions.reply,
                                            fields: [
                                                "session_id": agent.sessionId,
                                                "text": text,
                                            ])
                        if ok { replyText = "" }
                    }
                }
                // Off while there is nothing to send, and for no other
                // reason: a press is queued, not awaited.
                .disabled(replyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .buttonStyle(AlarmOutline())
                .accessibilityLabel(sending("send")
                                    ? spokenMark : "Send this message")
            }
        }
    }

    /// The one seam every control here sends through. `enqueue` writes the
    /// press down and returns at once; `true` means it is in the queue, and
    /// the two immediate refusals — a duplicate press, a declined Face ID
    /// sheet — go in the note. The Mac's own later words arrive through
    /// `queueNote(for:)` on redraw.
    @discardableResult
    private func send(key: String, action: String,
                      fields: [String: String]) async -> Bool {
        let result = await client.enqueue(action: action, fields: fields,
                                          scope: agent.sessionId)
        if result.ok {
            sendingKey = busy ? key : nil
            note = ""
            return true
        }
        if !result.detail.isEmpty { note = result.detail }
        return false
    }
}
