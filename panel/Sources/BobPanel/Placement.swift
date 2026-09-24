import AppKit
import SwiftUI

/// The facts the panel keeps about *where* it lives, in
/// `~/.dark-army/panel-position.json`.
///
/// It used to keep a single **left + top** corner for a popover hanging off the
/// status item; that pair went dead when the panel became one full-screen
/// workspace and was removed 23 Aug 2026. Now that the panel is an ordinary
/// titled, resizable window, a frame is back — but in a new shape, not a
/// revert: one frame **per screen**, keyed by the screen's `CGDirectDisplayID`,
/// because a size that fits a laptop panel is wrong on a 5K display and the
/// window should land where it was last left *on that screen*.
///
/// Every key is read tolerantly in both directions, and every write below is a
/// **merge**: the frame table, the board's tick set and the board's row
/// flips are written by different gestures at different times, and none may
/// drop the others. An older build's ribbon-placement keys are unknown keys
/// now, kept by every merge write and read by nobody. An older build
/// opening this file looks for `left`/`top`, finds neither, ignores `frames`,
/// and keeps its own keys intact.
enum PanelPlacement {
    /// Where the file lives. `PanelStateDirectory.root` is the real state
    /// folder in production and a throwaway temp folder under XCTest. Still
    /// overridable per test.
    static var root: URL = PanelStateDirectory.root

    static var path: URL {
        root.appendingPathComponent("panel-position.json")
    }

