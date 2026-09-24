import Foundation

/// Same read contract on Mac and phone. Missing measurements remain missing.
struct OutcomeObjective: Decodable, Equatable {
    var beneficiary = ""
    var intendedBenefit = ""
    var successCriterion = ""
    var checkOn = ""
    enum CodingKeys: String, CodingKey {
        case beneficiary
        case intendedBenefit = "intended_benefit"
        case successCriterion = "success_criterion"
        case checkOn = "outcome_check_on"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        beneficiary = c.value(.beneficiary, "")
        intendedBenefit = c.value(.intendedBenefit, "")
        successCriterion = c.value(.successCriterion, "")
        checkOn = c.value(.checkOn, "")
    }
    var fields: [String: String] {
        ["beneficiary": beneficiary, "intended_benefit": intendedBenefit,
         "success_criterion": successCriterion, "outcome_check_on": checkOn]
    }
    func checkLabel(now: Date = Date()) -> String {
        guard !checkOn.isEmpty else { return "" }
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        let today = formatter.string(from: now)
        return "Later check: \(checkOn)" + (checkOn < today ? " · overdue" : checkOn == today ? " · due today" : "")
    }
}

struct OutcomeCost: Decodable {
    var totals: [String: Double] = [:]
    var coverage = "unknown"
    enum CodingKeys: String, CodingKey { case totals, coverage }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        totals = c.value(.totals, [:])
        coverage = c.value(.coverage, "unknown")
    }
    var label: String {
        guard !totals.isEmpty else { return "Cost unknown" }
        let amounts = totals.keys.sorted().map { "\($0) \(String(format: "%.2f", totals[$0]!))" }.joined(separator: " · ")
        return "Observed cost: \(amounts) · \(coverage == "complete" ? "complete" : "partial")"
    }
}

struct OutcomeCard: Decodable {
    var id = ""
    var title = ""
    var objective = OutcomeObjective()
    var revision = 0
    var accepted = false
    var trackingSince: Double?
    var reworkCount = 0
    var waitSeconds: Double?
    var waitCoverage = "unknown"
    var waitCauses: [String: Double] = [:]
    var cost = OutcomeCost()
    enum CodingKeys: String, CodingKey {
        case id = "card_id"
        case title, objective, revision, accepted, cost
        case trackingSince = "tracking_since"
        case reworkCount = "rework_count"
        case waitSeconds = "wait_seconds"
        case waitCoverage = "wait_coverage"
        case waitCauses = "wait_causes"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        title = c.value(.title, "")
        objective = c.value(.objective, OutcomeObjective())
        revision = c.value(.revision, 0)
        accepted = c.value(.accepted, false)
        trackingSince = c.maybe(.trackingSince)
        reworkCount = c.value(.reworkCount, 0)
        waitSeconds = c.maybe(.waitSeconds)
        waitCoverage = c.value(.waitCoverage, "unknown")
        waitCauses = c.value(.waitCauses, [:])
        cost = c.value(.cost, OutcomeCost())
    }
    var statusLabel: String { accepted ? "Accepted outcome" : "Not accepted" }
    var waitLabel: String {
        guard waitCoverage != "unknown", let seconds = waitSeconds else { return "Time awaiting a person: unknown" }
        return "Observed time awaiting a person: \(String(format: "%.1f", seconds / 60)) min · \(waitCoverage == "complete_since_tracking" ? "since tracking began" : "partial")"
    }
}

struct OutcomeEvent: Decodable, Identifiable {
    var id = 0
    var ts: Double = 0
    var kind = ""
    var actor = ""
    var evidence = ""
    var objective = OutcomeObjective()
    enum CodingKeys: String, CodingKey { case id, ts, kind, actor, evidence, objective }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, 0)
        ts = c.value(.ts, 0)
        kind = c.value(.kind, "")
        actor = c.value(.actor, "")
        evidence = c.value(.evidence, "")
        objective = c.value(.objective, OutcomeObjective())
    }
    var label: String {
        switch kind {
        case "accepted": return "Accepted by you"
        case "submitted": return "Submitted for acceptance"
        case "scope_changed": return "Objective changed · acceptance removed"
        case "reopened": return "Reopened · rework"
        case "revision_requested": return "Revision requested · rework"
        case "decision_revision", "revision_note": return "Revision requested"
        default: return "Outcome event"
        }
    }
}

