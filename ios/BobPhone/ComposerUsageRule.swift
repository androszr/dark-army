import Foundation

/// Which usage bar is the 7d / 5h figure for an offered assistant.
///
/// Foundation only: the host-suite pin slices this enum and runs it under
/// `swiftc` against a fixture report. A stale bar is no reading — the same
/// doctrine as `menu_format.limit_percent` and the Usage tab's window row.
enum ComposerUsageRule {
    struct Figure: Equatable {
        let window: String      // "7d" or "5h"
        let percent: Double?    // nil where the phone holds no reading
        let stale: Bool
    }
    struct Row: Equatable {
        let provider: String
        let figures: [Figure]   // 7d first, then 5h where offered
    }

    /// The provider's seven-day bar, or nil. Claude: kind "weekly_all"
    /// (never "weekly_scoped"). Grok: kind "grok_weekly". Codex (kind
    /// prefixed "codex_"): the bar whose group is "weekly"; against a bar
    /// carrying no group, the one whose label ends in "d". Anyone else:
    /// group "weekly". First match in the daemon's order.
    static func sevenDay(for provider: String, in bars: [UsageBar]) -> UsageBar? {
        if provider == "claude" {
            return bars.first { $0.provider == provider && $0.kind == "weekly_all" }
        }
        if provider == "grok" {
            return bars.first { $0.provider == provider && $0.kind == "grok_weekly" }
        }
        if provider == "codex" {
            if let weekly = bars.first(where: {
                $0.provider == provider && $0.kind.hasPrefix("codex_") && $0.group == "weekly"
            }) {
                return weekly
            }
            return bars.first {
                $0.provider == provider
                    && $0.kind.hasPrefix("codex_")
                    && $0.group.isEmpty
                    && $0.shortLabel.hasSuffix("d")
            }
        }
        return bars.first { $0.provider == provider && $0.group == "weekly" }
    }

    /// Claude's five-hour bar (kind "session"); nil for every other provider.
    static func fiveHour(for provider: String, in bars: [UsageBar]) -> UsageBar? {
        guard provider == "claude" else { return nil }
        return bars.first { $0.provider == provider && $0.kind == "session" }
    }

    /// One row per offered tool in the daemon's order — the switch's
    /// tile order — each with its 7d figure and, for Claude, its 5h.
    /// A provider with no matching bar gets a Figure with percent nil.
    static func rows(tools: [String], bars: [UsageBar]) -> [Row] {
        tools.map { provider in
            var figures = [figure(window: "7d", bar: sevenDay(for: provider, in: bars))]
            if provider == "claude" {
                figures.append(figure(window: "5h", bar: fiveHour(for: provider, in: bars)))
            }
            return Row(provider: provider, figures: figures)
        }
    }

    /// "Claude, 7 day 42 percent, 5 hour 18 percent" /
    /// "Grok, 7 day no reading". Stale reads as "no reading".
    static func spoken(row: Row, name: String) -> String {
        var parts = [name]
        for item in row.figures {
            let window: String
            switch item.window {
            case "7d": window = "7 day"
            case "5h": window = "5 hour"
            default: window = item.window
            }
            if let percent = item.percent, !item.stale {
                parts.append("\(window) \(Int(percent)) percent")
            } else {
                parts.append("\(window) no reading")
            }
        }
        return parts.joined(separator: ", ")
    }

    /// A missing bar and a stale bar are both no reading; `stale` stays
    /// so the view can say which of the two it is.
    static func figure(window: String, bar: UsageBar?) -> Figure {
        guard let bar else {
            return Figure(window: window, percent: nil, stale: false)
        }
        if bar.stale {
            return Figure(window: window, percent: nil, stale: true)
        }
        return Figure(window: window, percent: bar.percent, stale: false)
    }
}
