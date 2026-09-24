import XCTest
@testable import BobPanel

final class AreaGridTests: XCTestCase {
    func testTileLabelNamesAreaAndUsualLead() {
        XCTAssertEqual(AreaGrid.label(Areas.all[0]), "Backbone, services, data & transport, usually Relay")
    }
    func testSelectionCanBeCleared() {
        XCTAssertEqual(AreaGrid.selection("desk", current: "backbone"), "desk")
        XCTAssertEqual(AreaGrid.selection("desk", current: "desk"), "")
    }
    func testSuggestionNeverOverwritesTypedArea() {
        XCTAssertEqual(AreaSuggestion.decide(offer: "desk", current: ""), "desk")
        XCTAssertNil(AreaSuggestion.decide(offer: "desk", current: "gate"))
        XCTAssertNil(AreaSuggestion.decide(offer: "made-up", current: ""))
    }
    func testAreaDecodesAndMissingFieldDefaults() throws {
        let decoder = JSONDecoder()
        XCTAssertEqual(try decoder.decode(BoardCard.self, from: Data(#"{"area":"desk"}"#.utf8)).area, "desk")
        XCTAssertEqual(try decoder.decode(BoardCard.self, from: Data("{}".utf8)).area, "")
    }
    func testAreaDraftRoundTrips() throws {
        let draft = CardDraft(id: "area-only", area: "pocket")
        let restored = try XCTUnwrap(CardDraft(id: draft.id, any: draft.body))
        XCTAssertEqual(restored.area, "pocket")
        var active = BoardDraft()
        active.area = restored.area
        XCTAssertTrue(CardDrafts.worthKeeping(draft: active, staged: []))
    }

}
