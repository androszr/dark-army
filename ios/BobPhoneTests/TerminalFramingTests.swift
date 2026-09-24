import XCTest
@testable import BobPhone

/// The live terminal screen's plumbing: `TerminalFrameCodec`,
/// `TerminalStreamHead.parse` and `TerminalBytesFrame`.
///
/// `host/tests/test_phone_terminal_stream_drift.py` compares this block with
/// the panel's byte for byte, which catches a *divergence* and not a *fault*:
/// both copies can be wrong together. The length is assembled by hand,
/// big-endian, byte by byte, and the parser slices the buffer by
/// `startIndex` offsets after `removeSubrange` — an off-by-one or an
/// endianness flip desynchronises the stream for ever, and a wrong `end`
/// feeds header bytes into the emulator.
///
/// Seam: `Data` literals. No socket and no HTTP client.
@MainActor
final class TerminalFramingTests: XCTestCase {

    // MARK: - encode

    func testEncodePutsTheKindFirstAndTheLengthBigEndian() {
        let payload = Data(repeating: 0x41, count: 0x0102_0304)
        let frame = TerminalFrameCodec.encode(kind: UInt8(ascii: "D"),
                                              payload: payload)
        XCTAssertEqual(frame.count, TerminalFrameCodec.headSize + payload.count)
        XCTAssertEqual(frame[0], UInt8(ascii: "D"))
        // Big-endian, most significant byte first. A flip fails right here.
        XCTAssertEqual(Array(frame[1...4]), [0x01, 0x02, 0x03, 0x04])
    }

    func testEncodeOfAnEmptyPayloadIsJustTheHead() {
        let frame = TerminalFrameCodec.encode(kind: UInt8(ascii: "X"),
                                              payload: Data())
        XCTAssertEqual(frame.count, TerminalFrameCodec.headSize)
        XCTAssertEqual(Array(frame), [UInt8(ascii: "X"), 0, 0, 0, 0])
    }

    // MARK: - the parser

    func testOneWholeFrameComesBackOut() throws {
        var parser = TerminalFrameCodec.Parser()
        let payload = Data("hello, friend".utf8)
        let frames = try parser.feed(
            TerminalFrameCodec.encode(kind: UInt8(ascii: "D"), payload: payload))
        XCTAssertEqual(frames.count, 1)
        XCTAssertEqual(frames.first?.kind, UInt8(ascii: "D"))
        XCTAssertEqual(frames.first?.payload, payload)
    }

    func testTwoFramesInOneArrivalComeBackInOrder() throws {
        var parser = TerminalFrameCodec.Parser()
        var wire = TerminalFrameCodec.encode(kind: UInt8(ascii: "D"),
                                             payload: Data("one".utf8))
        wire.append(TerminalFrameCodec.encode(kind: UInt8(ascii: "X"),
                                              payload: Data("two".utf8)))
        let frames = try parser.feed(wire)
        XCTAssertEqual(frames.count, 2)
        XCTAssertEqual(frames[0].kind, UInt8(ascii: "D"))
        XCTAssertEqual(frames[0].payload, Data("one".utf8))
        XCTAssertEqual(frames[1].kind, UInt8(ascii: "X"))
        XCTAssertEqual(frames[1].payload, Data("two".utf8))
    }

    func testAZeroLengthPayloadRoundTrips() throws {
        // An `X` exit frame carries none.
        var parser = TerminalFrameCodec.Parser()
        let frames = try parser.feed(
            TerminalFrameCodec.encode(kind: UInt8(ascii: "X"), payload: Data()))
        XCTAssertEqual(frames.count, 1)
        XCTAssertEqual(frames.first?.payload, Data())
    }

    func testAFrameSplitAcrossThreeArrivalsIsReassembled() throws {
        var parser = TerminalFrameCodec.Parser()
        let payload = Data("a screen's worth of bytes".utf8)
        let wire = TerminalFrameCodec.encode(kind: UInt8(ascii: "D"),
                                             payload: payload)
        let head = wire.prefix(TerminalFrameCodec.headSize)
        let middle = wire.dropFirst(TerminalFrameCodec.headSize).prefix(4)
        let rest = wire.dropFirst(TerminalFrameCodec.headSize + 4)

        XCTAssertEqual(try parser.feed(Data(head)).count, 0)
        XCTAssertEqual(try parser.feed(Data(middle)).count, 0)

        // The tail of this frame plus a whole second one: if `removeSubrange`
        // left the buffer misaligned, the second frame never appears.
        var tail = Data(rest)
        tail.append(TerminalFrameCodec.encode(kind: UInt8(ascii: "E"),
                                              payload: Data("refused".utf8)))
        let frames = try parser.feed(tail)
        XCTAssertEqual(frames.count, 2)
        XCTAssertEqual(frames[0].kind, UInt8(ascii: "D"))
        XCTAssertEqual(frames[0].payload, payload)
        XCTAssertEqual(frames[1].kind, UInt8(ascii: "E"))
        XCTAssertEqual(frames[1].payload, Data("refused".utf8))
    }

    func testAFrameAboveTheCeilingThrows() {
        var parser = TerminalFrameCodec.Parser()
        let declared = UInt32(TerminalFrameCodec.maxFrame + 1).bigEndian
        var head = Data([UInt8(ascii: "D")])
        withUnsafeBytes(of: declared) { head.append(contentsOf: $0) }
        XCTAssertThrowsError(try parser.feed(head)) { error in
            guard case TerminalFrameCodec.StreamError.frameTooLarge = error else {
                return XCTFail("expected frameTooLarge, got \(error)")
            }
        }
    }

