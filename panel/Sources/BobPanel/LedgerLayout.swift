import Foundation

/// How the seven-column History picture is laid out. Pure geometry over a
/// `LedgerPicture`: every figure and word still comes from `LedgerFold`,
/// and this only decides where it sits, in what order and how tall. The
/// thin columns' widths stay in `LedgerColumnLayout`.
enum LedgerWeekLayout {
    static let columnGap: Double = 5
    /// The column's inner padding. The usual line is pulled out by exactly
    /// this much on both sides so it reaches the column's border.
    static let columnPadding: Double = 6
    static let barWidth: Double = 26
    static let barHeight: Double = 72
    /// The full-scale height of a segment; the rest of `barHeight` is room
    /// for the usual line's word.
    static let segmentMax: Double = 64
    /// A drawn segment is never shorter than this, so a small day does not
    /// vanish. A drawing rule, not a figure.
    static let segmentMin: Double = 4
    /// The gap between the picture and the dock beneath it.
    static let dockGap: Double = 4

    struct Segment: Equatable {
        var ink: LedgerInk
        var height: Double
    }

    /// The bar's segments, **top to bottom**: Claude's token cost, then
    /// Grok's, then Codex's. Every segment is inside the total; a reported
    /// dollar is not a segment. The tokens and time lenses draw one segment
    /// of the lens's magnitude. A zero value is omitted; a day with no body
    /// has no segments, and the view draws its hairline instead.
    static func segments(_ column: LedgerDayColumn, lens: LedgerLens,
                         scale: Double) -> [Segment] {
        guard scale > 0 else { return [] }
        let values: [(LedgerInk, Double)]
        if lens == .spent {
            // The order is `LedgerWeek.segments`', the phone's own.
            values = LedgerWeek.segments(claude: column.claudeToken,
                                         grok: column.grokToken,
                                         codex: column.codexToken).map {
                (ink(provider: $0.provider), $0.value)
            }
        } else {
            values = [(.claude, column.magnitude)]
        }
        return values.compactMap { ink, value in
            guard value > 0 else { return nil }
            return Segment(ink: ink, height: max(segmentMin, value / scale * segmentMax))
        }
    }

    private static func ink(provider: String) -> LedgerInk {
        switch provider {
        case "grok": return .grok
        case "codex": return .codex
        default: return .claude
        }
    }

    /// How far above the bar's foot the usual line sits: spent lens only,
    /// clamped to the full scale. Nil means no line.
    static func usualLift(usual: Double?, lens: LedgerLens, scale: Double) -> Double? {
        guard lens == .spent, scale > 0, let usual else { return nil }
        return min(max(usual / scale, 0), 1) * segmentMax
    }

    struct FigurePart: Equatable {
        var text: String
        var ink: LedgerInk
    }

    /// The day's figure on one line: the token cost, then the reported
    /// dollar when one exists (`$0.00 reported` included), then the
    /// not-priced dash when part of the day is unknown and the figure is not
    /// already that dash. An empty figure (a day nothing happened) is left
    /// out rather than drawn as $0.00.
    static func figureLine(_ column: LedgerDayColumn) -> [FigurePart] {
        var parts: [FigurePart] = []
        if !column.figure.isEmpty {
            parts.append(FigurePart(text: column.figure, ink: .claude))
        }
        if let mark = column.reportedMark {
            parts.append(FigurePart(text: mark, ink: .reported))
        }
        if column.unknown, column.figure != LedgerFold.dash {
            parts.append(FigurePart(text: LedgerFold.dash, ink: .unknown))
        }
        return parts
    }

    /// The tile's last line: the card's figure and its turns, the two
    /// strings the tile always drew, on one line.
    static func tileMeta(_ card: LedgerCardLine) -> String {
        "\(card.figure) · \(card.turns) turns"
    }

    /// The acceptance sentence split for two weights: the three counts lead,
    /// the rest follows with its separator. A sentence of fewer than four
    /// parts — a reason, not counts — comes back whole in `rest`.
    static func acceptanceSplit(_ sentence: String) -> (lead: String, rest: String) {
        let parts = sentence.components(separatedBy: " · ")
        guard parts.count >= 4 else { return ("", sentence) }
        return (parts[0..<3].joined(separator: " · "),
                " · " + parts[3...].joined(separator: " · "))
    }
}
