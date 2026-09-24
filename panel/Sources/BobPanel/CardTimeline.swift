import Foundation

/// The card's timeline, as the daemon composes it (`card_timeline.py`) and
/// as both card screens draw it — the moments in the card's life in order,
/// the time between neighbours, and one line saying where the card is now.
///
/// **Words on the wire, drawn verbatim.** Every label, every gap word and
/// the open line's label come from the daemon; this file decides only how a
/// step becomes a row (`rows`), how seconds become `"2h 10m"` (`elapsed`,
/// pinned equal to the Python `elapsed_text` by one fixture table), and how
/// the open line is composed. No colour is a meaning: a `Row` carries words
/// only. Foundation only. Copied byte-equal into the phone — the three
/// structs into `ios/BobPhone/Models.swift` after `CardPlan` (`CardFull`
/// names them), the rule into `ios/BobPhone/CardDetailView.swift` after
/// `CardSections` — and pinned by `host/tests/test_card_timeline.py`; tabled in
/// `panel/Tests/BobPanelTests/CardTimelineTests.swift`.
///
/// Every field decodes through the tolerant helpers: `at`,
/// `since_previous_seconds`, `open` and `note` are legitimately absent, and
/// an unknown `kind` is kept and drawn by its `label`.
struct CardTimelineStep: Decodable, Identifiable {
    var kind = ""
    var label = ""
    var at: Double?
    var observed = false
    var note = ""
    var durable = true
    var sincePreviousSeconds: Double?
    var elapsed = ""
    var gap = ""

    var id: String {
        kind + "@" + (at.map { String($0) } ?? "none") + "/" + note
    }

    enum CodingKeys: String, CodingKey {
        case kind, label, at, observed, note, durable, elapsed, gap
        case sincePreviousSeconds = "since_previous_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, "")
        label = c.value(.label, "")
        at = c.maybe(.at)
        observed = c.value(.observed, at != nil)
        note = c.value(.note, "")
        durable = c.value(.durable, true)
        sincePreviousSeconds = c.maybe(.sincePreviousSeconds)
        elapsed = c.value(.elapsed, "")
        gap = c.value(.gap, "")
    }

    init(kind: String = "", label: String = "", at: Double? = nil,
         note: String = "", durable: Bool = true,
         sincePreviousSeconds: Double? = nil, gap: String = "") {
        self.kind = kind
        self.label = label
        self.at = at
        self.observed = at != nil
        self.note = note
        self.durable = durable
        self.sincePreviousSeconds = sincePreviousSeconds
        // The daemon's own figure; `CardTimeline.rows` derives the drawn
        // gap from `sincePreviousSeconds`, never from this string.
        self.elapsed = ""
        self.gap = gap
    }
}

/// Where the card is now. `since` nil means the moment it entered that
/// state was never witnessed; the line then reads "not observed".
struct CardTimelineOpen: Decodable, Equatable {
    var kind = ""
    var label = ""
    var since: Double?

    enum CodingKeys: String, CodingKey { case kind, label, since }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, "")
        label = c.value(.label, "")
        since = c.maybe(.since)
    }

    init(kind: String = "", label: String = "", since: Double? = nil) {
        self.kind = kind
        self.label = label
        self.since = since
    }
}

struct CardTimelineReport: Decodable {
    var available = false
    var reason = ""
    var generatedAt: Double = 0
    var trackingSince: Double?
    var leftCensored = false
    var recentOnlyNote = ""
    var truncated = false
    var steps: [CardTimelineStep] = []
    var open: CardTimelineOpen?

    enum CodingKeys: String, CodingKey {
        case available, reason, steps, open, truncated
        case generatedAt = "generated_at"
        case trackingSince = "tracking_since"
        case leftCensored = "left_censored"
        case recentOnlyNote = "recent_only_note"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        reason = c.value(.reason, "")
        generatedAt = c.value(.generatedAt, 0)
        trackingSince = c.maybe(.trackingSince)
        leftCensored = c.value(.leftCensored, false)
        recentOnlyNote = c.value(.recentOnlyNote, "")
        truncated = c.value(.truncated, false)
        steps = c.value(.steps, [])
        open = c.maybe(.open)
    }

    init(available: Bool = true, generatedAt: Double = 0,
         steps: [CardTimelineStep] = [], open: CardTimelineOpen? = nil,
         recentOnlyNote: String = "", reason: String = "") {
        self.available = available
        self.generatedAt = generatedAt
        self.steps = steps
        self.open = open
        self.recentOnlyNote = recentOnlyNote
        self.reason = reason
    }
}

enum CardTimeline {
    /// The words for a moment the daemon did not witness, and for the gap
    /// after it. The daemon's own constant, mirrored.
    static let notObserved = "not observed"
    /// The daemon's `RECENT_ONLY_NOTE`, mirrored for a client that wants to
    /// recognise it; drawn from the report verbatim, never from here.
    static let recentOnlyNote = "Permission asks older than a day are not kept."

    /// One drawn row. `when` is the moment as a short local date-time or
    /// `notObserved`; `gap` is the elapsed figure since the previous row,
    /// the daemon's gap words, or nil on the first row. Words only.
    struct Row: Identifiable, Equatable {
        let id: Int
        let label: String
        let note: String
        let when: String
        let gap: String?
        let durable: Bool
    }

    /// `"12s"`, `"5m"`, `"2h 10m"`, `"3d 4h"`; `""` for nil or a negative.
    /// Matches the daemon's `elapsed_text` exactly.
    static func elapsed(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "" }
        let total = Int(seconds)
        if total < 60 { return "\(total)s" }
        var minutes = total / 60
        if minutes < 60 { return "\(minutes)m" }
        var hours = minutes / 60
        minutes -= hours * 60
        if hours < 24 {
            return minutes > 0 ? "\(hours)h \(minutes)m" : "\(hours)h"
        }
        let days = hours / 24
        hours -= days * 24
        return hours > 0 ? "\(days)d \(hours)h" : "\(days)d"
    }

    /// A moment as a short local date-time, or `notObserved`.
    static func when(_ at: Double?) -> String {
        guard let at, at > 0 else { return notObserved }
        let formatter = DateFormatter()
        formatter.setLocalizedDateFormatFromTemplate("d MMM HH:mm")
        return formatter.string(from: Date(timeIntervalSince1970: at))
    }

    /// The report's steps as rows, in the daemon's order: the first row
    /// has no gap; a row whose `since_previous_seconds` is present draws
    /// its elapsed figure; otherwise the daemon's gap words, or nil where
    /// it named none.
    static func rows(_ report: CardTimelineReport) -> [Row] {
        var out: [Row] = []
        for (index, step) in report.steps.enumerated() {
            var gap: String? = nil
            if index > 0 {
                if let since = step.sincePreviousSeconds {
                    gap = elapsed(since)
                } else if !step.gap.isEmpty {
                    gap = step.gap
                }
            }
            out.append(Row(id: index,
                           label: step.label.isEmpty ? step.kind : step.label,
                           note: step.note,
                           when: step.observed ? when(step.at) : notObserved,
                           gap: gap,
                           durable: step.durable))
        }
        return out
    }

    /// `"Waiting for your review · 2d 4h"`, or the label with `notObserved`
    /// where the entry moment was never seen; nil where nothing is waiting.
    /// `now` is the report's own clock, so nothing here ticks.
    static func openLine(_ report: CardTimelineReport, now: Double) -> String? {
        guard let open = report.open, !open.label.isEmpty else { return nil }
        guard let since = open.since else {
            return open.label + " · " + notObserved
        }
        return open.label + " · " + elapsed(now - since)
    }
}
