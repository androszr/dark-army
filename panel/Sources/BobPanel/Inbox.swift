import CryptoKit
import Foundation

/// One inbox: every decision waiting on a person, in one list, and nothing
/// that is merely there to be read.
///
/// This file is the whole judgment and it is deliberately view-free — a pure
/// function over what the snapshot already carries, called during `body`
/// evaluation exactly as `attentionRows` is. Nothing is stored in this file;
/// hide state arrives on the snapshot. This file still does not answer.
///
/// **The standing rule this file obeys:** a judgment goes into the daemon
/// exactly when it needs evidence the panel does not have on the wire.
/// `needs_you` and `manual_check_due` are that kind of judgment — they read
/// the hook-stream freshness only the daemon holds — so they are *consumed*
/// here and never re-derived. Everything else (a permission relayed out, a
/// question published on a row, a bucket the daemon already assigned) is
/// arrangement, and stays in Swift.
///
/// **Three kinds, named by what you do.** An entry is something to
/// *answer*, something to *look at*, or an agent that *stopped*; the raw
/// value is the rank. Every entry blocks somebody — a ready plan is the
/// Backlog tab's, a finished card awaiting review is the Done column's, a
/// burst of refused knocks is the access log's — so nothing here is
/// merely for your information, and there is no tag beside the edge.
enum InboxKind: Int, CaseIterable, Hashable {
    /// A tool call stopped at a yes-or-no, or an agent put a question to
    /// its human. The agent cannot go on until you say.
    case answer = 0
    /// A card that needs a person: its assistant has gone (the daemon's
    /// `needs_you`), or the session that did the work asked for a hand-check
    /// (the daemon's `manual_check_due`).
    case look
    /// An agent simply stopped and is waiting on somebody.
    case stopped

    /// The one word on the row, in place of a tag. Colour is never alone.
    var word: String {
        switch self {
        case .answer: return "ANSWER"
        case .look: return "LOOK AT"
        case .stopped: return "STOPPED"
        }
    }
}

/// The finer name an entry carries on the wire — what the daemon's
/// `inbox_ack` store and its fingerprint table are keyed on. Kept apart from
/// `InboxKind` so the three words a person reads and the five names a
/// store compares can move independently; the raw value ranks inside a
/// kind, permission before question.
enum InboxWireKind: Int, Hashable {
    case permission = 0
    case question
    case endedWork
    case manualCheck
    case waiting
    /// Mission Control asked for this card to be started. Last so every
    /// earlier raw value — and so every stored id — is unchanged; its kind
    /// is ANSWER, because the person is deciding yes or no.
    case startAsked

    var name: String {
        switch self {
        case .permission: return "permission"
        case .question: return "question"
        case .endedWork: return "ended_work"
        case .manualCheck: return "manual_check"
        case .waiting: return "waiting"
        case .startAsked: return "start_asked"
        }
    }

    var kind: InboxKind {
        switch self {
        case .permission, .question, .startAsked: return .answer
        case .endedWork, .manualCheck: return .look
        case .waiting: return .stopped
        }
    }

    /// Whether **Dismiss** may hide this entry until its subject changes.
    ///
    /// One verb with one meaning, and one exception: a permission ask still
    /// blocks its tool call, so a press that hid it would be a press that
    /// quietly did nothing. Everything else is honest to hide — the daemon's
    /// ack settles a session subject too, so the row goes quiet everywhere.
    var dismissable: Bool { self != .permission }
}

/// SHA-256 of UTF-8 material, kind-prefixed — `BoardCardSheet.digest`'s
/// algorithm. Waiting's fingerprint is the token `waiting`.
enum InboxFingerprint {
    static func value(kind: String, material: String) -> String {
        if kind == "waiting" { return "waiting" }
        // Lowercase hex by table, byte-identical to `String(format: "%02x")`
        // per byte: the formatter ran 32 times per entry, on every snapshot,
        // from `PanelView.body` — a measurable slice of each frame's
        // main-thread cost while the board was being scrolled.
        let hex = Array("0123456789abcdef".utf8)
        var digest: [UInt8] = []
        digest.reserveCapacity(64)
        for byte in SHA256.hash(data: Data(material.utf8)) {
            digest.append(hex[Int(byte >> 4)])
            digest.append(hex[Int(byte & 0x0f)])
        }
        return kind + ":" + String(decoding: digest, as: UTF8.self)
    }