    /// The whole file as a dictionary, empty when absent or unreadable.
    private static func readBody() -> [String: Any] {
        guard let data = try? Data(contentsOf: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    private static func writeBody(_ body: [String: Any]) {
        let file = path
        if body.isEmpty {
            try? FileManager.default.removeItem(at: file)
            return
        }
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        try? data.write(to: file, options: .atomic)
    }

    /// The board's ticked projects, or nil when none were ever saved. `""` is
    /// a legitimate member — the "Other" pile. A wrong shape decodes as
    /// absent, never as a selection.
    static func boardProjects() -> [String]? {
        readBody()["board_projects"] as? [String]
    }

    /// Merge the tick set in, removing the key when empty — the empty set is
    /// the default ("all projects") and needs no record; leaving a stale
    /// empty array would be a second spelling of the same state. An older
    /// build ignores the key, and the merge keeps every other key and the
    /// frames intact in both directions.
    static func saveBoardProjects(_ names: [String]) {
        var body = readBody()
        if names.isEmpty {
            body.removeValue(forKey: "board_projects")
        } else {
            body["board_projects"] = names
        }
        writeBody(body)
    }

    /// The board rows a person has flipped away from their default fold
    /// (`BoardRowFold`), or nil when none were ever saved. A wrong shape
    /// decodes as absent — the defaults — never as a fold.
    ///
    /// A **new** key, not the retired one the swimlane folders' folds used:
    /// a preference key is never renamed or repurposed, and that one is left
    /// in the file untouched for any older build that still reads it
    /// (`BoardFoldTests` pins that every writer here leaves it alone).
    static func boardRowFlips() -> [String]? {
        readBody()["board_row_flips"] as? [String]
    }

    /// Merge the flipped rows in, removing the key when empty — the empty
    /// set is the defaults and needs no record, `saveBoardProjects`' rule.
    /// Every other key, the frames and the ticks included, survives.
    static func saveBoardRowFlips(_ ids: [String]) {
        var body = readBody()
        if ids.isEmpty {
            body.removeValue(forKey: "board_row_flips")
        } else {
            body["board_row_flips"] = ids
        }
        writeBody(body)
    }

    /// The project root the last successfully filed composer card used, or
    /// nil when none has been saved (or the stored value is garbage).
    static func composerRoot() -> String? {
        readBody()["composer_last_root"] as? String
    }

    /// Merge the last-filed composer root in, removing the key when empty —
    /// `saveBoardProjects`' rule. Every other key survives.
    static func saveComposerRoot(_ root: String) {
        var body = readBody()
        if root.isEmpty {
            body.removeValue(forKey: "composer_last_root")
        } else {
            body["composer_last_root"] = root
        }
        writeBody(body)
    }

    // MARK: - First-run checklist

    /// The key under which "setup was completed once" is remembered. A
    /// panel-local fact with no authority: un-enrolling still stops
    /// observation at once whatever this says, and the daemon never reads it.
    static let checklistCompletedKey = "first_run_checklist_completed"

    /// Whether the first-run checklist has been completed (or skipped by an
    /// install that already had a session) on this Mac. Absent, malformed
    /// and future-shaped values all decode as **false**.
    static func checklistCompleted() -> Bool {
        // A JSON `1` bridges to an NSNumber that `as? Bool` happily reads
        // as true; only a genuine boolean counts.
        guard let number = readBody()[checklistCompletedKey] as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return false }
        return number.boolValue
    }

    /// Merge the completion in, `saveBoardProjects`' rule: `false` removes
    /// the key rather than writing a second spelling of the default, and
    /// every other key — frames, ticks, flips, keys this build has never
    /// heard of — survives in both directions.
    static func saveChecklistCompleted(_ done: Bool) {
        var body = readBody()
        if done {
            body[checklistCompletedKey] = true
        } else {
            body.removeValue(forKey: checklistCompletedKey)
        }
        writeBody(body)
    }

    // MARK: - Per-screen window frames

    /// The window's last frame on this display, or nil when it has never been
    /// placed there (or the stored value is garbage — a wrong shape decodes as
    /// absent, never as a frame).
    static func frame(for displayID: Int) -> NSRect? {
        guard let frames = readBody()["frames"] as? [String: Any],
              let raw = frames[String(displayID)] as? [Any]
        else { return nil }
        let nums = raw.compactMap { ($0 as? NSNumber)?.doubleValue }
        guard nums.count == 4, nums[2] > 0, nums[3] > 0 else { return nil }
        return NSRect(x: nums[0], y: nums[1], width: nums[2], height: nums[3])
    }

    /// Record the frame under this display, keeping every other display's
    /// frame and every other key in the file.
    static func saveFrame(_ rect: NSRect, for displayID: Int) {
        var body = readBody()
        var frames = body["frames"] as? [String: Any] ?? [:]
        frames[String(displayID)] = [rect.origin.x, rect.origin.y,
                                     rect.size.width, rect.size.height]
        body["frames"] = frames
        writeBody(body)
    }

    /// Where the window opens the first time it appears on a screen: centred,
    /// 0.8 of the visible frame in each dimension. Pure — the caller supplies
    /// the screen's visible frame, so tests need no display.
    static func defaultFrame(in visibleFrame: NSRect) -> NSRect {
        let width = (visibleFrame.width * 0.8).rounded()
        let height = (visibleFrame.height * 0.8).rounded()
        return NSRect(x: (visibleFrame.midX - width / 2).rounded(),
                      y: (visibleFrame.midY - height / 2).rounded(),
                      width: width, height: height)
    }

    /// The size floor in *logical* points, shared with the window's own
    /// `contentMinSize`: the fleet rail's width (`PanelMetrics.width`), and
    /// enough height for the header, a few rows and the footer. Multiplied
    /// by the panel scale when applied to a physical frame. Without it a
    /// garbage-small stored frame round-trips forever — `frame(for:)` only
    /// refuses non-positive sizes — and the user can shrink Dark Army to a sliver.
    static let minWidth: CGFloat = PanelMetrics.width
    static let minHeight: CGFloat = 360

    /// A frame that is certainly reachable on this screen: size floored at
    /// `minWidth`/`minHeight` times `scale`, capped to the visible frame (the
    /// cap wins on a screen smaller than the floor), origin shifted until
    /// the whole window is on it. Run on every show, which is what handles
    /// a stored frame for a screen whose geometry has changed — or a frame
    /// left behind by an unplugged display. `scale` defaults to 1 so the
    /// existing floor tests stay valid unchanged.
    static func clamped(_ frame: NSRect, to visibleFrame: NSRect,
                        scale: CGFloat = 1) -> NSRect {
        var out = frame
        let floorW = minWidth * scale
        let floorH = minHeight * scale
        out.size.width = min(max(out.width, floorW), visibleFrame.width)
        out.size.height = min(max(out.height, floorH), visibleFrame.height)
        if out.maxX > visibleFrame.maxX { out.origin.x = visibleFrame.maxX - out.width }
        if out.minX < visibleFrame.minX { out.origin.x = visibleFrame.minX }
        if out.maxY > visibleFrame.maxY { out.origin.y = visibleFrame.maxY - out.height }
        if out.minY < visibleFrame.minY { out.origin.y = visibleFrame.minY }
        return out
    }
}

extension NSScreen {
    /// The screen's `CGDirectDisplayID` — the stable per-display key
    /// `panel-position.json` files the window's per-screen frames under.
    var displayId: Int? {
        (deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")]
            as? NSNumber)?.intValue
    }
}
