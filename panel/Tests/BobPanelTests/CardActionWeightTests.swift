import XCTest
@testable import BobPanel

/// One outlined verb per card, chosen by its column, tabled.
///
/// `plans/2026-09-20-one-primary-button-per-card.md`. The rule is a pure
/// static over the daemon's column id; both card faces read it and neither
/// re-derives it, so this file is the whole decision.
final class CardActionWeightTests: XCTestCase {

    private let columns = ["prep", "backlog", "in_progress", "done"]
    private let verbs: [CardActionWeight.Verb] = [.refine, .start, .startHere, .done]

    /// Refine in Prep, START in Backlog, Done in In progress, none in Done.
    func testTheTable() {
        XCTAssertEqual(CardActionWeight.primary(column: "prep"), .refine)
        XCTAssertEqual(CardActionWeight.primary(column: "backlog"), .start)
        XCTAssertEqual(CardActionWeight.primary(column: "in_progress"), .done)
        XCTAssertNil(CardActionWeight.primary(column: "done"))
    }

    /// An id the daemon does not speak outlines nothing.
    func testUnknownColumnsHaveNoPrimary() {
        XCTAssertNil(CardActionWeight.primary(column: ""))
        XCTAssertNil(CardActionWeight.primary(column: "bogus"))
    }

    /// The own-terminal HERE stays secondary everywhere.
    func testStartHereIsNeverPrimary() {
        for column in columns {
            XCTAssertFalse(CardActionWeight.isPrimary(.startHere, column: column), column)
        }
    }

    /// `isPrimary` is the table: exactly one true per column that has a
    /// primary, none in Done.
    func testIsPrimaryAgreesWithTheTable() {
        for column in columns {
            let expected = CardActionWeight.primary(column: column)
            var trues = 0
            for verb in verbs {
                let primary = CardActionWeight.isPrimary(verb, column: column)
                XCTAssertEqual(primary, verb == expected, "\(column) \(verb)")
                if primary { trues += 1 }
            }
            XCTAssertEqual(trues, column == "done" ? 0 : 1, column)
        }
    }

    /// A scout in Prep outlines START, because it has no plan to refine.
    func testScoutInPrepOutlinesStart() {
        XCTAssertEqual(CardActionWeight.primary(column: "prep", kind: "scout"), .start)
        XCTAssertEqual(CardActionWeight.primary(column: "prep"), .refine)
    }

    /// A scout in every other column answers as a build card.
    func testScoutInOtherColumnsAnswersAsBuild() {
        XCTAssertEqual(CardActionWeight.primary(column: "backlog", kind: "scout"), .start)
        XCTAssertEqual(CardActionWeight.primary(column: "in_progress", kind: "scout"), .done)
        XCTAssertNil(CardActionWeight.primary(column: "done", kind: "scout"))
    }
}
