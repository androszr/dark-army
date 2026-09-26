import CryptoKit
import Foundation

/// Phone decision list: the Mac Inbox's kinds and predicates. Admission is
/// the Mac's too — a live row (`liveBuckets`) that is waiting, carries a
/// notification card or has an open prompt — minus the desk's attention
/// dwell, which needs a clock the phone does not keep.
///
/// **Three kinds, named by what you do**, the Mac's `InboxKind` rung for
/// rung: something to *answer*, something to *look at*, an agent that
/// *stopped*. Every entry blocks somebody — a ready plan is the Board's, a
/// closed card awaiting review is its Done column's, a burst alert is the
/// Profile screen's access log — so there is no FYI here and no tag.
///
/// Foundation only. Navigation to `PhoneSheet` lives in `PhoneInboxRoute`.
/// Catch up's `DecisionItem` is a historical episode and is not this type.
enum PhoneInboxKind: Int, CaseIterable, Hashable {
    case answer = 0
    case look
    case stopped

    /// The one word on the row, in place of a tag.
    var word: String {
        switch self {
        case .answer: return "ANSWER"
        case .look: return "LOOK AT"
        case .stopped: return "STOPPED"
        }
    }
}

/// The finer name an entry carries on the wire — the Mac's `InboxWireKind`:
/// what the daemon's `inbox_ack` store compares, and the rank inside a kind.
enum PhoneInboxWireKind: Int, Hashable {
    case permission = 0
    case question
    case endedWork
    case manualCheck
    case waiting
    /// Mission Control asked for this card to be started — the Mac's
    /// `InboxWireKind.startAsked`, last for the same reason.
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

    var kind: PhoneInboxKind {
        switch self {
        case .permission, .question, .startAsked: return .answer
        case .endedWork, .manualCheck: return .look
        case .waiting: return .stopped
        }
    }

    /// Whether **Dismiss** may hide this entry until it changes. One
    /// exception: a permission ask still blocks its tool call.
    var dismissable: Bool { self != .permission }
}

enum PhoneInboxTarget: Hashable {
    case session(String)
    case card(String)

    var key: String {
        switch self {
        case .session(let id): return "s:" + id
        case .card(let id): return "c:" + id
        }
    }
}

struct PhoneInboxItem: Identifiable, Hashable {
    let wire: PhoneInboxWireKind
    let project: String
    let title: String
    let detail: String
    let target: PhoneInboxTarget
    let cardId: String
    var sessionId: String = ""
    /// Fleet bucket for a session subject. Nil is an orphan permission:
    /// published prompt, no agent row, no navigation.
    var category: Category? = nil
    /// When this started waiting on a person, as a UTC epoch; `0` where
    /// nothing on the wire dates it (every card: the phone carries no
    /// finished clock). Read against a live clock, never an age.
    var since: Double = 0

    var kind: PhoneInboxKind { wire.kind }

    /// Stable subject identity, independent of the current reason.
    var id: String { target.key }
}

struct PhoneInboxGroup: Identifiable, Hashable {
    let project: String
    let items: [PhoneInboxItem]

    var id: String { project }
    var heading: String { project.isEmpty ? "Other" : project }
}

enum InboxFingerprint {
    static func value(kind: String, material: String) -> String {
        if kind == "waiting" { return "waiting" }
        let digest = SHA256.hash(data: Data(material.utf8))
            .map { String(format: "%02x", $0) }.joined()
        return kind + ":" + digest
    }

    static func questionMaterial(_ questions: [AgentQuestion]) -> String {
        let nonempty = questions.filter { !$0.text.isEmpty }
        if !nonempty.isEmpty && nonempty.allSatisfy({ !$0.id.isEmpty }) {
            return nonempty.map(\.id).joined(separator: "|")
        }
        return nonempty.map(\.text).joined(separator: "|")
    }

    static func value(wire: PhoneInboxWireKind, agent: Agent? = nil,
                      card: BoardCard? = nil) -> String {
        let name = wire.name
        switch wire {
        case .waiting:
            return value(kind: name, material: "")
        case .question:
            return value(kind: name,
                         material: questionMaterial(agent?.questionList ?? []))
        case .manualCheck:
            return value(kind: name, material: card?.manualSteps ?? "")
        case .startAsked:
            return value(kind: name, material: card?.startAskId ?? "")
        case .permission, .endedWork:
            return value(kind: name, material: "")
        }
    }
}

