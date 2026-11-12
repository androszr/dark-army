import XCTest
@testable import BobPanel

/// `message_queue` is the daemon's composed list of one session's waiting and
/// not-delivered messages. The panel draws it verbatim and decides nothing; a
/// ragged payload must never throw out of `Agent.init` and blank the panel.
final class MessageQueueDecodeTests: XCTestCase {

    private func agent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    func testAnAbsentKeyIsNilAndDoesNotThrow() throws {
        XCTAssertNil(try agent(#"{"session_id": "s1"}"#).messageQueue)
    }

    func testAnEmptyObjectGivesNoItems() throws {
        let a = try agent(#"{"session_id": "s1", "message_queue": {}}"#)
        XCTAssertEqual(a.messageQueue?.items.count, 0)
        XCTAssertEqual(a.messageQueue?.headline, "")
    }

    func testAWronglyTypedMemberDecodesToItsDefault() throws {
        let a = try agent(#"{"session_id": "s1", "message_queue": {"items": [{"group": 5, "label": "x"}]}}"#)
        XCTAssertEqual(a.messageQueue?.items.count, 1)
        XCTAssertEqual(a.messageQueue?.items.first?.group, "")
        XCTAssertEqual(a.messageQueue?.items.first?.label, "x")
    }

    func testAnUnknownGroupIsNotUndelivered() throws {
        let a = try agent(#"{"session_id": "s1", "message_queue": {"items": [{"group": "pigeon"}]}}"#)
        XCTAssertEqual(a.messageQueue?.items.first?.undelivered, false)
        XCTAssertEqual(a.messageQueue?.hasUndelivered, false)
    }

    func testAMalformedItemElementNeverThrows() throws {
        let a = try agent(#"{"session_id": "s1", "message_queue": {"items": ["nope", {"label": "ok"}]}}"#)
        XCTAssertEqual(a.messageQueue?.items.count, 2)
        XCTAssertEqual(a.messageQueue?.items.last?.label, "ok")
    }

    func testAFullObjectDecodesVerbatim() throws {
        let a = try agent(#"""
        {"session_id": "s1", "message_queue": {
          "headline": "1 waiting to be typed \u00b7 1 not delivered",
          "items": [
            {"group": "waiting", "label": "A message from the phone, 14:03",
             "status": "waiting its turn", "excerpt": "hello", "why": ""},
            {"group": "undelivered", "label": "A line from the Mac, 14:01",
             "status": "not delivered", "excerpt": "", "why": "Too much waiting."}],
          "more_line": "and 2 more"}}
        """#)
        let q = try XCTUnwrap(a.messageQueue)
        XCTAssertEqual(q.headline, "1 waiting to be typed \u{00b7} 1 not delivered")
        XCTAssertEqual(q.items.map(\.status), ["waiting its turn", "not delivered"])
        XCTAssertEqual(q.items.map(\.undelivered), [false, true])
        XCTAssertEqual(q.items[1].why, "Too much waiting.")
        XCTAssertEqual(q.moreLine, "and 2 more")
        XCTAssertTrue(q.hasUndelivered)
    }
}
