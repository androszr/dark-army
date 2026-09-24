import XCTest
@testable import BobPanel

/// The search bar's chrome. Pure statics, so this needs no view and no
/// `BoardState`.
final class BoardSearchBarTests: XCTestCase {
    func testEmptyQueryHidesTheClearChip() {
        XCTAssertFalse(BoardSearchBar.chrome(queryEmpty: true).showClear)
    }

    func testTypedQueryShowsTheClearChip() {
        XCTAssertTrue(BoardSearchBar.chrome(queryEmpty: false).showClear)
    }
}
