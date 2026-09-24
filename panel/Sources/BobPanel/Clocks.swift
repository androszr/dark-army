import Foundation
import SwiftUI

/// The panel's one once-a-second clock.
///
/// Every 1 Hz `TimelineView` in the panel — the fleet table's age column,
/// the agent detail's two relative figures, the inbox's "waiting for" — ticks
/// on `SecondHand`, which lands on the wall clock's whole seconds rather than
/// on the instant each row appeared. N rows then wake on one beat, and
/// SwiftUI draws them in one pass instead of N staggered ones. Each call site
/// keeps its own visibility gate (`ticking:` or `client.visible`); this
/// schedule never pauses itself.
enum Clocks {

    /// The next whole second strictly after `date`.
    static func wholeSecond(after date: Date) -> Date {
        let t = date.timeIntervalSinceReferenceDate
        var next = t.rounded(.up)
        if next <= t { next += 1 }
        return Date(timeIntervalSinceReferenceDate: next)
    }

    /// The instant asked for first, so a timeline appearing mid-second draws
    /// the current moment at once, then every whole second after it. The
    /// same entries for either mode.
    struct SecondHand: TimelineSchedule {
        func entries(from start: Date, mode: TimelineScheduleMode) -> AnyIterator<Date> {
            var last: Date?
            return AnyIterator {
                let next = last.map(Clocks.wholeSecond(after:)) ?? start
                last = next
                return next
            }
        }
    }
}