    static func questionMaterial(_ questions: [AgentQuestion]) -> String {
        let nonempty = questions.filter { !$0.text.isEmpty }
        if !nonempty.isEmpty && nonempty.allSatisfy({ !$0.id.isEmpty }) {
            return nonempty.map(\.id).joined(separator: "|")
        }
        return nonempty.map(\.text).joined(separator: "|")
    }

    static func value(wire: InboxWireKind, questions: [AgentQuestion] = [],
                      card: BoardCard? = nil) -> String {
        let name = wire.name
        switch wire {
        case .waiting:
            return value(kind: name, material: "")
        case .question:
            return value(kind: name, material: questionMaterial(questions))
        case .manualCheck:
            return value(kind: name, material: card?.manualSteps ?? "")
        case .startAsked:
            return value(kind: name, material: card?.startAskId ?? "")
        case .permission, .endedWork:
            return value(kind: name, material: "")
        }
    }
}

/// What an entry's press aims at. Its own enum rather than a bare string so a
/// session id and a card id can never be confused for one another.
enum InboxTarget: Hashable {
    case session(String)
    case card(String)

    var key: String {
        switch self {
        case .session(let id): return "s:" + id
        case .card(let id): return "c:" + id
        }
    }
}

struct InboxItem: Identifiable, Hashable {
    let wire: InboxWireKind
    let project: String
    let title: String
    let detail: String
    let target: InboxTarget
    /// The card a refusal from a press would land on, `""` for a session
    /// entry. Not derived from `target`: a session entry has no card.
    let cardId: String
    /// The session this entry is about, `""` where there is none. Carried
    /// beside `target` rather than dug out of it, because a *card* entry can
    /// name a session too — the one that worked it — and that session is
    /// whose face the row draws.
    var sessionId: String = ""
    /// When this started waiting on a person, as a UTC epoch; `0` where
    /// nothing on the wire dates it. Never an age: an age computed once at
    /// decode freezes on a quiet minute, and the row's clock is a
    /// `TimelineView` over this stamp.
    var since: Double = 0

    var kind: InboxKind { wire.kind }
    var id: String { "\(target.key)#\(wire.rawValue)" }
}

struct InboxGroup: Identifiable, Hashable {
    let project: String
    let items: [InboxItem]

    var id: String { project }
    /// The heading, matching the project tabs' own word for a nameless one.
    var heading: String { project.isEmpty ? "Other" : project }
}

enum Inbox {
    /// The line under a start-asked entry, the same words on the phone.
    static let startAskedDetail =
        "Mission Control asks to start this. Open it and press START, or Dismiss."

