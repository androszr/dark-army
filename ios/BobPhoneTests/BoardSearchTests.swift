import XCTest
@testable import BobPhone

/// The Board tab's grep rule: title, summary, project.
final class BoardSearchTests: XCTestCase {
    func testTitleSummaryAndProjectEachMatch() {
        XCTAssertTrue(BoardSearch.matches(query: "refine", title: "Refine the board",
                                          summary: "", project: ""))
        XCTAssertTrue(BoardSearch.matches(query: "one-line", title: "",
                                          summary: "a one-line summary", project: ""))
        XCTAssertTrue(BoardSearch.matches(query: "dark-army", title: "",
                                          summary: "", project: "dark-army"))
        XCTAssertFalse(BoardSearch.matches(query: "refine", title: "other",
                                           summary: "elsewhere", project: "desk"))
    }

    func testMatchingIgnoresCaseAndDiacritics() {
        XCTAssertTrue(BoardSearch.matches(query: "zOsIa", title: "Zosia's card",
                                          summary: "", project: ""))
        XCTAssertTrue(BoardSearch.matches(query: "cafe", title: "café",
                                          summary: "", project: ""))
    }

    func testWhitespaceAroundTheQueryIsTrimmed() {
        XCTAssertTrue(BoardSearch.matches(query: "  vex ", title: "Vex",
                                          summary: "", project: ""))
        XCTAssertEqual(BoardSearch.needle("  vex "), "vex")
    }

    func testAnEmptyOrBlankQueryMatchesEverythingAndIsNotASearch() {
        XCTAssertTrue(BoardSearch.matches(query: "", title: "anything",
                                          summary: "x", project: "y"))
        XCTAssertTrue(BoardSearch.matches(query: "   ", title: "anything",
                                          summary: "x", project: "y"))
        XCTAssertFalse(BoardSearch.isSearching(""))
        XCTAssertFalse(BoardSearch.isSearching("   "))
        XCTAssertTrue(BoardSearch.isSearching("vex"))
    }

    func testNoFieldCarryingTheWordsIsNoMatch() {
        XCTAssertFalse(BoardSearch.matches(query: "zzqx", title: "title",
                                           summary: "summary", project: "project"))
    }

    func testTheNoMatchLineQuotesTheTrimmedQuery() {
        XCTAssertEqual(BoardSearch.noMatchLine(query: "  zzqx  "),
                       "“zzqx” is not in a title, a summary or a project.")
    }

    func testThePlaceholderNamesTheThreeFields() {
        XCTAssertEqual(BoardSearch.placeholder, "grep title, summary, project…")
        XCTAssertTrue(BoardSearch.placeholder.contains("title"))
        XCTAssertTrue(BoardSearch.placeholder.contains("summary"))
        XCTAssertTrue(BoardSearch.placeholder.contains("project"))
    }
}
