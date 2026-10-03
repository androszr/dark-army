// --- rules begin
import Foundation

/// The Mac's `card_prepare.py` reading rules, ported function for function
/// so a card the phone prepares by itself is read exactly as one the Mac
/// prepared. Foundation only, nothing else from the target: the parity test
/// (`host/tests/test_phone_prepare_parity.py`) slices this file between its
/// two marker lines, builds it with `swiftc` and runs the same answer texts
/// through both readers, insisting on identical fields. A rule that drifts is
/// caught there, never on a phone.
///
/// Every sentence a person can read is the Mac's, verbatim.
enum CardPrepareRules {
    // The Mac's bounds, by name.
    static let maxIdeaInput = 2000
    static let maxProjectChoices = 40
    static let maxDependencyChoices = 40
    static let maxDependencyTitleChars = 120
    /// `board.MAX_BLOCKERS`: what survives the reader, as in the store.
    static let maxBlockers = 8
    static let maxTitleOut = 80
    static let maxTitleWords = 14
    static let maxSummaryChars = 2000
    static let maxPromptOut = 4000
    static let maxStages = 12
    static let maxStageChars = 48
    static let objectiveLimits: [(key: String, cap: Int, words: String)] = [
        ("beneficiary", 200, "who benefits"),
        ("intended_benefit", 1000, "intended benefit"),
        ("success_criterion", 1000, "success criterion"),
    ]
    static let objectiveLabels: [(label: String, key: String)] = [
        ("BENEFICIARY", "beneficiary"),
        ("BENEFIT", "intended_benefit"),
        ("CRITERION", "success_criterion"),
    ]
    static let listMarkers = ["- ", "* ", "1. "]
    static let ideaLabels = ["TITLE", "SUMMARY", "BENEFICIARY", "BENEFIT",
                             "CRITERION", "INSTRUCTIONS", "SPECIALISTS"]

    // --- the prompt -----------------------------------------------------

    /// `card_prepare.prompt_for` in idea mode, with a roster of bare names
    /// (the phone lists no descriptions) and the closed set of roots and
    /// areas handed in. Byte-equal to the Mac's string for the same inputs.
    static func promptForIdea(modeHead: String, idea: String, tool: String,
                              project: String, roster: [String],
                              roots: [String],
                              candidates: [String] = [],
                              areas: [(name: String, concept: String)]) -> String {
        let thought = String(collapse(idea).unicodeScalars.prefix(maxIdeaInput))
        return modeHead
            + rosterBlock(roster)
            + foldersBlock(roots)
            + dependenciesBlock(candidates)
            + "IDEA: \(thought)\n"
            + "ASSISTANT: \(tool)\n"
            + "PROJECT: \(project)\n"
            + areasBlock(areas)
    }

    /// Python's `" ".join(text.split())`.
    static func collapse(_ text: String) -> String {
        text.split(whereSeparator: { $0.isWhitespace || $0.isNewline })
            .joined(separator: " ")
    }

    static func rosterBlock(_ roster: [String]) -> String {
        if roster.isEmpty {
            return "There are no helpers available for this project. "
                + "Write NONE under SPECIALISTS.\n\n"
        }
        var lines = ["SPECIALISTS may name only these helpers, exactly as written, "
                     + "one per line, or NONE:"]
        for name in roster { lines.append("- \(name)") }
        return lines.joined(separator: "\n") + "\n\n"
    }

    static func foldersBlock(_ roots: [String]) -> String {
        let choices = roots.filter { !trim($0).isEmpty }
        if choices.count < 2 { return "" }
        var lines = ["After SPECIALISTS, add one final section:",
                     "FOLDER:",
                     "FOLDER names the project folder this work belongs in. It must "
                     + "be exactly one of the paths below, copied character for "
                     + "character on a single line, or NONE."]
        for root in choices.prefix(maxProjectChoices) { lines.append("- \(root)") }
        return lines.joined(separator: "\n") + "\n\n"
    }

    /// `card_prepare._dependencies_block`: the project's unfinished cards the
    /// idea may wait on, as titles; empty for none, so a prompt without
    /// candidates is byte-equal to the one before this section existed.
    static func dependenciesBlock(_ titles: [String]) -> String {
        let shown = titles.filter { !trim($0).isEmpty }
        if shown.isEmpty { return "" }
        var lines = ["After SPECIALISTS and FOLDER, and before AREA, add one section:",
                     "DEPENDS ON:",
                     "DEPENDS ON lists the cards below that this work must wait for, "
                     + "one title per line, copied character for character, or NONE."]
        for title in shown.prefix(maxDependencyChoices) { lines.append("- \(title)") }
        return lines.joined(separator: "\n") + "\n\n"
    }

