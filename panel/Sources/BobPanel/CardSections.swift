import Foundation

/// Which of a card's sections lead, which fold, and in what order — decided
/// by the card's **stage**, the same on the Mac and the phone.
///
/// A card's detail used to draw every section in one fixed order wherever the
/// card sat on the board, so a card waiting on your check buried "what was
/// done" and "what to check" under the instructions, the plan and the session
/// box. This is the one rule both card screens read instead: for each of the
/// six stages a card can be in, the full order of its sections, the set that
/// opens by default, the four that are never behind a chevron, the one
/// MORE row everything else sits behind, the one next action the card leads
/// with, and the closed-row tail (`"WHAT CHANGED · 4 files"`) composed from
/// facts the card already carries.
///
/// **Pure, and never the tool's.** `stage` reads `column`, `link_state` and
/// the daemon's own `manual_check_due` — never the assistant's name — so it is
/// right for Claude, Grok and Codex alike. The open set is screen state on the
/// view (`@State`), kept nowhere, so every open and every column change starts
/// back at this rule. Copied byte-equal into `ios/BobPhone/CardDetailView.swift`
/// and pinned by `host/tests/test_card_sections.py`; tabled in
/// `panel/Tests/BobPanelTests/CardSectionsTests.swift`.
enum CardSections {
    /// Every section a card screen may draw. The raw value is the fold row's
    /// label, so the two surfaces cannot word one differently.
    enum Section: String, CaseIterable {
        case status = "STATUS"
        case manualCheck = "MANUAL CHECK"
        case closeSignature = "CLOSED BY"
        case objective = "OBJECTIVE"
        case verbs = "ACTIONS"
        case plan = "PLAN"
        case workRecord = "WHAT CHANGED"
        case run = "WHAT RAN"
        case editor = "EDIT CARD"
        case instructions = "INSTRUCTIONS"
        case crew = "CREW"
        case session = "SESSION"
        case thread = "MESSAGES"
        case queue = "QUEUED"
        case timeline = "TIMELINE"
        case documents = "DOCUMENTS"
        case attachments = "ATTACHMENTS"
        case danger = "DELETE"
        /// The three added by `plans/2026-09-20-simplify-card-details.md`,
        /// after Delete so the earlier labels keep their lines: the
        /// evidence fold that used to sit outside the rule, the verbs that
        /// are not the one next action, and the one row everything folded
        /// sits behind.
        case collaboration = "COLLABORATION"
        case otherVerbs = "OTHER ACTIONS"
        case more = "MORE"
        /// A scout's written report — the deliverable of a scout card, so
        /// it is a section of its own rather than a path under DOCUMENTS
        /// (added 21 Sep 2026, after MORE so the earlier labels keep their
        /// lines). Drawn on every scout: the report once it is attached,
        /// and a line saying the scout is still out until then.
        case report = "REPORT"
        /// The cards this one waits on, whether each is done, and the cards
        /// it unblocks (added 24 Sep 2026, after REPORT so the earlier labels
        /// keep their lines). Pinned beside QUEUED, because a card held by a
        /// dependency says so in the queued line and the reason sits here.
        case dependencies = "WAITS ON"
    }

    /// Where the card is in its life. Four columns, with In progress split
    /// three ways by what the daemon says about the bound session.
    enum Stage: String, CaseIterable {
        case prep, backlog, running, manualCheck, ended, done
    }

    /// The one sentence drawn at the top of a card waiting on a check.
    static let statusLine = "Finished — waiting on your check"

    /// `manualCheckDue` is the daemon's `manual_check_due` — steps present
    /// *and* the assistant no longer typing — and it wins over `linkState`,
    /// because a check somebody has to do is the reason the card is open.
    /// `runActive` is the daemon's `run_active`, the pipeline band's own
    /// RUN/ENDED split: a `live` card whose session Dark Army can no longer hear
    /// never reaches `link_state == "ended"`, and drawing it as running
    /// while the band draws it ENDED is the two-surfaces-disagree bug. So
    /// a bound card (`linkState` non-empty) that is not running is `.ended`
    /// here too; a card with no link at all is `.running` by column and
    /// says so in `stateWords(for:linked:)`. An unknown column is Backlog,
    /// the panel's own `BoardColumn` fallback.
    static func stage(column: String, linkState: String,
                      manualCheckDue: Bool, runActive: Bool) -> Stage {
        switch column {
        case "prep": return .prep
        case "backlog": return .backlog
        case "done": return .done
        case "in_progress":
            if manualCheckDue { return .manualCheck }
            if linkState == "ended" { return .ended }
            if !linkState.isEmpty && !runActive { return .ended }
            return .running
        default: return .backlog
        }
    }

