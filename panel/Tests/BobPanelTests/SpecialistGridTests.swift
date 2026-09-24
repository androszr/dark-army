import XCTest
@testable import BobPanel

final class SpecialistGridTests: XCTestCase {
    func testKnownStagesHaveJobsAndNoFaces() {
        XCTAssertEqual(Specialists.table.count, 7)
        for name in Specialists.table.keys {
            XCTAssertFalse(Specialists.role(for: name)!.isEmpty)
            XCTAssertEqual(Specialists.face(for: name), "")
        }
        XCTAssertEqual(Specialists.short("bc-implementer"), "impl")
        XCTAssertEqual(Specialists.short("bc-verifier"), "verify")
    }
    func testUnknownStageKeepsStableHash() {
        XCTAssertFalse(Specialists.face(for: "custom-agent").isEmpty)
        XCTAssertEqual(Specialists.face(for: "custom-agent"), Specialists.face(for: "CUSTOM-AGENT"))
        XCTAssertNil(Specialists.role(for: "custom-agent"))
    }
    func testParseKeepsBoardStages() {
        for value in ["bc-planner\n\n bc-verifier ", "", "one"] {
            XCTAssertEqual(Specialists.parse(value), BoardCard.stages(value))
        }
    }
}