    static func areasBlock(_ areas: [(name: String, concept: String)]) -> String {
        "\nAfter SPECIALISTS and FOLDER, add the final section AREA:\n"
            + "Choose exactly one area name below, or NONE.\n"
            + areas.map { "- \($0.name) — \($0.concept)" }.joined(separator: "\n") + "\n"
    }

    // --- the readers ----------------------------------------------------

    /// `(title, summary, prompt, stages)`; everything empty when the answer
    /// has no `INSTRUCTIONS:` section at all.
    static func parseIdea(_ raw: String, roster: [String])
        -> (title: String, summary: String, prompt: String, stages: [String]) {
        let text = stripFences(raw)
        guard text.contains("INSTRUCTIONS:") else { return ("", "", "", []) }
        let found = partitionIdea(text)
        guard let instructions = found["INSTRUCTIONS"] else { return ("", "", "", []) }
        return (cleanLine(found["TITLE"] ?? ""),
                cleanLine(found["SUMMARY"] ?? ""),
                cleanInstructions(instructions),
                cleanSpecialists(cutAtDependsOn(found["SPECIALISTS"] ?? ""), roster: roster))
    }

    /// The SPECIALISTS body up to the first `DEPENDS ON:` label: that section
    /// is asked for after it, so its lines land inside this body and a title
    /// starting with a roster name must not be read as a specialist.
    static func cutAtDependsOn(_ body: String) -> String {
        guard let hit = dependsLabelPattern?.firstMatch(
                in: body, options: [], range: NSRange(body.startIndex..., in: body)),
              let range = Range(hit.range, in: body) else { return body }
        return String(body[..<range.lowerBound])
    }

    /// The three objective lines keyed by card field; `NONE`, absent and
    /// empty all read as `""`.
    static func parseObjective(_ raw: String) -> [String: String] {
        let found = partitionIdea(stripFences(raw))
        var out: [String: String] = [:]
        for (label, key) in objectiveLabels {
            var line = cleanLine(found[label] ?? "")
            if line.uppercased() == "NONE" { line = "" }
            out[key] = line
        }
        return out
    }

    /// The last `FOLDER:` answer canonicalised to a member of `roots`, else
    /// `""`. Exact match first, then a basename unique across the list.
    static func parseFolder(_ raw: String, roots: [String]) -> String {
        let choices = roots.filter { !trim($0).isEmpty }
        guard !choices.isEmpty else { return "" }
        let text = stripFences(raw)
        guard let range = text.range(of: "FOLDER:", options: .backwards) else { return "" }
        var line = ""
        for candidate in splitLines(String(text[range.upperBound...])) {
            if !trim(candidate).isEmpty { line = trim(candidate); break }
        }
        line = dropListMarker(line)
        line = trim(unquote(line))
        if line.isEmpty || line.uppercased() == "NONE" { return "" }
        var byRoot: [String: String] = [:]
        for root in choices where byRoot[root.lowercased()] == nil {
            byRoot[root.lowercased()] = root
        }
        if let match = byRoot[line.lowercased()] { return match }
        var byBase: [String: [String]] = [:]
        for root in choices {
            let base = basename(root).lowercased()
            if !base.isEmpty { byBase[base, default: []].append(root) }
        }
        let hits = byBase[line.lowercased()] ?? []
        return hits.count == 1 ? hits[0] : ""
    }

    /// The last `AREA:` answer as a slug from the closed table, else `""`.
    /// `areas` is `(slug, name)`; the match is on either, case-insensitive.
    static func parseArea(_ raw: String, areas: [(slug: String, name: String)]) -> String {
        let text = stripFences(raw)
        guard let range = text.range(of: "AREA:", options: .backwards) else { return "" }
        let key = cleanLine(String(text[range.upperBound...])).lowercased()
        if key.isEmpty || key == "none" { return "" }
        for area in areas where key == area.slug || key == area.name.lowercased() {
            return area.slug
        }
        return ""
    }

    // --- the refusals ---------------------------------------------------

