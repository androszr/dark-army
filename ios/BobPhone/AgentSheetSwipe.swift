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

    /// Where the sheet's agent stands in the Needs you order, in the person's
    /// words: `index` counts from one.
    struct Position: Equatable {
        let index: Int
        let count: Int
    }

    /// The agent's place in `order` (the `order(_:id:)` result, the same list
    /// the swipe reads, taken at the moment the header draws); nil for fewer
    /// than two waiters, where there is no swipe and so no mark, and for a
    /// session that is not in the list.
    static func position(of session: String, in order: [String]) -> Position? {
        guard order.count >= 2, let index = order.firstIndex(of: session) else { return nil }
        return Position(index: index + 1, count: order.count)
    }

    /// The words drawn after the name.
    static func mark(_ position: Position) -> String {
        "\(position.index) of \(position.count)"
    }

    /// The words VoiceOver adds to the title.
    static func spokenMark(_ position: Position) -> String {
        "\(position.index) of \(position.count) waiting"
    }

    /// The header row has one flexible slot, the title, and the phone never
    /// clips prose: the mark is a trailing run of that title and, at the
    /// accessibility text sizes, the first and only thing that yields; the
    /// spoken title keeps it. The threshold is the environment's
    /// `isAccessibilitySize`, read by the view and passed in, never measured.
    static func showsMark(_ position: Position?, accessibilitySize: Bool) -> Bool {
        position != nil && !accessibilitySize
    }
}
