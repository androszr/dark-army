import Foundation

/// Tolerant lifecycle timing report. The wire owns the arithmetic.
struct LifecycleReport: Decodable {
    var supported = false
    var available = false
    var measurementsAvailable = false
    var reason = ""
    var schema = 0
    var from: Double = 0
    var to: Double = 0
    var asOf: Double = 0
    var generation = ""
    var root = ""
    var cardId = ""
    var title = ""
    var live = true
    var removedNote = ""
    var trackingSince: Double?
    var retentionDays = 0
    var quantileAlgorithm = ""
    var units = ""
    var overlapNote = ""
    var sort = ""
    var offset = 0
    var limit = 0
    var nextOffset: Int?
    var summaries: [String: LifecycleSummary] = [:]
    var cards: [LifecycleDelayedCard] = []
    var episodes: [LifecycleEpisode] = []
    var retention: [String: LifecycleRetention] = [:]

    enum CodingKeys: String, CodingKey {
        case supported, available, reason, schema, from, to, generation, root
        case title, live, units, sort, offset, limit, summaries, cards, episodes
        case retention
        case measurementsAvailable = "measurements_available"
        case asOf = "as_of"
        case cardId = "card_id"
        case removedNote = "removed_note"
        case trackingSince = "tracking_since"
        case retentionDays = "retention_days"
        case quantileAlgorithm = "quantile_algorithm"
        case overlapNote = "overlap_note"
        case nextOffset = "next_offset"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        measurementsAvailable = c.value(.measurementsAvailable, false)
        reason = c.value(.reason, "")
        schema = c.value(.schema, 0)
        from = c.value(.from, 0)
        to = c.value(.to, 0)
        asOf = c.value(.asOf, 0)
        generation = c.value(.generation, "")
        root = c.value(.root, "")
        cardId = c.value(.cardId, "")
        title = c.value(.title, "")
        live = c.value(.live, true)
        removedNote = c.value(.removedNote, "")
        trackingSince = c.maybe(.trackingSince)
        retentionDays = c.value(.retentionDays, 0)
        quantileAlgorithm = c.value(.quantileAlgorithm, "")
        units = c.value(.units, "")
        overlapNote = c.value(.overlapNote, "")
        sort = c.value(.sort, "")
        offset = c.value(.offset, 0)
        limit = c.value(.limit, 0)
        nextOffset = c.maybe(.nextOffset)
        summaries = c.value(.summaries, [:])
        cards = c.value(.cards, [])
        episodes = c.value(.episodes, [])
        retention = c.value(.retention, [:])
    }

    init() {}
}

struct LifecycleRetention: Decodable {
    var expiredCount = 0
    var expiredSeconds: Double = 0

    enum CodingKeys: String, CodingKey {
        case expiredCount = "expired_count"
        case expiredSeconds = "expired_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        expiredCount = c.value(.expiredCount, 0)
        expiredSeconds = c.value(.expiredSeconds, 0)
    }

    init() {}
}

struct LifecycleSummary: Decodable {
    var category = ""
    var observedSeconds: Double = 0
    var n = 0
    var N = 0
    var cards = 0
    var p50: Double?
    var p90: Double?
    var min: Double?
    var max: Double?
    var buckets: [Int] = []
    var open = 0
    var cancelled = 0
    var partial = 0
    var unknown = 0
    var leftCensored = 0
    var coverage: Double?
    var sampleCount = 0
    var units = ""
    var quantileAlgorithm = ""
    var causeSeconds: [String: Double] = [:]
    var carryIn = LifecycleCarryIn()

    enum CodingKeys: String, CodingKey {
        case category, n, cards, p50, p90, min, max, buckets, open, cancelled
        case partial, unknown, coverage, units
        case observedSeconds = "observed_seconds"
        case N
        case leftCensored = "left_censored"
        case sampleCount = "sample_count"
        case quantileAlgorithm = "quantile_algorithm"
        case causeSeconds = "cause_seconds"
        case carryIn = "carry_in"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        category = c.value(.category, "")
        observedSeconds = c.value(.observedSeconds, 0)
        n = c.value(.n, 0)
        N = c.value(.N, 0)
        cards = c.value(.cards, 0)
        p50 = c.maybe(.p50)
        p90 = c.maybe(.p90)
        min = c.maybe(.min)
        max = c.maybe(.max)
        buckets = c.value(.buckets, [])
        open = c.value(.open, 0)
        cancelled = c.value(.cancelled, 0)
        partial = c.value(.partial, 0)
        unknown = c.value(.unknown, 0)
        leftCensored = c.value(.leftCensored, 0)
        coverage = c.maybe(.coverage)
        sampleCount = c.value(.sampleCount, 0)
        units = c.value(.units, "")
        quantileAlgorithm = c.value(.quantileAlgorithm, "")
        causeSeconds = c.value(.causeSeconds, [:])
        carryIn = c.value(.carryIn, LifecycleCarryIn())
    }

    init() {}
}

struct LifecycleCarryIn: Decodable {
    var open = 0
    var completed = 0
    var cancelled = 0
    var partial = 0
    var observedSeconds: Double = 0

    enum CodingKeys: String, CodingKey {
        case open, completed, cancelled, partial
        case observedSeconds = "observed_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        open = c.value(.open, 0)
        completed = c.value(.completed, 0)
        cancelled = c.value(.cancelled, 0)
        partial = c.value(.partial, 0)
        observedSeconds = c.value(.observedSeconds, 0)
    }

