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
                              areas: [(name: String, concept: String)]) -> String {
        let thought = String(collapse(idea).unicodeScalars.prefix(maxIdeaInput))
        return modeHead
            + rosterBlock(roster)
            + foldersBlock(roots)
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
                cleanSpecialists(found["SPECIALISTS"] ?? "", roster: roster))
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
