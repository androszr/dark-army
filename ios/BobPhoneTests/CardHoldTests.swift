import XCTest
@testable import BobPhone

/// The Board tab's hold: when a half-second press on a Prep or Backlog card
/// enters select mode, which release is swallowed and the bar's words. The
/// same tables `host/tests/test_phone_card_hold.py` runs under `swiftc`.
final class CardHoldTests: XCTestCase {
    private func card(_ fields: [String: Any]) -> BoardCard {
        var json: [String: Any] = ["id": "c1", "column_name": "backlog", "root": "/r/a",
                                   "plan_path": "plans/x.md", "tool": "claude"]
        for (key, value) in fields { json[key] = value }
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(BoardCard.self, from: data)
    }

    private func enters(_ held: BoardCard, drawn: [BoardCard], column: String = "backlog",
                        supported: Bool = true, pressOut: Bool = false,
                        selecting: String? = nil, dispatchEnabled: Bool = true) -> Bool {
        PhoneCardHold.entersSelect(column: column, card: held, cards: drawn,
                                   supported: supported, pressOut: pressOut,
                                   selecting: selecting, dispatchEnabled: dispatchEnabled)
    }

    func testEntersSelectNeedsEveryTermOnBacklog() {
        let a = card(["id": "a"])
        let b = card(["id": "b"])
        XCTAssertTrue(enters(a, drawn: [a, b]))
        XCTAssertFalse(enters(a, drawn: [a, b], column: "in_progress"))
        XCTAssertFalse(enters(a, drawn: [a, b], column: "done"))
        XCTAssertFalse(enters(a, drawn: [a, b], supported: false))
        XCTAssertFalse(enters(a, drawn: [a, b], pressOut: true))
        XCTAssertFalse(enters(a, drawn: [a, b], selecting: "prep"))
        XCTAssertFalse(enters(a, drawn: [a, b], selecting: "backlog"))
        XCTAssertFalse(enters(card(["id": "a", "session_id": "s1"]), drawn: [a, b]))
        XCTAssertFalse(enters(a, drawn: [a]))
        XCTAssertFalse(enters(a, drawn: [a, b], dispatchEnabled: false))
    }

    func testEntersSelectNeedsEveryTermOnPrep() {
        let a = card(["id": "a", "column_name": "prep", "plan_path": ""])
        let b = card(["id": "b", "column_name": "prep", "plan_path": ""])
        XCTAssertTrue(enters(a, drawn: [a, b], column: "prep"))
        XCTAssertFalse(enters(a, drawn: [a, b], column: "prep", supported: false))
        XCTAssertFalse(enters(a, drawn: [a, b], column: "prep", pressOut: true))
        XCTAssertFalse(enters(a, drawn: [a, b], column: "prep", selecting: "backlog"))
        let scout = card(["id": "s", "column_name": "prep", "plan_path": "", "kind": "scout"])
        XCTAssertFalse(enters(scout, drawn: [scout, b], column: "prep"))
        XCTAssertFalse(enters(a, drawn: [a], column: "prep"))
        XCTAssertFalse(enters(a, drawn: [a, b], column: "prep", dispatchEnabled: false))
    }

    func testTheHoldRuleIsTheSelectWordsRule() {
        let a = card(["id": "a"])
        let b = card(["id": "b"])
        for supported in [true, false] {
            for pressOut in [true, false] {
                for selecting in [nil, "prep", "backlog"] as [String?] {
                    let expected = supported && !pressOut && selecting == nil
                        && PhoneRowSelection.tickable("backlog", card: a, dispatchEnabled: true)
                        && PhoneRowSelection.offersSelect("backlog", cards: [a, b],
                                                          dispatchEnabled: true)
                    XCTAssertEqual(enters(a, drawn: [a, b], supported: supported,
                                          pressOut: pressOut, selecting: selecting), expected)
                }
            }
        }
    }

    func testConsumesTapOnTheHeldIdOnly() {
        let at = Date(timeIntervalSince1970: 1000)
        let soon = at.addingTimeInterval(0.3)
        XCTAssertFalse(PhoneCardHold.consumesTap(held: nil, tapped: "a", heldAt: at, now: soon))
        XCTAssertFalse(PhoneCardHold.consumesTap(held: "a", tapped: "a", heldAt: nil, now: soon))
        XCTAssertTrue(PhoneCardHold.consumesTap(held: "a", tapped: "a", heldAt: at, now: soon))
        XCTAssertFalse(PhoneCardHold.consumesTap(held: "a", tapped: "b", heldAt: at, now: soon))
        // A release that never came does not eat a later tap.
        XCTAssertFalse(PhoneCardHold.consumesTap(
            held: "a", tapped: "a", heldAt: at,
            now: at.addingTimeInterval(PhoneCardHold.releaseWindow + 0.1)))
    }

    func testTheWordsAndTheConstants() {
        XCTAssertEqual(PhoneCardHold.countLine(count: 1), "1 selected")
        XCTAssertEqual(PhoneCardHold.countLine(count: 3), "3 selected")
        XCTAssertEqual(PhoneCardHold.barLabel(column: "prep", count: 2),
                       "Batch refine, 2 selected")
        XCTAssertEqual(PhoneCardHold.barLabel(column: "backlog", count: 2),
                       "Batch start, 2 selected")
        XCTAssertEqual(PhoneCardHold.barLabel(column: nil, count: 0), "")
        XCTAssertEqual(PhoneCardHold.minimumDuration, 0.5)
        XCTAssertEqual(PhoneCardHold.maximumDistance, 10)
        XCTAssertEqual(PhoneCardHold.releaseWindow, 1)
        XCTAssertEqual(PhoneCardHold.rows, ["prep", "backlog"])
    }
}
