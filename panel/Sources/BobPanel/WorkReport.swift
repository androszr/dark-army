import Foundation

/// A finished agent's `## Work done` report as its labelled parts — the one
/// wording and layout rule both clients draw it by.
///
/// **Byte-pinned with `ios/BobPhone/WorkReport.swift` from the
/// `enum WorkReport {` line down** (`host/tests/test_work_report_parse.py`),
/// the `RunFigures` / `CommRules` discipline: edit both copies together.
/// Only this header may differ. Foundation only, so the phone can carry it
/// unchanged.
///
/// The daemon parses the report once (`work_report.py`) and publishes the
/// parts beside `last_report` as `work_report`; neither client re-parses the
/// text. An older daemon sends no key and the report is drawn raw, as before.
enum WorkReport {
    /// The wire object, decoded tolerantly: every member through `value`, so
    /// `{}` decodes to an unlabelled report and one wrong-typed member never
    /// throws out of `Agent`.
    struct Parsed: Decodable, Equatable {
        var labelled = false
        var cut = false
        var asked = ""
        var changed: [String] = []
        var verified: [String] = []
        var unchecked: [String] = []
        var nothingUnchecked = false
        var whyNotAutomated = ""
        var card = ""
        var headline = ""
        /// The true lengths of the three lists: the daemon keeps at most
        /// twelve items of each, and says how many there were. Never smaller
        /// than the list; an older daemon sends none and the list's own
        /// length stands.
        var changedTotal = 0
        var verifiedTotal = 0
        var uncheckedTotal = 0

        enum CodingKeys: String, CodingKey {
            case labelled, cut, asked, changed, verified, unchecked, card, headline
            case nothingUnchecked = "nothing_unchecked"
            case whyNotAutomated = "why_not_automated"
            case changedTotal = "changed_total"
            case verifiedTotal = "verified_total"
            case uncheckedTotal = "unchecked_total"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            labelled = c.value(.labelled, false)
            cut = c.value(.cut, false)
            asked = c.value(.asked, "")
            changed = c.value(.changed, [])
            verified = c.value(.verified, [])
            unchecked = c.value(.unchecked, [])
            nothingUnchecked = c.value(.nothingUnchecked, false)
            whyNotAutomated = c.value(.whyNotAutomated, "")
            card = c.value(.card, "")
            headline = c.value(.headline, "")
            changedTotal = max(c.value(.changedTotal, 0), changed.count)
            verifiedTotal = max(c.value(.verifiedTotal, 0), verified.count)
            uncheckedTotal = max(c.value(.uncheckedTotal, 0), unchecked.count)
        }

        init(labelled: Bool = false, cut: Bool = false, asked: String = "",
             changed: [String] = [], verified: [String] = [],
             unchecked: [String] = [], nothingUnchecked: Bool = false,
             whyNotAutomated: String = "", card: String = "",
             headline: String = "", changedTotal: Int = 0,
             verifiedTotal: Int = 0, uncheckedTotal: Int = 0) {
            self.labelled = labelled
            self.cut = cut
            self.asked = asked
            self.changed = changed
            self.verified = verified
            self.unchecked = unchecked
            self.nothingUnchecked = nothingUnchecked
            self.whyNotAutomated = whyNotAutomated
            self.card = card
            self.headline = headline
            self.changedTotal = max(changedTotal, changed.count)
            self.verifiedTotal = max(verifiedTotal, verified.count)
            self.uncheckedTotal = max(uncheckedTotal, unchecked.count)
        }
    }

    /// The caption over the whole block, both clients' old word.
    static let heading = "# work done"

    /// The heading `WORK_REPORT_HINT` dictates and the daemon slices on.
    static let reportHeading = "## Work done"

    /// The parts in the order the hint dictates them.
    enum Section: CaseIterable {
        case asked, changed, verified, unchecked, card

