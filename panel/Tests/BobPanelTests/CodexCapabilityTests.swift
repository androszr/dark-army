import XCTest
@testable import BobPanel

final class CodexCapabilityTests: XCTestCase {
    func testNavigationOnlyNullablePIDEnablesOnlyJump() throws {
        let row = try agent(["provider": "codex", "pid": NSNull(), "can_jump": true,
                             "can_stop": false, "can_close": false, "can_type": false,
                             "channel": false])
        XCTAssertNil(row.pid)
        XCTAssertTrue(row.canJump)
        XCTAssertFalse(row.canStop)
        XCTAssertFalse(row.canClose)
        XCTAssertFalse(row.canType)
        XCTAssertFalse(row.channel)
        XCTAssertFalse(try agent([:]).canJump)
        XCTAssertFalse(try agent([:]).isJumpable)
    }

    func testHostedTerminalIsJumpableWithoutEditorJump() throws {
        let hosted = try agent(["own_terminal": true, "can_jump": false])
        XCTAssertTrue(hosted.ownTerminal)
        XCTAssertFalse(hosted.canJump)
        XCTAssertTrue(hosted.isJumpable)
        let editor = try agent(["own_terminal": false, "can_jump": true])
        XCTAssertTrue(editor.isJumpable)
        XCTAssertFalse(try agent([:]).ownTerminal)
    }

    private func agent(_ fields: [String: Any]) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: JSONSerialization.data(withJSONObject: fields))
    }

    func testMissingAndMalformedFieldsStayUnavailable() throws {
        for fields: [String: Any] in [[:], ["interaction_note": 5, "can_hide": "yes"]] {
            let row = try agent(fields)
            XCTAssertEqual(row.interactionNote, "")
            XCTAssertFalse(row.canHide)
            XCTAssertFalse(row.canType)
            XCTAssertFalse(row.channel)
        }
    }

    func testCodexOwnershipAndQuestionCapabilitiesAreIndependent() throws {
        let note = "Answer it in the original Codex session."
        for (kind, stop, jump, hide) in [
            ("read-only", false, true, true), ("exact-resume", true, true, false),
            ("ambiguous", false, false, true), ("child", false, false, false),
            ("finished", false, false, false),
        ] {
            let row = try agent(["provider": "codex", "can_stop": stop,
                                 "can_jump": jump, "can_hide": hide, "interaction_note": note,
                                 "question": ["id": "ask", "text": "Proceed?", "options": ["Yes", "No"]]])
            XCTAssertEqual(row.canStop, stop, kind)
            XCTAssertEqual(row.canJump, jump, kind)
            XCTAssertEqual(row.canHide, hide, kind)
            XCTAssertFalse(row.canType, kind)
            XCTAssertFalse(row.channel, kind)
            XCTAssertEqual(row.question.options, ["Yes", "No"])
            XCTAssertEqual(StdoutPane.interactionExplanation(note: row.interactionNote, captions: true), note)
        }
    }

    func testExplanationPrecedenceAndOlderSnapshotFallback() {
        XCTAssertEqual(StdoutPane.interactionExplanation(note: "Codex note", captions: true), "Codex note")
        XCTAssertEqual(StdoutPane.interactionExplanation(note: "Codex note", captions: false), "Codex note")
        XCTAssertEqual(StdoutPane.interactionExplanation(note: "", captions: true), StdoutPane.cannotTypeNote)
        XCTAssertEqual(StdoutPane.interactionExplanation(note: "", captions: false), "")
    }
}
