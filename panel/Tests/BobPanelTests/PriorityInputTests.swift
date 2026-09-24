import XCTest
@testable import BobPanel

/// The priority box's own sentence, judged as it is typed.
final class PriorityInputTests: XCTestCase {
    func testEmptyAndInRangeAreFine() {
        XCTAssertNil(PriorityInput.problem(""))
        XCTAssertNil(PriorityInput.problem("  "))
        XCTAssertNil(PriorityInput.problem("0"))
        XCTAssertNil(PriorityInput.problem("100"))
        XCTAssertNil(PriorityInput.problem(" 42 "))
    }

    func testOutOfRangeAndNonDigitsSayWhy() {
        XCTAssertEqual(PriorityInput.problem("101"), PriorityInput.words)
        XCTAssertEqual(PriorityInput.problem("-1"), PriorityInput.words)
        XCTAssertEqual(PriorityInput.problem("high"), PriorityInput.words)
        XCTAssertEqual(PriorityInput.problem("4.5"), PriorityInput.words)
        // Arabic-Indic digits pass `isNumber` and would store a string no
        // client parses the same way — the store's own test, restated.
        XCTAssertEqual(PriorityInput.problem("٤٢"), PriorityInput.words)
    }
}