struct OutcomeCoveredCost: Decodable {
    var perOutcome: Double?
    var covered = 0
    var outcomes = 0
    enum CodingKeys: String, CodingKey {
        case perOutcome = "per_accepted_outcome"
        case covered, outcomes
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        perOutcome = c.maybe(.perOutcome)
        covered = c.value(.covered, 0)
        outcomes = c.value(.outcomes, 0)
    }
}

struct OutcomeSummary: Decodable {
    var accepted = 0
    var awaiting = 0
    var submitted = 0
    var reworked = 0
    var carryIn = 0
    var reworkRate: Double?
    var cardHours: Double?
    var waitCoverage: [String: Int] = [:]
    var cost: [String: OutcomeCoveredCost] = [:]
    /// Every reading Dark Army watched, complete or not — the figure the summary
    /// actually shows. `cost` stays the narrower, more-trusted one.
    var observedCost: [String: OutcomeCoveredCost] = [:]
    var awaitingCost: [String: Double] = [:]
    var awaitingCovered = 0
    /// Dark Army's words for why there is no figure, and its caveat. Drawn
    /// verbatim; an older daemon sends neither and the view falls back.
    var costReason = ""
    var observedCostNote = ""
    var partialCost: [String: Double] = [:]
    enum CodingKeys: String, CodingKey {
        case accepted = "accepted_outcomes"
        case awaiting = "awaiting_acceptance"
        case submitted = "submitted_cards"
        case reworked = "reworked_cards"
        case carryIn = "carry_in_rework"
        case reworkRate = "rework_rate"
        case cardHours = "observed_card_hours"
        case waitCoverage = "wait_coverage"
        case cost = "cost_per_outcome"
        case observedCost = "observed_cost_per_outcome"
        case awaitingCost = "awaiting_cost_totals"
        case awaitingCovered = "awaiting_cost_covered"
        case costReason = "cost_reason"
        case observedCostNote = "observed_cost_note"
        case partialCost = "partial_cost_totals"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        accepted = c.value(.accepted, 0)
        awaiting = c.value(.awaiting, 0)
        submitted = c.value(.submitted, 0)
        reworked = c.value(.reworked, 0)
        carryIn = c.value(.carryIn, 0)
        reworkRate = c.maybe(.reworkRate)
        cardHours = c.maybe(.cardHours)
        waitCoverage = c.value(.waitCoverage, [:])
        cost = c.value(.cost, [:])
        observedCost = c.value(.observedCost, [:])
        awaitingCost = c.value(.awaitingCost, [:])
        awaitingCovered = c.value(.awaitingCovered, 0)
        costReason = c.value(.costReason, "")
        observedCostNote = c.value(.observedCostNote, "")
        partialCost = c.value(.partialCost, [:])
    }
}

struct OutcomeReport: Decodable {
    var supported = false
    var available = false
    var measurementsAvailable = false
    var root = ""
    var from: Double?
    var to: Double?
    var trackingSince: Double?
    var card: OutcomeCard?
    var events: [OutcomeEvent] = []
    var nextOffset: Int?
    var summary: OutcomeSummary?
    enum CodingKeys: String, CodingKey {
        case supported, available, root, from, to, card, events, summary
        case measurementsAvailable = "measurements_available"
        case trackingSince = "tracking_since"
        case nextOffset = "next_offset"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        measurementsAvailable = c.value(.measurementsAvailable, false)
        root = c.value(.root, "")
        from = c.maybe(.from)
        to = c.maybe(.to)
        trackingSince = c.maybe(.trackingSince)
        card = c.maybe(.card)
        events = c.value(.events, [])
        nextOffset = c.maybe(.nextOffset)
        summary = c.maybe(.summary)
    }
    func matches(cardId: String, revision: Int) -> Bool {
        card?.id == cardId && card?.revision == revision
    }
}

