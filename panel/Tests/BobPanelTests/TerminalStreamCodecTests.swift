import XCTest
@testable import BobPanel

final class TerminalStreamCodecTests: XCTestCase {
    func testEncodeThenParseRoundTrips() throws {
        let payload = Data("héllo\u{1b}[H".utf8)
        let frame = TerminalFrameCodec.encode(kind: UInt8(ascii: "D"), payload: payload)
        XCTAssertEqual(frame.count, 5 + payload.count)
        XCTAssertEqual(frame[0], UInt8(ascii: "D"))
        var parser = TerminalFrameCodec.Parser()
        let out = try parser.feed(frame)
        XCTAssertEqual(out.count, 1)
        XCTAssertEqual(out[0].kind, UInt8(ascii: "D"))
        XCTAssertEqual(out[0].payload, payload)
    }

    func testFramesSplitAcrossReadsAndPackedTogetherBothParse() throws {
        let a = TerminalFrameCodec.encode(kind: UInt8(ascii: "D"), payload: Data("abc".utf8))
        let b = TerminalFrameCodec.encode(kind: UInt8(ascii: "X"), payload: Data())
        var parser = TerminalFrameCodec.Parser()
        let joined = a + b
        XCTAssertEqual(try parser.feed(joined.prefix(3)).count, 0)
        XCTAssertEqual(try parser.feed(joined.dropFirst(3).prefix(4)).count, 0)
        let rest = try parser.feed(joined.dropFirst(7))
        XCTAssertEqual(rest.map { $0.kind }, [UInt8(ascii: "D"), UInt8(ascii: "X")])
        XCTAssertEqual(rest[0].payload, Data("abc".utf8))
        XCTAssertEqual(rest[1].payload.count, 0)
    }

    func testAnOversizeFrameIsRefused() {
        var head = Data([UInt8(ascii: "D")])
        let big = UInt32(TerminalFrameCodec.maxFrame + 1).bigEndian
        withUnsafeBytes(of: big) { head.append(contentsOf: $0) }
        var parser = TerminalFrameCodec.Parser()
        XCTAssertThrowsError(try parser.feed(head))
    }

    func testHeadParsesStatusTypeAndLength() {
        let raw = Data("HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: 12\r\n\r\n{\"ok\":false}".utf8)
        let head = TerminalStreamHead.parse(raw)
        XCTAssertEqual(head?.status, 409)
        XCTAssertEqual(head?.contentType, "application/json")
        XCTAssertEqual(head?.contentLength, 12)
        XCTAssertEqual(head?.end, raw.count - 12)
        XCTAssertNil(TerminalStreamHead.parse(Data("HTTP/1.1 200 OK\r\nContent-Type: x".utf8)))
    }

    func testTheLookIsTheEditorsDarkModern() {
        XCTAssertEqual(TerminalLook.backgroundHex, 0x1f1f1f)
        XCTAssertEqual(TerminalLook.foregroundHex, 0xcccccc)
        XCTAssertEqual(TerminalLook.ansiHex.count, 16)
    }
}
