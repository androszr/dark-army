import Foundation

/// Which way a swipe on the agent sheet's header counts, who the neighbouring
/// agent is and how the Needs you list is flattened, as rules with no view in
/// them. Foundation only, so the host suite compiles and runs them under
/// `swiftc` (`host/tests/test_phone_sheet_swipe.py`) and the phone job runs the
/// same table (`AgentSheetSwipeTests.swift`).
///
/// "As drawn" means the groups in the order the Needs you tab draws them, the
/// items inside each group in the order it draws them. The view, not the rule,
/// decides which items resolve to an agent (`PhoneInbox.uniqueAgent`): this
/// file only sees the session ids it is handed.
enum AgentSheetSwipe {
    enum Direction { case next, previous }

    /// How far sideways a swipe must travel to count.
    static let minimumTravel: CGFloat = 60
    /// The horizontal travel must be at least this multiple of the vertical,
    /// so a diagonal pull on the header is the sheet's drag, not a swipe.
    static let dominance: CGFloat = 2

    /// Leftward is next, rightward previous; nil for a short or diagonal pull.
    static func direction(dx: CGFloat, dy: CGFloat) -> Direction? {
        guard abs(dx) >= minimumTravel, abs(dx) >= dominance * abs(dy) else { return nil }
        return dx < 0 ? .next : .previous
    }

    /// The entry one step along from `session`'s first index in `order`; nil
    /// at either end (no wrap). A session absent from `order` (the agent just
    /// left Needs you) goes next to the first waiting and has no previous.
    static func neighbour(of session: String, in order: [String],
                          _ direction: Direction) -> String? {
        guard let index = order.firstIndex(of: session) else {
            return direction == .next ? order.first : nil
        }
        let target = direction == .next ? index + 1 : index - 1
        return order.indices.contains(target) ? order[target] : nil
    }

    /// The groups flattened in drawn order, run through `id`, duplicates
    /// dropped keeping the first.
    static func order<Item>(_ groups: [[Item]], id: (Item) -> String?) -> [String] {
        var seen = Set<String>()
        var result: [String] = []
        for group in groups {
            for item in group {
                guard let key = id(item), seen.insert(key).inserted else { continue }
                result.append(key)
            }
        }
        return result
    }
}
