import XCTest
@testable import BobPanel

/// The per-project work queue as the panel sees it: the plan-table contract
/// it shares with the daemon, the tolerant decode of the queue fields, the
/// grouping the board's headings read, and the two heading controls.
final class BoardQueueTests: XCTestCase {

    // MARK: - The contract, pinned at both ends

    /// **Byte-identical to `CONTRACT_TABLE` in
    /// `host/tests/test_board_workflow.py`.** `PlanStructure.parseFiles` and
    /// `board_workflow.parse_declared_files` are two implementations of one
    /// cell-level rule; a disagreement means the two sides are reading
    /// different plans. Change one end and the other end's test fails,
    /// which is the whole point of writing it out twice.
    private let contractTable = """
    # A plan

    - **Stages:** bc-implementer

    ## Files to change

    | File | Change |
    |---|---|
    | `host/dark_army_daemon/board.py` | schema 8 |
    | `panel/Sources/BobPanel/PanelView.swift` | the band |

    ## New files

    | File | Purpose |
    |---|---|
    | `host/dark_army_daemon/board_queue.py` | the policy |

    ## Out of scope

    Nothing here is a table.
    """

    func testTheContractTableParsesToTheFilesBothEndsAgreeOn() {
        XCTAssertEqual(PlanStructure.parseFiles(contractTable), [
            "host/dark_army_daemon/board.py",
            "panel/Sources/BobPanel/PanelView.swift",
        ])
        // The panel deliberately reads only `## Files to change`: the
        // diagram is about what the plan will change, and `## New files` is a
        // wider reading the daemon keeps for itself.
        XCTAssertFalse(PlanStructure.parseFiles(contractTable)
            .contains("host/dark_army_daemon/board_queue.py"))
    }

