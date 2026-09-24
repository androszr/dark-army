import Combine
import XCTest
@testable import BobPanel

/// The republish proof: the board and its cards observe `BoardFeed`, and an
/// applied frame reaches that feed only where the board, a card-named
/// session's row or a card face's board-wide facts actually changed.
///
/// Frames go through the stream's own route — `prepareFrame` then `apply` —
/// so the slim-frame carry-forward and the Done splice run exactly as they do
/// against the daemon. Each frame differs from the last once its clock is
/// stripped, so the payload dedupe never swallows one.
@MainActor
final class BoardFeedTests: XCTestCase {
    private var client: DaemonClient!
    private var bag: Set<AnyCancellable> = []
    private var clientPublishes = 0
    private var feedPublishes = 0
    private var boardPublishes = 0
    private var rowsPublishes = 0
    private var chromePublishes = 0

    override func setUp() async throws {
        client = DaemonClient()
        bag = []
        client.objectWillChange.sink { [unowned self] _ in clientPublishes += 1 }
            .store(in: &bag)
        client.boardFeed.objectWillChange.sink { [unowned self] _ in feedPublishes += 1 }
            .store(in: &bag)
        // `dropFirst`: a `@Published` publisher replays its current value on
        // subscribe, which is not a publish.
        client.boardFeed.$board.dropFirst().sink { [unowned self] _ in boardPublishes += 1 }
            .store(in: &bag)
        client.boardFeed.$rows.dropFirst().sink { [unowned self] _ in rowsPublishes += 1 }
            .store(in: &bag)
        client.boardFeed.$chrome.dropFirst().sink { [unowned self] _ in chromePublishes += 1 }
            .store(in: &bag)
    }

    override func tearDown() async throws {
        bag = []
        client = nil
    }

    private func resetCounts() {
        clientPublishes = 0
        feedPublishes = 0
        boardPublishes = 0
        rowsPublishes = 0
        chromePublishes = 0
    }

    /// Feed one payload through the stream's route.
    private func feed(_ text: String,
                      file: StaticString = #filePath, line: UInt = #line) async throws {
        let prepared = await DaemonClient.prepareFrame(text, lastApplied: "", via: "test")
        let frame = try XCTUnwrap(prepared, "frame rejected", file: file, line: line)
        client.apply(frame.snapshot, normalized: frame.normalized, via: "test")
    }

    // MARK: - Frames

    /// One row as the daemon sends it: the face fields a card draws, and the
    /// clocks and meters it recomputes on every refresh. `tick` moves every
    /// one of the latter — `idle_seconds`, `stats.duration_seconds`,
    /// `metrics.cost_usd`, `trend.ctx_pct_per_min`, `last_text` — so a frame
    /// built at another tick differs in exactly what a live fleet changes
    /// between frames and in nothing a card draws.
    private static func agent(_ id: String, tool: String = "Edit", tick: Int = 0,
                              helpers: [String] = [], canJump: Bool = true) -> String {
        let rows = helpers
            .map { "{\"agent_id\": \"\($0)-id\", \"label\": \"\($0)\"}" }
            .joined(separator: ",")
        return """
        {"session_id": "\(id)", "nickname": "\(id)-nick", "current_tool": "\(tool)",
         "can_jump": \(canJump), "idle_seconds": \(Double(tick) * 4.5 + 1),
         "last_text": "turn \(tick)",
         "stats": {"duration_seconds": \(100 + tick * 7), "output_tokens": \(1000 + tick)},
         "metrics": {"cost_usd": \(0.5 + Double(tick) / 10), "ctx_used_pct": \(20 + tick)},
         "trend": {"ctx_pct_per_min": \(0.25 + Double(tick) / 100), "samples": \(3 + tick)},
         "subagent_rows": [\(rows)]}
        """
    }

    private static func agents(running: [String] = [], waiting: [String] = [],
                               sleeping: [String] = [], finished: [String] = [],
                               abandoned: [String] = []) -> String {
        """
        {"running": [\(running.joined(separator: ","))],
         "waiting": [\(waiting.joined(separator: ","))],
         "sleeping": [\(sleeping.joined(separator: ","))],
         "finished": [\(finished.joined(separator: ","))],
         "abandoned": [\(abandoned.joined(separator: ","))]}
        """
    }

    /// Three cards: one bound to `s1`, one agent-authored by `s2`, one under
    /// a live refinement by `s3`.
    private static func board(boundColumn: String = "in_progress",
                              dispatch: Bool = true) -> String {
        """
        {"available": true, "dispatch_enabled": \(dispatch),
         "tools": ["claude", "codex"],
         "counts": {"backlog": 1, "in_progress": 1, "prep": 1},
         "cards": [
           {"id": "c1", "title": "Bound", "column_name": "\(boundColumn)",
            "session_id": "s1", "link_state": "live"},
           {"id": "c2", "title": "Filed by an agent", "column_name": "backlog",
            "author": "s2"},
           {"id": "c3", "title": "Refining", "column_name": "prep",
            "refine_state": "live", "refine_session_id": "s3"}
         ]}
        """
    }