    func testAFrameExactlyAtTheCeilingDoesNotThrow() {
        var parser = TerminalFrameCodec.Parser()
        let declared = UInt32(TerminalFrameCodec.maxFrame).bigEndian
        var head = Data([UInt8(ascii: "D")])
        withUnsafeBytes(of: declared) { head.append(contentsOf: $0) }
        // The payload has not arrived, so nothing comes out — but nothing
        // throws either, which is the boundary being asserted.
        XCTAssertEqual(try? parser.feed(head).count, 0)
    }

    // MARK: - the hand-parsed HTTP head

    private func head(_ text: String) -> Data { Data(text.utf8) }

    func testTheHeadIsNilUntilTheBlankLineArrives() {
        let partial = head("HTTP/1.1 200 OK\r\nContent-Length: 12\r\n")
        XCTAssertNil(TerminalStreamHead.parse(partial))
    }

    func testAWholeHeadReportsStatusTypeLengthAndWhereTheBodyBegins() throws {
        let text = "HTTP/1.1 200 OK\r\n"
            + "Content-Type: application/octet-stream\r\n"
            + "Content-Length: 12\r\n\r\n"
        let body = Data("hello friend".utf8)
        var buffer = head(text)
        buffer.append(body)

        let parsed = try XCTUnwrap(TerminalStreamHead.parse(buffer))
        XCTAssertEqual(parsed.status, 200)
        XCTAssertEqual(parsed.contentType, "application/octet-stream")
        XCTAssertEqual(parsed.contentLength, 12)
        XCTAssertEqual(parsed.end, text.utf8.count)
        // The off-by-one that would feed header bytes to the emulator.
        XCTAssertEqual(Data(buffer[parsed.end...]), body)
    }

    func testHeaderNamesMatchCaseInsensitively() throws {
        let buffer = head("HTTP/1.1 200 OK\r\nCONTENT-LENGTH: 7\r\n"
                          + "CONTENT-TYPE: text/plain\r\n\r\n")
        let parsed = try XCTUnwrap(TerminalStreamHead.parse(buffer))
        XCTAssertEqual(parsed.contentLength, 7)
        XCTAssertEqual(parsed.contentType, "text/plain")
    }

    func testAMalformedStatusLineReportsZero() throws {
        let parsed = try XCTUnwrap(
            TerminalStreamHead.parse(head("garbage\r\n\r\n")))
        XCTAssertEqual(parsed.status, 0)
    }

    func testANonTwoHundredHeadStillParses() throws {
        let parsed = try XCTUnwrap(
            TerminalStreamHead.parse(head("HTTP/1.1 403 Forbidden\r\n\r\n")))
        XCTAssertEqual(parsed.status, 403)
        XCTAssertEqual(parsed.contentLength, 0)
    }

    // MARK: - the away phone's poll frame

    private func bytesFrame(_ json: String) throws -> TerminalBytesFrame {
        try JSONDecoder().decode(TerminalBytesFrame.self, from: Data(json.utf8))
    }

    func testAnEmptyBodyKeepsThePaintMeSentinel() throws {
        // `bytesRead` defaults **-1**: flipped to 0 the away phone silently
        // stops repainting.
        let frame = try bytesFrame("{}")
        XCTAssertEqual(frame.bytesRead, -1)
        XCTAssertFalse(frame.painted)
        XCTAssertFalse(frame.dataMore)
        XCTAssertFalse(frame.available)
        XCTAssertEqual(frame.bytes, Data())
    }

    func testBase64DataBecomesBytes() throws {
        let frame = try bytesFrame(#"{"data": "aGk="}"#)
        XCTAssertEqual(frame.bytes, Data("hi".utf8))
    }

    func testSomethingThatIsNotBase64IsNoBytesRatherThanACrash() throws {
        let frame = try bytesFrame(#"{"data": "not base64!"}"#)
        XCTAssertEqual(frame.bytes, Data())
    }

    func testTheSnakeCaseKeysDecode() throws {
        let frame = try bytesFrame(
            #"{"bytes_read": 42, "data_more": true, "ring_overflowed": true}"#)
        XCTAssertEqual(frame.bytesRead, 42)
        XCTAssertTrue(frame.dataMore)
        XCTAssertTrue(frame.ringOverflowed)
    }
}

/// The fold above the live terminal (`TerminalStrip`, `AgentDetailView.swift`):
/// folded by default so the screen is the terminal, opened by itself only
/// where something is blocking the agent, and labelled with what it holds.
final class TerminalStripTests: XCTestCase {

    func testNothingBlockingKeepsTheFoldShut() {
        XCTAssertFalse(TerminalStrip.needsPress(prompts: 0, questions: 0))
        XCTAssertEqual(TerminalStrip.foldLabel(open: false, prompts: 0, questions: 0), "▸ controls")
        XCTAssertEqual(TerminalStrip.foldLabel(open: true, prompts: 0, questions: 0), "▾ controls")
    }

    func testAnAskOpensTheFoldAndNamesItself() {
        XCTAssertTrue(TerminalStrip.needsPress(prompts: 1, questions: 0))
        XCTAssertTrue(TerminalStrip.needsPress(prompts: 0, questions: 2))
        XCTAssertEqual(TerminalStrip.foldLabel(open: false, prompts: 0, questions: 2), "▸ question")
        // A permission ask outranks a question: it is the one blocking a tool call.
        XCTAssertEqual(TerminalStrip.foldLabel(open: true, prompts: 1, questions: 2), "▾ permission")
    }
}
