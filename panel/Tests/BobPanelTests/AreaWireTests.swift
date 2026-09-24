import XCTest
@testable import BobPanel

final class AreaWireTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    func testAreaFieldsKeepMissingDefaultsAndValidValues() throws {
        XCTAssertEqual(try decode(BoardCard.self, "{}").area, "")
        XCTAssertEqual(try decode(Agent.self, "{}").areaLine, "")
        let valid = try decode(Snapshot.self,
            #"{"board":{"areas_supported":true,"cards":[{"area":"desk","title":17}]},"agents":{"running":[{"area":"desk","area_line":"Vex · Desk lead"}]}}"#)
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

    func testAreaCardReportsAndConflictRootsRejectWrongTypes() throws {
        XCTAssertThrowsError(try decode(BoardReport.self, #"{"cards":[{"area":null}]}"#))
        XCTAssertThrowsError(try decode(OutcomeActionReply.self, #"{"current":{"area":1}}"#))
    }
}