enum PhoneInbox {
    /// The Mac's `Inbox.startAskedDetail`, word for word.
    static let startAskedDetail =
        "Mission Control asks to start this. Open it and press START, or Dismiss."
    static let orphanUnavailable = "Session details are unavailable"

    /// Every live decision, one per session subject and one per card.
    static func items(from snapshot: Snapshot) -> [PhoneInboxItem] {
        var out: [PhoneInboxItem] = []
        out.append(contentsOf: sessionItems(from: snapshot))
        out.append(contentsOf: cardItems(from: snapshot))
        let acks = snapshot.inbox.available ? snapshot.inbox.acks : []
        if !acks.isEmpty {
            out.removeAll { item in
                let fp = fingerprint(item, snapshot: snapshot)
                return acks.contains {
                    $0.key == item.target.key
                        && $0.kind == item.wire.name
                        && $0.fp == fp
                }
            }
        }
        return oneEntryPerSubject(out.sorted(by: before))
    }

    /// **One entry per subject, where a card and a session are one subject** —
    /// the desktop's `Inbox.oneEntryPerSubject`, byte for byte in its rule: the
    /// list is ranked, the first entry naming a session wins, every later
    /// entry naming the same session is dropped, and it runs after the
    /// dismissals so dismissing the card's entry lets the session's reappear.
    static func oneEntryPerSubject(_ ranked: [PhoneInboxItem]) -> [PhoneInboxItem] {
        var seen: Set<String> = []
        return ranked.filter { item in
            guard !item.sessionId.isEmpty else { return true }
            return seen.insert(item.sessionId).inserted
        }
    }

    static func fingerprint(_ item: PhoneInboxItem, snapshot: Snapshot) -> String {
        InboxFingerprint.value(wire: item.wire,
                               agent: item.agent(in: snapshot),
                               card: item.card(in: snapshot))
    }

    /// Grouped under project headings in the desktop order; the view draws
    /// a heading only where there is more than one group.
    static func groups(_ items: [PhoneInboxItem]) -> [PhoneInboxGroup] {
        var byProject: [String: [PhoneInboxItem]] = [:]
        for item in items { byProject[item.project, default: []].append(item) }
        return byProject.keys.sorted(by: projectNameOrder).map { project in
            PhoneInboxGroup(project: project,
                            items: (byProject[project] ?? []).sorted(by: before))
        }
    }

    /// What **Dismiss all** covers — the Mac's `Inbox.dismissable`, rung
    /// for rung: every entry but a permission ask.
    static func dismissable(_ items: [PhoneInboxItem]) -> [PhoneInboxItem] {
        items.filter { $0.wire.dismissable }
    }

