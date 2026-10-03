import XCTest
@testable import BobPanel

/// The Review section's rules, tabled from the panel's side
/// (`host/tests/test_review_rules.py` runs the same functions under `swiftc`
/// and pins the two `ReviewRules.swift` copies byte-equal), and the section's
/// decode and its inbox entry.
final class ReviewRulesTests: XCTestCase {
    func testTheStateWords() {
        XCTAssertEqual(ReviewRules.stateWord("reviewing"), "reviewing")
        XCTAssertEqual(ReviewRules.stateWord("picks"), "pick the fixes")
        XCTAssertEqual(ReviewRules.stateWord("fixing"), "fixing and shipping")
        XCTAssertEqual(ReviewRules.stateWord("done"), "finished")
        XCTAssertEqual(ReviewRules.stateWord("exited"), "terminal ended")
        XCTAssertEqual(ReviewRules.stateWord("ended"), "ended")
        XCTAssertEqual(ReviewRules.stateWord("from-the-future"), "from-the-future")
    }

    func testStartNeedsASupportingMacTheLauncherAProjectAndAProvider() {
        XCTAssertTrue(ReviewRules.canStart(root: "/p", tool: "claude",
                                           supported: true, dispatchEnabled: true))
        XCTAssertFalse(ReviewRules.canStart(root: "", tool: "claude",
                                            supported: true, dispatchEnabled: true))
        XCTAssertFalse(ReviewRules.canStart(root: "/p", tool: "",
                                            supported: true, dispatchEnabled: true))
        XCTAssertFalse(ReviewRules.canStart(root: "/p", tool: "claude",
                                            supported: false, dispatchEnabled: true))
        XCTAssertFalse(ReviewRules.canStart(root: "/p", tool: "claude",
                                            supported: true, dispatchEnabled: false))
        XCTAssertEqual(ReviewRules.defaultProvider, "claude")
    }

    func testContinueAndEndFollowTheState() {
        XCTAssertTrue(ReviewRules.canContinue(state: "picks"))
        for state in ["reviewing", "fixing", "done", "exited", "ended"] {
            XCTAssertFalse(ReviewRules.canContinue(state: state), state)
        }
        XCTAssertEqual(
            ["reviewing", "picks", "fixing", "done", "exited", "ended"]
                .map { ReviewRules.showsEnd(state: $0) },
            [true, true, true, false, false, false])
        XCTAssertTrue(ReviewRules.picksEditable(state: "picks"))
        XCTAssertFalse(ReviewRules.picksEditable(state: "fixing"))
    }

    func testTheLinesAreTheDaemonsWordsInOneShape() {
        XCTAssertEqual(ReviewRules.findingLine(grade: "FIX", line: "x"), "FIX · x")
        XCTAssertEqual(ReviewRules.stepLine(label: "Commit", status: "", words: ""),
                       "Commit — waiting")
        XCTAssertEqual(ReviewRules.stepLine(label: "Commit", status: "done", words: "abc"),
                       "Commit — done: abc")
        XCTAssertEqual(ReviewRules.ledgerLine(id: "push", status: "failed", words: "no upstream"),
                       "push: failed — no upstream")
    }

    func testOnlyARunWaitingOnPicksMakesAnEntry() throws {
        let entry = try XCTUnwrap(ReviewRules.picksEntry(
            runId: "ab12cd34", state: "picks", scopeLine: "no remote branch, uncommitted changes only",
            findingCount: 4))
        XCTAssertEqual(entry.key, "r:ab12cd34")
        XCTAssertEqual(entry.detail,
                       "no remote branch, uncommitted changes only — 4 findings — pick the fixes")
        XCTAssertNil(ReviewRules.picksEntry(runId: "ab12cd34", state: "fixing",
                                            scopeLine: "", findingCount: 4))
        XCTAssertEqual(ReviewRules.fingerprintMaterial(runId: "ab12cd34", findingsAt: 1700.9),
                       "ab12cd34:1700")
    }

    func testTheSectionDecodesTolerantlyAndDropsOneBadFinding() throws {
        let json = #"""
        {"available": true, "runs": [
          {"id": "r1", "state": "picks", "scope_line": "s", "findings_at": 5.0,
           "findings": [{"index": 1, "grade": "FIX", "line": "a"}, "junk", {"index": 2}],
           "picks": [1, "x"], "steps": [{"id": "commit", "label": "Commit"}]},
          7
        ]}
        """#
        let section = try JSONDecoder().decode(ReviewSection.self, from: Data(json.utf8))
        XCTAssertTrue(section.available)
        XCTAssertEqual(section.runs.count, 1)
        XCTAssertEqual(section.runs[0].findings.map(\.index), [1, 2])
        XCTAssertEqual(section.runs[0].picks, [1])
        XCTAssertEqual(section.runs[0].steps.first?.label, "Commit")
        let absent = try JSONDecoder().decode(ReviewSection.self, from: Data("{}".utf8))
        XCTAssertFalse(absent.available)
    }

    func testARunWaitingOnPicksIsOneNeedsYouEntryAndItsPlainWaitIsNotAnother() throws {
        let section = ReviewSection(available: true, runs: [try JSONDecoder().decode(
            ReviewRun.self, from: Data(#"""
            {"id": "r1", "project": "p", "state": "picks", "session_id": "s1",
             "scope_line": "scope", "findings_at": 9.0,
             "findings": [{"index": 1, "grade": "FIX", "line": "a"}]}
            """#.utf8))])
        let waiting = SectionRow(
            agent: try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"s1"}"#.utf8)),
            category: .waiting)
        let items = Inbox.items(rows: [waiting], prompts: [], cards: [], review: section)
        XCTAssertEqual(items.map(\.wire), [.reviewPicks])
        XCTAssertEqual(items[0].target.key, "r:r1")
        XCTAssertEqual(InboxWireKind.reviewPicks.name, "review_picks")
        XCTAssertEqual(InboxWireKind.reviewPicks.kind, .answer)
        XCTAssertTrue(InboxWireKind.reviewPicks.dismissable)
        // Dismiss hides it until the findings arrive again.
        let fp = InboxFingerprint.value(wire: .reviewPicks, material: items[0].material)
        let hidden = Inbox.items(
            rows: [waiting], prompts: [], cards: [],
            acks: [InboxAckRecord(key: "r:r1", kind: "review_picks", fp: fp)],
            review: section)
        XCTAssertTrue(hidden.isEmpty)
    }

    func testAPlainWaitWhileTheRunIsFixingIsStillListed() throws {
        let section = ReviewSection(available: true, runs: [try JSONDecoder().decode(
            ReviewRun.self, from: Data(#"{"id": "r1", "state": "fixing", "session_id": "s1"}"#.utf8))])
        let waiting = SectionRow(
            agent: try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"s1"}"#.utf8)),
            category: .waiting)
        let items = Inbox.items(rows: [waiting], prompts: [], cards: [], review: section)
        XCTAssertEqual(items.map(\.wire), [.waiting])
        XCTAssertEqual(ReviewRules.coveredStates, ["picks", "done"])
    }
}
