import XCTest
@testable import BobPanel

/// The wrap-up button's reach is `canClose`, defaulting false so an older
/// daemon draws no button rather than one that types `/clear`.
final class WrapUpReachTests: XCTestCase {

    private func decode(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    func testCanCloseTrue() throws {
        let agent = try decode(#"{"session_id":"s1","can_close":true}"#)
        XCTAssertTrue(agent.canClose)
    }

    func testCanCloseFalse() throws {
        let agent = try decode(#"{"session_id":"s1","can_close":false}"#)
        XCTAssertFalse(agent.canClose)
    }

    func testCanCloseAbsentIsFalse() throws {
        let agent = try decode(#"{"session_id":"s1"}"#)
        XCTAssertFalse(agent.canClose)
    }

    func testCanCloseOnACodexRow() throws {
        let agent = try decode(
            #"{"session_id":"codex:abc","provider":"codex","can_close":true}"#)
        XCTAssertEqual(agent.provider, "codex")
        XCTAssertTrue(agent.canClose)
        XCTAssertFalse(agent.canType)
    }

    func testWrapUpBarCaptionsNameTheCloseAndNotAClear() {
        let captions = [
            WrapUpBar.prompt,
            WrapUpBar.armedPrompt,
            WrapUpBar.button,
            WrapUpBar.armedButton,
        ]
        for caption in captions {
            let lower = caption.lowercased()
            XCTAssertTrue(lower.contains("close") || lower.contains("terminal"),
                          "\(caption) should name the close")
            XCTAssertFalse(lower.contains("clear"),
                           "\(caption) must not mention a clear")
        }
    }
}