    /// Drawn open wherever they appear, with no chevron: a chevron is a
    /// promise that something is behind it, and these are never hidden.
    /// WAITS ON is pinned beside QUEUED and, like it, draws nothing on a
    /// card with nothing to say.
    static let pinned: Set<Section> = [.status, .verbs, .queue, .dependencies]

    /// The full order per stage: the pinned four and the stage's lead
    /// first, then MORE, then everything MORE hides, Delete always last.
    /// QUEUED is in every order because a queued card does not move — a
    /// Prep card pressed Start waits in Prep — and it draws nothing unless
    /// the card is queued. WAITS ON follows QUEUED in every order for the
    /// same reason. `.more` appears exactly once in every order and
    /// everything after it is behind that one row until it is pressed.
    static func order(for stage: Stage) -> [Section] {
        switch stage {
        case .prep:
            return [.status, .verbs, .queue, .dependencies, .more, .report, .editor, .objective,
                    .instructions, .crew, .otherVerbs, .collaboration, .timeline,
                    .attachments, .documents, .thread, .danger]
        case .backlog:
            return [.status, .verbs, .queue, .dependencies, .more, .plan, .report, .editor,
                    .objective, .instructions, .crew, .otherVerbs, .collaboration,
                    .timeline, .documents, .attachments, .thread, .danger]
        case .running:
            return [.status, .verbs, .queue, .dependencies, .more, .report, .session, .thread,
                    .manualCheck, .workRecord, .plan, .instructions, .editor,
                    .crew, .run, .otherVerbs, .objective, .collaboration,
                    .timeline, .attachments, .documents, .danger]
        case .manualCheck:
            return [.status, .manualCheck, .verbs, .queue, .dependencies, .more, .workRecord,
                    .report, .run, .plan, .instructions, .editor, .crew, .session,
                    .thread, .otherVerbs, .objective, .collaboration, .timeline,
                    .attachments, .documents, .danger]
        case .ended:
            return [.status, .report, .verbs, .queue, .dependencies, .more, .workRecord, .run,
                    .manualCheck, .closeSignature, .plan, .instructions, .editor,
                    .session, .thread, .crew, .otherVerbs, .objective,
                    .collaboration, .timeline, .attachments,
                    .documents, .danger]
        case .done:
            return [.status, .closeSignature, .report, .verbs, .queue, .dependencies, .more,
                    .objective, .workRecord, .run, .plan, .instructions, .crew,
                    .session, .thread, .editor, .otherVerbs, .collaboration,
                    .timeline, .attachments, .documents, .danger]
        }
    }

    /// Open by default at this stage; the pinned four are implied. Three
    /// exceptions to "everything behind MORE", and only three: a card waiting
    /// on your check opens with the check's steps, because the steps are the
    /// next action; a finished card opens with its close note, because
    /// Reviewed is a judgment about that note; and a scout that has come
    /// back — its run ended or the card Done — opens with its report,
    /// because the report is what the card was for. REPORT leads only on a
    /// scout: on a build card the section has no content and draws nothing.
    /// `.more` is never here.
    static func leads(for stage: Stage) -> Set<Section> {
        switch stage {
        case .prep: return []
        case .backlog: return []
        case .running: return []
        case .manualCheck: return [.manualCheck]
        case .ended: return [.report]
        case .done: return [.closeSignature, .report]
        }
    }

    static func isOpen(_ s: Section, stage: Stage, opened: Set<Section>) -> Bool {
        pinned.contains(s) || leads(for: stage).contains(s) || opened.contains(s)
    }

    /// The sections MORE hides at this stage, in the order's own order: the
    /// order minus the pinned, the leads and the MORE row itself.
    static func hidden(for stage: Stage) -> [Section] {
        order(for: stage).filter { $0 != .more && !isOpen($0, stage: stage, opened: []) }
    }

    /// Whether a section is on screen at all — as a body or as a row. The
    /// MORE row and everything the rule opens always are; the rest only once
    /// MORE has been pressed. MORE's own open state rides `opened` like any
    /// other section's, so a screen needs no second slot for it and the
    /// card-swap and stage-change resets already cover it.
    static func shown(_ s: Section, stage: Stage, opened: Set<Section>) -> Bool {
        s == .more || isOpen(s, stage: stage, opened: []) || opened.contains(.more)
    }

    /// One sentence saying where the card stands, always present on the
    /// card screen, in words a person can read without knowing a column
    /// name. The manual-check stage keeps the sentence the screens already
    /// draw, so `statusLine` stays the one wording of it. `linked` is
    /// whether a session is bound at all (`link_state` non-empty): an In
    /// progress card nobody is working — moved there with the launcher
    /// off, or sent Back and dropped in again — is `.running` by column,
    /// and saying an assistant is working on it would be a lie.
    static func stateWords(for stage: Stage, linked: Bool) -> String {
        switch stage {
        case .prep: return "Not planned yet"
        case .backlog: return "Planned — ready to start"
        case .running:
            return linked ? "An assistant is working on it"
                          : "In progress — nobody working on it yet"
        case .manualCheck: return statusLine
        case .ended: return "The run ended — not marked done yet"
        case .done: return "Done"
        }
    }