    /// The opening full frame: agents s1 and s2 running, s3 and the
    /// unrelated s9 sleeping, and the three-card board.
    private func openWithFullFrame() async throws {
        try await feed("""
        {"generated_at": 1,
         "agents": \(Self.agents(running: [Self.agent("s1"), Self.agent("s2")],
                                 sleeping: [Self.agent("s3"), Self.agent("s9")])),
         "board": \(Self.board())}
        """)
    }

    // MARK: - 1. The opening frame

    func testAFullFramePublishesTheBoardAndTheCardNamedRows() async throws {
        try await openWithFullFrame()
        XCTAssertEqual(boardPublishes, 1)
        XCTAssertEqual(rowsPublishes, 1)
        XCTAssertEqual(chromePublishes, 1, "dispatch on and two tools differ from the default")
        XCTAssertEqual(feedPublishes, 3, "one publish per value that changed, no more")
        XCTAssertEqual(Set(client.boardFeed.rows.keys), ["s1", "s2", "s3"],
                       "bound, author and live refiner — and never the unrelated s9")
        XCTAssertEqual(client.boardFeed.rows["s1"]?.bucket, "running")
        XCTAssertEqual(client.boardFeed.rows["s3"]?.bucket, "sleeping")
        XCTAssertEqual(client.boardFeed.board.cards.map(\.id), ["c1", "c2", "c3"])
        XCTAssertEqual(client.boardFeed.board, client.snapshot.board)
    }

    // MARK: - 2. The success criterion

    func testAgentsOnlyFrameDoesNotRepublishTheBoard() async throws {
        try await openWithFullFrame()
        let before = client.boardFeed.board
        resetCounts()
        // A slim frame: only `agents`, and in it only the unrelated s9 moved
        // (a new tool). The board key is absent and is carried forward.
        try await feed("""
        {"generated_at": 2,
         "agents": \(Self.agents(running: [Self.agent("s1", tick: 1), Self.agent("s2", tick: 1)],
                                 sleeping: [Self.agent("s3", tick: 1),
                                            Self.agent("s9", tool: "Bash", tick: 1)]))}
        """)
        XCTAssertEqual(clientPublishes, 1, "the client still applies the frame")
        XCTAssertEqual(client.snapshot.agents.running.first?.idleSeconds, 5.5,
                       "the card-named rows' clocks did move on the wire")
        XCTAssertEqual(client.snapshot.agents.sleeping.last?.currentTool, "Bash")
        XCTAssertEqual(feedPublishes, 0, "an agents-only frame must not reach the board")
        XCTAssertEqual(boardPublishes, 0)
        XCTAssertEqual(rowsPublishes, 0)
        XCTAssertEqual(chromePublishes, 0)
        XCTAssertEqual(client.boardFeed.board, before)
        XCTAssertTrue(client.boardFeed.board.available,
                      "the absent board was carried, never decoded as empty")
    }

    // MARK: - 3. A card's own session moves

    func testACardNamedSessionMovingRepublishesOnlyItsRow() async throws {
        try await openWithFullFrame()
        let before = client.boardFeed.board
        resetCounts()
        try await feed("""
        {"generated_at": 3,
         "agents": \(Self.agents(running: [Self.agent("s2", tick: 2)],
                                 waiting: [Self.agent("s1", tick: 2)],
                                 sleeping: [Self.agent("s3", tick: 2), Self.agent("s9", tick: 2)]))}
        """)
        XCTAssertEqual(feedPublishes, 1, "the rows, and only the rows")
        XCTAssertEqual(rowsPublishes, 1)
        XCTAssertEqual(boardPublishes, 0)
        XCTAssertEqual(client.boardFeed.board, before)
        XCTAssertEqual(client.boardFeed.rows["s1"]?.bucket, "waiting")
    }

    // MARK: - 3a. A card's own session only ticks

    /// The live-fleet case: every card-named row recomputed its clocks and
    /// meters (and s1 even changed tool, which no card draws) while each
    /// row's face stayed put. Nothing the board draws moved, so nothing is
    /// published to it.
    func testACardNamedSessionChangingOnlyItsClocksIsNotNews() async throws {
        try await openWithFullFrame()
        let s1Before = try XCTUnwrap(client.boardFeed.rows["s1"])
        resetCounts()
        for tick in 1...3 {
            try await feed("""
            {"generated_at": \(10 + tick),
             "agents": \(Self.agents(running: [Self.agent("s1", tool: "Read", tick: tick),
                                             Self.agent("s2", tick: tick)],
                                     sleeping: [Self.agent("s3", tick: tick),
                                                Self.agent("s9", tick: tick)]))}
            """)
        }
        XCTAssertEqual(clientPublishes, 3, "three distinct frames were applied")
        XCTAssertEqual(client.snapshot.agents.running.first?.stats.durationSeconds, 121)
        XCTAssertEqual(feedPublishes, 0, "clocks and meters are not what a card draws")
        XCTAssertEqual(rowsPublishes, 0)
        XCTAssertEqual(client.boardFeed.rows["s1"], s1Before)
    }

