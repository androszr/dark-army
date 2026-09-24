import SwiftUI

/// The phone's copy of the Mac panel's Markdown renderer.
///
/// A *copy*, deliberately, and the same arrangement `Theme.swift` and
/// `Cast.swift` already live under: the phone is an Xcode app target and the
/// panel is a SwiftPM module, there is no shared package, and coupling the two
/// builds to share one file was refused. What keeps the copies honest is
/// `host/tests/test_phone_theme_drift.py`, which pins everything from the line
/// `enum Markdown {` down as **byte-identical** to
/// `panel/Sources/BobPanel/Markdown.swift`. Only this header may differ, and
/// any change to the renderer must land on both sides in one commit.
///
/// The design rationale — a parser rather than a library, links stripped to
/// their labels, only `.md` rendered — is stated once, in the panel's own
/// header, and is not restated here. The consequence of the byte pin is that
/// the comments below speak in the Mac panel's terms ("the rail", "the stdout
/// pane"); rewriting them for the phone would break the pin that keeps the two
/// parsers one parser, so they are left exactly as they are.
enum Markdown {

    /// One parsed block. `id` is the index, assigned on the way out: a document
    /// is re-parsed whole whenever it changes, so there is no identity to
    /// preserve across edits and a positional id is honest about that.
    struct Block: Identifiable {
        enum Kind {
            case heading(level: Int)
            /// `indent` is the source's leading spaces, floored into the same
            /// depths a bullet uses: a paragraph written under a list item —
            /// blank line, then indented text — is part of that item, and
            /// flattening it to the margin is what makes a plan's "How I'll
            /// build it" read as twice as many top-level points as it has.
            case paragraph(indent: Int)
            /// `marker` is what to draw in the gutter — a bullet, or the
            /// original number, which is kept rather than recomputed so a list
            /// starting at 3 still starts at 3.
            case bullet(depth: Int, marker: String)
            case code
            case quote
            case rule
            /// A run of `|`-delimited rows. Drawn as a real grid — header
            /// row, alignment row dropped, each cell rendered inline — because
            /// verbatim rows on a phone-wide pane were a wall of pipes with
            /// the backticks still in them. `tableRows` does the split.
            case table
        }
        let id: Int
        let kind: Kind
        let text: String
    }

    // MARK: - Parsing

    private static let fence = "```"
    private static let commentOpen = "<!--"
    private static let commentClose = "-->"

