import Foundation

/// What a click on the Dock icon does — a pure rule, no AppKit, so every case
/// is a test with an injected clock (`DockToggleTests`).
///
/// The Dock icon toggles, like the strip: a click with the window hidden
/// shows it, and a second click on the same icon puts it away. The one trap
/// is a window that is on screen but **behind another app**: that click
/// activates Dark Army first and delivers the reopen after, so at the moment
/// the reopen is asked the app already looks active. A click that is what
/// just made the app active is a "bring it to the front", never a hide — so
/// the owner stamps the moment of activation and the rule asks how recent it
/// is.
enum DockToggle {
    /// How close an activation must be to the reopen to count as caused by
    /// that same click. The Dock's activation and its reopen event arrive
    /// within a few milliseconds of each other; a person's second click is
    /// far slower than this.
    static let activationWindow: TimeInterval = 0.5

    enum Verdict: Equatable {
        /// Nothing of ours is on screen, or it is behind another app: show it.
        case show
        /// The window is in front and the app was already active: put it away.
        case hide
    }

    /// - Parameters:
    ///   - seen: the window is on screen and not fully covered.
    ///   - active: the app is active as the reopen is asked.
    ///   - becameActive: when the app last became active, if ever.
    static func verdict(seen: Bool, active: Bool, becameActive: Date?,
                        now: Date) -> Verdict {
        guard seen, active else { return .show }
        if let becameActive, now.timeIntervalSince(becameActive) < activationWindow {
            return .show
        }
        return .hide
    }
}
