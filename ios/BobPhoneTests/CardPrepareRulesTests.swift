import XCTest
@testable import BobPhone

/// The phone's port of the Mac's Prepare readers, over synthetic answers.
/// The byte-for-byte parity with `card_prepare.py` is
/// `host/tests/test_phone_prepare_parity.py`'s job; this file is the seam an
/// implementer runs in Xcode.
final class CardPrepareRulesTests: XCTestCase {
    private let roster = ["bc-implementer", "bc-verifier", "bc-security-reviewer"]
    private let roots = ["/Users/x/Code/alpha", "/Users/x/Code/beta", "/Users/x/other/beta"]

    func testFencedAnswerReadsEveryField() {
        let raw = """
        ```
        TITLE: Ship it
        SUMMARY: One line.
        BENEFICIARY: Rob
        BENEFIT: Less walking.
        CRITERION: The tile moves.
        INSTRUCTIONS: Do it.
        SPECIALISTS:
        - bc-implementer
        FOLDER: /Users/x/Code/alpha
        ```
        """
        let read = CardPrepareRules.parseIdea(raw, roster: roster)
        XCTAssertEqual(read.title, "Ship it")
        XCTAssertEqual(read.summary, "One line.")
        XCTAssertEqual(read.prompt, "Do it.")
        XCTAssertEqual(read.stages, ["bc-implementer"])
        XCTAssertEqual(CardPrepareRules.parseObjective(raw)["beneficiary"], "Rob")
        XCTAssertEqual(CardPrepareRules.parseFolder(raw, roots: roots), "/Users/x/Code/alpha")
    }

    func testListMarkerAndQuotesComeOffATitle() {
        let raw = "TITLE: - \"Quoted\"\nSUMMARY: 'S'\nINSTRUCTIONS: * Go.\nSPECIALISTS: NONE"
        let read = CardPrepareRules.parseIdea(raw, roster: roster)
        XCTAssertEqual(read.title, "Quoted")
        XCTAssertEqual(read.summary, "S")
        XCTAssertEqual(read.prompt, "Go.")
        XCTAssertEqual(read.stages, [])
    }

    func testNoneObjectiveIsEmptyNotARefusal() {
        let raw = "TITLE: T\nSUMMARY: S\nBENEFICIARY: NONE\nINSTRUCTIONS: Go.\nSPECIALISTS: NONE"
        let objective = CardPrepareRules.parseObjective(raw)
        XCTAssertEqual(objective["beneficiary"], "")
        XCTAssertNil(CardPrepareRules.objectiveRefusal(objective))
    }

    func testOverLongObjectiveIsRefused() {
        let objective = ["beneficiary": String(repeating: "x", count: 201)]
        XCTAssertEqual(CardPrepareRules.objectiveRefusal(objective),
                       "Dark Army's who benefits was longer than 200 characters — not usable")
    }

    func testFolderMatchesExactThenUniqueBasename() {
        XCTAssertEqual(CardPrepareRules.parseFolder("FOLDER: alpha", roots: roots), "/Users/x/Code/alpha")
        XCTAssertEqual(CardPrepareRules.parseFolder("FOLDER: /USERS/X/CODE/BETA", roots: roots), "/Users/x/Code/beta")
        XCTAssertEqual(CardPrepareRules.parseFolder("FOLDER: beta", roots: roots), "")
        XCTAssertEqual(CardPrepareRules.parseFolder("FOLDER: /nope", roots: roots), "")
        XCTAssertEqual(CardPrepareRules.parseFolder("FOLDER: NONE", roots: roots), "")
    }

    func testTitleRefusals() {
        XCTAssertEqual(CardPrepareRules.titleRefusal(""),
                       "Dark Army did not write a title — press Prepare again, or type one")
        XCTAssertNotNil(CardPrepareRules.titleRefusal(Array(repeating: "w", count: 15).joined(separator: " ")))
        XCTAssertNotNil(CardPrepareRules.titleRefusal(String(repeating: "t", count: 81)))
        XCTAssertNil(CardPrepareRules.titleRefusal("A fine title"))
    }

    func testSpecialistsAreCanonicalisedAndParentheticalsDropped() {
        let raw = "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Go.\nSPECIALISTS:\n- BC-Implementer (build)\n- bc-nobody\n- bc-verifier checks it\n- bc-implementer"
        XCTAssertEqual(CardPrepareRules.parseIdea(raw, roster: roster).stages,
                       ["bc-implementer", "bc-verifier"])
    }

    func testMissingInstructionsReadsAsNothing() {
        let read = CardPrepareRules.parseIdea("TITLE: T\nSUMMARY: S\n", roster: roster)
        XCTAssertEqual(read.title, "")
        XCTAssertEqual(read.prompt, "")
        XCTAssertNotNil(CardPrepareRules.promptRefusal(read.prompt))
    }

    func testPromptWithFewerThanTwoRootsHasNoFolderBlock() {
        let prompt = CardPrepareRules.promptForIdea(
            modeHead: PreparerBrief.modeHeadIdea, idea: "  make   it  ", tool: "claude",
            project: "alpha", roster: roster, roots: ["/only"], areas: [])
        XCTAssertFalse(prompt.contains("FOLDER:"))
        XCTAssertTrue(prompt.contains("IDEA: make it\n"))
        XCTAssertTrue(prompt.hasPrefix(PreparerBrief.modeHeadIdea))
    }

    func testPhoneRosterLeavesThePreparerOut() {
        let roster = PhonePreparer.roster(from: Array(Specialists.table.keys))
        XCTAssertFalse(roster.contains("bc-card-preparer"))
        XCTAssertEqual(roster, roster.sorted())
        XCTAssertTrue(roster.contains("bc-security-reviewer"))
    }
}