    /// Dark Army's words, or nothing: a refusal, then a queue reason. The one
    /// phone-written line is for an orphan permission — a published prompt
    /// with no agent row behind it — which has no screen to open.
    static func sentence(for item: PhoneInboxItem,
                         refusal: String = "",
                         queueReason: String = "") -> String {
        if item.wire == .permission, item.category == nil {
            return orphanUnavailable
        }
        let refused = refusal.trimmingCharacters(in: .whitespacesAndNewlines)
        if !refused.isEmpty { return refused }
        return queueReason.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// One spoken sentence: the kind's word, title, fleet bucket, project,
    /// detail, then Dark Army's line where it has one. The fleet bucket uses the
    /// same words `PhoneProcessRow` speaks (`stopped` for abandoned).
    static func spokenLabel(for item: PhoneInboxItem, card: BoardCard? = nil) -> String {
        let project = item.project.isEmpty ? "Other" : item.project
        var parts: [String] = [item.kind.word, item.title]
        if let bucket = spokenCategory(item.category) { parts.append(bucket) }
        parts.append(project)
        if !item.detail.isEmpty { parts.append(item.detail) }
        let line = sentence(for: item,
                            refusal: card?.dispatchError ?? "",
                            queueReason: card?.queueReason ?? "")
        if !line.isEmpty { parts.append(line) }
        return parts.joined(separator: ", ")
    }

    static func spokenCategory(_ category: Category?) -> String? {
        switch category {
        case .waiting: return "waiting"
        case .running: return "running"
        case .sleeping: return "sleeping"
        case .finished: return "finished"
        case .abandoned: return "stopped"
        case .none: return nil
        }
    }

    /// Same bucket order as `Agents.row(session:)`, with abandoned last.
    static func uniqueAgent(session id: String, agents: Agents) -> (Agent, Category)? {
        guard !id.isEmpty else { return nil }
        let buckets: [(Category, [Agent])] = [
            (.running, agents.running),
            (.waiting, agents.waiting),
            (.sleeping, agents.sleeping),
            (.finished, agents.finished),
            (.abandoned, agents.abandoned),
        ]
        for (category, rows) in buckets {
            if let hit = rows.first(where: { $0.sessionId == id }) {
                return (hit, category)
            }
        }
        return nil
    }

    /// Localized name, empty-name (`Other`) last — the desktop comparator.
    static func projectNameOrder(_ a: String, _ b: String) -> Bool {
        if a.isEmpty != b.isEmpty { return b.isEmpty }
        return a.localizedStandardCompare(b) == .orderedAscending
    }

    private static func before(_ a: PhoneInboxItem, _ b: PhoneInboxItem) -> Bool {
        if a.kind != b.kind { return a.kind.rawValue < b.kind.rawValue }
        if a.wire != b.wire { return a.wire.rawValue < b.wire.rawValue }
        if a.title != b.title {
            return a.title.localizedStandardCompare(b.title) == .orderedAscending
        }
        return a.target.key < b.target.key
    }

    /// The fleet buckets a session entry may come from — the Mac's
    /// `Category.live`, and the rule is the Mac's: a run that is over
    /// (*Recently finished*, a tombstone) or abandoned needs nobody, however
    /// its row still reads. Listing those here drew entries the Mac never
    /// had — a finished row still carrying its last card or question sat
    /// under Needs you on the phone after the desk had moved on.
    static let liveBuckets: [Category] = [.running, .waiting, .sleeping]

    private static func sessionItems(from snapshot: Snapshot) -> [PhoneInboxItem] {
        var seen = Set<String>()
        var unique: [(Agent, Category)] = []
        let buckets: [(Category, [Agent])] = [
            (.running, snapshot.agents.running),
            (.waiting, snapshot.agents.waiting),
            (.sleeping, snapshot.agents.sleeping),
            (.finished, snapshot.agents.finished),
            (.abandoned, snapshot.agents.abandoned),
        ]
        for (category, rows) in buckets {
            for agent in rows {
                let id = agent.sessionId
                if id.isEmpty || seen.contains(id) { continue }
                // Marked seen either way, so a prompt on a finished row is
                // not re-listed below as an orphan; it is simply not an entry.
                seen.insert(id)
                guard liveBuckets.contains(category) else { continue }
                unique.append((agent, category))
            }
        }

        let waitingIds = Set(
            snapshot.agents.waiting.map(\.sessionId).filter { !$0.isEmpty })
        let notifyIds = Set(
            snapshot.notifications.map(\.sessionId).filter { !$0.isEmpty })
        var promptsBySession: [String: [PermissionPrompt]] = [:]
        for prompt in snapshot.permissions {
            if prompt.sessionId.isEmpty { continue }
            promptsBySession[prompt.sessionId, default: []].append(prompt)
        }

        var out: [PhoneInboxItem] = []
        for (agent, category) in unique {
            let id = agent.sessionId
            let prompts = promptsBySession[id] ?? []
            let waiting = waitingIds.contains(id)
            let notified = notifyIds.contains(id)
            // The Mac's `needsHuman`: the waiting bucket, a notification
            // card or an open prompt admit a row. A question decides the
            // *kind* below and admits nothing by itself — a stale
            // `questions` list on a row that has gone quiet would otherwise
            // list a question the desk is not showing.
            if !waiting && prompts.isEmpty && !notified { continue }

            let wire: PhoneInboxWireKind
            let detail: String
            if let prompt = prompts.first {
                wire = .permission
                detail = prompt.detail.isEmpty ? prompt.toolName
                    : "\(prompt.toolName): \(prompt.detail)"
            } else if let question = agent.questionList.first(where: { !$0.text.isEmpty }) {
                wire = .question
                detail = question.header.isEmpty
                    ? question.text : "\(question.header): \(question.text)"
            } else {
                wire = .waiting
                // What the agent last said in one line, where it left one;
                // its work report's headline (the Mac's line) next; the tool
                // it stopped on otherwise — the Mac's rule.
                let headline = agent.workReport?.headline ?? ""
                detail = !agent.lastSummary.isEmpty ? agent.lastSummary
                    : !headline.isEmpty ? headline : agent.currentTool
            }
            let name = agent.nickname.isEmpty
                ? (agent.name.isEmpty ? agent.sessionId : agent.name)
                : agent.nickname
            // The moment the row went quiet: the daemon's own stamp on the
            // row (`quiet_since`, the number its Live Activity push also
            // carries), else — an older Mac — the frame's stamp minus the
            // row's idle figure, the Mac's `waitingSince`. Zero leaves it
            // undated, which draws no clock rather than a wrong one.
            let since: Double
            if agent.quietSince > 0 {
                since = agent.quietSince
            } else {
                since = snapshot.generatedAt > 0 && agent.idleSeconds > 0
                    ? snapshot.generatedAt - agent.idleSeconds : 0
            }
            out.append(PhoneInboxItem(
                wire: wire, project: agent.project, title: name, detail: detail,
                target: .session(id), cardId: "", sessionId: id,
                category: category, since: since))
        }

        for (id, prompts) in promptsBySession {
            if seen.contains(id) { continue }
            guard let prompt = prompts.first else { continue }
            let detail = prompt.detail.isEmpty ? prompt.toolName
                : "\(prompt.toolName): \(prompt.detail)"
            out.append(PhoneInboxItem(
                wire: .permission, project: prompt.projectHint,
                title: id, detail: detail,
                target: .session(id), cardId: "", sessionId: id,
                category: nil))
        }
        return out
    }

    /// The work-report headline on the row of `session`, in any bucket, or
    /// `""` — the Mac's line, read off the row and never re-derived.
    private static func reportedHeadline(session: String, in snapshot: Snapshot) -> String {
        guard !session.isEmpty else { return "" }
        let agents = snapshot.agents
        for bucket in [agents.running, agents.waiting, agents.sleeping,
                       agents.finished, agents.abandoned] {
            if let row = bucket.first(where: { $0.sessionId == session }) {
                return row.workReport?.headline ?? ""
            }
        }
        return ""
    }

    private static func cardItems(from snapshot: Snapshot) -> [PhoneInboxItem] {
        var seen = Set<String>()
        var out: [PhoneInboxItem] = []
        for card in snapshot.board.cards {
            if card.id.isEmpty || seen.contains(card.id) { continue }
            seen.insert(card.id)
            let wire: PhoneInboxWireKind
            let detail: String
            if card.needsYou {
                wire = .endedWork
                // What the ended session reported, where its row carries a
                // report; the card's summary else — the Mac's rule.
                let reported = reportedHeadline(session: card.sessionId, in: snapshot)
                detail = reported.isEmpty ? card.summary : reported
            } else if card.manualCheckDue {
                wire = .manualCheck
                detail = card.manualSteps
            } else if !card.startAskId.isEmpty {
                // Opens the card, where START is the yes; Dismiss is the no.
                wire = .startAsked
                detail = PhoneInbox.startAskedDetail
            } else {
                continue
            }
            let name = card.title.isEmpty ? "Untitled card" : card.title
            out.append(PhoneInboxItem(
                wire: wire, project: card.project, title: name, detail: detail,
                target: .card(card.id), cardId: card.id,
                sessionId: card.sessionId, category: nil))
        }
        return out
    }
}

extension PhoneInboxItem {
    func agent(in snapshot: Snapshot) -> Agent? {
        guard case .session(let id) = target else { return nil }
        return PhoneInbox.uniqueAgent(session: id, agents: snapshot.agents)?.0
    }

    func card(in snapshot: Snapshot) -> BoardCard? {
        guard case .card(let id) = target, !id.isEmpty else { return nil }
        return snapshot.board.cards.first { $0.id == id }
    }
}

private extension PermissionPrompt {
    /// Orphan permissions have no agent project; never invent one.
    var projectHint: String { "" }
}
