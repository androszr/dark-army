import XCTest
@testable import BobPanel

/// The Low priority button's reach is `canLowPriority`, defaulting false so
/// an older daemon draws no button rather than one that types a toggle into
/// a terminal. The card's `errorKind` decodes with an empty default for the
/// same reason: one absent key must never blank the panel.
final class LowPriorityReachTests: XCTestCase {

    private func decodeAgent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    private func decodeCard(_ json: String) throws -> BobPanel.Notification {
        try JSONDecoder().decode(BobPanel.Notification.self, from: Data(json.utf8))
    }

    func testCanLowPriorityTrue() throws {
        let agent = try decodeAgent(#"{"session_id":"s1","can_low_priority":true}"#)
        XCTAssertTrue(agent.canLowPriority)
    }

    func testCanLowPriorityFalse() throws {
        let agent = try decodeAgent(#"{"session_id":"s1","can_low_priority":false}"#)
        XCTAssertFalse(agent.canLowPriority)
    }

    func testCanLowPriorityAbsentIsFalse() throws {
        let agent = try decodeAgent(#"{"session_id":"s1"}"#)
        XCTAssertFalse(agent.canLowPriority)
    }

    func testCanLowPriorityIsIndependentOfCanClose() throws {
        let agent = try decodeAgent(
            #"{"session_id":"s1","can_close":true,"can_low_priority":false}"#)
        XCTAssertTrue(agent.canClose)
        XCTAssertFalse(agent.canLowPriority)
    }

    func testErrorKindDecodes() throws {
        let card = try decodeCard(
            #"{"session_id":"s1","hook":"StopFailure","error_kind":"rate_limit"}"#)
        XCTAssertEqual(card.errorKind, "rate_limit")
        XCTAssertEqual(card.hook, "StopFailure")
    }

    func testErrorKindAbsentIsEmpty() throws {
        let card = try decodeCard(#"{"session_id":"s1","hook":"Stop"}"#)
        XCTAssertEqual(card.errorKind, "")
    }

    func testLowPriorityBarCaptionsNameLowPriorityAndNeverAClear() {
        let captions = [
            LowPriorityBar.prompt,
            LowPriorityBar.armedPrompt,
            LowPriorityBar.button,
            LowPriorityBar.armedButton,
        ]
        for caption in captions {
            let lower = caption.lowercased()
            XCTAssertTrue(lower.contains("low priority") || lower.contains("low-priority"),
                          "\(caption) should name low priority")
            XCTAssertFalse(lower.contains("clear"),
                           "\(caption) must not mention a clear")
        }
    }
}
