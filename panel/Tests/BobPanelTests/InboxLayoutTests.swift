import SwiftUI
import XCTest
@testable import BobPanel

/// The inbox's arithmetic half: the numbers the drawing is built from and
/// the one ink decision. Pure statics — no window, no snapshot.
final class InboxLayoutTests: XCTestCase {

    func testFaceIsTheSlotAndTheMinimumTitleHeight() {
        XCTAssertEqual(InboxLayout.face, 28)
    }

    func testEntryPaddingAndTextSpacing() {
        XCTAssertEqual(InboxLayout.entryVPad, 10)
        XCTAssertEqual(InboxLayout.textSpacing, 4)
    }

    /// The whole diagnosis in one line: an entry's own lines must sit closer
    /// together than two entries do.
    func testAnEntryIsTighterInsideThanBetween() {
        XCTAssertGreaterThan(InboxLayout.entryVPad, InboxLayout.textSpacing)
    }

    func testActionChromeSize() {
        XCTAssertEqual(InboxLayout.actionSize, 10)
    }

    func testArmedInkIsTheAlarmAndPlainInkIsPhosphor() {
        XCTAssertEqual(InboxLayout.actionInk(armed: true), Theme.alarm)
        XCTAssertEqual(InboxLayout.actionInk(armed: false), Theme.phosphor)
    }
}
