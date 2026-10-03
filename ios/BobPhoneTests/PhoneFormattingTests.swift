import XCTest
@testable import BobPhone

/// What the phone actually says and shows, checked rather than spelled: the
/// summary lines under a piece of work, the number of agents a project may
/// run at once, a stored id list back into ids, and which face belongs to
/// which agent.
///
/// Seam: values decoded from JSON literals, and plain structs.
@MainActor
final class PhoneFormattingTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    // MARK: - one file's counts

    func testGPT6CatalogueKeepsRawOptionsAndOnlyAstraHasASpecialLabel() throws {
        let board = try decode(Board.self,
            #"{"models":{"codex":["gpt-6-astra","gpt-6-sol","gpt-6-luna","gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna","gpt-5.5"]}}"#)
        let options = board.modelOptions(for: "codex")
        XCTAssertEqual(options, ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                                 "gpt-5.6-sol", "gpt-5.6-terra",
                                 "gpt-5.6-luna", "gpt-5.5"])
        XCTAssertEqual(options.map(Board.modelLabel),
                       ["Astra 6", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"])
        for model in ["", "opus", "grok-4.6", "future-model", "GPT-6-Astra"] {
            XCTAssertEqual(Board.modelLabel(model), model)
        }
        XCTAssertTrue(board.modelOptions(for: "").isEmpty)
        XCTAssertTrue(board.modelOptions(for: "unknown-provider").isEmpty)
        for json in [#"{}"#, #"{"models":{}}"#, #"{"models":{"codex":[]}}"#] {
            let older = try decode(Board.self, json)
            XCTAssertTrue(older.modelOptions(for: "codex").isEmpty)
        }
    }

    func testAstraCardDecodesTheRawSelectionAndOlderCardsKeepDefault() throws {
        let card = try decode(BoardCard.self,
            #"{"id":"astra","tool":"codex","model":"gpt-6-astra"}"#)
        XCTAssertEqual(card.model, "gpt-6-astra")
        XCTAssertEqual(Board.modelLabel(card.model), "Astra 6")
        let older = try decode(BoardCard.self, #"{"id":"older","tool":"codex"}"#)
        XCTAssertEqual(older.model, "")
    }

    func testBinaryWinsOverEverything() throws {
        let file = try decode(
            WorkRecordFile.self,
            #"{"path": "a.png", "binary": true, "new": true, "added": 3, "removed": 1}"#)
        XCTAssertEqual(WorkRecordFormat.counts(file), "binary")
    }

    func testCountlessFilesSayNewOrNothingAtAll() throws {
        let fresh = try decode(WorkRecordFile.self,
                               #"{"path": "a.swift", "new": true}"#)
        XCTAssertEqual(WorkRecordFormat.counts(fresh), "new")
        let old = try decode(WorkRecordFile.self, #"{"path": "a.swift"}"#)
        XCTAssertEqual(WorkRecordFormat.counts(old), "—")
    }

    func testCountedFilesUseTheMinusSignAndNotAHyphen() throws {
        let fresh = try decode(
            WorkRecordFile.self,
            #"{"path": "a.swift", "new": true, "added": 3, "removed": 1}"#)
        XCTAssertEqual(WorkRecordFormat.counts(fresh), "new · +3 \u{2212}1")
        let old = try decode(
            WorkRecordFile.self,
            #"{"path": "a.swift", "added": 3, "removed": 1}"#)
        XCTAssertEqual(WorkRecordFormat.counts(old), "+3 \u{2212}1")
        XCTAssertFalse(WorkRecordFormat.counts(old).contains("-"))
    }

    // MARK: - the record's own line

    func testAnUnavailableRecordShowsTheMacsReasonVerbatim() throws {
        let record = try decode(
            WorkRecord.self,
            #"{"files_available": false, "files_reason": "not a git repository"}"#)
        XCTAssertEqual(WorkRecordFormat.summary(record), "not a git repository")
    }

    func testNothingChangedSaysSo() throws {
        let record = try decode(WorkRecord.self, #"{"files_available": true}"#)
        XCTAssertEqual(WorkRecordFormat.summary(record), "No files changed.")
    }

    func testOneFileIsSingularAndTwoArePlural() throws {
        let one = try decode(
            WorkRecord.self,
            #"{"files_available": true, "files_total": 1, "files_changed": 1, "lines_added": 2, "lines_removed": 0}"#)
        XCTAssertEqual(WorkRecordFormat.summary(one),
                       "1 file changed, +2 \u{2212}0.")
        let two = try decode(
            WorkRecord.self,
            #"{"files_available": true, "files_total": 2, "files_changed": 2, "lines_added": 2, "lines_removed": 1}"#)
        XCTAssertEqual(WorkRecordFormat.summary(two),
                       "2 files changed, +2 \u{2212}1.")
    }

    func testAShortenedFileListAppendsHowManyAreListed() throws {
        let clipped = try decode(
            WorkRecord.self,
            #"{"files_available": true, "files_total": 9, "files_changed": 4, "lines_added": 2, "lines_removed": 1}"#)
        XCTAssertEqual(WorkRecordFormat.summary(clipped),
                       "9 files changed, +2 \u{2212}1 · 4 listed.")
    }

    // MARK: - the verdict

    func testTheThreeKnownVerdictsHaveSentencesAndAnUnknownOneHasNone() {
        XCTAssertFalse(WorkRecordFormat.verdictWords("closed").isEmpty)
        XCTAssertFalse(WorkRecordFormat.verdictWords("manual").isEmpty)
        XCTAssertFalse(WorkRecordFormat.verdictWords("quiet").isEmpty)
        // A verdict this build does not know draws nothing, never an
        // invented line.
        XCTAssertEqual(WorkRecordFormat.verdictWords("something-new"), "")
        XCTAssertEqual(WorkRecordFormat.verdictWords(""), "")
    }

    // MARK: - how many may run at once

    private func card(_ json: String) throws -> BoardCard {
        try decode(BoardCard.self, json)
    }

    func testTheFirstCardWithAResolvedLimitWins() throws {
        let cards = [
            try card(#"{"id": "a", "parallel_limit": 0}"#),
            try card(#"{"id": "b", "parallel_limit": 3}"#),
            try card(#"{"id": "c", "parallel_limit": 2}"#),
        ]
        XCTAssertEqual(Board.resolvedLimit(cards: cards, fallback: 1), 3)
    }

    func testTheScalarIsUsedOnlyWhereEveryCardReadsZero() throws {
        let cards = [
            try card(#"{"id": "a"}"#),
            try card(#"{"id": "b"}"#),
        ]
        XCTAssertEqual(Board.resolvedLimit(cards: cards, fallback: 2), 2)
        // Floored at one: a fallback of zero is still a project that may run
        // one agent, never a denominator of nothing.
        XCTAssertEqual(Board.resolvedLimit(cards: cards, fallback: 0), 1)
        XCTAssertEqual(Board.resolvedLimit(cards: [], fallback: 0), 1)
    }

    func testThePipelineRootIsTheFirstCardThatNamesOne() throws {
        let cards = [
            try card(#"{"id": "a", "root": ""}"#),
            try card(#"{"id": "b", "root": "/a/proj"}"#),
            try card(#"{"id": "c", "root": "/b/other"}"#),
        ]
        XCTAssertEqual(Board.pipelineRoot(cards: cards), "/a/proj")
        XCTAssertEqual(
            Board.pipelineRoot(cards: [try card(#"{"id": "a"}"#)]), "")
    }

    func testTheRunHeadingIsAbsentWhereNothingIsRunningOrQueued() {
        XCTAssertFalse(Board.showsRunHeading(running: 0, queued: 0))
        XCTAssertTrue(Board.showsRunHeading(running: 1, queued: 0))
        XCTAssertTrue(Board.showsRunHeading(running: 0, queued: 1))
    }

    // MARK: - a stored id list

    func testIdsSplitOnCommasOrNewlinesAndTidyAsTheStoreDoes() {
        XCTAssertEqual(BoardCard.ids("a,b,c"), ["a", "b", "c"])
        XCTAssertEqual(BoardCard.ids("a\nb\nc"), ["a", "b", "c"])
        XCTAssertEqual(BoardCard.ids("  a , b  "), ["a", "b"])
        XCTAssertEqual(BoardCard.ids("b,a,b"), ["b", "a"])
        XCTAssertEqual(BoardCard.ids(""), [])
        XCTAssertEqual(BoardCard.ids(" , "), [])
    }

    // MARK: - who is this

    private func agent(nickname: String, sessionId: String) throws -> Agent {
        try decode(
            Agent.self,
            #"{"nickname": "\#(nickname)", "session_id": "\#(sessionId)"}"#)
    }

    func testARosterNicknameWearsItsOwnFace() throws {
        XCTAssertEqual(
            Cast.character(for: try agent(nickname: "Cipher", sessionId: "s")),
            "cipher")
        XCTAssertEqual(
            Cast.character(for: try agent(nickname: "Nyx", sessionId: "s")),
            "nyx")
    }

    func testAnOverflowNicknameWearsItsStemsFace() throws {
        XCTAssertEqual(
            Cast.character(for: try agent(nickname: "Cipher-ab12",
                                          sessionId: "s")),
            "cipher")
    }

    func testANameOffTheRosterFallsToAStableHashedFace() throws {
        let stranger = try agent(nickname: "Nobody", sessionId: "sess-alpha-01")
        let slug = Cast.character(for: stranger)
        XCTAssertTrue(Cast.names.map { $0.lowercased() }.contains(slug), slug)
        XCTAssertEqual(slug, Cast.character(for: stranger))
    }

    func testTheHashAgreesWithTheDaemonsOwnAnswer() throws {
        // Regenerated 9 Sep 2026 for twenty names, by running
        // `host/dark_army_daemon/cast.character_for` here, and hardcoded,
        // in the direction `host/tests/test_cast.py` establishes. CLAUDE.md
        // requires this rung match the panel's "rung for rung" — `&*` / `&+`
        // are load-bearing, and a plain `*` traps or wraps differently, so
        // one session would wear two faces.
        let golden: [(String, String)] = [
            ("sess-alpha-01", "canon"),
            ("9f3c7d2e-0000-4444-8888-aabbccddeeff", "zosia"),
            ("x", "velvet"),
        ]
        for (sessionId, expected) in golden {
            let row = try agent(nickname: "Nobody", sessionId: sessionId)
            XCTAssertEqual(Cast.character(for: row), expected, sessionId)
        }
    }

    func testPtysNameAndPackagedPortrait() throws {
        for nickname in ["Ptys", "PTYS", "Ptys-ab12"] {
            XCTAssertEqual(
                Cast.character(for: try agent(nickname: nickname, sessionId: "s")),
                "ptys")
        }
        XCTAssertEqual(CrewBand.display("ptys"), "Ptys")
        XCTAssertTrue(Areas.all.first { $0.slug == "pocket" }?.pool.contains("ptys") == true)
        XCTAssertNotNil(Cast.portrait("ptys"))
    }


    // MARK: - the project chip a person picked

    func testAProjectThatIsGoneFallsBackToAll() {
        XCTAssertEqual(
            FleetProjects.resolve(selected: "gone", in: ["alpha", "beta"], heard: true), "")
    }

    func testAProjectStillListedIsKept() {
        XCTAssertEqual(
            FleetProjects.resolve(selected: "alpha", in: ["alpha", "beta"], heard: true),
            "alpha")
    }

    func testAllIsNeverOverridden() {
        XCTAssertEqual(
            FleetProjects.resolve(selected: "", in: ["alpha"], heard: true), "")
    }

    func testHavingHeardNothingIsNoOpinionRatherThanAVanishedProject() {
        // Offline, mid transport switch, or the first frame before the first
        // snapshot: none of those may erase a choice somebody made.
        XCTAssertEqual(
            FleetProjects.resolve(selected: "alpha", in: [], heard: false), "alpha")
    }

    func testRowsWithNoProjectStillForgetAVanishedChoice() {
        // Rows exist and every one carries an empty `project` — a real
        // published state, which the Mac draws under `Other`. An empty name
        // list is not the same question as "the phone has heard nothing", and
        // a stale choice must still fall back to `ALL` here or the lists
        // empty with no chip lit.
        XCTAssertEqual(
            FleetProjects.resolve(selected: "alpha", in: [], heard: true), "")
    }

    func testProjectNamesAreUniqueNonEmptyAndSorted() {
        XCTAssertEqual(FleetProjects.names(["beta", "alpha", "", "alpha"]), ["alpha", "beta"])
    }

    // MARK: - the card timeline

    /// The same fixture table the Mac's `CardTimelineTests` and the daemon's
    /// `test_card_timeline.py` run: the phone copy of `CardTimeline` is
    /// byte-pinned to the panel's, and this is the check that the pin holds
    /// something that works.
    func testTimelineElapsedMatchesTheSharedTable() {
        let table: [(Double?, String)] = [
            (0, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"),
            (7800, "2h 10m"), (86400, "1d"), (100800, "1d 4h"), (nil, ""),
        ]
        for (seconds, want) in table {
            XCTAssertEqual(CardTimeline.elapsed(seconds), want, "\(String(describing: seconds))")
        }
    }

    /// The on-open read carries the timeline beside the plan; an older Mac
    /// sends no key and the field is nil, never a decode failure.
    func testCardFullDecodesWithAndWithoutATimeline() throws {
        let older = try decode(CardFull.self, #"{"available": true, "cards": []}"#)
        XCTAssertNil(older.timeline)
        let newer = try decode(CardFull.self, """
        {"available": true, "cards": [],
         "timeline": {"available": true, "generated_at": 1000,
                      "steps": [{"kind": "created", "label": "Written down",
                                 "at": 100, "observed": true},
                                {"kind": "plan_attached", "label": "Plan attached",
                                 "observed": false, "gap": "not observed"}],
                      "open": {"kind": "planned",
                               "label": "Planned, waiting to be started"}}}
        """)
        let report = try XCTUnwrap(newer.timeline)
        let rows = CardTimeline.rows(report)
        XCTAssertEqual(rows.map(\.label), ["Written down", "Plan attached"])
        XCTAssertNil(rows[0].gap)
        XCTAssertEqual(rows[1].when, CardTimeline.notObserved)
        XCTAssertEqual(rows[1].gap, "not observed")
        XCTAssertEqual(CardTimeline.openLine(report, now: report.generatedAt),
                       "Planned, waiting to be started · not observed")
    }

    func testAreaFieldsKeepMissingDefaultsAndValidValues() throws {
        let older = try decode(Snapshot.self, "{}")
        XCTAssertFalse(older.board.areasSupported)
        XCTAssertEqual(try decode(BoardCard.self, "{}").area, "")
        XCTAssertEqual(try decode(Agent.self, "{}").areaLine, "")
        let valid = try decode(Snapshot.self,
            #"{"board":{"areas_supported":true,"cards":[{"area":"desk","title":17}]},"agents":{"running":[{"area":"desk","area_line":"Vex · Desk lead"}]}}"#)
        XCTAssertTrue(valid.board.areasSupported)
        XCTAssertEqual(valid.board.cards.first?.area, "desk")
        XCTAssertEqual(valid.board.cards.first?.title, "") // old wrong-type fallback stays
        XCTAssertEqual(valid.agents.running.first?.areaLine, "Vex · Desk lead")
    }

    func testAreaWrongTypesRejectTheWholeSnapshot() throws {
        for wrong in ["null", "12", "true", "[]", "{}"] {
            XCTAssertThrowsError(try decode(Snapshot.self,
                "{\"board\":{\"cards\":[{\"area\":\(wrong)}]}}"))
            for bucket in ["running", "sleeping", "waiting", "abandoned", "finished"] {
                for key in ["area", "area_line"] {
                    XCTAssertThrowsError(try decode(Snapshot.self,
                        "{\"agents\":{\"\(bucket)\":[{\"\(key)\":\(wrong)}]}}"))
                }
            }
        }
        for wrong in ["null", "1", "0", "\"true\"", "[]", "{}"] {
            XCTAssertThrowsError(try decode(Snapshot.self,
                "{\"board\":{\"areas_supported\":\(wrong)}}"))
        }
    }

    func testAreaDirectRowsAndRawRepliesRejectWrongTypes() throws {
        XCTAssertThrowsError(try decode(BoardCard.self, #"{"area":null}"#))
        XCTAssertThrowsError(try decode(Agent.self, #"{"area_line":false}"#))
        XCTAssertThrowsError(try decode(Board.self, #"{"areas_supported":1}"#))
        for raw in [#"{"suggested_area":null}"#, #"{"suggested_area":1}"#,
                    #"{"current":{"area":false}}"#] {
            XCTAssertFalse(AreaWireFields.accepts(Data(raw.utf8)))
        }
        XCTAssertTrue(AreaWireFields.accepts(Data(#"{"suggested_area":"desk"}"#.utf8)))
        XCTAssertTrue(AreaWireFields.accepts(Data(#"{"current":{"revision":2}}"#.utf8)))
    }

    func testAreaCardPagesAndPrepareRejectWrongTypes() throws {
        let raw = #"{"cards":[{"area":null}]}"#
        XCTAssertThrowsError(try decode(CardFull.self, raw))
        XCTAssertThrowsError(try decode(CardSyncPage.self, raw))
        XCTAssertThrowsError(try decode(DoneArchivePage.self, raw))
        for value in ["null", "1", "true", "[]", "{}"] {
            let data = Data("{\"ok\":true,\"suggested_area\":\(value)}".utf8)
            XCTAssertFalse(PhonePrepareResult.parse(data: data, code: 200).ok)
        }
        XCTAssertTrue(PhonePrepareResult.parse(data: Data(#"{"ok":true}"#.utf8), code: 200).ok)
        let conflict = PhoneActionResult.parse(
            data: Data(#"{"detail":"this card changed on the Mac","current":{"area":null,"revision":9}}"#.utf8), code: 409)
        XCTAssertFalse(conflict.ok)
        XCTAssertNil(conflict.current)
        XCTAssertEqual(conflict.detail, "this card changed on the Mac")
    }
}


final class PhoneEffortCatalogueTests: XCTestCase {
    private func board(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    func testEffortOptionsNarrowByModelAndFallBackToTheToolsEntry() throws {
        let decoded = try board(
            #"{"efforts":{"codex":{"":["low","max"],"gpt-5.5":["low"]}}}"#)
        XCTAssertEqual(decoded.effortOptions(for: "codex", model: "gpt-5.5"), ["low"])
        XCTAssertEqual(decoded.effortOptions(for: "codex", model: ""), ["low", "max"])
        XCTAssertEqual(decoded.effortOptions(for: "codex", model: "gpt-6-sol"), ["low", "max"])
        XCTAssertTrue(decoded.effortOptions(for: "", model: "").isEmpty)
    }

    func testAnOlderMacSendsNoEffortsAndTheCardReadsDefault() throws {
        let older = try board(#"{"available": true}"#)
        XCTAssertTrue(older.effortOptions(for: "claude", model: "").isEmpty)
        let card = try JSONDecoder().decode(
            BoardCard.self, from: Data(#"{"id":"a","tool":"claude"}"#.utf8))
        XCTAssertEqual(card.effort, "")
    }
}