    /// The document as blocks, in order.
    static func blocks(_ source: String) -> [Block] {
        var out: [(Block.Kind, String)] = []
        var paragraph: [String] = []
        var paragraphIndent = 0
        var quote: [String] = []
        var code: [String] = []
        var table: [String] = []
        /// An ASCII CRT / box drawing (`.----.` / `| … |` / `'----'` plus the
        /// stand). Held separately from `table` because the same `|` ink is
        /// a bezel here and a cell break in a markdown table; flushing it as
        /// `.code` keeps the drawing one block.
        var art: [String] = []
        /// The list item still open. Held rather than emitted, because a
        /// Markdown list item continues onto every following line indented
        /// under it (the hanging indent), and those lines are the second half
        /// of the sentence the marker started.
        var item: (depth: Int, marker: String, lines: [String])?
        var inCode = false
        var inComment = false

        func flushItem() {
            if let open = item {
                out.append((.bullet(depth: open.depth, marker: open.marker),
                            open.lines.joined(separator: " ")))
                item = nil
            }
        }
        func flushParagraph() {
            if !paragraph.isEmpty {
                out.append((.paragraph(indent: paragraphIndent),
                            paragraph.joined(separator: " ")))
                paragraph = []
                paragraphIndent = 0
            }
        }
        func flushQuote() {
            if !quote.isEmpty {
                out.append((.quote, quote.joined(separator: " ")))
                quote = []
            }
        }
        func flushTable() {
            if !table.isEmpty {
                out.append((.table, table.joined(separator: "\n")))
                table = []
            }
        }
        func flushArt() {
            if !art.isEmpty {
                out.append((.code, art.joined(separator: "\n")))
                art = []
            }
        }
        /// Everything that is not the block about to be opened.
        func flushProse() {
            flushItem()
            flushParagraph()
            flushQuote()
            flushTable()
            flushArt()
        }

        for rawLine in source.components(separatedBy: .newlines) {
            let line = rawLine.replacingOccurrences(of: "\t", with: "    ")
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let indent = line.prefix { $0 == " " }.count

            // A fence wins over everything, including a comment: inside a code
            // block `<!--` is text.
            if trimmed.hasPrefix(fence), !inComment {
                if inCode {
                    out.append((.code, code.joined(separator: "\n")))
                    code = []
                    inCode = false
                } else {
                    flushProse()
                    inCode = true
                }
                continue
            }
            if inCode { code.append(line); continue }

            // HTML comments are invisible in rendered Markdown, and that is the
            // whole point here — a plan's zone banners are comments, and they
            // are the noisiest thing in the file.
            if inComment {
                if let r = trimmed.range(of: commentClose) {
                    inComment = false
                    let tail = String(trimmed[r.upperBound...])
                        .trimmingCharacters(in: .whitespaces)
                    if !tail.isEmpty { paragraph.append(tail) }
                }
                continue
            }
            if trimmed.hasPrefix(commentOpen) {
                flushProse()
                if let r = trimmed.range(of: commentClose) {
                    let tail = String(trimmed[r.upperBound...])
                        .trimmingCharacters(in: .whitespaces)
                    if !tail.isEmpty { paragraph.append(tail) }
                } else {
                    inComment = true
                }
                continue
            }

            if trimmed.isEmpty { flushProse(); continue }

            if isRule(trimmed) {
                flushProse()
                out.append((.rule, ""))
                continue
            }

            if let heading = heading(trimmed) {
                flushProse()
                out.append((.heading(level: heading.0), heading.1))
                continue
            }

            // A CRT/box drawing is `|` ink, not a table. A top/bottom border
            // opens it; side and stand lines continue it, interior pipes
            // included. A markdown table never starts with `.----.`.
            if isBoxBorder(trimmed)
                || (!art.isEmpty && ((trimmed.hasPrefix("|") && trimmed.hasSuffix("|"))
                                     || isBoxStand(trimmed))) {
                flushItem()
                flushParagraph()
                flushQuote()
                flushTable()
                art.append(line)
                continue
            }
            flushArt()

            if trimmed.hasPrefix("|"), trimmed.hasSuffix("|") {
                flushItem()
                flushParagraph()
                flushQuote()
                table.append(trimmed)
                continue
            }
            flushTable()

            if trimmed.hasPrefix(">") {
                flushItem()
                flushParagraph()
                quote.append(String(trimmed.dropFirst())
                    .trimmingCharacters(in: .whitespaces))
                continue
            }
            flushQuote()

            if let parsed = bullet(line) {
                flushItem()
                flushParagraph()
                item = (parsed.depth, parsed.marker, [parsed.text])
                continue
            }

            // The hanging indent: a plain line under an open item continues it.
            if item != nil, indent >= 2 {
                item?.lines.append(trimmed)
                continue
            }
            flushItem()

            if paragraph.isEmpty { paragraphIndent = min(indent / 2, 3) }
            paragraph.append(trimmed)
        }

        if inCode, !code.isEmpty { out.append((.code, code.joined(separator: "\n"))) }
        flushProse()
        return out.enumerated().map { Block(id: $0.offset, kind: $0.element.0,
                                            text: $0.element.1) }
    }

    /// `###` through `######`, ATX only. Setext (`===` under a line) is not
    /// handled: it collides with the rule test and no plan in this repo uses it.
    private static func heading(_ trimmed: String) -> (Int, String)? {
        var level = 0
        for ch in trimmed {
            if ch == "#" { level += 1 } else { break }
        }
        guard level > 0, level <= 6 else { return nil }
        let rest = String(trimmed.dropFirst(level))
        // `#hashtag` is not a heading; the space is required.
        guard rest.isEmpty || rest.hasPrefix(" ") else { return nil }
        return (level, rest.trimmingCharacters(in: .whitespaces))
    }

    /// Three or more of `-`, `*` or `_`, and nothing else.
    private static func isRule(_ trimmed: String) -> Bool {
        for mark in ["-", "*", "_"] {
            let stripped = trimmed.replacingOccurrences(of: " ", with: "")
            if stripped.count >= 3,
               stripped.allSatisfy({ String($0) == mark }) { return true }
        }
        return false
    }

