import XCTest
@testable import BobPanel

/// A card's place in a batch-implement session: the tolerant decode of the
/// snapshot's `batch` key and the one line the tile draws for it.
/// `plans/2026-09-25-batch-implement-backlog-cards.md`.
final class BoardBatchMarkTests: XCTestCase {

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    func testAbsentBatchIsNilAndDrawsNoLine() throws {
        let decoded = try card(#"{"id":"a","column_name":"backlog"}"#)
        XCTAssertNil(decoded.batch)
        XCTAssertEqual(decoded.batchLine, "")
        XCTAssertFalse(decoded.isBatchWaiting)
    }

    func testMalformedBatchIsNilAndNeverBlanksTheCard() throws {
        for raw in [#""batch":3"#, #""batch":"x""#, #""batch":{}"#,
                    #""batch":{"rank":0,"size":3,"state":"waiting"}"#,
                    #""batch":{"rank":2,"size":3}"#] {
            let decoded = try card(#"{"id":"a","title":"kept","#
                                   + raw + "}")
            XCTAssertNil(decoded.batch, raw)
            XCTAssertEqual(decoded.title, "kept", raw)
        }
    }

    func testPresentBatchDrawsItsLine() throws {
        let waiting = try card(#"""
            {"id":"b","batch":{"rank":2,"size":3,"state":"waiting"}}
            """#)
        XCTAssertEqual(waiting.batch, BatchMark(rank: 2, size: 3, state: "waiting"))
        XCTAssertEqual(waiting.batchLine, "BATCH 2/3 · waiting")
        XCTAssertTrue(waiting.isBatchWaiting)

        let working = try card(#"""
            {"id":"a","batch":{"rank":1,"size":3,"state":"working"}}
            """#)
        XCTAssertEqual(working.batchLine, "BATCH 1/3 · working")
        XCTAssertFalse(working.isBatchWaiting)
    }

    func testALeftMarkSaysSoAndStillHoldsTheCard() throws {
        let left = try card(#"""
            {"id":"d","column_name":"in_progress",
             "batch":{"rank":2,"size":3,"state":"left"}}
            """#)
        XCTAssertEqual(left.batchLine, "BATCH 2/3 · left the line")
        XCTAssertTrue(left.holdsBatchMark)
        XCTAssertFalse(left.isBatchWaiting)
        let working = try card(#"""
            {"id":"a","batch":{"rank":1,"size":3,"state":"working"}}
            """#)
        XCTAssertFalse(working.holdsBatchMark)
    }

    func testASizeBelowTheRankReadsAsTheRank() throws {
        let decoded = try card(#"""
            {"id":"c","batch":{"rank":3,"size":1,"state":"waiting"}}
            """#)
        XCTAssertEqual(decoded.batchLine, "BATCH 3/3 · waiting")
    }
}