struct OutcomeActionReply: Decodable {
    var ok = false
    var detail = "The daemon did not answer."
    var revision: Int?
    /// The **card's** change number after the write — the store's own
    /// counter, not the outcome ring's. `nil` from a daemon older than the
    /// column, which is why the sheet re-seeds from the next snapshot too.
    var cardRevision: Int?
    /// What the store holds, reported inside `CARD_CHANGED_REFUSAL` alone.
    /// The revision is all the sheet needs: it offers **Save anyway** at
    /// that number rather than drawing the other copy, because on the Mac
    /// the other copy is one refresh away.
    var currentRevision: Int?

    /// The refusal a `board_update` is given when it was judged against a
    /// change number the store has already moved past. Matched on its
    /// opening words, `ActionResult.isCardChangedRefusal`'s rule.
    var isCardChangedRefusal: Bool {
        detail.hasPrefix("this card changed on the Mac")
    }

    enum CodingKeys: String, CodingKey {
        case ok, detail, current
        case revision = "outcome_revision"
        case cardRevision = "revision"
    }
    init() {}
    init(from decoder: Decoder) throws {
        try AreaWireFields.validate(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.value(.ok, false)
        detail = c.value(.detail, "The outcome could not be saved.")
        revision = c.maybe(.revision)
        cardRevision = c.maybe(.cardRevision)
        currentRevision = (try? c.decode(CurrentCard.self, forKey: .current))?.revision
    }

    /// Only the field the sheet acts on; every other key is ignored, which
    /// is what keeps a widened 409 body from breaking this decode.
    private struct CurrentCard: Decodable {
        var revision: Int?
        enum CodingKeys: String, CodingKey { case revision }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            revision = c.maybe(.revision)
        }
    }
}

/// Request ownership shared by both card readers. Revision prevents an older
/// outcome winning after a write; generation orders reads at the same revision.
struct OutcomeRequestState {
    struct Request {
        let generation: Int
        let cardId: String
        let revision: Int
        let offset: Int
    }
    private var generation = 0
    private var cardId = ""
    private var revision = 0
    private(set) var report: OutcomeReport?

    mutating func invalidate(cardId: String, revision: Int) {
        generation += 1
        if self.cardId != cardId { report = nil; self.revision = 0 }
        self.cardId = cardId
        self.revision = max(self.revision, revision)
    }

    mutating func begin(cardId: String, revision: Int, offset: Int = 0) -> Request {
        invalidate(cardId: cardId, revision: revision)
        let page = report?.card?.revision == self.revision ? offset : 0
        return Request(generation: generation, cardId: cardId, revision: self.revision, offset: page)
    }

    @discardableResult
    mutating func apply(_ fresh: OutcomeReport, request: Request,
                        currentCardId: String, currentRevision: Int) -> Bool {
        guard request.generation == generation, request.cardId == cardId,
              currentCardId == cardId, request.revision >= max(revision, currentRevision) else { return false }
        // Unavailability belongs to the request, not a nonexistent card in its
        // payload. Withdraw old evidence/measurements even on a page failure.
        guard fresh.available else { report = fresh; return true }
        if let card = fresh.card {
            guard card.id == cardId, card.revision >= request.revision else { return false }
            if request.offset > 0 {
                guard var prior = report, prior.available,
                      prior.card?.revision == card.revision else { return false }
                prior.events += fresh.events
                prior.nextOffset = fresh.nextOffset
                report = prior
            } else { report = fresh }
            revision = card.revision
        } else { report = fresh }
        return true
    }
}