    /// Top or bottom of an ASCII CRT/box: `.----.`, `'----'`, `+----+`.
    /// A markdown rule is all dashes and is not this; a table row starts
    /// with `|`.
    private static func isBoxBorder(_ trimmed: String) -> Bool {
        guard trimmed.count >= 8,
              let first = trimmed.first, let last = trimmed.last else { return false }
        let corners: Set<Character> = [".", "'", "+"]
        guard corners.contains(first), corners.contains(last) else { return false }
        let inner = trimmed.dropFirst().dropLast()
        return inner.contains("-") && inner.allSatisfy({ $0 == "-" || $0 == " " })
    }

    /// The neck (`|||`) or plinth (`____|_|____` / `----_|_----`) under a
    /// CRT drawing. Only continues a screen already open. Both join spellings
    /// show up in banners: `|_|` (two pipes) and `_|_` (one).
    private static func isBoxStand(_ trimmed: String) -> Bool {
        if trimmed.isEmpty { return false }
        guard trimmed.contains("|") else { return false }
        return trimmed.allSatisfy({ $0 == "|" || $0 == "_" || $0 == "-" || $0 == " " })
    }

    private static func bullet(_ line: String)
        -> (depth: Int, marker: String, text: String)? {
        let indent = line.prefix { $0 == " " }.count
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        let depth = min(indent / 2, 3)
        for mark in ["- ", "* ", "+ "] {
            if trimmed.hasPrefix(mark) {
                return (depth, "•", String(trimmed.dropFirst(2)))
            }
        }
        // `12. ` / `3) ` — the number is kept verbatim.
        let digits = trimmed.prefix { $0.isNumber }
        if !digits.isEmpty, digits.count <= 3 {
            let after = trimmed.dropFirst(digits.count)
            if after.hasPrefix(". ") || after.hasPrefix(") ") {
                return (depth, digits + ".", String(after.dropFirst(2)))
            }
        }
        return nil
    }

    // MARK: - Inline

    /// The inline half, handed to the platform. Falls back to the plain string
    /// on anything it refuses — a document that will not parse must still be
    /// readable, which is the whole reason this pane exists.
    static func inline(_ text: String) -> AttributedString {
        guard var attributed = try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        else { return AttributedString(text) }
        // See the type comment: the label stays, the destination goes.
        for run in attributed.runs where run.link != nil {
            attributed[run.range].link = nil
            attributed[run.range].foregroundColor = .accentColor
        }
        // A code span in a pane that is already monospaced is invisible
        // without a tint: `HEAD` and HEAD drew the same. The platform keeps
        // the monospaced face; this adds the ground.
        for run in attributed.runs
        where run.inlinePresentationIntent?.contains(.code) == true {
            attributed[run.range].backgroundColor = Color.secondary.opacity(0.18)
        }
        return attributed
    }

    // MARK: - Tables

    /// A table block's rows as cells, header first. The `|---|:--:|`
    /// alignment row is syntax rather than content and is dropped; every
    /// cell is trimmed, so `| a | b |` and `|a|b|` are one row; and ragged
    /// rows are padded to the widest, because a `Grid` needs every row the
    /// same width. An escaped `\|` inside a cell is not handled.
    static func tableRows(_ text: String) -> [[String]] {
        var rows: [[String]] = []
        for line in text.components(separatedBy: "\n") {
            var body = Substring(line.trimmingCharacters(in: .whitespaces))
            if body.hasPrefix("|") { body = body.dropFirst() }
            if body.hasSuffix("|") { body = body.dropLast() }
            let cells = body.split(separator: "|", omittingEmptySubsequences: false)
                .map { $0.trimmingCharacters(in: .whitespaces) }
            if isAlignmentRow(cells) { continue }
            rows.append(cells)
        }
        let width = rows.map(\.count).max() ?? 0
        return rows.map { $0 + Array(repeating: "", count: width - $0.count) }
    }