    static func titleRefusal(_ title: String) -> String? {
        let text = trim(title)
        if text.isEmpty {
            return "Dark Army did not write a title — press Prepare again, or type one"
        }
        if text.contains("\n") {
            return "Dark Army's title ran to more than one line — press Prepare again, "
                + "or type one"
        }
        if text.unicodeScalars.count > maxTitleOut {
            return "Dark Army's title was longer than \(maxTitleOut) characters — "
                + "that is a sentence rather than a name for the work"
        }
        if words(text).count > maxTitleWords {
            return "Dark Army's title ran to more than \(maxTitleWords) words — "
                + "that is a sentence rather than a name for the work"
        }
        return nil
    }

    static func summaryRefusal(_ summary: String) -> String? {
        let text = trim(summary)
        if text.isEmpty {
            return "Dark Army did not write a description — press Prepare again, or "
                + "type one"
        }
        if text.contains("\n") {
            return "Dark Army's description ran to more than one line — press Prepare "
                + "again, or type one"
        }
        if text.unicodeScalars.count > maxSummaryChars {
            return "Dark Army's description was longer than "
                + "\(maxSummaryChars) characters — not usable"
        }
        return nil
    }

    static func objectiveRefusal(_ fields: [String: String]) -> String? {
        for (key, cap, words) in objectiveLimits {
            let text = trim(fields[key] ?? "")
            if text.unicodeScalars.count > cap {
                return "Dark Army's \(words) was longer than \(cap) characters "
                    + "— not usable"
            }
        }
        return nil
    }

    /// Empty, over-long and a leading `-`. The per-tool subcommand rule is
    /// `dispatch.guard`'s at Start and is not repeated here.
    static func promptRefusal(_ prompt: String) -> String? {
        let text = trim(prompt)
        if text.isEmpty { return "Dark Army's answer was not usable" }
        if text.unicodeScalars.count > maxPromptOut {
            return "Dark Army's answer was longer than \(maxPromptOut) characters "
                + "— not usable as instructions"
        }
        if text.hasPrefix("-") {
            return "a prompt cannot start with '-' — the assistant's own command "
                + "line would read it as an option, not as instructions"
        }
        return nil
    }

    // --- DEPENDS ON ------------------------------------------------------

    /// Constant patterns, written to compile; a failure would leave the
    /// pattern nil and the rule it guards silent, which the parity test
    /// catches on the Mac, never on a phone.
    private static func regex(_ pattern: String) -> NSRegularExpression? {
        try? NSRegularExpression(pattern: pattern, options: [.anchorsMatchLines])
    }

    private static let sectionIdeaPattern = regex(
        "^[ \\t]*#{1,6}[ \\t]*[*_`]*(" + ideaLabels.joined(separator: "|")
        + ")[*_`]*:?[*_`]*[ \\t]*$"
        + "|[*_`]*(" + ideaLabels.joined(separator: "|")
        + ")[*_`]*:(?:[*_`]*(?=[ \\t]|$))?")
    private static let areaLabelPattern = regex(
        "^[ \\t]*#{1,6}[ \\t]*[*_`]*AREA[*_`]*:?[*_`]*[ \\t]*$"
        + "|[*_`]*AREA[*_`]*:(?:[*_`]*(?=[ \\t]|$))?")
    private static let dependsLabelPattern = regex(
        "^[ \\t]*#{1,6}[ \\t]*[*_`]*DEPENDS[ \\t]+ON[*_`]*:?[*_`]*[ \\t]*$"
        + "|[*_`]*DEPENDS[ \\t]+ON[*_`]*:(?:[*_`]*(?=[ \\t]|$))?")

    private static func matches(_ pattern: NSRegularExpression?, _ text: String) -> Bool {
        pattern?.firstMatch(in: text, options: [],
                            range: NSRange(text.startIndex..., in: text)) != nil
    }

    /// `card_prepare.candidate_ok`: a title shaped like a section label
    /// would cut a section when echoed into the answer, so it is not offered.
    static func candidateOk(_ title: String) -> Bool {
        let text = trim(title)
        if text.isEmpty { return false }
        return !(matches(sectionIdeaPattern, text) || matches(areaLabelPattern, text)
                 || matches(dependsLabelPattern, text) || text.contains("FOLDER:"))
    }

    /// The closed list the DEPENDS ON menu offers, `[(id, title)]`: the
    /// cards of `root` not in Done, titles collapsed and clipped before they
    /// are offered *and* matched, a label-shaped title or one shared by two
    /// cards left out, board order, at most forty. The Mac's daemon builds
    /// the same list in `_dependency_candidates`.
    static func candidates(from rows: [(id: String, title: String, root: String, column: String)],
                           root: String) -> [(id: String, title: String)] {
        var counts: [String: Int] = [:]
        var kept: [(id: String, title: String)] = []
        for row in rows where row.root == root && row.column != "done" {
            let title = String(collapse(row.title).unicodeScalars.prefix(maxDependencyTitleChars))
            if row.id.isEmpty || !candidateOk(title) { continue }
            counts[title.lowercased(), default: 0] += 1
            kept.append((row.id, title))
        }
        return Array(kept.filter { counts[$0.title.lowercased()] == 1 }
            .prefix(maxDependencyChoices))
    }

