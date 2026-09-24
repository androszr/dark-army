import XCTest
@testable import BobPanel

/// The outstanding hand-check as the panel sees it: a tolerant decode of the
/// one field, and the rule that non-empty *is* the flag.
final class ManualCheckTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    /// A daemon that has not been restarted after this panel shipped sends
    /// cards with no `manual_steps` key at all. Swift's synthesized `Decodable`
    /// throws on a missing key even where the property has a default, so one
    /// absent field would blank the whole board — Models.swift's documented trap.
    func testAnAbsentManualStepsKeyDecodesToNoCheck() throws {
        let row = try card(#"{"id":"a","title":"t"}"#)
        XCTAssertEqual(row.manualSteps, "")
        XCTAssertFalse(row.needsManualCheck)
    }

    /// Non-empty **is** the flag: there is no separate boolean, because a flag
    /// and a note are two things that can disagree, and a badge with no steps
    /// behind it is the state the column exists to be incapable of showing.
    func testStepsPresentAreTheFlag() throws {
        let row = try card(
            #"{"id":"a","title":"t","manual_steps":"1. Open the board.\nWhy not automated: a real screen."}"#)
        XCTAssertTrue(row.needsManualCheck)
        XCTAssertTrue(row.manualSteps.hasPrefix("1. Open the board."))
    }

    /// An empty string is *not* a flag — the store writes `''` on a card
    /// nobody flagged and on one a person has just marked checked, and both
    /// must draw no badge.
    func testAnEmptyStringIsNotAFlag() throws {
        let row = try card(#"{"id":"a","title":"t","manual_steps":""}"#)
        XCTAssertFalse(row.needsManualCheck)
    }

    /// The older-daemon fallback: an absent `manual_check_due` must read as
    /// "show it", reproducing the always-show behaviour exactly, so a
    /// half-upgraded machine never hides a check it should be showing.
    func testAnAbsentDueKeyFallsBackToTheNote() throws {
        let row = try card(
            #"{"id":"a","title":"t","manual_steps":"1. look"}"#)
        XCTAssertTrue(row.manualCheckDue)
        XCTAssertEqual(row.manualCheckDue, row.needsManualCheck)
    }

    /// The badge waits its turn: the daemon says the assistant is still
    /// working, so the card and the rail draw nothing — while the sheet's own
    /// unconditional flag stays on, because a person who opened the detail may
    /// read and clear a stale note at any time.
    func testAWorkingCardHidesTheBadgeButKeepsTheSheetFlag() throws {
        let row = try card(
            #"{"id":"a","title":"t","manual_steps":"1. look","manual_check_due":false}"#)
        XCTAssertFalse(row.manualCheckDue)
        XCTAssertTrue(row.needsManualCheck)
        XCTAssertEqual(row.manualSteps, "1. look")
    }

    /// The invariant survives the wire: a daemon bug sending `true` on a card
    /// with no steps must not draw a badge with nothing behind it.
    func testDueWithoutStepsIsNotABadge() throws {
        let row = try card(
            #"{"id":"a","title":"t","manual_steps":"","manual_check_due":true}"#)
        XCTAssertFalse(row.manualCheckDue)
        XCTAssertFalse(row.needsManualCheck)
    }

    /// The title is never rewritten to say a check is outstanding: that would
    /// edit the person's own words and would not survive being cleared. The
    /// badge is a separate line, so the title decodes untouched either way.
    func testTheTitleIsUntouchedByTheFlag() throws {
        let row = try card(
            #"{"id":"a","title":"Rewrite the sprite pipeline","manual_steps":"1. look"}"#)
        XCTAssertEqual(row.title, "Rewrite the sprite pipeline")
    }
}
