import Foundation

/// The Review section of `/api/state` (`BobDaemon.review_snapshot`) and the
/// offer `GET /api/review` answers. `MissionSection`'s shape: `available` is
/// stated by the daemon and decodes false when the section is absent (an
/// older daemon); every other key goes through the tolerant helpers, and an
/// array decodes element by element, so one malformed finding drops that
/// finding and never the frame. Never a handle, a digest or a folder path.
struct ReviewSection: Decodable, Equatable {
    var available = false
    var runs: [ReviewRun] = []

    enum CodingKeys: String, CodingKey { case available, runs }

    init(available: Bool = false, runs: [ReviewRun] = []) {
        self.available = available
        self.runs = runs
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        let rows: [ReviewLossy<ReviewRun>] = c.value(.runs, [])
        runs = rows.compactMap(\.value)
    }

    func run(id: String) -> ReviewRun? { runs.first { $0.id == id } }
}

struct ReviewRun: Decodable, Equatable, Identifiable {
    var id = ""
    var root = ""
    var project = ""
    var tool = ""
    var state = ""
    var scope = ""
    var scopeLine = ""
    var upstream = ""
    var sessionId = ""
    var startedAt: Double = 0
    var findingsAt: Double = 0
    var decidedAt: Double = 0
    var finishedAt: Double = 0
    var verdict = ""
    var error = ""
    var truncated = false
    var findings: [ReviewFinding] = []
    var picks: [Int] = []
    var steps: [ReviewStep] = []
    var ledger: [ReviewLedgerLine] = []

    enum CodingKeys: String, CodingKey {
        case id, root, project, tool, state, scope, upstream, verdict, error
        case truncated, findings, picks, steps, ledger
        case scopeLine = "scope_line"
        case sessionId = "session_id"
        case startedAt = "started_at"
        case findingsAt = "findings_at"
        case decidedAt = "decided_at"
        case finishedAt = "finished_at"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        root = c.value(.root, "")
        project = c.value(.project, "")
        tool = c.value(.tool, "")
        state = c.value(.state, "")
        scope = c.value(.scope, "")
        scopeLine = c.value(.scopeLine, "")
        upstream = c.value(.upstream, "")
        sessionId = c.value(.sessionId, "")
        startedAt = c.value(.startedAt, 0)
        findingsAt = c.value(.findingsAt, 0)
        decidedAt = c.value(.decidedAt, 0)
        finishedAt = c.value(.finishedAt, 0)
        verdict = c.value(.verdict, "")
        error = c.value(.error, "")
        truncated = c.value(.truncated, false)
        let found: [ReviewLossy<ReviewFinding>] = c.value(.findings, [])
        findings = found.compactMap(\.value)
        let picked: [ReviewLossy<Int>] = c.value(.picks, [])
        picks = picked.compactMap(\.value)
        let stepRows: [ReviewLossy<ReviewStep>] = c.value(.steps, [])
        steps = stepRows.compactMap(\.value)
        let lines: [ReviewLossy<ReviewLedgerLine>] = c.value(.ledger, [])
        ledger = lines.compactMap(\.value)
    }
}

struct ReviewFinding: Decodable, Equatable, Identifiable {
    var index = 0
    var grade = ""
    var line = ""
    var `where` = ""
    var fix = ""
    var confidence = ""

    var id: Int { index }

    enum CodingKeys: String, CodingKey {
        case index, grade, line, `where`, fix, confidence
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        index = c.value(.index, 0)
        grade = c.value(.grade, "")
        line = c.value(.line, "")
        `where` = c.value(.where, "")
        fix = c.value(.fix, "")
        confidence = c.value(.confidence, "")
    }
}

struct ReviewStep: Decodable, Equatable, Identifiable {
    var id = ""
    var label = ""
    var status = ""
    var words = ""
    /// Only on an offer: how the step is done. A run's snapshot omits it.
    var how = ""

    enum CodingKeys: String, CodingKey { case id, label, status, words, how }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        label = c.value(.label, "")
        status = c.value(.status, "")
        words = c.value(.words, "")
        how = c.value(.how, "")
    }
}

struct ReviewLedgerLine: Decodable, Equatable, Identifiable {
    var id = ""
    var status = ""
    var words = ""

    enum CodingKeys: String, CodingKey { case id, status, words }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        status = c.value(.status, "")
        words = c.value(.words, "")
    }
}

/// What a review of one project would cover and what it may do afterwards
/// (`BobDaemon.review_offer`): the scope sentence in the daemon's own words
/// and the after-steps this project offers.
struct ReviewOffer: Decodable, Equatable {
    var available = false
    var root = ""
    var project = ""
    var scope = ""
    var scopeLine = ""
    var upstream = ""
    var reason = ""
    var steps: [ReviewStep] = []

    enum CodingKeys: String, CodingKey {
        case available, root, project, scope, upstream, reason, steps
        case scopeLine = "scope_line"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        root = c.value(.root, "")
        project = c.value(.project, "")
        scope = c.value(.scope, "")
        scopeLine = c.value(.scopeLine, "")
        upstream = c.value(.upstream, "")
        reason = c.value(.reason, "")
        let rows: [ReviewLossy<ReviewStep>] = c.value(.steps, [])
        steps = rows.compactMap(\.value)
    }
}

/// One ragged element: a malformed one decodes to nil and is dropped.
struct ReviewLossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}
