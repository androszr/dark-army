import XCTest
@testable import BobPanel

/// `reply_via` is the daemon's word for which route the next reply takes.
/// The panel reads it verbatim and names it in the reply box; it decides
/// nothing from it. An older daemon sends no key, and Swift's synthesized
/// `Decodable` would throw on that — so the tolerant default is the point.
final class ReplyViaDecodeTests: XCTestCase {

    private func agent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    func testAbsentDecodesAsEmptyAndDoesNotThrow() throws {
        let a = try agent(#"{"session_id": "s1", "channel": true}"#)
        XCTAssertEqual(a.replyVia, "")
        XCTAssertTrue(a.channel)
    }

    func testTypedAndChannelDecodeVerbatim() throws {
        XCTAssertEqual(try agent(#"{"session_id": "s1", "reply_via": "typed"}"#).replyVia,
                       "typed")
        XCTAssertEqual(try agent(#"{"session_id": "s1", "reply_via": "channel"}"#).replyVia,
                       "channel")
    }

    func testUnknownValueIsKeptAndReadsAsTheChannel() throws {
        let a = try agent(#"{"session_id": "s1", "reply_via": "pigeon"}"#)
        XCTAssertEqual(a.replyVia, "pigeon")
        XCTAssertEqual(ReplyBar.placeholder(replyVia: a.replyVia), "Reply…")
        XCTAssertEqual(ReplyBar.sendHelp(replyVia: a.replyVia),
                       "Sent through Dark Army's channel")
    }

    func testTypedNamesTheTerminalInPlaceholderAndHelp() {
        XCTAssertEqual(ReplyBar.placeholder(replyVia: "typed"),
                       "Reply — typed into its terminal…")
        XCTAssertTrue(ReplyBar.sendHelp(replyVia: "typed").contains("input line"))
        XCTAssertEqual(ReplyBar.placeholder(replyVia: ""), "Reply…")
        XCTAssertEqual(ReplyBar.placeholder(replyVia: "channel"), "Reply…")
    }
}