    init() {}
}

struct LifecycleDelayedCard: Decodable, Identifiable {
    var id: String { cardId }
    var cardId = ""
    var title = ""
    var root = ""
    var live = true
    var observedSeconds: [String: Double] = [:]
    var openAge: Double?
    var periodObserved: Double = 0

    enum CodingKeys: String, CodingKey {
        case title, root, live
        case cardId = "card_id"
        case observedSeconds = "observed_seconds"
        case openAge = "open_age"
        case periodObserved = "period_observed"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cardId = c.value(.cardId, "")
        title = c.value(.title, "")
        root = c.value(.root, "")
        live = c.value(.live, true)
        observedSeconds = c.value(.observedSeconds, [:])
        openAge = c.maybe(.openAge)
        periodObserved = c.value(.periodObserved, 0)
    }
}

struct LifecycleEpisode: Decodable, Identifiable {
    var id: Int { episodeId }
    var episodeId = 0
    var kind = ""
    var cause = ""
    var disposition = ""
    var startedAt: Double?
    var endedAt: Double?
    var observedSeconds: Double = 0
    var elapsedSeconds: Double?
    var coverage = ""
    var gapCount = 0
    var gapReasons = ""
    var provenance = ""
    var attemptId: Int?
    var provider = ""
    var sessionId = ""
    var overlay = false

    enum CodingKeys: String, CodingKey {
        case kind, cause, disposition, coverage, provenance, provider, overlay
        case episodeId = "episode_id"
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case observedSeconds = "observed_seconds"
        case elapsedSeconds = "elapsed_seconds"
        case gapCount = "gap_count"
        case gapReasons = "gap_reasons"
        case attemptId = "attempt_id"
        case sessionId = "session_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        episodeId = c.value(.episodeId, 0)
        kind = c.value(.kind, "")
        cause = c.value(.cause, "")
        disposition = c.value(.disposition, "")
        startedAt = c.maybe(.startedAt)
        endedAt = c.maybe(.endedAt)
        observedSeconds = c.value(.observedSeconds, 0)
        elapsedSeconds = c.maybe(.elapsedSeconds)
        coverage = c.value(.coverage, "")
        gapCount = c.value(.gapCount, 0)
        gapReasons = c.value(.gapReasons, "")
        provenance = c.value(.provenance, "")
        attemptId = c.maybe(.attemptId)
        provider = c.value(.provider, "")
        sessionId = c.value(.sessionId, "")
        overlay = c.value(.overlay, false)
    }
}

enum LifecycleFormat {
    static let overlapNote = "Rework overlaps the other categories; do not add these rows."
    static let empty = "No observed episodes in this period"
    static let removed = "No longer on the board"
    static let categories = ["queue", "execution", "review", "rework"]
    static let labels = [
        "queue": "Queue wait",
        "execution": "Execution",
        "review": "Human review",
        "rework": "Rework",
    ]

    static func seconds(_ value: Double?) -> String {
        guard let value else { return "—" }
        if value == 0 { return "0s" }
        if value < 60 { return String(format: "%.0fs", value) }
        if value < 3600 { return String(format: "%.0fm", value / 60) }
        return String(format: "%.1fh", value / 3600)
    }

    static func nOfN(_ summary: LifecycleSummary) -> String {
        "\(summary.n) of \(summary.N) episodes"
    }

    static func coverage(_ summary: LifecycleSummary) -> String {
        if summary.N == 0 { return "coverage unavailable" }
        if summary.n == 0 { return "\(summary.n) of \(summary.N) complete; quantiles unavailable" }
        return nOfN(summary)
    }

    static func retentionLine(_ retention: [String: LifecycleRetention]) -> String {
        let n = retention.values.reduce(0) { $0 + $1.expiredCount }
        guard n > 0 else { return "" }
        return "Outside retention: \(n) expired records. Coverage is not complete."
    }

    /// A generation mismatch is not a loaded success — retry with a fresh token.
    static func shouldRetry(_ report: LifecycleReport) -> Bool {
        !report.available && report.reason.localizedCaseInsensitiveContains("refresh")
    }

    /// Interpret the picker's calendar day as UTC midnight; `to` is exclusive
    /// (the day after the selected end date) so a same-day range is 24 hours.
    static func utcPeriod(from: Date, to: Date, calendar: Calendar = .current) -> (Double, Double) {
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone.gmt
        func dayStart(_ date: Date) -> Date {
            let parts = calendar.dateComponents([.year, .month, .day], from: date)
            return utc.date(from: DateComponents(year: parts.year, month: parts.month, day: parts.day)) ?? date
        }
        let start = dayStart(from)
        let endDay = dayStart(to)
        let end = utc.date(byAdding: .day, value: 1, to: endDay) ?? endDay.addingTimeInterval(86400)
        let a = start.timeIntervalSince1970
        var b = end.timeIntervalSince1970
        if b <= a { b = a + 86400 }
        return (a, b)
    }
}

struct LifecycleRequest: Equatable {
    var root = ""
    var cardId = ""
    var from: Double = 0
    var to: Double = 0
    var sort = "queue"
    var generation = ""
    var offset = 0
    var serial = 0
}
