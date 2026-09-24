import XCTest
@testable import BobPanel

/// `TerminalNoteHold`'s three rules and the orderings that matter: a
/// refusal survives the pane's own reconnect, real screen bytes clear it
/// exactly once, an open with nothing held says nothing, and a refusal
/// that arrives after screen bytes is drawn but never held.
final class TerminalNoteHoldTests: XCTestCase {

    func testARefusalSurvivesAReconnect() {
        var hold = TerminalNoteHold()
        XCTAssertEqual(hold.refused("Dark Army could not send this terminal's screen."),
                       "Dark Army could not send this terminal's screen.")
        // The stream closed behind the `E` frame; the pane reopens.
        XCTAssertEqual(hold.opened(), "Dark Army could not send this terminal's screen.")
        // And again — the sentence is still there, not blinked away.
        XCTAssertEqual(hold.opened(), "Dark Army could not send this terminal's screen.")
    }

    func testRealScreenDataClearsItOnce() {
        var hold = TerminalNoteHold()
        _ = hold.refused("refused")
        XCTAssertEqual(hold.received(), "")
        // Every later `D` frame: nothing to redraw.
        XCTAssertNil(hold.received())
        XCTAssertNil(hold.received())
        // Refuse → receive → open: the note is empty.
        XCTAssertEqual(hold.opened(), "")
    }

    func testAnOpenWithNothingHeldIsEmpty() {
        var hold = TerminalNoteHold()
        XCTAssertEqual(hold.opened(), "")
        XCTAssertEqual(hold, TerminalNoteHold())
        XCTAssertNil(hold.received())
        XCTAssertEqual(hold.held, "")
        XCTAssertTrue(hold.sawData)
    }

    func testARefusalAfterScreenBytesIsDrawnButNotHeld() {
        // A refused keystroke on a printing agent: `E` mid-stream, after
        // the paint. It is said once, as before, and the next `D` frame
        // must not erase it — so it is never held.
        var hold = TerminalNoteHold()
        XCTAssertEqual(hold.opened(), "")
        XCTAssertNil(hold.received())
        XCTAssertEqual(hold.refused("too long"), "too long")
        XCTAssertEqual(hold.held, "")
        XCTAssertNil(hold.received())
        // The connection dropped and reopened: nothing to restate.
        XCTAssertEqual(hold.opened(), "")
    }

    func testAnOpenStartsAFreshConnectionSoAnEarlyRefusalIsHeldAgain() {
        // Bytes on connection one; connection two refuses before any `D`.
        var hold = TerminalNoteHold()
        _ = hold.opened()
        _ = hold.received()
        _ = hold.opened()
        XCTAssertEqual(hold.refused("could not send"), "could not send")
        XCTAssertEqual(hold.opened(), "could not send")
        XCTAssertEqual(hold.received(), "")
    }

    func testALaterRefusalReplacesTheEarlier() {
        var hold = TerminalNoteHold()
        _ = hold.refused("first")
        _ = hold.refused("second")
        XCTAssertEqual(hold.opened(), "second")
        XCTAssertEqual(hold.received(), "")
        XCTAssertNil(hold.received())
    }
}
