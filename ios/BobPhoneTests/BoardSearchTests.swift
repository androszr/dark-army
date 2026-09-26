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

/// The Board tab scrolls without stalling: the state answer is read off the
/// main actor in one parse, and a poll that did not move a card redraws no
/// tile. See `StateFrame` (Models.swift), `BoardView.projectCards(in:)` and
/// `extension PhoneBoardCard: Equatable`.
final class BoardScrollTests: XCTestCase {
    private func card(_ id: String, column: String = "backlog",
                      extra: String = "") -> String {
        """
        {"id":"\(id)","title":"Card \(id)","summary":"what \(id) is for",
         "project":"dark-army","column":"\(column)","revision":3,
         "prompt":"\(String(repeating: "p", count: 300))"\(extra)}
        """
    }

    private func state(cards: [String], agents: String = "[]",
                       extra: String = "") -> Data {
        Data("""
        {"generated_at": 100, "state_digest": "d1",
         "agents": {"running": \(agents)},
         "board": {"available": true, "cards": [\(cards.joined(separator: ","))]},
         "usage": {"limits": {"bars": []}}\(extra)}
        """.utf8)
    }

    // MARK: StateFrame reads what applyState and takeUsage read

    func testAWholeAnswerDecodesThePictureTheAnswerAndTheUsageInOneFrame() throws {
        let body = state(cards: (0..<5).map { card("c\($0)") })
        let frame = StateFrame.decode(body)
        let direct = try JSONDecoder().decode(Snapshot.self, from: body)
        XCTAssertFalse(frame.answer.unchanged)
        XCTAssertEqual(frame.answer.stateDigest, "d1")
        let picture = try XCTUnwrap(frame.snapshot)
        XCTAssertEqual(picture.board.cards, direct.board.cards)
        XCTAssertEqual(picture.board.cards.count, 5)
        XCTAssertEqual(picture.generatedAt, 100)
        XCTAssertNotNil(frame.usage?.usage)
    }

    func testAnUnchangedAnswerNeverBecomesAPicture() {
        // `Snapshot` decodes anything: read as one, this body is a blank
        // board. The frame keeps `applyState`'s order and skips it.
        let frame = StateFrame.decode(Data(#"{"unchanged": true, "state_digest": "d1"}"#.utf8))
        XCTAssertTrue(frame.answer.unchanged)
        XCTAssertEqual(frame.answer.stateDigest, "d1")
        XCTAssertNil(frame.snapshot)
        XCTAssertNil(frame.usage?.usage)
    }

    func testAnUnreadableBodyIsNoPictureAndNoAnswer() {
        let frame = StateFrame.decode(Data("not json".utf8))
        XCTAssertNil(frame.snapshot)
        XCTAssertFalse(frame.answer.unchanged)
        XCTAssertNil(frame.usage)
    }

    func testTheSectionDeltaMarkersSurviveTheOneParse() {
        let frame = StateFrame.decode(Data("""
        {"generated_at": 5, "state_digest": "d2",
         "sections_unchanged": ["board"], "section_digests": {"board": "b1"}}
        """.utf8))
        XCTAssertEqual(frame.answer.sectionsUnchanged, ["board"])
        XCTAssertEqual(frame.answer.sectionDigests, ["board": "b1"])
        XCTAssertNotNil(frame.snapshot)
    }

    func testTheFrameIsReadOffTheMainThread() async {
        let body = state(cards: [card("a")])
        let onMain = await Task.detached { () -> Bool in
            _ = StateFrame.decode(body)
            return Thread.isMainThread
        }.value
        XCTAssertFalse(onMain)
        let frame = await StateFrame.offMain(body)
        XCTAssertEqual(frame.snapshot?.board.cards.map(\.id), ["a"])
    }

    // MARK: a poll that moved no card redraws no tile

    func testAnAgentsOnlyPollLeavesEveryTileEqual() throws {
        let cards = (0..<180).map { card("c\($0)", column: $0 % 2 == 0 ? "backlog" : "prep") }
        let before = try JSONDecoder().decode(Snapshot.self, from: state(cards: cards))
        // The next poll: a fresh parse of the same board, one agent more.
        let after = try JSONDecoder().decode(Snapshot.self, from: state(
            cards: cards,
            agents: #"[{"session_id": "s1", "project": "dark-army"}]"#))
        XCTAssertEqual(before.board.cards, after.board.cards)
        for (old, new) in zip(before.board.cards, after.board.cards) {
            XCTAssertEqual(PhoneBoardCard(card: old), PhoneBoardCard(card: new))
        }
    }

    func testACarriedBoardIsTheSameBoard() throws {
        let held = try JSONDecoder().decode(Snapshot.self,
                                            from: state(cards: [card("a"), card("b")]))
        var delta = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 101, "state_digest": "d3", "sections_unchanged": ["board"]}
        """.utf8))
        XCTAssertTrue(delta.carry("board", from: held))
        XCTAssertEqual(delta.board.cards, held.board.cards)
    }

    func testEveryDrawnInputStillRedrawsItsTile() throws {
        let base = try JSONDecoder().decode(BoardCard.self, from: Data(card("a").utf8))
        let tile = PhoneBoardCard(card: base)
        var moved = base
        moved.title = "Renamed"
        XCTAssertNotEqual(tile, PhoneBoardCard(card: moved))
        moved = base
        moved.linkState = "dispatching"
        XCTAssertNotEqual(tile, PhoneBoardCard(card: moved))
        moved = base
        moved.revision += 1
        XCTAssertNotEqual(tile, PhoneBoardCard(card: moved))
        XCTAssertNotEqual(tile, PhoneBoardCard(card: base, notice: "gave up"))
        XCTAssertNotEqual(tile, PhoneBoardCard(card: base, leaving: true))
        XCTAssertNotEqual(tile, PhoneBoardCard(card: base, live: ["verifier"]))
        XCTAssertNotEqual(tile, PhoneBoardCard(card: base, tick: .open))
        XCTAssertEqual(tile, PhoneBoardCard(card: base))
    }
}
