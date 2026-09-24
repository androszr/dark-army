import XCTest
@testable import BobPanel

/// The swimlane folds went with the folders; the row folds took their
/// place under a **new** key. What is pinned: a fresh `BoardState` reads
/// the project ticks and the row flips out of the habit file, and a
/// `board_folded_lanes` key an older build left behind is neither read nor
/// destroyed by either writer — the merge discipline every writer of that
/// file lives under. The temp `root` swap happens in `setUp` *before* the first
/// `BoardState()`, so none of this touches the real
/// `~/.dark-army/panel-position.json`.
final class BoardFoldTests: XCTestCase {
    private var originalRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalRoot = PanelPlacement.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-boardfold-tests-\(UUID().uuidString)",
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
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    @MainActor
    func testAStaleFoldedLanesKeyIsLeftAloneByEveryWriter() throws {
        try writeRaw(["board_folded_lanes": ["", "folder-a"],
                      "board_projects": ["p"]])
        let state = BoardState()
        XCTAssertEqual(state.projectFilter, ["p"])
        state.toggleProjectFilter("q")
        let body = try readRaw()
        XCTAssertEqual(body["board_folded_lanes"] as? [String], ["", "folder-a"])
        XCTAssertEqual(Set(body["board_projects"] as? [String] ?? []), ["p", "q"])
    }

    /// The row flips ride `board_row_flips`: an unknown id in the file is
    /// dropped on read, a toggle writes the set back in row order, and the
    /// other keys — the ticks and the stale lanes key — survive the write.
    @MainActor
    func testRowFlipsAreReadDroppedAndWrittenInRowOrderBesideTheOtherKeys() throws {
        try writeRaw(["board_row_flips": ["done", "bogus"],
                      "board_projects": ["p"],
                      "board_folded_lanes": ["x"]])
        let state = BoardState()
        XCTAssertEqual(state.rowFlips, ["done"])
        // Done flipped is open; Backlog unflipped is open.
        XCTAssertFalse(state.rowFolded(.done))
        XCTAssertFalse(state.rowFolded(.backlog))
        state.toggleRowFold(.backlog)
        XCTAssertTrue(state.rowFolded(.backlog))
        let body = try readRaw()
        XCTAssertEqual(body["board_row_flips"] as? [String], ["backlog", "done"])
        XCTAssertEqual(body["board_projects"] as? [String], ["p"])
        XCTAssertEqual(body["board_folded_lanes"] as? [String], ["x"])
        // A search draws every row open without touching the record.
        state.query = "x"
        XCTAssertFalse(state.rowFolded(.backlog))
        XCTAssertEqual(try readRaw()["board_row_flips"] as? [String], ["backlog", "done"])
        // Flipping both back to the defaults removes the key rather than
        // leaving an empty array — the empty set needs no record.
        state.query = ""
        state.toggleRowFold(.backlog)
        state.toggleRowFold(.done)
        XCTAssertNil(try readRaw()["board_row_flips"])
        XCTAssertEqual(try readRaw()["board_projects"] as? [String], ["p"])
    }
}