    /// Every decision waiting, one per row and one per card.
    ///
    /// `rows` arrives already ranked (the `attentionRows` slice). `prompts` is
    /// the published permission list; `answered` are the ids this panel has
    /// already pressed, which is the same exclusion `prompt(for:)` applies —
    /// passing it keeps the pure function honest without teaching it about
    /// `RowActions`.
    ///
    /// **At most one item per row and one per card.** A thing that needs you
    /// twice is still one thing to go and look at, and emitting two would both
    /// inflate the count and draw the same subject in two places.
    /// `now` is the snapshot's own stamp (`generated_at`), used for one
    /// thing: turning a row's `idleSeconds` — the only "how long" the fleet
    /// publishes — into the absolute moment it started waiting. Zero leaves
    /// every session entry undated, which draws no clock rather than a
    /// wrong one.
    static func items(rows: [SectionRow],
                      prompts: [PermissionPrompt],
                      cards: [BoardCard],
                      answered: Set<String> = [],
                      now: Double = 0,
                      acks: [InboxAckRecord] = [],
                      fleet: Agents = Agents()) -> [InboxItem] {
        var out: [InboxItem] = []

        for row in rows {
            let agent = row.agent
            let name = agent.nickname.isEmpty
                ? (agent.name.isEmpty ? agent.sessionId : agent.name)
                : agent.nickname
            let target = InboxTarget.session(agent.id)
            // The moment this row went quiet: the fleet publishes how long
            // it has been idle, and that is the same clock for all three
            // session kinds — the ask, the question and the plain wait all
            // begin when the turn stopped.
            let waitingSince = now > 0 && agent.idleSeconds > 0
                ? now - agent.idleSeconds : 0
            if !agent.sessionId.isEmpty,
               let prompt = prompts.first(where: {
                   $0.sessionId == agent.sessionId && !answered.contains($0.requestId)
               }) {
                out.append(InboxItem(wire: .permission, project: agent.project,
                                     title: name, detail: prompt.summary,
                                     target: target, cardId: "",
                                     sessionId: agent.sessionId,
                                     since: waitingSince))
            } else if let question = agent.questionList.first(where: { !$0.text.isEmpty }) {
                let asked = question.header.isEmpty
                    ? question.text : "\(question.header): \(question.text)"
                out.append(InboxItem(wire: .question, project: agent.project,
                                     title: name, detail: asked,
                                     target: target, cardId: "",
                                     sessionId: agent.sessionId,
                                     since: waitingSince))
            } else {
                // What the agent last said in one line, where it left one;
                // its work report's headline (the daemon's line) next; the
                // tool it stopped on otherwise.
                let headline = agent.workReport?.headline ?? ""
                let detail = !agent.lastSummary.isEmpty ? agent.lastSummary
                    : !headline.isEmpty ? headline : agent.currentTool
                out.append(InboxItem(wire: .waiting, project: agent.project,
                                     title: name, detail: detail,
                                     target: target, cardId: "",
                                     sessionId: agent.sessionId,
                                     since: waitingSince))
            }
        }

        for card in cards {
            let target = InboxTarget.card(card.id)
            let name = card.title.isEmpty ? "Untitled card" : card.title
            // What a card's clock reads from: the moment the thing that
            // needs a person happened. A card the daemon dated with none
            // falls back to when it was written — never to `now`, which
            // would restart the clock on every frame.
            if card.needsYou {
                // What the ended session reported, where its row carries a
                // report (`fleet`, every bucket); the card's summary else.
                let ended = card.sessionId.isEmpty ? nil
                    : rows.first(where: { $0.agent.sessionId == card.sessionId })?.agent
                        ?? fleet.row(session: card.sessionId)?.0
                let reported = ended?.workReport?.headline ?? ""
                out.append(InboxItem(wire: .endedWork, project: card.project,
                                     title: name,
                                     detail: reported.isEmpty ? card.summary : reported,
                                     target: target, cardId: card.id,
                                     sessionId: card.sessionId,
                                     since: card.knownFinishedAt ?? card.createdAt))
            } else if card.manualCheckDue {
                out.append(InboxItem(wire: .manualCheck, project: card.project,
                                     title: name, detail: card.manualSteps,
                                     target: target, cardId: card.id,
                                     sessionId: card.sessionId,
                                     since: card.knownFinishedAt ?? card.createdAt))
            } else if !card.startAskId.isEmpty {
                // The daemon publishes the ask only while the card could
                // still take a Start; the entry opens the card, where START
                // is the yes and Dismiss here is the no.
                out.append(InboxItem(wire: .startAsked, project: card.project,
                                     title: name, detail: Inbox.startAskedDetail,
                                     target: target, cardId: card.id,
                                     sessionId: "",
                                     since: card.startAskedAt > 0
                                         ? card.startAskedAt : card.createdAt))
            }
        }

        if !acks.isEmpty {
            out.removeAll { item in
                let fp = InboxFingerprint.value(
                    wire: item.wire,
                    questions: questions(for: item, rows: rows),
                    // Only a card entry names a card. A session entry's
                    // `""` used to walk every card on the board (each a
                    // large value copied into the predicate) and find
                    // none — per entry, per call, per snapshot.
                    card: item.cardId.isEmpty
                        ? nil : cards.first { $0.id == item.cardId })
                return acks.contains {
                    $0.key == item.target.key
                        && $0.kind == item.wire.name
                        && $0.fp == fp
                }
            }
        }
        return oneEntryPerSubject(out.sorted(by: before))
    }

