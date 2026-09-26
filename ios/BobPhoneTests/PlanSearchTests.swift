import XCTest
@testable import BobPhone

/// The Plans screen's grep rule and the row's lines (`Plans.swift`), the
/// same table `test_phone_plans.py` runs under `swiftc`.
final class PlanSearchTests: XCTestCase {
    private func row() -> PlanRow {
        var row = PlanRow()
        row.path = "/Users/x/zebra-root/plans/2026-09-25-phone-plans-library.md"
        row.root = "/Users/x/zebra-root"
        row.name = "2026-09-25-phone-plans-library.md"
        row.slug = "phone-plans-library"
        row.day = "2026-09-25"
        row.title = "The phone lists every project's plans"
        row.project = "dark-army"
        row.status = "draft"
        row.area = "pocket"
        row.cardTitle = "Phone plans library"
        row.cardColumn = "backlog"
        return row
    }

    func testEachSearchedFieldMatches() {
        for query in ["every project", "dark-army", "draft", "pocket",
                      "plans-library", "library"] {
            XCTAssertTrue(PlanSearch.matches(query: query, row: row()), query)
        }
        XCTAssertFalse(PlanSearch.matches(query: "zzqx", row: row()))
    }

    func testAWordOnlyInThePathTheRootOrTheDayDoesNotMatch() {
        XCTAssertFalse(PlanSearch.matches(query: "zebra", row: row()))
        XCTAssertFalse(PlanSearch.matches(query: "2026-09-25", row: row()))
    }

    func testMatchingIgnoresCaseAndDiacritics() {
        var r = row()
        r.title = "Café plan"
        XCTAssertTrue(PlanSearch.matches(query: "CAFE", row: r))
    }

    func testAnEmptyOrBlankQueryMatchesEverythingAndIsNotASearch() {
        XCTAssertTrue(PlanSearch.matches(query: "", row: row()))
        XCTAssertTrue(PlanSearch.matches(query: "   ", row: row()))
        XCTAssertFalse(PlanSearch.isSearching("  "))
        XCTAssertTrue(PlanSearch.isSearching("plans"))
        XCTAssertTrue(PlanSearch.noMatchLine(query: "  zzqx ").hasPrefix("“zzqx” is not in a title"))
    }

    func testDisplayTitleFallsBackToTheName() {
        var r = row()
        r.title = ""
        XCTAssertEqual(r.displayTitle, "2026-09-25-phone-plans-library.md")
    }

    func testCardLineAndDateLine() {
        XCTAssertEqual(row().cardLine, "card: Phone plans library · backlog")
        var bare = row()
        bare.cardTitle = ""
        bare.cardColumn = ""
        XCTAssertEqual(bare.cardLine, "")
        XCTAssertEqual(row().dateLine, "2026-09-25")
        XCTAssertEqual(row().headerLine, "draft · pocket")
        XCTAssertEqual(row().metaLine, "dark-army · 2026-09-25")
    }

    func testAStaleReplyIsDroppedAndAFailedOneSaysSo() {
        XCTAssertNil(PlansLoad.apply(requested: "a", current: "b", fetched: PlanIndex()))
        let failed = PlansLoad.apply(requested: "a", current: "a", fetched: PlanIndex?.none)
        XCTAssertEqual(failed?.detail, PlanIndex.fetchFailedLine)
        XCTAssertEqual(failed?.value.rows.count, 0)
    }
}