    /// `card_prepare.parse_dependencies`: the last `DEPENDS ON:` section's
    /// lines, each matched case-insensitively against an offered title or
    /// exactly against an offered id; only ids the caller supplied come back,
    /// in answer order, de-duplicated, at most eight.
    static func parseDependencies(_ raw: String,
                                  candidates: [(id: String, title: String)]) -> [String] {
        var byTitle: [String: [String]] = [:]
        var ids = Set<String>()
        for candidate in candidates {
            let key = String(collapse(candidate.title).unicodeScalars.prefix(maxDependencyTitleChars))
            if candidate.id.isEmpty || key.isEmpty { continue }
            ids.insert(candidate.id)
            byTitle[key.lowercased(), default: []].append(candidate.id)
        }
        if ids.isEmpty { return [] }
        let text = stripFences(raw)
        let whole = NSRange(text.startIndex..., in: text)
        guard let last = dependsLabelPattern?.matches(in: text, options: [], range: whole).last,
              let labelEnd = Range(last.range, in: text)?.upperBound else { return [] }
        var found: [String] = []
        for (position, line) in splitLines(String(text[labelEnd...])).enumerated() {
            if trim(line).isEmpty {
                if position == 0 { continue }
                break
            }
            if matches(sectionIdeaPattern, line) || matches(areaLabelPattern, line)
                || line.contains("FOLDER:") { break }
            var item = undecorate(trim(line))
            item = dropListMarker(item)
            item = undecorate(trim(unquote(item)))
            if item.isEmpty || item.uppercased() == "NONE" { continue }
            let hit: String
            if ids.contains(item) {
                hit = item
            } else {
                let hits = byTitle[collapse(item).lowercased()] ?? []
                if hits.count != 1 { continue }
                hit = hits[0]
            }
            if !found.contains(hit) { found.append(hit) }
        }
        return Array(found.prefix(maxBlockers))
    }

    /// `card_prepare._undecorate`: one heading marker, then every matching
    /// wrapper pair (`**Foo**`, `` `Foo` ``) off both ends; a pair whose
    /// inner text carries the token again is two spans and is left whole.
    static func undecorate(_ text: String) -> String {
        var line = trim(text)
        var hashes = 0
        for ch in line { if ch == "#" { hashes += 1 } else { break } }
        if (1...6).contains(hashes) {
            let after = line.dropFirst(hashes)
            if let first = after.first, first == " " || first == "\t" {
                line = trim(String(after))
            }
        }
        let wraps = ["**", "__", "`", "*", "_"]
        outer: while true {
            for wrap in wraps {
                let n = wrap.count
                if line.count > 2 * n && line.hasPrefix(wrap) && line.hasSuffix(wrap) {
                    let inner = String(line.dropFirst(n).dropLast(n))
                    if inner.contains(wrap) { continue }
                    line = trim(inner)
                    continue outer
                }
            }
            return line
        }
    }

    // --- the helpers ----------------------------------------------------

    static func stripFences(_ raw: String) -> String {
        let text = trim(raw)
        guard text.hasPrefix("```") else { return text }
        var lines = splitLines(text)
        if let first = lines.first, first.hasPrefix("```") { lines.removeFirst() }
        while let last = lines.last, trim(last).isEmpty { lines.removeLast() }
        if let last = lines.last, trim(last) == "```" { lines.removeLast() }
        return trim(lines.joined(separator: "\n"))
    }

    static func cleanInstructions(_ block: String) -> String {
        let text = stripFences(block)
        if text.isEmpty { return "" }
        var lines = splitLines(text)
        var first = String(lines[0].drop(while: { $0.isWhitespace }))
        for prefix in listMarkers where first.hasPrefix(prefix) {
            first = String(first.dropFirst(prefix.count))
            break
        }
        lines[0] = first
        return trim(lines.joined(separator: "\n"))
    }

