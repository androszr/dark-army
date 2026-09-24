import XCTest
@testable import BobPanel

/// The fold rule the card window and the phone share: a plan splits at the
/// first line that is exactly the template's `## Technical detail` heading,
/// losslessly, and a plan without one is shown whole.
final class PlanSplitTests: XCTestCase {

    private let fixture = """
    # Fold a plan's detail

    - **Date:** 2026-09-06
    - **Slug:** fold-plan
    - **Status:** draft
    - **Surfaces:** panel | phone
    - **Stages:** bc-implementer | bc-verifier | bc-bug-auditor

    ## What this does

    Words for a person.

    ## How I'll build it

    1. A step.

    ## How you'll know it worked

    - A check.

    ---

    ## Technical detail

    Everything below is the implementation record.

    ## Idea

    The user said so.
    """

    func testSplitsAtTheFirstExactHeading() {
        let split = PlanSplit.split(fixture)
        XCTAssertTrue(split.summary.hasSuffix("---\n\n"),
                      "summary ends with the rule and the blank line before the heading")
        XCTAssertFalse(split.summary.contains("## Technical detail"))
        XCTAssertEqual(split.detail?.hasPrefix("## Technical detail"), true)
        XCTAssertEqual(split.detail?.contains("## Idea"), true)
    }

    func testIsLossless() {
        let split = PlanSplit.split(fixture)
        XCTAssertNotNil(split.detail)
        XCTAssertEqual(split.summary + (split.detail ?? ""), fixture)
    }

    func testNoHeadingMeansWholeAndNil() {
        let old = "# An older plan\n\n## Context\n\nNo template heading here.\n"
        let split = PlanSplit.split(old)
        XCTAssertEqual(split.summary, old)
        XCTAssertNil(split.detail)
    }

    func testTrimsButDoesNotFuzzyMatch() {
        let padded = "a\n  ## Technical detail  \nb\n"
        XCTAssertEqual(PlanSplit.split(padded).detail, "  ## Technical detail  \nb\n")
        XCTAssertEqual(PlanSplit.split(padded).summary, "a\n")
        for near in ["a\n### Technical detail\nb\n",
                     "a\n## Technical details\nb\n",
                     "a\nsee ## Technical detail below\nb\n"] {
            let split = PlanSplit.split(near)
            XCTAssertNil(split.detail, near)
            XCTAssertEqual(split.summary, near)
        }
    }

    func testKeepsTheHeaderListInTheSummary() {
        let split = PlanSplit.split(fixture)
        XCTAssertTrue(split.summary.contains("- **Stages:** bc-implementer"))
        XCTAssertEqual(PlanStructure.parseStages(split.summary),
                       ["bc-implementer", "bc-verifier", "bc-bug-auditor"])
    }

    func testMarkerIsTheTemplatesHeading() {
        XCTAssertEqual(PlanSplit.marker, "## Technical detail")
    }
}
