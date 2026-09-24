import XCTest
@testable import BobPanel

/// What Dark Army observed a run do: tolerant decode of the headline, the record and
/// one file's diff, plus the pure formatting the sheet draws.
///
/// A separate file from `CardRunRecordTests`, which belongs to the WHAT RAN
/// *session* record — the two are different things about the same card and
/// keeping their tests apart is how they stay that way.
final class WorkRecordTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func record(_ json: String) throws -> WorkRecord {
        try JSONDecoder().decode(WorkRecord.self, from: Data(json.utf8))
    }

    // MARK: - absence

    func testAnAbsentRecordDecodesToNil() throws {
        // The load-bearing case: a card nobody has run must be distinguishable
        // from a run that changed nothing.
        let c = try card(#"{"id":"c1","title":"t"}"#)
        XCTAssertNil(c.workRecord)
        XCTAssertNil(c.workRecordFull)
    }

    func testAHeadlineDecodesOffTheCard() throws {
        let c = try card("""
            {"id":"c1","title":"t","work_record":{"verdict":"closed",
             "at":1700.5,"files":3,"files_total":9,"added":40,"removed":7,
             "report":true,"files_available":true}}
            """)
        let head = try XCTUnwrap(c.workRecord)
        XCTAssertEqual(head.verdict, "closed")
        XCTAssertEqual(head.at, 1700.5)
        XCTAssertEqual(head.files, 3)
        XCTAssertEqual(head.filesTotal, 9)
        XCTAssertEqual(head.added, 40)
        XCTAssertEqual(head.removed, 7)
        XCTAssertTrue(head.report)
        XCTAssertTrue(head.filesAvailable)
    }

    func testAPartialHeadlineDecodesWithoutThrowing() throws {
        // Swift's synthesized Decodable throws on a missing key even where the
        // property has a default; one absent field must never blank the board.
        let c = try card(#"{"id":"c1","work_record":{"verdict":"quiet"}}"#)
        let head = try XCTUnwrap(c.workRecord)
        XCTAssertEqual(head.verdict, "quiet")
        XCTAssertEqual(head.files, 0)
        XCTAssertFalse(head.filesAvailable)
    }

    func testAPartialRecordDecodesWithoutThrowing() throws {
        let rec = try record(#"{"verdict":"manual"}"#)
        XCTAssertEqual(rec.verdict, "manual")
        XCTAssertTrue(rec.files.isEmpty)
        XCTAssertNil(rec.recordedAt)
        XCTAssertFalse(rec.filesAvailable)
    }

    // MARK: - the record

    func testAWholeRecordDecodes() throws {
        let rec = try record("""
            {"card_id":"c1","run_at":100,"session_id":"s1","verdict":"closed",
             "report":"## Work done","summary":"did it",
             "files":[{"path":"a.py","added":3,"removed":1,"binary":false,
                       "new":false},
                      {"path":"logo.png","binary":true,"new":false},
                      {"path":"b.py","added":9,"removed":0,"binary":false,
                       "new":true}],
             "files_available":true,"files_reason":"","files_changed":3,
             "files_total":3,"lines_added":12,"lines_removed":1,
             "recorded_at":1700}
            """)
        XCTAssertEqual(rec.cardId, "c1")
        XCTAssertEqual(rec.report, "## Work done")
        XCTAssertEqual(rec.files.count, 3)
        XCTAssertEqual(rec.recordedAt, 1700)
        // A binary file's counts are absent, not zero.
        XCTAssertNil(rec.files[1].added)
        XCTAssertNil(rec.files[1].removed)
        XCTAssertTrue(rec.files[1].binary)
        XCTAssertTrue(rec.files[2].isNew)
    }

    func testTheReportWrapperStatesAvailability() throws {
        let report = try JSONDecoder().decode(
            WorkRecordReport.self,
            data: #"{"available":true,"record":null,"caption":"c"}"#)
        XCTAssertTrue(report.available)
        XCTAssertNil(report.record)
        XCTAssertEqual(report.caption, "c")
        let shut = try JSONDecoder().decode(
            WorkRecordReport.self, data: #"{"available":false}"#)
        XCTAssertFalse(shut.available)
        XCTAssertNil(shut.record)
    }

    func testADiffStatesItsTruncation() throws {
        let diff = try JSONDecoder().decode(
            WorkRecordDiff.self,
            data: #"{"available":true,"path":"a.py","new":true,"text":"@@","truncated":true}"#)
        XCTAssertTrue(diff.available)
        XCTAssertTrue(diff.isNew)
        XCTAssertTrue(diff.truncated)
        let refused = try JSONDecoder().decode(
            WorkRecordDiff.self,
            data: #"{"available":false,"reason":"could not read"}"#)
        XCTAssertFalse(refused.available)
        XCTAssertEqual(refused.reason, "could not read")
    }

    // MARK: - the shunt helper's figures

    func testTheHeadlineAndRecordCarryTheShuntKeys() throws {
        let c = try card("""
            {"id":"c1","work_record":{"verdict":"closed","shunt_delegations":3,
             "shunt_lines_kept_out":2140,"shunt_worker_cost_usd":0.04}}
            """)
        let head = try XCTUnwrap(c.workRecord)
        XCTAssertEqual(head.shuntDelegations, 3)
        XCTAssertEqual(head.shuntLinesKeptOut, 2140)
        XCTAssertEqual(head.shuntWorkerCostUsd, 0.04)
        let rec = try record("""
            {"verdict":"closed","shunt_delegations":3,"shunt_lines_kept_out":2140,
             "shunt_worker_cost_usd":null,
             "shunt_words":"3 delegations kept 2,140 lines out of the main model; helper cost unknown"}
            """)
        XCTAssertEqual(rec.shuntDelegations, 3)
        XCTAssertEqual(rec.shuntLinesKeptOut, 2140)
        // Unknown is nil, never zero: the daemon said it did not know.
        XCTAssertNil(rec.shuntWorkerCostUsd)
        XCTAssertEqual(rec.shuntWords,
                       "3 delegations kept 2,140 lines out of the main model; helper cost unknown")
    }

    func testAnOlderDaemonWithoutTheShuntKeysStillDecodes() throws {
        // A daemon from before the shunt skill sends none of the three keys;
        // the record must decode and read as "nothing delegated".
        let c = try card(#"{"id":"c1","work_record":{"verdict":"quiet","files":1}}"#)
        let head = try XCTUnwrap(c.workRecord)
        XCTAssertEqual(head.shuntDelegations, 0)
        XCTAssertEqual(head.shuntLinesKeptOut, 0)
        XCTAssertNil(head.shuntWorkerCostUsd)
        let rec = try record(#"{"verdict":"quiet"}"#)
        XCTAssertEqual(rec.shuntDelegations, 0)
        XCTAssertEqual(rec.shuntWords, "")
    }

    // MARK: - formatting

    func testCountsRenderAsPlusAndMinus() throws {
        let rec = try record("""
            {"files":[{"path":"a.py","added":3,"removed":1},
                      {"path":"logo.png","binary":true},
                      {"path":"b.py","added":9,"removed":0,"new":true},
                      {"path":"c.txt","new":true}]}
            """)
        XCTAssertEqual(WorkRecordFormat.counts(rec.files[0]), "+3 −1")
        // A binary row renders as a word, never as a pair of zeros.
        XCTAssertEqual(WorkRecordFormat.counts(rec.files[1]), "binary")
        XCTAssertEqual(WorkRecordFormat.counts(rec.files[2]), "new · +9 −0")
        XCTAssertEqual(WorkRecordFormat.counts(rec.files[3]), "new")
    }

    func testTheSummaryLineSaysWhatItKnows() throws {
        let whole = try record("""
            {"files_available":true,"files_changed":3,"files_total":3,
             "lines_added":12,"lines_removed":1}
            """)
        XCTAssertEqual(WorkRecordFormat.summary(whole),
                       "3 files changed, +12 −1.")
        let one = try record("""
            {"files_available":true,"files_changed":1,"files_total":1,
             "lines_added":2,"lines_removed":0}
            """)
        XCTAssertEqual(WorkRecordFormat.summary(one),
                       "1 file changed, +2 −0.")
        // The list is bounded; the count stays honest, and says so.
        let bounded = try record("""
            {"files_available":true,"files_changed":200,"files_total":431,
             "lines_added":9,"lines_removed":9}
            """)
        XCTAssertTrue(WorkRecordFormat.summary(bounded).contains("431 files"))
        XCTAssertTrue(WorkRecordFormat.summary(bounded).contains("200 listed"))
    }

    func testAnUnreadableFileListIsWordsNotAZero() throws {
        let rec = try record("""
            {"files_available":false,"files_reason":"Dark Army could not read it.",
             "files_changed":0,"files_total":0}
            """)
        XCTAssertEqual(WorkRecordFormat.summary(rec), "Dark Army could not read it.")
    }

    func testNoFilesChangedIsItsOwnSentence() throws {
        let rec = try record(#"{"files_available":true,"files_total":0}"#)
        XCTAssertEqual(WorkRecordFormat.summary(rec), "No files changed.")
    }
}

private extension JSONDecoder {
    func decode<T: Decodable>(_ type: T.Type, data json: String) throws -> T {
        try decode(type, from: Data(json.utf8))
    }
}
