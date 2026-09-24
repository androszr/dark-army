import XCTest
@testable import BobPanel

/// `HiddenTerminalBuffer`'s two rules: what a hidden pane holds replays in
/// order, and past the cap nothing partial is ever replayed — the drain
/// asks for a repaint exactly once and the buffer is clean after it.
final class HiddenTerminalBufferTests: XCTestCase {

    private func bytes(_ s: String) -> Data { Data(s.utf8) }

    func testTheCapIsOneMebibyte() {
        XCTAssertEqual(HiddenTerminalBuffer.capBytes, 1 << 20)
    }

    func testHoldsDrainAsTheConcatenationInOrder() {
        var buffer = HiddenTerminalBuffer()
        buffer.hold(bytes("line 1\r\n"))
        buffer.hold(bytes("\u{1b}[31mline 2\u{1b}[0m\r\n"))
        buffer.hold(bytes("line 3"))
        XCTAssertEqual(buffer.drain(),
                       .bytes(bytes("line 1\r\n\u{1b}[31mline 2\u{1b}[0m\r\nline 3")))
        // Drained means empty.
        XCTAssertEqual(buffer.drain(), .nothing)
        XCTAssertTrue(buffer.held.isEmpty)
    }

    func testAnEmptyDrainIsNothing() {
        var buffer = HiddenTerminalBuffer()
        XCTAssertEqual(buffer.drain(), .nothing)
        XCTAssertEqual(buffer, HiddenTerminalBuffer())
    }

    func testAHoldPastTheCapOverflowsEmptiesAndDropsLaterHolds() {
        var buffer = HiddenTerminalBuffer()
        buffer.hold(Data(count: HiddenTerminalBuffer.capBytes - 1))
        XCTAssertFalse(buffer.overflowed)
        buffer.hold(bytes("ab"))            // one byte too many
        XCTAssertTrue(buffer.overflowed)
        XCTAssertTrue(buffer.held.isEmpty)
        buffer.hold(bytes("later"))
        XCTAssertTrue(buffer.held.isEmpty, "every hold after an overflow is dropped")
    }

    func testAHoldExactlyAtTheCapIsKept() {
        var buffer = HiddenTerminalBuffer()
        buffer.hold(Data(count: HiddenTerminalBuffer.capBytes))
        XCTAssertFalse(buffer.overflowed)
        XCTAssertEqual(buffer.held.count, HiddenTerminalBuffer.capBytes)
    }

    func testADrainAfterOverflowRepaintsExactlyOnceAndLeavesItClean() {
        var buffer = HiddenTerminalBuffer()
        buffer.hold(Data(count: HiddenTerminalBuffer.capBytes + 1))
        XCTAssertEqual(buffer.drain(), .repaint)
        XCTAssertEqual(buffer, HiddenTerminalBuffer())
        XCTAssertEqual(buffer.drain(), .nothing)
        // Clean means it holds again.
        buffer.hold(bytes("after"))
        XCTAssertEqual(buffer.drain(), .bytes(bytes("after")))
    }
}
