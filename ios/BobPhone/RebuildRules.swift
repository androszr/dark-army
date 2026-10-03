import Foundation

/// The words Rebuild & restart wears on the phone, in one place so a test can
/// pin them and the Menu screen and an agent's screen say the same thing.
///
/// The Mac's `rebuild` section (`RebuildSection`) is the only fact; this
/// derives nothing but a sentence. Three things make it more than a lookup:
///
/// * the **held picture** keeps `rebuilding == true` through the gap while the
///   Mac restarts, so a dropped link after a press reads "waiting for the
///   Mac", never "Rebuilding…" as if live;
/// * "Rebuilt at" is only ever claimed for a finish **at or after this press**
///   (`lastFinishedAt >= pressedAt`), or a stamp from last week would read as
///   this press landing;
/// * from away the verb is offered only where the Mac says it accepts an away
///   press (`Board.rebuildAwaySupported`); against an older Mac the screen says
///   so instead of asking for Face ID and being turned away.
enum RebuildRules {
    static let awayLine =
        "This Mac's Dark Army offers Rebuild & restart on home Wi-Fi only."
    static let unavailableLine =
        "This Mac is not running Dark Army from a source checkout, so it has nothing to rebuild."
    static let waitingLine = "Restarting — waiting for the Mac"
    static let rebuildingLine =
        "Rebuilding… the Mac will drop off the link for a minute and come back by itself"
    static let askedLine = "Asked the Mac — waiting for it to start"
    static let idleLabel = "Rebuild & restart"
    static let armedLabel = "Really rebuild and restart?"
    static let armedWarning =
        "Builds from the Mac's working tree as it stands and restarts Dark Army. The Mac drops off the link for about a minute."
    static let awayArmedWarning =
        "Builds from the Mac's working tree as it stands and restarts Dark Army. You are away: the Mac drops off the link for a minute or two, and if the build fails its last lines show here."
    static let agentPrompt =
        "This agent says Dark Army needs rebuilding and restarting to pick up its work."

    /// Whether the tile is lit and the button drawn: the Mac says it can
    /// rebuild, and the phone is home or the Mac accepts the press away.
    static func offered(section: RebuildSection, away: Bool,
                        awayAllowed: Bool) -> Bool {
        section.available && (!away || awayAllowed)
    }

    /// Whether the press may be sent: offered, and no rebuild already running.
    static func canPress(section: RebuildSection, away: Bool,
                         awayAllowed: Bool = false) -> Bool {
        offered(section: section, away: away, awayAllowed: awayAllowed)
            && !section.rebuilding && !section.restarting
    }

    /// `HH:mm` in the phone's own time zone.
    static func clock(_ epoch: Double, timeZone: TimeZone = .current) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: Date(timeIntervalSince1970: epoch))
    }

    /// The one status sentence. `pressedAt` is this screen's own press (epoch
    /// seconds), nil when nothing was pressed here.
    static func line(section: RebuildSection, connected: Bool,
                     away: Bool = false, awayAllowed: Bool = false,
                     pressedAt: Double? = nil,
                     timeZone: TimeZone = .current) -> String {
        if away && !awayAllowed { return awayLine }
        if !section.available { return unavailableLine }
        if !connected, pressedAt != nil || section.rebuilding {
            return waitingLine
        }
        if section.restarting { return waitingLine }
        if section.rebuilding { return rebuildingLine }
        // An outcome only counts for this press when it finished after it.
        let fresh: Bool
        if let pressed = pressedAt {
            fresh = (section.lastFinishedAt ?? 0) >= pressed
        } else {
            fresh = true
        }
        if fresh, section.lastOutcome == "failed" {
            let why = section.lastError.trimmingCharacters(in: .whitespacesAndNewlines)
            return why.isEmpty ? "Rebuild failed" : "Rebuild failed: \(why)"
        }
        if pressedAt != nil {
            if fresh, section.lastOutcome == "ok", let at = section.lastFinishedAt {
                return "Rebuilt at \(clock(at, timeZone: timeZone))"
            }
            return askedLine
        }
        return section.label.isEmpty ? idleLabel : section.label
    }

    /// Whether the line is a failure, so the screen draws it in the alarm
    /// colour.
    static func isFailure(section: RebuildSection, pressedAt: Double? = nil) -> Bool {
        guard !section.rebuilding, section.lastOutcome == "failed" else { return false }
        if let pressed = pressedAt { return (section.lastFinishedAt ?? 0) >= pressed }
        return true
    }
}
