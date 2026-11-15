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

    func testTheFetchKeyMovesWithTheColumn() {
        let open = WorktreeRows.fetchKey(
            cards: [(id: "a", column: "in_progress", mergeState: "", mergeLine: "")],
            listed: ["a"])
        let done = WorktreeRows.fetchKey(
            cards: [(id: "a", column: "done", mergeState: "", mergeLine: "")],
            listed: ["a"])
        XCTAssertNotEqual(open, done)
    }

    func testTheWords() {
        XCTAssertEqual(WorktreeRows.verb(count: 2), "MERGE 2")
        XCTAssertEqual(WorktreeRows.armedVerb(count: 2), "Merge 2 into main?")
        XCTAssertEqual(WorktreeRows.maximum, 8)
    }

    func testABranchWithNothingAheadIsNeverTickedOrReady() {
        XCTAssertFalse(WorktreeRows.tickable(
            rowMergeable: true, branchTip: "a1", card: facts(), mergeWritable: true,
            status: "nothing"))
        XCTAssertTrue(WorktreeRows.tickable(
            rowMergeable: true, branchTip: "a1", card: facts(), mergeWritable: true,
            status: "done"))
        XCTAssertEqual(WorktreeRows.group(status: "nothing", tickable: true), .finished)
        XCTAssertEqual(WorktreeRows.group(status: "nothing", tickable: false), .finished)
        for status in ["merged", "gone", "no_card"] {
            XCTAssertEqual(WorktreeRows.group(status: status, tickable: false), .finished)
        }
        XCTAssertEqual(WorktreeRows.group(status: "done", tickable: true), .ready)
        XCTAssertEqual(WorktreeRows.group(status: "conflict", tickable: false), .attention)
        XCTAssertEqual(WorktreeRows.group(status: "unreadable", tickable: false), .attention)
        XCTAssertEqual(WorktreeRows.group(status: "working", tickable: false), .notReady)
        XCTAssertEqual(WorktreeRows.group(status: "waiting", tickable: false), .notReady)
    }

    func testTheProjectFilter() {
        XCTAssertEqual(WorktreeRows.projectNames(["b", "", "a", "b"]), ["a", "b"])
        XCTAssertTrue(WorktreeRows.matches(project: "x", active: ""))
        XCTAssertTrue(WorktreeRows.matches(project: "", active: ""))
        XCTAssertTrue(WorktreeRows.matches(project: "x", active: "x"))
        XCTAssertFalse(WorktreeRows.matches(project: "y", active: "x"))
        XCTAssertEqual(WorktreeRows.counts(["a", "b", "a"]), ["a": 2, "b": 1])
        XCTAssertEqual(WorktreeRows.filterLine(project: "dark-army"),
                       "no dark-army worktrees · ALL shows every project")
    }

    func testOnlyVisibleTicksAreSent() {
        // The screen passes the filter's survivors as `listed`: a tick on a
        // hidden row is held but not sent.
        let visible = ["a", "c"]
        XCTAssertEqual(WorktreeRows.orderedIds(ticked: ["a", "b", "c"], listed: visible),
                       ["a", "c"])
    }

    func testDeleteIsDrawnOnlyWhereTheMacOffersIt() {
        XCTAssertTrue(WorktreeRows.removable(rowRemovable: true, status: "nothing", writable: true))
        XCTAssertTrue(WorktreeRows.removable(rowRemovable: true, status: "no_card", writable: true))
        XCTAssertFalse(WorktreeRows.removable(rowRemovable: true, status: "done", writable: true))
        XCTAssertFalse(WorktreeRows.removable(rowRemovable: true, status: "merged", writable: false))
        XCTAssertFalse(WorktreeRows.removable(rowRemovable: false, status: "merged", writable: true))
        XCTAssertEqual(WorktreeRows.removeVerb(armed: false), "DELETE")
        XCTAssertEqual(WorktreeRows.removeVerb(armed: true), "Delete this folder?")
    }

    func testTheRowDecodesRemovableTolerantly() throws {
        let json = #"{"card_id":"c","status":"nothing","removable":true}"#
        let row = try JSONDecoder().decode(WorktreeRow.self, from: Data(json.utf8))
        XCTAssertTrue(row.removable)
        let bare = try JSONDecoder().decode(WorktreeRow.self,
                                            from: Data(#"{"card_id":"c"}"#.utf8))
        XCTAssertFalse(bare.removable)
    }
}
