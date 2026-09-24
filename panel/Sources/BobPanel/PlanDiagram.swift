import SwiftUI

/// The shape of a plan, parsed out of its already-read text: the stages its
/// metadata header declares, and the files its `## Files to change` table
/// names.
///
/// A parser struct beside the view rather than inside it, with the string
/// functions `internal`, so the interesting part — which lines count and which
/// are prose — is testable without drawing anything. The line shapes are the
/// ones the daemon already committed to: `board_workflow._STAGES_LINE` for the
/// header (`- **Stages:** a | b | c`, metadata only, never prose that happens
/// to contain the phrase) and the plan template's Markdown table for the
/// files. Either half absent means that half is **omitted, not faked**: a plan
/// that declares no stages draws no hollow tiles — `agent_trail`'s rule,
/// applied to a picture.
struct PlanStructure: Equatable {
    var stages: [String] = []
    var files: [String] = []

    static func parse(_ source: String) -> PlanStructure {
        PlanStructure(stages: parseStages(source), files: parseFiles(source))
    }

    /// The `- **Stages:**` metadata line, split on `|`. Only lines above the
    /// first `## ` heading count — `board_workflow._parse_header_stages`'
    /// rule, without which any prose mentioning "**Stages:**" becomes a
    /// diagram.
    static func parseStages(_ source: String) -> [String] {
        for line in source.split(separator: "\n", omittingEmptySubsequences: false) {
            let text = line.trimmingCharacters(in: .whitespaces)
            if text.hasPrefix("## ") { break }
            guard let names = stagesLine(text) else { continue }
            return names
        }
        return []
    }

    /// One header line's stage names, or nil when the line is not the header.
    static func stagesLine(_ line: String) -> [String]? {
        var text = line
        if text.hasPrefix("-") {
            text = String(text.dropFirst()).trimmingCharacters(in: .whitespaces)
        }
        let marker = "**Stages:**"
        guard text.hasPrefix(marker) else { return nil }
        let value = String(text.dropFirst(marker.count))
        let names = value.split(separator: "|")
            .map {
                $0.trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "`"))
            }
            .filter { !$0.isEmpty }
        return names
    }

    /// First cells of the `## Files to change` table, from the heading to the
    /// next `## `. The header row and the `---` separator row are dropped;
    /// backticks are stripped from the cell, which is how the template writes
    /// paths.
    static func parseFiles(_ source: String) -> [String] {
        var inSection = false
        var out: [String] = []
        for line in source.split(separator: "\n", omittingEmptySubsequences: false) {
            let text = line.trimmingCharacters(in: .whitespaces)
            if text.hasPrefix("## ") {
                if inSection { break }
                inSection = text.lowercased().hasPrefix("## files to change")
                continue
            }
            guard inSection, text.hasPrefix("|") else { continue }
            guard let cell = firstCell(text) else { continue }
            out.append(cell)
            if out.count >= 24 { break }
        }
        return out
    }

    /// A table row's first cell, or nil for the header and separator rows.
    static func firstCell(_ row: String) -> String? {
        let cells = row.split(separator: "|")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard let first = cells.first else { return nil }
        // The separator row is dashes and colons; the header row is the
        // template's own literal label. Neither is a file.
        let separatorish = CharacterSet(charactersIn: "-: ")
        if first.unicodeScalars.allSatisfy({ separatorish.contains($0) }) {
            return nil
        }
        if first.caseInsensitiveCompare("File") == .orderedSame { return nil }
        return first.trimmingCharacters(in: CharacterSet(charactersIn: "`"))
    }

    /// The SF Symbol a file row wears, by extension.
    static func icon(for path: String) -> String {
        switch (path as NSString).pathExtension.lowercased() {
        case "md", "markdown": return "doc.richtext"
        case "swift", "py", "js", "ts", "sh": return "doc.text"
        default: return "doc"
        }
    }
}

