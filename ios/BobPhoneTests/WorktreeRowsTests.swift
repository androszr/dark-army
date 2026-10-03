import XCTest
@testable import BobPhone

/// The Worktrees screen's pure rule: which rows tick, the list's order and
/// the button's words. The same table `host/tests/test_phone_worktrees.py`
/// runs under `swiftc`.
final class WorktreeRowsTests: XCTestCase {
    private func facts(column: String = "done", branch: String = "card/abc",
                       state: String = "", manualDue: Bool = false,
                       offered: Bool = true) -> WorktreeRows.CardFacts {
        WorktreeRows.CardFacts(column: column, branch: branch, mergeState: state,
                               manualDue: manualDue, mergeOffered: offered)
    }

    private func tick(_ mergeable: Bool = true, tip: String = "a1",
                      card: WorktreeRows.CardFacts?, writable: Bool = true) -> Bool {
        WorktreeRows.tickable(rowMergeable: mergeable, branchTip: tip, card: card,
                              mergeWritable: writable)
    }

    func testADoneRowWithAnOfferedCardTicks() {
        XCTAssertTrue(tick(card: facts()))
        XCTAssertTrue(tick(card: facts(state: "conflict")))
    }

    func testRowsThatCannotMergeDoNotTick() {
        XCTAssertFalse(tick(false, card: facts()))
        XCTAssertFalse(tick(card: nil))
        XCTAssertFalse(tick(card: facts(state: "merging")))
        XCTAssertFalse(tick(card: facts(state: "queued")))
        XCTAssertFalse(tick(card: facts(offered: false)))
        XCTAssertFalse(tick(card: facts(manualDue: true)))
        XCTAssertFalse(tick(card: facts(column: "in_progress")))
        XCTAssertFalse(tick(tip: "", card: facts()))
        XCTAssertFalse(tick(card: facts(), writable: false))
    }

    func testTheOrderAndTheTipsFollowTheList() {
        let listed = ["c", "a", "b"]
        XCTAssertEqual(WorktreeRows.orderedIds(ticked: ["b", "c"], listed: listed),
                       ["c", "b"])
        let entries = [WorktreeRows.Entry(id: "a", branchTip: "ta", mergeable: true),
                       WorktreeRows.Entry(id: "b", branchTip: "tb", mergeable: true),
                       WorktreeRows.Entry(id: "c", branchTip: "tc", mergeable: true)]
        XCTAssertEqual(WorktreeRows.tips(for: ["c", "b"], entries: entries),
                       ["tc", "tb"])
    }

    func testTheWords() {
        XCTAssertEqual(WorktreeRows.verb(count: 2), "MERGE 2")
        XCTAssertEqual(WorktreeRows.armedVerb(count: 2), "Merge 2 into main?")
        XCTAssertEqual(WorktreeRows.maximum, 8)
    }
}
