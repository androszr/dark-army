import XCTest
@testable import BobPhone

/// The composer's own pre-selection of the assistant and the project, and
/// the sentence PREPARE shows while it is held for want of one. These are
/// the rules behind the fix for a form opened before the Mac's project list
/// had landed: the project stayed empty for good and PREPARE was held with
/// nothing on screen saying why.
final class ComposerDefaultsTests: XCTestCase {
    func testEmptyListsMeanWaitNotNothing() {
        XCTAssertNil(ComposerDefaults.projectRoot(current: "", roots: [], lastRoot: ""))
        XCTAssertNil(ComposerDefaults.tool(current: "", tools: []))
        XCTAssertEqual(ComposerDefaults.holdReason(projectRoot: "", tool: "claude"),
                       ComposerDefaults.waitingForProjects)
    }

    func testListsArrivingLaterFillAnEmptyChoice() {
        XCTAssertEqual(ComposerDefaults.projectRoot(current: "", roots: ["/a", "/b"], lastRoot: ""), "/a")
        XCTAssertEqual(ComposerDefaults.tool(current: "", tools: ["claude", "codex"]), "claude")
    }

    func testLastUsedProjectWinsWhereStillOffered() {
        XCTAssertEqual(ComposerDefaults.projectRoot(current: "", roots: ["/a", "/b"], lastRoot: "/b"), "/b")
        XCTAssertEqual(ComposerDefaults.projectRoot(current: "", roots: ["/a", "/b"], lastRoot: "/gone"), "/a")
    }

    func testAChoiceIsNeverOverwritten() {
        XCTAssertNil(ComposerDefaults.projectRoot(current: "/b", roots: ["/a", "/b"], lastRoot: "/a"))
        XCTAssertNil(ComposerDefaults.projectRoot(current: "/stale", roots: ["/a"], lastRoot: ""))
        XCTAssertNil(ComposerDefaults.tool(current: "codex", tools: ["claude"]))
    }

    func testHoldReasonNamesTheAssistantNext() {
        XCTAssertEqual(ComposerDefaults.holdReason(projectRoot: "/a", tool: ""),
                       ComposerDefaults.noAssistant)
        XCTAssertNil(ComposerDefaults.holdReason(projectRoot: "/a", tool: "claude"))
    }
}
