import SwiftUI

// Every animation the panel owns goes through here, so Reduce Motion is
// honoured in one place. The decrypt caption and the waiting chatter keep
// their own reduced forms (`DecryptFeedback`, `AgentChatter`). **Byte-pinned
// to the phone** from the marker line down (`ios/BobPhone/Motion.swift`,
// `host/tests/test_reduced_motion.py`): edit both copies together.
enum Motion {
    /// `withAnimation`, unless the person asked for less motion: then the
    /// same change lands at once. The caller reads
    /// `@Environment(\.accessibilityReduceMotion)` in its own view and
    /// passes it here, so the rule is one line and the setting is the
    /// system's, never a preference of ours.
    @discardableResult
    static func animate<Result>(_ animation: Animation, reduced: Bool,
                                _ body: () throws -> Result) rethrows -> Result {
        try withAnimation(reduced ? nil : animation, body)
    }

    /// The animation for an `.animation(_:value:)` modifier, or none under
    /// Reduce Motion.
    static func animation(_ animation: Animation, reduced: Bool) -> Animation? {
        reduced ? nil : animation
    }
}
