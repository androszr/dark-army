import XCTest
@testable import BobPhone

/// The Board tab's swipe on a Prep or Backlog card: which verb the first
/// button wears, its words and the arithmetic of the slide. The same tables
/// `host/tests/test_phone_card_swipe.py` runs under `swiftc`.
final class CardSwipeTests: XCTestCase {
    private func card(_ fields: [String: Any]) -> BoardCard {
        var json: [String: Any] = ["id": "c1", "column_name": "backlog", "root": "/r/a",
                                   "plan_path": "plans/x.md", "tool": "claude"]
        for (key, value) in fields { json[key] = value }
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(BoardCard.self, from: data)
    }

    func testCanStartMirrorsTheCardScreensSevenTerms() {
        XCTAssertTrue(PhoneCardSwipe.canStart(card: card([:]), dispatchEnabled: true))
        XCTAssertFalse(PhoneCardSwipe.canStart(card: card([:]), dispatchEnabled: false))
        for broken: [String: Any] in [
            ["column_name": "in_progress"],
            ["tool": ""],
            ["queue_state": "queued"],
            ["session_id": "s1"],
            ["link_state": "dispatching"],
            ["link_state": "live"],
            ["refine_state": "dispatching"],
            ["refine_state": "live"],
            ["batch": ["rank": 2, "size": 3, "state": "waiting"]],
        ] {
            XCTAssertFalse(PhoneCardSwipe.canStart(card: card(broken), dispatchEnabled: true),
                           "\(broken)")
        }
    }

    func testPrimaryFollowsTheColumnAndTheGates() {
        XCTAssertEqual(PhoneCardSwipe.primary(column: "prep", canRefine: true, canStart: true),
                       .refine)
        XCTAssertEqual(PhoneCardSwipe.primary(column: "prep", canRefine: false, canStart: true),
                       .start)
        XCTAssertNil(PhoneCardSwipe.primary(column: "prep", canRefine: false, canStart: false))
        XCTAssertEqual(PhoneCardSwipe.primary(column: "backlog", canRefine: true, canStart: true),
                       .start)
        XCTAssertNil(PhoneCardSwipe.primary(column: "backlog", canRefine: true, canStart: false))
        for column in ["in_progress", "done", ""] {
            XCTAssertNil(PhoneCardSwipe.primary(column: column, canRefine: true, canStart: true))
        }
        XCTAssertEqual(PhoneCardSwipe.rows, ["prep", "backlog"])
    }

    func testTheLabelsAreTheCardScreensWords() {
        XCTAssertEqual(PhoneCardSwipe.startLabel(armed: false, unplanned: true), "START")
        XCTAssertEqual(PhoneCardSwipe.startLabel(armed: true, unplanned: false), "Really start?")
        XCTAssertEqual(PhoneCardSwipe.startLabel(armed: true, unplanned: true), "Start unplanned?")
        XCTAssertEqual(PhoneCardSwipe.refineLabel(armed: false), "Refine")
        XCTAssertEqual(PhoneCardSwipe.refineLabel(armed: true), "Really refine?")
        XCTAssertEqual(PhoneCardSwipe.deleteRow, "Delete card")
        XCTAssertEqual(PhoneCardSwipe.deleteTitle, "Delete this card?")
        XCTAssertEqual(PhoneCardSwipe.deleteMessage, "Are you sure? There is no undo.")
        XCTAssertEqual(PhoneCardSwipe.moreLabel, "⋯")
        XCTAssertEqual(PhoneCardSwipe.moreSpoken, "More actions")
    }

    func testTheGestureHelpersClampAndThreshold() {
        XCTAssertEqual(PhoneCardSwipe.minimumDrag, 20)
        XCTAssertEqual(PhoneCardSwipe.revealTravel, 56)
        XCTAssertEqual(PhoneCardSwipe.offset(forTranslation: -300, revealed: false),
                       -PhoneCardSwipe.actionsWidth)
        XCTAssertEqual(PhoneCardSwipe.offset(forTranslation: 300, revealed: true), 0)
        XCTAssertEqual(PhoneCardSwipe.offset(forTranslation: -40, revealed: false), -40)
        XCTAssertTrue(PhoneCardSwipe.revealAfter(translation: -57, revealed: false))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: -55, revealed: false))
        XCTAssertFalse(PhoneCardSwipe.revealAfter(translation: 57, revealed: true))
        XCTAssertTrue(PhoneCardSwipe.revealAfter(translation: 0, revealed: true))
        XCTAssertTrue(PhoneCardSwipe.dominantHorizontal(dx: 30, dy: 10))
        XCTAssertFalse(PhoneCardSwipe.dominantHorizontal(dx: 10, dy: 30))
    }
}
