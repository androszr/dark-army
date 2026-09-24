import SwiftUI
import XCTest
@testable import BobPanel

/// The panel's shared second hand: every once-a-second clock lands on the
/// wall clock's whole seconds, so rows that appeared at different instants
/// tick together.
final class ClocksTests: XCTestCase {

    private let t = Date(timeIntervalSinceReferenceDate: 800_000_000)

    private func fraction(_ date: Date) -> Double {
        let s = date.timeIntervalSinceReferenceDate
        return s - s.rounded(.down)
    }

    func testWholeSecondIsStrictlyAfterAndWhole() {
        for start in [t.addingTimeInterval(0.3), t] {
            let next = Clocks.wholeSecond(after: start)
            XCTAssertEqual(fraction(next), 0)
            XCTAssertGreaterThan(next, start)
            XCTAssertLessThanOrEqual(next.timeIntervalSince(start), 1)
        }
        XCTAssertEqual(Clocks.wholeSecond(after: t), t.addingTimeInterval(1))
        XCTAssertEqual(Clocks.wholeSecond(after: t.addingTimeInterval(0.3)),
                       t.addingTimeInterval(1))
    }

    func testSecondHandFirstEntryIsTheInstantAsked() {
        let start = t.addingTimeInterval(0.42)
        XCTAssertEqual(Clocks.SecondHand().entries(from: start, mode: .normal)
                        .makeIterator().next(), start)
    }

    func testSecondHandTicksOnWholeSeconds() {
        let entries = Array(Clocks.SecondHand()
            .entries(from: t.addingTimeInterval(0.6), mode: .normal).prefix(6))
        let whole = Array(entries.dropFirst())
        XCTAssertEqual(whole.count, 5)
        for date in whole { XCTAssertEqual(fraction(date), 0) }
        for (later, earlier) in zip(whole.dropFirst(), whole) {
            XCTAssertEqual(later.timeIntervalSince(earlier), 1)
        }
        XCTAssertEqual(Array(Clocks.SecondHand()
            .entries(from: t, mode: .lowFrequency).prefix(6)),
                       Array(Clocks.SecondHand().entries(from: t, mode: .normal).prefix(6)))
    }

    func testTwoRowsStartedApartTickTogether() {
        let early = Array(Clocks.SecondHand()
            .entries(from: t.addingTimeInterval(0.2), mode: .normal).prefix(8))
        let late = Array(Clocks.SecondHand()
            .entries(from: t.addingTimeInterval(0.7), mode: .normal).prefix(8))
        XCTAssertEqual(Array(early.dropFirst()), Array(late.dropFirst()))
        XCTAssertNotEqual(early.first, late.first)
    }
}