        var caption: String {
            switch self {
            case .asked: return "ASKED"
            case .changed: return "CHANGED"
            case .verified: return "VERIFIED"
            case .unchecked: return "UNCHECKED"
            case .card: return "CARD"
            }
        }
    }

    /// Said under the heading of a report the daemon cut from the head: what
    /// is drawn is its end, not all of it.
    static let cutNote = "(report cut — start not kept)"

    /// Under a list the daemon clamped: how many more items there were, or
    /// "" when every one is drawn.
    static func more(shown: Int, total: Int) -> String {
        total > shown ? "+\(total - shown) more" : ""
    }

    /// A list part's true count (`*_total`), never smaller than what is drawn.
    static func total(_ parsed: Parsed, _ section: Section) -> Int {
        switch section {
        case .changed: return max(parsed.changedTotal, parsed.changed.count)
        case .verified: return max(parsed.verifiedTotal, parsed.verified.count)
        case .unchecked: return max(parsed.uncheckedTotal, parsed.unchecked.count)
        case .asked, .card: return 0
        }
    }

    /// What the Unchecked part says when the agent said every check ran.
    static let nothingUnchecked = "Nothing unchecked — every check ran"

    /// Unchecked's caption: the count of steps left for a person, or the bare
    /// word when there are none (the body then says so in words).
    static func uncheckedCaption(count: Int, nothing: Bool) -> String {
        if nothing || count <= 0 { return Section.unchecked.caption }
        return "\(Section.unchecked.caption) (\(count))"
    }

    /// Steps as a person follows them: `1. …`, `2. …`.
    static func numbered(_ steps: [String]) -> [String] {
        steps.enumerated().map { "\($0.offset + 1). \($0.element)" }
    }

    /// A row's command column with the headline at its front: `"<headline> ·
    /// <base>"`, the base alone where there is no headline, the headline
    /// alone where the base is the empty-column dash.
    static func rowLead(headline: String, base: String) -> String {
        let lead = headline.trimmingCharacters(in: .whitespacesAndNewlines)
        if lead.isEmpty { return base }
        if base == "—" || base.isEmpty { return lead }
        return "\(lead) · \(base)"
    }

    /// The parts this report has something to say under, in order. Unchecked
    /// is drawn whenever the report said anything about checks — steps, the
    /// reason, or that nothing was left.
    static func sections(_ parsed: Parsed) -> [Section] {
        Section.allCases.filter { section in
            switch section {
            case .asked: return !parsed.asked.isEmpty
            case .changed: return !parsed.changed.isEmpty
            case .verified: return !parsed.verified.isEmpty
            case .unchecked:
                return parsed.nothingUnchecked || !parsed.unchecked.isEmpty
                    || !parsed.whyNotAutomated.isEmpty
            case .card: return !parsed.card.isEmpty
            }
        }
    }

    /// The latest words with the report taken off their end, for a message
    /// that *is* the report: the labelled sections draw it, so the prose
    /// before the heading is all the message line keeps. Cut where the
    /// daemon slices (`session_stats._WORK_REPORT_RE`): the **last** line
    /// that is the heading whole — a passing mention of it in the prose, or
    /// a `### Work done` line, cuts nothing. A message without such a line
    /// comes back unchanged.
    static func prose(before latest: String) -> String {
        var cut: String.Index?
        latest.enumerateSubstrings(in: latest.startIndex..., options: .byLines) {
            line, range, _, _ in
            if let line, isReportHeading(line) { cut = range.lowerBound }
        }
        guard let cut else { return latest }
        return String(latest[..<cut]).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Whether one line is the report's heading as the daemon reads it:
    /// `##`, at least one space or tab, `Work done`, trailing blanks only.
    static func isReportHeading(_ line: String) -> Bool {
        guard line.hasPrefix("##") else { return false }
        let rest = line.dropFirst(2)
        guard let first = rest.first, first == " " || first == "\t" else { return false }
        return rest.trimmingCharacters(in: CharacterSet(charactersIn: " \t")) == "Work done"
    }
}