    /// And the face's fields are news: a helper appearing under s1 (the
    /// stage track's live marker) and s2 losing its jump reach each
    /// republish the rows.
    func testAFaceFieldChangeOnACardNamedSessionIsNews() async throws {
        try await openWithFullFrame()
        resetCounts()
        try await feed("""
        {"generated_at": 20,
         "agents": \(Self.agents(running: [Self.agent("s1", tick: 4, helpers: ["verifier"]),
                                         Self.agent("s2", tick: 4)],
                                 sleeping: [Self.agent("s3", tick: 4), Self.agent("s9", tick: 4)]))}
        """)
        XCTAssertEqual(rowsPublishes, 1)
        XCTAssertEqual(client.boardFeed.rows["s1"]?.agent.subagentRows.map(\.label), ["verifier"])
        try await feed("""
        {"generated_at": 21,
         "agents": \(Self.agents(running: [Self.agent("s1", tick: 5, helpers: ["verifier"]),
                                         Self.agent("s2", tick: 5, canJump: false)],
                                 sleeping: [Self.agent("s3", tick: 5), Self.agent("s9", tick: 5)]))}
        """)
        XCTAssertEqual(rowsPublishes, 2)
        XCTAssertEqual(client.boardFeed.rows["s2"]?.agent.isJumpable, false)
        XCTAssertEqual(boardPublishes, 0)
    }

    // MARK: - 4. A card moves

    func testABoardFrameThatMovesACardRepublishesTheBoardOnce() async throws {
        try await openWithFullFrame()
        resetCounts()
        try await feed("""
        {"generated_at": 4, "board": \(Self.board(boundColumn: "done"))}
        """)
        XCTAssertEqual(feedPublishes, 1)
        XCTAssertEqual(boardPublishes, 1)
        XCTAssertEqual(rowsPublishes, 0, "the agents were carried forward unchanged")
        XCTAssertEqual(chromePublishes, 0)
        XCTAssertEqual(client.boardFeed.board.cards.first { $0.id == "c1" }?.column, "done")
    }

    // MARK: - 5. Only a board-wide fact changes

    func testFlippingDispatchRepublishesTheChromeNotTheRows() async throws {
        try await openWithFullFrame()
        XCTAssertTrue(client.boardFeed.chrome.dispatchEnabled)
        resetCounts()
        try await feed("""
        {"generated_at": 5, "board": \(Self.board(dispatch: false))}
        """)
        XCTAssertEqual(chromePublishes, 1)
        XCTAssertEqual(rowsPublishes, 0)
        XCTAssertFalse(client.boardFeed.chrome.dispatchEnabled)
        XCTAssertEqual(client.boardFeed.chrome.tools, ["claude", "codex"])
    }

    // MARK: - 6. The index is row(session:) as a dictionary

    func testTheIndexMatchesRowSessionAcrossTheFourBuckets() throws {
        let json = Self.agents(
            running: [Self.agent("r1"), Self.agent("dup")],
            waiting: [Self.agent("w1"), Self.agent("dup")],
            sleeping: [Self.agent("z1")],
            finished: [Self.agent("f1"), #"{"session_id": ""}"#],
            abandoned: [Self.agent("a1"), Self.agent("w1")])
        let agents = try JSONDecoder().decode(Agents.self, from: Data(json.utf8))
        let index = agents.index()
        for id in ["r1", "w1", "z1", "f1", "dup"] {
            let row = try XCTUnwrap(agents.row(session: id), id)
            XCTAssertEqual(index[id], FleetRow(agent: row.0, bucket: row.1), id)
        }
        XCTAssertEqual(index["dup"]?.bucket, "running", "first bucket wins, row(session:)'s order")
        XCTAssertEqual(index["w1"]?.bucket, "waiting", "an abandoned duplicate never overrides")
        XCTAssertNil(index["a1"], "abandoned is never walked")
        XCTAssertNil(agents.row(session: "a1"))
        XCTAssertNil(index[""], "an empty id resolves to nothing")
        XCTAssertEqual(index.count, 5)
    }

    /// A direct assignment — the tests' and `loadDoneArchive`'s route — feeds
    /// the board too, through the one `didSet`.
    func testADirectAssignmentFeedsTheBoard() {
        var snap = Snapshot()
        snap.board.available = true
        client.snapshot = snap
        XCTAssertTrue(client.boardFeed.board.available)
        resetCounts()
        client.snapshot = snap
        XCTAssertEqual(feedPublishes, 0, "an equal board is not republished")
    }
}
