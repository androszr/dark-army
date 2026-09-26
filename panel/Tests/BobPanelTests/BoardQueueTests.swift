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

    // MARK: - Card dependencies

    /// A daemon older than dependencies sends none of the five keys, and a
    /// card with no links sends none of the derived four: every one decodes
    /// to empty, and the card still decodes.
    func testAbsentDependencyFieldsDecodeToEmpty() throws {
        let row = try card(#"{"id":"a","title":"t"}"#)
        XCTAssertEqual(row.blockedBy, "")
        XCTAssertEqual(row.dependencyIds, [])
        XCTAssertEqual(row.dependencies, [])
        XCTAssertEqual(row.dependents, [])
        XCTAssertEqual(row.dependencyLine, "")
        XCTAssertEqual(row.dependentsLine, "")
    }

    func testDependencyFieldsDecodeWhenPresent() throws {
        let row = try card(#"""
        {"id":"a","title":"t","blocked_by":"b\nc",
         "dependencies":[{"id":"b","title":"Build it","column_name":"done","met":true},
                         {"id":"c","title":"Ship it","column_name":"backlog","met":false}],
         "dependents":[{"id":"d","title":"Later"}],
         "dependency_line":"Waits on: \"Build it\" (done) · \"Ship it\" (not yet)",
         "dependents_line":"Unblocks: \"Later\""}
        """#)
        XCTAssertEqual(row.dependencyIds, ["b", "c"])
        XCTAssertEqual(row.dependencies.map(\.id), ["b", "c"])
        XCTAssertEqual(row.dependencies.first?.column, "done")
        XCTAssertEqual(row.dependencies.map(\.met), [true, false])
        XCTAssertEqual(row.dependents, [CardLink(id: "d", title: "Later")])
        XCTAssertEqual(row.dependencyLine,
                       #"Waits on: "Build it" (done) · "Ship it" (not yet)"#)
        XCTAssertEqual(row.dependentsLine, #"Unblocks: "Later""#)
    }

    /// A ragged entry (a newer or older daemon) still decodes, with the
    /// missing members empty — never a thrown board.
    func testARaggedDependencyEntryStillDecodes() throws {
        let row = try card(#"{"id":"a","dependencies":[{"id":"b"}],"dependents":[{}]}"#)
        XCTAssertEqual(row.dependencies, [CardDependency(id: "b")])
        XCTAssertEqual(row.dependents, [CardLink()])
    }

    /// A card held by a dependency wears the daemon's dependency sentence as
    /// its `queue_reason`, and `queuedLine` draws it verbatim, unchanged.
    func testTheDependencySentenceIsTheQueuedLineVerbatim() throws {
        let board = try boardWith(#"""
        {"cards":[{"id":"q","project":"bob","queue_state":"queued",
                   "queued_at":1.0,"blocked_by":"b",
                   "queue_reason":"Queued — Dark Army will start it once \"Build it\" is done"}]}
        """#)
        let card = try XCTUnwrap(board.cards.first)
        let sentence = "Queued — Dark Army will start it once \"Build it\" is done"
        XCTAssertEqual(card.queuedLine(autostart: true), sentence)
        XCTAssertEqual(card.queuedLine(autostart: false), sentence)
    }

    private func linkCard(_ id: String, root: String = "/p", column: String = "backlog",
                          blockedBy: String = "") -> BoardCard {
        var c = BoardCard()
        c.id = id
        c.title = id
        c.root = root
        c.column = column
        c.blockedBy = blockedBy
        // The daemon resolves every stored id that still names a card; the
        // table uses ids that start with "gone" for deleted ones.
        c.dependencies = BoardCard.stages(blockedBy)
            .filter { !$0.hasPrefix("gone") }
            .map { CardDependency(id: $0, title: $0) }
        return c
    }

    /// The Add… filter, tabled: same folder, not the card itself, not already
    /// listed, not in Done — and nothing at all once the list holds eight.
    func testTheDependencyChoicesAreThisProjectsOtherOpenUnlistedCards() {
        let me = linkCard("me", blockedBy: "listed")
        let cards = [
            me,
            linkCard("open"),
            linkCard("in-progress", column: "in_progress"),
            linkCard("prep", column: "prep"),
            linkCard("listed"),
            linkCard("finished", column: "done"),
            linkCard("elsewhere", root: "/other"),
        ]
        XCTAssertEqual(BoardCard.dependencyChoices(for: me, in: cards).map(\.id),
                       ["open", "in-progress", "prep"])
        // The board's own order is kept, and an empty board offers nothing.
        XCTAssertEqual(BoardCard.dependencyChoices(for: me, in: []), [])
        // A list already at the store's bound offers nothing: a ninth would
        // be cut without a word.
        let full = linkCard("full", blockedBy: (1...8).map { "d\($0)" }
                                .joined(separator: "\n"))
        XCTAssertEqual(BoardCard.maxDependencies, 8)
        XCTAssertEqual(BoardCard.dependencyChoices(for: full, in: cards + [full]), [])
        let seven = linkCard("seven", blockedBy: (1...7).map { "d\($0)" }
                                 .joined(separator: "\n"))
        XCTAssertEqual(BoardCard.dependencyChoices(for: seven, in: cards).map(\.id),
                       ["me", "open", "in-progress", "prep", "listed"])
        // Deleted cards' ids still in the stored string count for nothing:
        // seven dead ids and one live one leave room, and the live list —
        // what ✕ and Add… write back — drops the dead ones.
        let haunted = linkCard("haunted", blockedBy: (["listed"]
            + (1...7).map { "gone\($0)" }).joined(separator: "\n"))
        XCTAssertEqual(haunted.dependencyIds.count, 8)
        XCTAssertEqual(haunted.linkedIds, ["listed"])
        XCTAssertEqual(BoardCard.dependencyChoices(for: haunted, in: cards).map(\.id),
                       ["me", "open", "in-progress", "prep"])
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
