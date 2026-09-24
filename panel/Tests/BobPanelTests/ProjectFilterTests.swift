import XCTest
@testable import BobPanel

/// The multi-select project filter: the ALL chip's lit state, the switch
/// rows, the set semantics on `BoardState`, and the filing ladder under a
/// set. The state cases redirect `PanelPlacement.root` at a temp directory
/// first — `BoardState.init` seeds from the placement file and both
/// mutators write through to it, and none of that may touch the real
/// `~/.dark-army/panel-position.json`.
final class ProjectFilterTests: XCTestCase {
    private var originalRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalRoot = PanelPlacement.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-projectfilter-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot,
                                                withIntermediateDirectories: true)
        PanelPlacement.root = tempRoot
    }

    override func tearDownWithError() throws {
        PanelPlacement.root = originalRoot
        try? FileManager.default.removeItem(at: tempRoot)
    }

    private func writeRaw(_ body: [String: Any]) throws {
        let data = try JSONSerialization.data(withJSONObject: body)
        try data.write(to: PanelPlacement.path)
    }

    private func readRaw() throws -> [String: Any] {
        let data = try Data(contentsOf: PanelPlacement.path)
        return try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - The ALL chip's lit state

    func testShowsAllOnEmptySelection() {
        XCTAssertTrue(projectFilterShowsAll(selected: [],
                                            allNames: ["a", "b"]))
    }

    /// Every known name ticked is indistinguishable from no filter, and ALL
    /// lights — the explicit set is still kept underneath.
    func testShowsAllOnFullCover() {
        XCTAssertTrue(projectFilterShowsAll(selected: ["a", "b", ""],
                                            allNames: ["a", "b", ""]))
    }

    /// `Set([""])` is a selection — the "Other" pile — never ALL.
    func testShowsAllFalseForOtherOnly() {
        XCTAssertFalse(projectFilterShowsAll(selected: [""],
                                             allNames: ["a", ""]))
    }

    /// Before the first snapshot, `allNames` is empty and `allSatisfy` is
    /// vacuously true; a stale tick must not light ALL.
    func testShowsAllFalseWhenNamesAreNotKnownYet() {
        XCTAssertFalse(projectFilterShowsAll(selected: ["gone"],
                                             allNames: []))
    }

    func testSwitchRowsDropOnlyTheAllRow() {
        let items = projectSwitchRows(selected: ["gone"],
                                      allNames: ["a", ""])
        XCTAssertEqual(items.compactMap(\.name), ["a", "", "gone"])
        XCTAssertEqual(items.map(\.title), ["a", "Other", "gone"])
        XCTAssertEqual(items.map(\.ticked), [false, false, true])
        XCTAssertTrue(items.allSatisfy { $0.name != nil })
    }

    // MARK: - The item list

    func testItemsLeadWithAllProjectsNeverTicked() {
        let items = projectFilterItems(selected: ["a"], allNames: ["a", "b"])
        XCTAssertEqual(items.first?.title, "All projects")
        XCTAssertNil(items.first?.name)
        XCTAssertEqual(items.first?.ticked, false)
    }

    /// A persisted tick whose project has no card and no open window must
    /// still appear, or a stale tick becomes a filter that cannot be
    /// individually cleared.
    func testItemsUnionIncludesStalePersistedTick() {
        let items = projectFilterItems(selected: ["gone"], allNames: ["a"])
        let stale = items.first { $0.name == "gone" }
        XCTAssertNotNil(stale)
        XCTAssertEqual(stale?.ticked, true)
        // Appended after the daemon's own list, not mixed into it.
        XCTAssertEqual(items.compactMap(\.name), ["a", "gone"])
    }

    /// "Other" is a row of its own, distinct from "All projects": one carries
    /// `""` and toggles a tick, the other carries no name and clears.
    func testItemsOtherRowDistinctFromAllProjects() {
        let items = projectFilterItems(selected: [], allNames: ["a", ""])
        let other = items.first { $0.name == "" }
        XCTAssertEqual(other?.title, "Other")
        XCTAssertEqual(items.filter { $0.title == "All projects" }.count, 1)
        XCTAssertNil(items.first { $0.title == "All projects" }?.name)
    }

    func testItemsTickStatesFollowTheSelection() {
        let items = projectFilterItems(selected: ["b", ""],
                                       allNames: ["a", "b", ""])
        XCTAssertEqual(items.map(\.ticked), [false, false, true, true])
    }

    // MARK: - BoardState semantics

    @MainActor
    func testShowsProjectSemantics() {
        let state = BoardState()
        // Empty set admits everything.
        XCTAssertTrue(state.showsProject("a"))
        XCTAssertTrue(state.showsProject(""))
        // One tick admits only itself.
        state.projectFilter = ["a"]
        XCTAssertTrue(state.showsProject("a"))
        XCTAssertFalse(state.showsProject("b"))
        XCTAssertFalse(state.showsProject(""))
        // Several ticks admit each member.
        state.projectFilter = ["a", "b"]
        XCTAssertTrue(state.showsProject("a"))
        XCTAssertTrue(state.showsProject("b"))
        XCTAssertFalse(state.showsProject("c"))
        // `Set([""])` is a real selection — Other only, not everything.
        state.projectFilter = [""]
        XCTAssertTrue(state.showsProject(""))
        XCTAssertFalse(state.showsProject("a"))
    }

    @MainActor
    func testSingleProjectNilForZeroAndSeveral() {
        let state = BoardState()
        XCTAssertNil(state.singleProject)
        state.projectFilter = ["a"]
        XCTAssertEqual(state.singleProject, "a")
        state.projectFilter = [""]
        XCTAssertEqual(state.singleProject, "")
        state.projectFilter = ["a", "b"]
        XCTAssertNil(state.singleProject)
    }

    @MainActor
    func testToggleRoundTripPersists() throws {
        let state = BoardState()
        state.toggleProjectFilter("a")
        state.toggleProjectFilter("b")
        XCTAssertEqual(state.projectFilter, ["a", "b"])
        XCTAssertEqual(PanelPlacement.boardProjects(), ["a", "b"])
        // A fresh state — the next launch — seeds from the file.
        XCTAssertEqual(BoardState().projectFilter, ["a", "b"])
        // Toggling back off removes the tick and, at empty, the key itself.
        state.toggleProjectFilter("a")
        XCTAssertEqual(PanelPlacement.boardProjects(), ["b"])
        state.toggleProjectFilter("b")
        XCTAssertNil(PanelPlacement.boardProjects())
    }

    @MainActor
    func testClearProjectFilterEmptiesAndRemovesKey() {
        let state = BoardState()
        state.toggleProjectFilter("a")
        state.clearProjectFilter()
        XCTAssertEqual(state.projectFilter, [])
        XCTAssertNil(PanelPlacement.boardProjects())
    }

    /// The tick set must never drop an unrelated writer — the merge
    /// discipline every writer of this file lives under.
    func testSaveBoardProjectsKeepsUnrelatedKeys() throws {
        try writeRaw(["future_key": "kept"])
        PanelPlacement.saveBoardProjects(["a"])
        XCTAssertEqual(try readRaw()["future_key"] as? String, "kept")
        XCTAssertEqual(PanelPlacement.boardProjects(), ["a"])
        PanelPlacement.saveBoardProjects([])
        XCTAssertEqual(try readRaw()["future_key"] as? String, "kept")
        XCTAssertNil(PanelPlacement.boardProjects())
    }

    // MARK: - filingProject under a set

    private func agent(project: String) throws -> Agent {
        let json = #"{"session_id":"s-\#(project)","project":"\#(project)"}"#
        return try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    private func board(names: [String]) -> Board {
        var board = Board()
        board.projects = names.map { name in
            var project = BoardProject()
            project.name = name
            project.root = "/tmp/\(name)"
            return project
        }
        return board
    }

    @MainActor
    func testFilingProjectSingleTickWins() throws {
        let state = BoardState()
        state.projectFilter = ["beta"]
        var agents = Agents()
        agents.running = [try agent(project: "alpha")]
        XCTAssertEqual(state.filingProject(board: board(names: ["alpha", "beta"]),
                                           agents: agents),
                       "beta")
    }

    /// Several ticks: the old ladder (busiest live, else first known) walks
    /// on, restricted to the ticks — a card filed under an unticked project
    /// would vanish from the board being looked at.
    @MainActor
    func testFilingProjectSeveralTicksNeverReturnsUnticked() throws {
        let state = BoardState()
        state.projectFilter = ["beta", "gamma"]
        var agents = Agents()
        // The busiest live project is unticked, so the ladder must skip it.
        agents.running = [try agent(project: "alpha"),
                          try agent(project: "gamma")]
        let known = board(names: ["alpha", "beta", "gamma"])
        XCTAssertEqual(state.filingProject(board: known, agents: agents),
                       "gamma")
        // No live session in a ticked project: first ticked known name.
        agents.running = [try agent(project: "alpha")]
        XCTAssertEqual(state.filingProject(board: known, agents: agents),
                       "beta")
        // Nothing known at all: the first ticked name, sorted.
        XCTAssertEqual(state.filingProject(board: Board(), agents: Agents()),
                       "beta")
    }

    @MainActor
    func testFilingProjectEmptySetMatchesOldLadder() throws {
        let state = BoardState()
        var agents = Agents()
        agents.running = [try agent(project: "alpha")]
        XCTAssertEqual(state.filingProject(board: board(names: ["beta"]),
                                           agents: agents),
                       "alpha")
        XCTAssertEqual(state.filingProject(board: board(names: ["beta"]),
                                           agents: Agents()),
                       "beta")
        XCTAssertEqual(state.filingProject(board: Board(), agents: Agents()),
                       "")
    }

    // MARK: - filingRoot never disagrees with filingProject

    /// With only "Other" ticked, the composer opens unanswered — never seeded
    /// with project `""` beside some named project's root, which the picker
    /// (reading the root) would display as a project a save would not send.
    @MainActor
    func testFilingRootOtherOnlySelectionIsUnanswered() {
        let state = BoardState()
        state.projectFilter = [""]
        let known = board(names: ["alpha", "beta"])
        XCTAssertEqual(state.filingProject(board: known, agents: Agents()), "")
        XCTAssertEqual(state.filingRoot(board: known, agents: Agents()), "")
    }

    /// The pair the composer is seeded with is one answer: whenever the
    /// filing root resolves to a known project at all, it is the filing
    /// project's own — and where it cannot be, it is empty, never arbitrary.
    @MainActor
    func testFilingRootNeverNamesAnotherProject() throws {
        let state = BoardState()
        let known = board(names: ["alpha", "beta", "gamma"])
        var agents = Agents()
        agents.running = [try agent(project: "beta")]
        for filter: Set<String> in [[], ["beta"], ["beta", "gamma"], [""]] {
            state.projectFilter = filter
            let project = state.filingProject(board: known, agents: agents)
            let root = state.filingRoot(board: known, agents: agents)
            if let hit = known.projects.first(where: { $0.root == root }) {
                XCTAssertEqual(hit.name, project,
                               "filter \(filter.sorted()) seeded a mismatch")
            } else {
                XCTAssertEqual(root, "",
                               "filter \(filter.sorted()) seeded an unknown root")
            }
        }
    }

    // MARK: - a reveal widens, never wipes

    /// A banner reveal widens the filter rather than wiping it: the revealed
    /// card's project joins the ticks, every other tick survives, and the
    /// persisted file follows.
    @MainActor
    func testIncludeProjectWidensAndPersists() {
        let state = BoardState()
        state.toggleProjectFilter("a")
        state.toggleProjectFilter("b")
        state.includeProjectInFilter("c")
        XCTAssertEqual(state.projectFilter, ["a", "b", "c"])
        XCTAssertTrue(state.showsProject("c"))
        XCTAssertEqual(PanelPlacement.boardProjects(), ["a", "b", "c"])
        // The Other pile widens the same way — a revealed card in project
        // `""` must become visible without dropping the named ticks.
        state.includeProjectInFilter("")
        XCTAssertTrue(state.showsProject(""))
        XCTAssertEqual(state.projectFilter, ["", "a", "b", "c"])
        XCTAssertEqual(PanelPlacement.boardProjects(), ["", "a", "b", "c"])
    }

    /// An empty filter already shows everything: including into it must not
    /// narrow the board, and an already-ticked name is a no-op.
    @MainActor
    func testIncludeProjectLeavesSatisfiedFilterAlone() {
        let state = BoardState()
        state.includeProjectInFilter("a")
        XCTAssertEqual(state.projectFilter, [])
        XCTAssertNil(PanelPlacement.boardProjects())
        state.projectFilter = ["a"]
        state.includeProjectInFilter("a")
        XCTAssertEqual(state.projectFilter, ["a"])
    }
}