/// Where a plan's plain half ends. The `/ship` template writes one H2 between
/// the person's zone (header list, what / how / how-you-know) and the
/// implementer's zone; the card window and the phone fold everything from
/// that line down behind a chevron. A plan with no such heading — one written
/// before the template carried it — is shown whole. Lossless: `summary +
/// detail == source` whenever `detail` is non-nil. Byte-identical on the
/// phone (`ios/BobPhone/CardDetailView.swift`), pinned by
/// `host/tests/test_phone_plan_fold.py`.
enum PlanSplit {
    static let marker = "## Technical detail"
    static func split(_ source: String) -> (summary: String, detail: String?) {
        let lines = source.split(separator: "\n", omittingEmptySubsequences: false)
        guard let at = lines.firstIndex(where: {
            $0.trimmingCharacters(in: .whitespaces) == marker
        }) else { return (source, nil) }
        let summary = lines[..<at].joined(separator: "\n") + "\n"
        let detail = lines[at...].joined(separator: "\n")
        return (summary, detail)
    }
}

/// The picture above the rendered plan in the card sheet: stages as bordered
/// tiles joined by arrows, files as an iconed list. Pure view — everything it
/// knows arrives as the already-read plan text.
struct PlanDiagram: View {
    let source: String

    var body: some View {
        let structure = PlanStructure.parse(source)
        if structure.stages.isEmpty && structure.files.isEmpty {
            EmptyView()
        } else {
            VStack(alignment: .leading, spacing: 8) {
                if !structure.stages.isEmpty {
                    stageTrack(structure.stages)
                }
                if !structure.files.isEmpty {
                    fileList(structure.files)
                }
            }
        }
    }

    /// The stages, left to right, wrapping when the sheet is narrower than
    /// the chain. `WrapLayout` below rather than an HStack in a scroll view:
    /// a diagram that has to be scrolled to be seen is not a diagram.
    private func stageTrack(_ stages: [String]) -> some View {
        WrapLayout(spacing: 4, lineSpacing: 6) {
            ForEach(Array(stages.enumerated()), id: \.offset) { index, stage in
                if index > 0 {
                    Image(systemName: "arrow.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(Theme.faint)
                }
                Text(stage)
                    .font(Theme.mono(9, weight: .medium))
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
            }
        }
    }

    private func fileList(_ files: [String]) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(files, id: \.self) { path in
                HStack(spacing: 5) {
                    Image(systemName: PlanStructure.icon(for: path))
                        .font(.system(size: 9))
                        .foregroundStyle(Theme.faint)
                    Text(path)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.dim)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
        }
    }
}

/// A minimal flowing layout: children left to right, wrapping to a new line
/// when the proposed width runs out. Enough for a dozen small tiles; not a
/// general-purpose flow.
struct WrapLayout: Layout {
    var spacing: CGFloat = 4
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var lineHeight: CGFloat = 0
        var maxX: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
            maxX = max(maxX, x - spacing)
        }
        return CGSize(width: width.isFinite ? width : maxX,
                      height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        // Two passes: break into lines first so each view can be centred
        // against its own line's height — an arrow glyph beside a taller
        // bordered tile should sit on its midline, not its cap height.
        var lines: [[(LayoutSubviews.Element, CGSize)]] = [[]]
        var x: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > bounds.width {
                lines.append([])
                x = 0
            }
            lines[lines.count - 1].append((view, size))
            x += size.width + spacing
        }
        var y = bounds.minY
        for line in lines {
            let lineHeight = line.map(\.1.height).max() ?? 0
            var lineX = bounds.minX
            for (view, size) in line {
                view.place(
                    at: CGPoint(x: lineX,
                                y: y + (lineHeight - size.height) / 2),
                    anchor: .topLeading,
                    proposal: .unspecified)
                lineX += size.width + spacing
            }
            y += lineHeight + lineSpacing
        }
    }
}