    /// **One entry per subject, where a card and a session are one subject.**
    /// A card bound to a waiting session used to be listed twice — the row's
    /// STOPPED and the card's LOOK AT — so "2 need you" named one agent. The
    /// list is already ranked, so the first entry that names a session wins
    /// and every later entry naming the same session is dropped: a
    /// permission or question on the session outranks the card's check, the
    /// card's check outranks the plain wait. Run **after** the dismissals,
    /// so dismissing the card's entry lets the session's own reappear.
    static func oneEntryPerSubject(_ ranked: [InboxItem]) -> [InboxItem] {
        var seen: Set<String> = []
        return ranked.filter { item in
            guard !item.sessionId.isEmpty else { return true }
            return seen.insert(item.sessionId).inserted
        }
    }

    private static func questions(for item: InboxItem, rows: [SectionRow]) -> [AgentQuestion] {
        guard case .session(let id) = item.target else { return [] }
        return rows.first { $0.agent.sessionId == id }?.agent.questionList ?? []
    }

    /// Kind, then the wire rank inside it, then title, then id — the raw
    /// values *are* the ranks, so ordering and the enums cannot disagree.
    private static func before(_ a: InboxItem, _ b: InboxItem) -> Bool {
        if a.kind != b.kind { return a.kind.rawValue < b.kind.rawValue }
        if a.wire != b.wire { return a.wire.rawValue < b.wire.rawValue }
        if a.title != b.title {
            return a.title.localizedStandardCompare(b.title) == .orderedAscending
        }
        return a.id < b.id
    }

    /// Group under project headings, in the project tabs' own order — reusing
    /// `projectNameOrder` rather than a second comparator is what keeps the
    /// two lists reading in the same sequence, with the nameless one last.
    /// A heading is drawn only where there is more than one group
    /// (`InboxView`); the grouping itself is the same either way.
    static func groups(_ items: [InboxItem]) -> [InboxGroup] {
        var byProject: [String: [InboxItem]] = [:]
        for item in items { byProject[item.project, default: []].append(item) }
        return byProject.keys.sorted(by: projectNameOrder).map { project in
            InboxGroup(project: project,
                       items: (byProject[project] ?? []).sorted(by: before))
        }
    }

    /// The entries **Dismiss all** would hide, in the order they are drawn.
    /// Pure, so the button's count and what the press actually dismisses
    /// are one answer.
    static func dismissable(_ items: [InboxItem]) -> [InboxItem] {
        items.filter { $0.wire.dismissable }
    }

    /// Dark Army's own words about an entry, or nothing.
    ///
    /// A refusal it just answered a press with wins, then a wait it has
    /// already explained — the preference order `queuedLine(autostart:)`
    /// obeys, for the same reason: the words on screen and the words the
    /// daemon used must be one string. There is no third, kind-written
    /// sentence: the word on the row and the detail beside it already say
    /// what this is, and a line that repeated them was one more line to
    /// read.
    static func sentence(for item: InboxItem,
                         refusal: String = "",
                         queueReason: String = "") -> String {
        let refused = refusal.trimmingCharacters(in: .whitespacesAndNewlines)
        if !refused.isEmpty { return refused }
        return queueReason.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// What VoiceOver reads for one entry — the row is one button, so its
    /// fragments are one sentence: the kind's word, the subject, how long it
    /// has waited, the detail and Dark Army's own line, in the order they are
    /// drawn. Composed from the drawn fields only, nothing off the snapshot.
    /// `waited` is the spoken age the row's clock draws (`FleetAge.spoken`),
    /// handed in so this file stays Foundation-only; empty for an undated
    /// entry, which says nothing. `sentence` is the line the row draws under
    /// the detail (`sentence(for:refusal:queueReason:)`), empty where it
    /// draws none.
    static func spokenLabel(for item: InboxItem, waited: String,
                            sentence: String = "") -> String {
        // The word is drawn in capitals; spoken, it is a word, not letters.
        var parts = [item.kind.word.lowercased(), item.title]
        if !waited.isEmpty { parts.append("waiting " + waited) }
        parts.append(item.detail)
        parts.append(sentence)
        return parts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: ", ")
    }
}