    /// `---`, `:---`, `---:` or `:---:` in every cell, and at least one dash.
    private static func isAlignmentRow(_ cells: [String]) -> Bool {
        !cells.isEmpty && cells.allSatisfy { cell in
            cell.contains("-") && cell.allSatisfy { $0 == "-" || $0 == ":" }
        }
    }
}

/// A parsed Markdown document, drawn.
///
/// Sizes are relative to `base` (11pt, the reader's own size) so this can be
/// dropped into a denser or roomier pane later without a second table of
/// constants. Headings step down rather than shouting: an h1 at 16pt inside a
/// 240pt pane is already a third of a line of the visible document.
struct MarkdownText: View, Equatable {
    let source: String
    var base: CGFloat = 11

    /// Equatable on the three inputs, with `.equatable()` at every call site:
    /// `blocks` re-parses the whole document on each body pass, and the pane
    /// containing this redraws at snapshot cadence — the parse should run
    /// when the document changes, not when the fleet does.
    static func == (a: MarkdownText, b: MarkdownText) -> Bool {
        a.source == b.source && a.base == b.base && a.mono == b.mono
    }
    /// Draw the document in the rail's monospace face. The stdout pane is a
    /// terminal readout and a proportional paragraph in the middle of it reads
    /// as a different application; the block structure is the thing being
    /// rendered here, not the typeface.
    var mono: Bool = false

    private func face(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        mono ? Theme.mono(size, weight: weight)
             : .system(size: size, weight: weight)
    }

    private var blocks: [Markdown.Block] { Markdown.blocks(source) }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(blocks) { block in
                view(for: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func view(for block: Markdown.Block) -> some View {
        switch block.kind {
        case .heading(let level):
            Text(Markdown.inline(block.text))
                .font(face(headingSize(level), level <= 2 ? .bold : .semibold))
                .padding(.top, level <= 2 ? 4 : 1)
        case .paragraph(let indent):
            Text(Markdown.inline(block.text))
                .font(face(base))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, CGFloat(indent) * 14)
        case .bullet(let depth, let marker):
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text(marker)
                    .font(face(base))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                Text(Markdown.inline(block.text))
                    .font(face(base))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.leading, CGFloat(depth) * 14)
        case .code:
            // Horizontal scrolling rather than wrapping: a wrapped code line
            // reads as two statements, and the pane's width is not the
            // document's. Nested inside the reader's vertical scroll, which is
            // the one direction each of them owns. The Text must take its
            // longest line's width — without `.fixedSize` the ScrollView
            // proposes the pane and the drawing wraps, which is what turns
            // an ASCII CRT into a ragged pile of pipes on a phone.
            ScrollView(.horizontal, showsIndicators: false) {
                Text(block.text)
                    .font(mono ? Theme.mono(base - 0.5)
                               : .system(size: base - 0.5, design: .monospaced))
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(6)
            }
            .background(RoundedRectangle(cornerRadius: 4)
                .fill(Color.secondary.opacity(0.10)))
        case .table:
            // A grid, not a scroll: a cell wraps rather than the row running
            // off the pane, which is the trade a table can make and a code
            // line cannot. The header row is the first; the platform sizes
            // the columns.
            let rows = Markdown.tableRows(block.text)
            Grid(alignment: .topLeading, horizontalSpacing: 12, verticalSpacing: 5) {
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(Markdown.inline(cell))
                                .font(face(base - 0.5, index == 0 ? .semibold : .regular))
                                .fixedSize(horizontal: false, vertical: true)
                                .gridColumnAlignment(.leading)
                        }
                    }
                    if index == 0 {
                        Divider().gridCellUnsizedAxes(.horizontal)
                    }
                }
            }
            .padding(6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 4)
                .fill(Color.secondary.opacity(0.10)))
        case .quote:
            HStack(alignment: .top, spacing: 7) {
                RoundedRectangle(cornerRadius: 1)
                    .fill(Color.secondary.opacity(0.45))
                    .frame(width: 2)
                Text(Markdown.inline(block.text))
                    .font(face(base))
                    .foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .rule:
            Divider().padding(.vertical, 1)
        }
    }

    private func headingSize(_ level: Int) -> CGFloat {
        switch level {
        case 1: return base + 5
        case 2: return base + 3
        case 3: return base + 1.5
        default: return base + 0.5
        }
    }
}
