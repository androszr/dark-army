import Foundation

struct DecisionItem: Decodable, Identifiable {
    var id = ""
    var cursor = 0
    var root = ""
    var project = ""
    var sessionId = ""
    var cardId = ""
    var sourceId = ""
    var kind = ""
    var title = ""
    var questionText = ""
    var questions: [AgentQuestion] = []
    var status = "ended_unknown"
    var outcome = ""
    var provenance = ""
    var openedAt: Double = 0
    var updatedAt: Double = 0
    var truncated = false
    var unresolved: Bool { status == "open" || status == "delivered_unconfirmed" }

    enum CodingKeys: String, CodingKey {
        case id, cursor, root, project, kind, title, questions, status, outcome, provenance, truncated
        case sessionId = "session_id", cardId = "card_id", sourceId = "source_id"
        case questionText = "question_text", openedAt = "opened_at", updatedAt = "updated_at"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, ""); cursor = c.value(.cursor, 0)
        root = c.value(.root, ""); project = c.value(.project, "")
        sessionId = c.value(.sessionId, ""); cardId = c.value(.cardId, "")
        sourceId = c.value(.sourceId, ""); kind = c.value(.kind, "")
        title = c.value(.title, ""); questionText = c.value(.questionText, "")
        questions = c.value(.questions, []); status = c.value(.status, "ended_unknown")
        outcome = c.value(.outcome, ""); provenance = c.value(.provenance, "")
        openedAt = c.value(.openedAt, 0); updatedAt = c.value(.updatedAt, 0)
        truncated = c.value(.truncated, false)
    }

    func liveAgent(in snapshot: Snapshot) -> Agent? {
        guard cardId.isEmpty, status == "open", !id.isEmpty else { return nil }
        let a = snapshot.agents
        guard let agent = (a.waiting + a.running + a.sleeping).first(where: { $0.sessionId == sessionId }) else { return nil }
        if kind == "permission" {
            return snapshot.permissions.contains(where: { $0.requestId == sourceId && $0.sessionId == sessionId }) ? agent : nil
        }
        return agent.decisionEpisodeId == id ? agent : nil
    }
}

struct CatchUpCoverage: Decodable {
    var observationStart: Double = 0
    var storageGap = false
    var retentionDays = 90
    var eventLimit = 20_000
    enum CodingKeys: String, CodingKey {
        case observationStart = "observation_start", storageGap = "storage_gap"
        case retentionDays = "retention_days", eventLimit = "event_limit"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        observationStart = c.value(.observationStart, 0); storageGap = c.value(.storageGap, false)
        retentionDays = c.value(.retentionDays, 90); eventLimit = c.value(.eventLimit, 20_000)
    }
}

struct CatchUpPage: Decodable {
    var available = false
    var items: [DecisionItem] = []
    var nextCursor: Int?
    var upperCursor = 0
    var oldestCursor = 0
    var historyGap = false
    var reason = ""
    var coverage = CatchUpCoverage()
    enum CodingKeys: String, CodingKey {
        case available, items, reason, coverage
        case nextCursor = "next_cursor", upperCursor = "upper_cursor"
        case oldestCursor = "oldest_cursor", historyGap = "history_gap"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false); items = c.value(.items, [])
        nextCursor = c.maybe(.nextCursor); upperCursor = c.value(.upperCursor, 0)
        oldestCursor = c.value(.oldestCursor, 0); historyGap = c.value(.historyGap, false)
        reason = c.value(.reason, ""); coverage = c.value(.coverage, CatchUpCoverage())
    }
}

struct CatchUpState {
    var items: [DecisionItem] = []
    var upperCursor: Int?
    var nextCursor: Int?
    var complete = false
    var subset = false
    mutating func append(_ page: CatchUpPage) {
        guard page.available, upperCursor == nil || upperCursor == page.upperCursor else { return }
        upperCursor = page.upperCursor; nextCursor = page.nextCursor
        var latest = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { a, b in a.cursor > b.cursor ? a : b })
        for item in page.items where item.cursor >= (latest[item.id]?.cursor ?? 0) { latest[item.id] = item }
        items = latest.values.sorted { a, b in
            a.unresolved != b.unresolved ? a.unresolved : a.cursor > b.cursor
        }
        complete = page.nextCursor == nil
    }
    func checkpoint(project: String, timeFilter: String) -> Int? {
        guard complete, !subset, project.isEmpty, timeFilter == "since" else { return nil }
        return upperCursor
    }
}

struct PendingReceipt: Codable, Equatable, Identifiable {
    var receiptId: String
    var sequence: Int
    var generation: String
    var id: Int { sequence }
}

struct DestinationState: Codable {
    var generation = ""
    var sequence = 0
    var pending: PendingReceipt?
    mutating func tap(_ receipt: String) {
        sequence += 1
        pending = PendingReceipt(receiptId: receipt, sequence: sequence, generation: generation)
    }
    mutating func pair(_ identity: String) {
        if !generation.isEmpty && generation != identity && pending?.generation.isEmpty != true { pending = nil }
        generation = identity
        if pending?.generation.isEmpty == true { pending?.generation = identity }
    }
    func accepts(_ route: PendingReceipt) -> Bool {
        pending == route && route.generation == generation && !generation.isEmpty
    }
    mutating func consume(_ route: PendingReceipt) {
        if accepts(route) { pending = nil }
    }
}

/// All history reads die with the unlocked client that started them. Kept
/// separate from polling so lock/re-pair also cancels view-initiated retries.
@MainActor
final class CatchUpRequests {
    private var generation = 0
    private var tasks: [UUID: Task<CatchUpPage?, Never>] = [:]

    func run(_ operation: @escaping @MainActor () async -> CatchUpPage?) async -> CatchUpPage? {
        let id = UUID()
        let started = generation
        let task = Task { await operation() }
        tasks[id] = task
        defer { tasks[id] = nil }
        let result = await withTaskCancellationHandler {
            await task.value
        } onCancel: { task.cancel() }
        guard !Task.isCancelled, started == generation else { return nil }
        return result
    }

    func cancel() {
        generation += 1
        for task in tasks.values { task.cancel() }
        tasks.removeAll()
    }
}