    // MARK: - Tolerant decode

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    private func boardWith(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    /// Swift's synthesized `Decodable` throws on a missing key even where the
    /// property has a default, so one absent field would blank the whole
    /// board — the trap `Models.swift` documents.
    func testAbsentQueueFieldsDecodeToNotQueued() throws {
        let row = try card(#"{"id":"a","title":"t"}"#)
        XCTAssertEqual(row.queueState, "")
        XCTAssertNil(row.queuedAt)
        XCTAssertNil(row.queueRank)
        XCTAssertFalse(row.isQueued)
    }

    func testQueueFieldsDecodeWhenPresent() throws {
        let row = try card(#"{"id":"a","title":"t","queue_state":"queued","queued_at":12.5}"#)
        XCTAssertTrue(row.isQueued)
        XCTAssertEqual(row.queuedAt, 12.5)
        XCTAssertNil(row.queueRank)
    }

    func testQueueRankDecodesWhenPresentAndNilWhenJsonNull() throws {
        let ranked = try card(
            #"{"id":"a","title":"t","queue_state":"queued","queued_at":30,"queue_rank":9.0}"#)
        XCTAssertEqual(ranked.queueRank, 9.0)
        let nully = try card(
            #"{"id":"a","title":"t","queue_state":"queued","queued_at":12.5,"queue_rank":null}"#)
        XCTAssertNil(nully.queueRank)
        XCTAssertEqual(nully.queuedAt, 12.5)
    }

    /// **False, not true.** A decode that treated absence as truncated would
    /// hold Save on every card.
    func testSummaryTruncatedDecodesWhenPresentAndFalseWhenAbsent() throws {
        XCTAssertTrue(try card(#"{"id":"a","title":"t","summary_truncated":true}"#)
            .summaryTruncated)
        XCTAssertFalse(try card(#"{"id":"a","title":"t"}"#).summaryTruncated)
    }

    /// **False, not true.** A queued card must never say "Dark Army will start it"
    /// on a build that will not.
    func testAutostartDefaultsToFalse() throws {
        XCTAssertFalse(try boardWith(#"{"available":true}"#).autostartEnabled)
        XCTAssertTrue(
            try boardWith(#"{"available":true,"autostart_enabled":true}"#)
                .autostartEnabled)
    }

    // MARK: - Grouping

    private func queued(_ id: String, _ project: String, at when: Double?,
                        rank: Double? = nil) -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = id
        c.project = project
        c.queueState = "queued"
        c.queuedAt = when
        c.queueRank = rank
        return c
    }

    private func running(_ id: String, _ project: String, _ state: String) -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = id
        c.project = project
        c.linkState = state
        return c
    }

    private func banked(_ id: String, project: String = "bob",
                        column: String = "backlog") -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = id
        c.project = project
        c.column = column
        return c
    }

    func testTheQueueIsOrderedByStampThenId() {
        var board = Board()
        board.cards = [queued("c", "bob", at: 20), queued("a", "bob", at: 10),
                       queued("b", "bob", at: 10)]
        // The id breaks the tie because two enqueues inside one clock tick
        // must still have *an* order — the daemon's drain sorts the same way.
        XCTAssertEqual(board.queued(in: "bob").map(\.id), ["a", "b", "c"])
    }

    func testTheQueueIsOrderedByTheCoalescedKey() {
        var board = Board()
        board.cards = [
            queued("c", "bob", at: 30, rank: 9.0),
            queued("a", "bob", at: 10),
            queued("b", "bob", at: 10),
        ]
        XCTAssertEqual(board.queued(in: "bob").map(\.id), ["c", "a", "b"])
    }

    func testTheQueueIsScopedToOneProject() {
        var board = Board()
        board.cards = [queued("a", "bob", at: 1), queued("b", "other", at: 2)]
        XCTAssertEqual(board.queued(in: "bob").map(\.id), ["a"])
        XCTAssertEqual(board.queued(in: "other").map(\.id), ["b"])
        XCTAssertTrue(board.queued(in: "nobody").isEmpty)
    }

    /// The heading holds against the same two link states the daemon's gate
    /// does, so the picture and the gate agree by construction. `ended`
    /// holds nothing.
    func testOnlyStartingAndRunningCardsCountAsRunning() {
        var board = Board()
        board.cards = [running("live", "bob", "live"),
                       running("disp", "bob", "dispatching"),
                       running("done", "bob", "ended"),
                       running("idle", "bob", ""),
                       running("far", "other", "live")]
        XCTAssertEqual(Set(board.running(in: "bob").map(\.id)), ["live", "disp"])
    }

    func testBacklogIsThisProjectsBacklogColumnAndNothingElse() {
        var board = Board()
        board.cards = [banked("mine"),
                       banked("theirs", project: "arpg-web"),
                       banked("prep", column: "prep"),
                       banked("running", column: "in_progress"),
                       banked("done", column: "done")]
        XCTAssertEqual(board.backlog(in: "bob").map(\.id), ["mine"])
    }

    /// The snapshot's own order is the store's — the order the board draws
    /// and the order the press walks — so the filter must not re-sort.
    func testBacklogKeepsTheSnapshotsOrder() {
        var board = Board()
        board.cards = [banked("c"), banked("a"), banked("b")]
        XCTAssertEqual(board.backlog(in: "bob").map(\.id), ["c", "a", "b"])
    }

    // MARK: - The parallel dial

    /// A zero would render "RUN 2/0" — a readout stating a rule nothing
    /// obeys. Both resolve to 1.
    func testTheParallelLimitIsToleratedAndFloored() throws {
        XCTAssertEqual(try boardWith("{\"cards\":[]}").parallelLimit, 1)
        XCTAssertEqual(
            try boardWith("{\"cards\":[],\"parallel_limit\":0}").parallelLimit, 1)
        XCTAssertEqual(
            try boardWith("{\"cards\":[],\"parallel_limit\":3}").parallelLimit, 3)
    }

    /// The per-project figure, published per card. **0 means unstated** and
    /// must stay distinguishable from a real limit of 1, because it is what
    /// sends the heading to the scalar.
    func testAPerCardParallelLimitIsToleratedAndSentinelled() throws {
        let with = try boardWith(
            "{\"cards\":[{\"id\":\"a\",\"parallel_limit\":3}]}")
        XCTAssertEqual(with.cards.first?.parallelLimit, 3)
        let without = try boardWith("{\"cards\":[{\"id\":\"a\"}]}")
        XCTAssertEqual(without.cards.first?.parallelLimit, 0)
        let zeroed = try boardWith(
            "{\"cards\":[{\"id\":\"a\",\"parallel_limit\":-2}]}")
        XCTAssertEqual(zeroed.cards.first?.parallelLimit, 0)
    }

    /// Tick-state only, and absent must never decode as anything but empty.
    func testTheOverrideMapIsTolerated() throws {
        XCTAssertEqual(try boardWith("{\"cards\":[]}").parallelOverrides, [:])
        XCTAssertEqual(
            try boardWith(
                "{\"cards\":[],\"parallel_overrides\":{\"/a/proj\":3}}")
                .parallelOverrides,
            ["/a/proj": 3])
    }

    /// The heading picks the daemon's answer up; it never computes one. The
    /// scalar is reached only where every card reads the sentinel.
    func testTheHeadingPrefersThePerCardFigureAndFallsBackToTheScalar() throws {
        let newer = try boardWith("""
        {"cards":[{"id":"a","root":"/a/proj","parallel_limit":3}],
         "parallel_limit":1}
        """)
        XCTAssertEqual(
            BoardProjectControls.resolvedLimit(cards: newer.cards,
                                               fallback: newer.parallelLimit), 3)
        XCTAssertEqual(BoardProjectControls.projectRoot(cards: newer.cards),
                       "/a/proj")

        let older = try boardWith("""
        {"cards":[{"id":"a","root":"/a/proj"}],"parallel_limit":2}
        """)
        XCTAssertEqual(
            BoardProjectControls.resolvedLimit(cards: older.cards,
                                               fallback: older.parallelLimit), 2)

        // Nothing to aim a press at, and nothing to draw: floored, never zero.
        XCTAssertEqual(BoardProjectControls.resolvedLimit(cards: [], fallback: 0), 1)
        XCTAssertEqual(BoardProjectControls.projectRoot(cards: []), "")
    }

    func testTheRunHeadingIsACountOverALimit() {
        XCTAssertEqual(BoardProjectControls.runHeading(count: 2, limit: 3), "RUN 2/3")
        XCTAssertEqual(BoardProjectControls.runHeading(count: 0, limit: 1), "RUN 0/1")
        // The same floor the decode applies, restated where a zero would be
        // read as a rule rather than as a missing field.
        XCTAssertEqual(BoardProjectControls.runHeading(count: 1, limit: 0), "RUN 1/1")
    }

    /// The heading answers two questions — spare capacity, and why the queue
    /// waits — so it is drawn exactly where one of them exists.
    func testTheHeadingIsDrawnOnlyWithRunningOrQueuedWork() {
        XCTAssertTrue(BoardProjectControls.showsRunHeading(running: 1, queued: 0))
        XCTAssertTrue(BoardProjectControls.showsRunHeading(running: 0, queued: 1))
        XCTAssertFalse(BoardProjectControls.showsRunHeading(running: 0, queued: 0))
    }

    // MARK: - The queued card's line

    /// The daemon's sentence wins outright: it is composed against a count the
    /// panel cannot see, and it is the same string the press was refused with.
    func testAPublishedQueueReasonIsShownVerbatim() throws {
        let board = try boardWith("""
        {"cards":[{"id":"q","project":"bob","queue_state":"queued",
                   "queued_at":1.0,
                   "queue_reason":"Queued — Dark Army will start it when one of the 2 agents working in this project finishes"}]}
        """)
        let card = try XCTUnwrap(board.cards.first)
        XCTAssertEqual(
            card.queuedLine(autostart: true),
            "Queued — Dark Army will start it when one of the 2 agents working in "
                + "this project finishes")
        XCTAssertEqual(card.queuedLine(autostart: false),
                       card.queuedLine(autostart: true))
    }

    /// Without one the panel draws the plain promise. Nothing is composed
    /// here: the count that explains the wait lives on the daemon.
    func testWithNoPublishedReasonTheLineIsThePlainPromise() throws {
        let board = try boardWith("""
        {"cards":[{"id":"q","project":"bob","queue_state":"queued",
                   "queued_at":1.0}]}
        """)
        let card = try XCTUnwrap(board.cards.first)
        XCTAssertEqual(card.queuedLine(autostart: true),
                       "Queued — Dark Army will start it")
        XCTAssertEqual(card.queuedLine(autostart: false),
                       "Queued — press Start")
    }

    /// A daemon still sending the retired holder key decodes cleanly and the
    /// key changes nothing.
    func testAStaleQueueHolderKeyIsIgnored() throws {
        let board = try boardWith("""
        {"cards":[{"id":"q","project":"bob","queue_state":"queued",
                   "queued_at":1.0,"queue_holder":"the running one"}]}
        """)
        let card = try XCTUnwrap(board.cards.first)
        XCTAssertEqual(card.queuedLine(autostart: true),
                       "Queued — Dark Army will start it")
    }

    // MARK: - What a refusal with no words reads as

    func testAnEmptyRefusalGetsPlainWordsRatherThanAStatusNumber() {
        XCTAssertEqual(ActionResult.refusalText(detail: ""),
                       "Dark Army refused this and did not say why.")
    }

    func testTheDaemonsOwnWordsAlwaysWin() {
        XCTAssertEqual(ActionResult.refusalText(detail: "x"), "x")
    }

    func testTheFallbackCarriesNoBareNumber() {
        XCTAssertNil(ActionResult.unexplainedRefusal.rangeOfCharacter(
            from: CharacterSet.decimalDigits))
    }

    func testTheFallbackIsNotMistakenForThePlanGate() {
        XCTAssertFalse(
            ActionResult(ok: false,
                         detail: ActionResult.refusalText(detail: ""))
                .isPlanGateRefusal)
    }
}
