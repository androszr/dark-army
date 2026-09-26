import Foundation

/// The bot's two grants, as the profile screen draws them — pure rules the
/// view and `BotAccessTests` share. The Mac is the authority: it publishes
/// each side's mode and, for a timed one, the moment it ends
/// (`bot_access` on the bot's devices row), and re-checks both on every
/// request the bot sends. The same three sentences as the Mac's
/// `SettingsMenuModel.botGrantWords`.
enum BotAccessRules {
    /// Every position one side can be put in, the Mac's `BOT_ACCESS_MODES`.
    static let modes = ["off", "1h", "6h", "24h", "forever"]

    /// The positions that run on a timer, the Mac's `BOT_ACCESS_SECONDS`.
    static let timed: Set<String> = ["1h", "6h", "24h"]

    /// One position as the menu says it.
    static func title(_ mode: String) -> String {
        switch mode {
        case "off": return "Off"
        case "1h": return "1 hour"
        case "6h": return "6 hours"
        case "24h": return "24 hours"
        case "forever": return "No timer"
        default: return mode
        }
    }

    /// One grant in words: off, on with no timer, or on until a moment. A
    /// timed grant whose moment has passed, and a word this build does not
    /// know, read off.
    static func words(mode: String, until: Double, now: Double) -> String {
        if mode == "forever" { return "on — no timer" }
        guard timed.contains(mode), until > now else { return "off" }
        return "on until " + AwaySpan.until(until, now: now)
    }

    /// Whether a menu choice is a press worth sending: a position this
    /// build offers that differs from the one the Mac publishes now.
    static func shouldPost(chosen: String, published: String) -> Bool {
        modes.contains(chosen) && chosen != published
    }

    /// The line drawn under a side after a refused press: the Mac's own
    /// words, or a plain fallback when the answer carried none.
    static func refusalWords(_ detail: String) -> String {
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "the Mac did not change it" : trimmed
    }

    /// Whether a "restart timer" press means anything: only while a timer
    /// runs. Choosing the same timer again restarts it from now on the Mac.
    static func showsRestart(_ mode: String) -> Bool {
        timed.contains(mode)
    }
}
