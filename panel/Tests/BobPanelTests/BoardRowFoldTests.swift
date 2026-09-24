import XCTest
@testable import BobPanel

/// The board's row folds, tabled.
///
/// `plans/2026-09-12-board-rows-instead-of-columns.md`. Every rule behind a
/// folded row is a pure static over `(id, flipped, searching)` — which rows
/// exist, which start folded, what a press on a heading does, what is drawn
/// while a search is up, and how the flipped set is spelled in the habit
/// file and the phone's `@AppStorage` string. Both views read these and
/// re-derive none of them, so this file is the whole decision.
final class BoardRowFoldTests: XCTestCase {

    /// Four rows, in the board's own order, spelled as the daemon's column ids.
    func testTheRowsAreTheFourColumnIdsInOrder() {
        XCTAssertEqual(BoardRowFold.rows, ["prep", "backlog", "in_progress", "done"])
    }

    /// Done starts folded; nothing else does.
    func testOnlyDoneStartsFolded() {
        XCTAssertTrue(BoardRowFold.defaultFolded("done"))
        XCTAssertFalse(BoardRowFold.defaultFolded("prep"))
        XCTAssertFalse(BoardRowFold.defaultFolded("backlog"))
        XCTAssertFalse(BoardRowFold.defaultFolded("in_progress"))
    }

    /// An empty flipped set is the defaults for every row.
    func testNothingFlippedIsTheDefaultForEveryRow() {
        for id in BoardRowFold.rows {
            XCTAssertEqual(BoardRowFold.folded(id, flipped: []),
                           BoardRowFold.defaultFolded(id), id)
        }
    }

    /// A flip inverts the default: Backlog flipped is folded, Done flipped
    /// is open.
    func testAFlipInvertsTheDefault() {
        XCTAssertTrue(BoardRowFold.folded("backlog", flipped: ["backlog"]))
        XCTAssertFalse(BoardRowFold.folded("done", flipped: ["done"]))
        XCTAssertTrue(BoardRowFold.folded("done", flipped: ["backlog"]))
    }

    /// A press flips, a second press flips back, and the other rows are
    /// untouched either way.
    func testToggledFlipsAndFlipsBack() {
        let once = BoardRowFold.toggled("backlog", flipped: ["done"])
        XCTAssertEqual(once, ["backlog", "done"])
        let twice = BoardRowFold.toggled("backlog", flipped: once)
        XCTAssertEqual(twice, ["done"])
    }

    /// An id that names no row changes nothing, so nothing stray can reach
    /// the habit file through a press.
    func testTogglingAnUnknownIdIsANoOp() {
        XCTAssertEqual(BoardRowFold.toggled("bogus", flipped: ["done"]), ["done"])
        XCTAssertEqual(BoardRowFold.toggled("", flipped: []), [])
    }

    /// A search overrides every fold, whatever is flipped.
    func testASearchDrawsEveryRowOpen() {
        for id in BoardRowFold.rows {
            XCTAssertFalse(BoardRowFold.drawnFolded(id, flipped: [], searching: true), id)
            XCTAssertFalse(BoardRowFold.drawnFolded(id, flipped: ["backlog", "done"],
                                                    searching: true), id)
        }
        // And with no search the fold is the fold.
        XCTAssertTrue(BoardRowFold.drawnFolded("done", flipped: [], searching: false))
        XCTAssertFalse(BoardRowFold.drawnFolded("done", flipped: ["done"], searching: false))
        XCTAssertTrue(BoardRowFold.drawnFolded("prep", flipped: ["prep"], searching: false))
    }

    /// Decoding keeps only real rows and reads an absent record as the
    /// defaults.
    func testDecodeDropsUnknownIdsAndReadsNilAsEmpty() {
        XCTAssertEqual(BoardRowFold.decode(["done", "bogus", ""]), ["done"])
        XCTAssertEqual(BoardRowFold.decode(nil), [])
        XCTAssertEqual(BoardRowFold.decode([]), [])
    }

    /// Encoding is in row order — stable on disk — and empty for the
    /// defaults, so a habit file holding the defaults holds no key.
    func testEncodeIsInRowOrderAndEmptyForTheDefaults() {
        XCTAssertEqual(BoardRowFold.encode(["done", "prep", "backlog"]),
                       ["prep", "backlog", "done"])
        XCTAssertEqual(BoardRowFold.encode([]), [])
    }

    /// The array spelling round-trips.
    func testArrayRoundTrip() {
        let set: Set<String> = ["in_progress", "done"]
        XCTAssertEqual(BoardRowFold.decode(BoardRowFold.encode(set)), set)
    }

    /// The phone's comma-joined spelling round-trips, drops what is not a
    /// row, and reads the empty string as the defaults.
    func testJoinedRoundTripAndEmpty() {
        let set: Set<String> = ["backlog", "done"]
        XCTAssertEqual(BoardRowFold.encode(joined: set), "backlog,done")
        XCTAssertEqual(BoardRowFold.decode(joined: "backlog,done"), set)
        XCTAssertEqual(BoardRowFold.decode(joined: "done,bogus,,prep"), ["prep", "done"])
        XCTAssertEqual(BoardRowFold.decode(joined: ""), [])
        XCTAssertEqual(BoardRowFold.encode(joined: []), "")
    }
}