    /// The bound reading, for a caller with no link to speak of.
    static func stateWords(for stage: Stage) -> String {
        stateWords(for: stage, linked: true)
    }

    /// What the screen drawing the card can actually do from where it is —
    /// filled by each surface from the gates it already computes (Dark Army's
    /// launcher on, a session it can type at, the daemon honouring the
    /// verb), all `false` by default so an older Mac offers nothing it
    /// cannot do. Never the tool.
    struct Reach: Equatable {
        var canRefine = false
        var canStart = false
        var canReply = false
        var canMarkChecked = false
        var canMarkDone = false
        var canReview = false
    }

    /// The one verb a card leads with. The raw value is the button's label
    /// where the surface draws a plain one; `none` draws no band at all.
    enum NextAction: String {
        case refine = "Refine"
        case start = "START"
        case reply = "SEND A MESSAGE"
        case markChecked = "Mark checked"
        case done = "Done"
        case review = "Reviewed"
        case none = ""
    }

    /// The one next action from the stage and what the screen can reach: a
    /// Prep card leads with Refine (Start where Refine is not offered), a
    /// planned card with Start, a card being worked with the message box, a
    /// card waiting on a check with Mark checked (the message box where that
    /// verb is not honoured), an ended run with Done, a finished card with
    /// Reviewed. Every other verb sits under OTHER ACTIONS.
    static func nextAction(stage: Stage, reach: Reach) -> NextAction {
        switch stage {
        case .prep:
            if reach.canRefine { return .refine }
            return reach.canStart ? .start : .none
        case .backlog:
            return reach.canStart ? .start : .none
        case .running:
            return reach.canReply ? .reply : .none
        case .manualCheck:
            if reach.canMarkChecked { return .markChecked }
            return reach.canReply ? .reply : .none
        case .ended:
            return reach.canMarkDone ? .done : .none
        case .done:
            return reach.canReview ? .review : .none
        }
    }

    /// What the closed row may say about its contents, all of it already on
    /// the card. Every member defaults, so a surface fills what it knows.
    struct Facts: Equatable {
        var files = 0
        var filesTotal = 0
        var filesAvailable = true
        var hasReport = false
        var planAttached = false
        var planApproved = false
        var planMissing = false
        /// `true` once `report_path` is set on a scout; the REPORT row reads
        /// `attached`, and `still out` on a scout that has not come back.
        var reportAttached = false
        var isScout = false
        var messages = 0
        var attachments = 0
        var documents = 0
        var manualNote = false
        var crewStages = 0
        /// How many cards this one waits on, for `WAITS ON · 2`.
        var dependencies = 0
        /// How long the card has existed, already formatted (`"3d 4h"`), so
        /// the closed TIMELINE row reads `TIMELINE · 3d 4h`. `""` unknown.
        var age = ""
        /// How many sections the MORE row hides on this card, so the closed
        /// row reads `MORE · 9`. 0 draws the bare label.
        var hidden = 0
    }

    /// The closed-row tail, `""` where nothing cheap is known. The row draws
    /// `label` and then `" · " + summary` only when this is non-empty.
    static func summary(_ s: Section, facts: Facts) -> String {
        switch s {
        case .workRecord:
            if !facts.filesAvailable { return "folder unreadable" }
            return facts.filesTotal == 1 ? "1 file" : "\(facts.filesTotal) files"
        case .plan:
            if facts.planMissing { return "missing" }
            if facts.planApproved { return "approved" }
            return facts.planAttached ? "attached" : ""
        case .report:
            if facts.reportAttached { return "attached" }
            return facts.isScout ? "still out" : ""
        case .thread: return facts.messages > 0 ? "\(facts.messages)" : ""
        case .attachments: return facts.attachments > 0 ? "\(facts.attachments)" : ""
        case .documents: return facts.documents > 0 ? "\(facts.documents)" : ""
        case .crew: return facts.crewStages > 0 ? "\(facts.crewStages)" : ""
        case .dependencies:
            return facts.dependencies > 0 ? "\(facts.dependencies)" : ""
        case .manualCheck: return facts.manualNote ? "note" : ""
        case .timeline: return facts.age
        case .more: return facts.hidden > 0 ? "\(facts.hidden)" : ""
        default: return ""
        }
    }

    /// The whole closed-row text, in one place so both surfaces draw it the
    /// same: `"WHAT CHANGED · 4 files"`, or the bare label.
    static func rowText(_ s: Section, facts: Facts) -> String {
        let tail = summary(s, facts: facts)
        return tail.isEmpty ? s.rawValue : s.rawValue + " · " + tail
    }
}