    static func cleanSpecialists(_ block: String, roster: [String]) -> [String] {
        let text = stripFences(block)
        if text.isEmpty || trim(text).uppercased() == "NONE" { return [] }
        var byKey: [String: String] = [:]
        for name in roster where byKey[name.lowercased()] == nil {
            byKey[name.lowercased()] = name
        }
        var names: [String] = []
        for line in splitLines(text) {
            var item = trim(line)
            item = dropListMarker(item)
            if item.isEmpty || item.uppercased() == "NONE" { continue }
            item = dropTrailingParenthetical(item)
            var match = byKey[item.lowercased()]
            if match == nil, let token = words(item).first {
                match = byKey[token.lowercased()]
            }
            if let match { names.append(match) }
        }
        return parseStages(names)
    }

    /// `board.parse_stages`: clamp each name, dedupe, at most twelve.
    static func parseStages(_ names: [String]) -> [String] {
        var out: [String] = []
        for part in names {
            let name = String(trim(part).unicodeScalars.prefix(maxStageChars))
            if !name.isEmpty && !out.contains(name) { out.append(name) }
            if out.count >= maxStages { break }
        }
        return out
    }

    static func cleanLine(_ block: String) -> String {
        let text = stripFences(block)
        if text.isEmpty { return "" }
        var line = ""
        for candidate in splitLines(text) where !trim(candidate).isEmpty {
            line = trim(candidate)
            break
        }
        line = dropListMarker(line)
        return trim(unquote(line))
    }

    static func partitionIdea(_ text: String) -> [String: String] {
        var found: [String: String] = [:]
        let matches = labelMatches(text)
        for (i, match) in matches.enumerated() {
            let start = match.end
            let end = i + 1 < matches.count ? matches[i + 1].start : text.endIndex
            found[match.label] = String(text[start..<end])
        }
        return found
    }

    /// Every `<LABEL>:` occurrence, in order — the Mac's `_SECTION_IDEA`
    /// regex, walked by hand so the file needs nothing but Foundation's
    /// String.
    static func labelMatches(_ text: String)
        -> [(label: String, start: String.Index, end: String.Index)] {
        var out: [(String, String.Index, String.Index)] = []
        var cursor = text.startIndex
        while cursor < text.endIndex {
            var hit: (String, String.Index, String.Index)?
            for label in ideaLabels {
                let token = label + ":"
                if text[cursor...].hasPrefix(token) {
                    hit = (label, cursor, text.index(cursor, offsetBy: token.count))
                    break
                }
            }
            if let hit {
                out.append(hit)
                cursor = hit.2
            } else {
                cursor = text.index(after: cursor)
            }
        }
        return out
    }

    static func unquote(_ value: String) -> String {
        guard value.count >= 2, let first = value.first, let last = value.last,
              first == last, first == "\"" || first == "'" else { return value }
        return trim(String(value.dropFirst().dropLast()))
    }

    static func dropListMarker(_ item: String) -> String {
        for prefix in listMarkers where item.hasPrefix(prefix) {
            return trim(String(item.dropFirst(prefix.count)))
        }
        return item
    }

    /// `\s*\([^)]*\)\s*$` removed once: the leftmost `(` with no `)`
    /// between it and the closing one, exactly as the regex matches.
    static func dropTrailingParenthetical(_ item: String) -> String {
        let trimmed = trimTrailing(item)
        guard trimmed.hasSuffix(")") else { return item }
        let body = trimmed[..<trimmed.index(before: trimmed.endIndex)]
        let afterLastClose = body.lastIndex(of: ")").map { body.index(after: $0) }
            ?? body.startIndex
        guard let open = body[afterLastClose...].firstIndex(of: "(") else { return item }
        return trimTrailing(String(body[..<open]))
    }

    static func trimTrailing(_ text: String) -> String {
        var out = text
        while let last = out.last, last.isWhitespace { out.removeLast() }
        return out
    }

    static func basename(_ root: String) -> String {
        var path = root
        while path.hasSuffix("/") { path.removeLast() }
        if let slash = path.lastIndex(of: "/") {
            return String(path[path.index(after: slash)...])
        }
        return path
    }

    /// Python's `str.strip()`.
    static func trim(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Python's `str.split()` with no separator.
    static func words(_ text: String) -> [String] {
        text.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).map(String.init)
    }

    /// Python's `str.splitlines()` for the line ends a model writes.
    static func splitLines(_ text: String) -> [String] {
        let unified = text.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var lines = unified.components(separatedBy: "\n")
        if lines.last == "" && !unified.isEmpty { lines.removeLast() }
        if unified.isEmpty { return [] }
        return lines
    }
}
// --- rules end
