import XCTest
@testable import BobPhone

final class AgentScreenTests: XCTestCase {
    func testTheConversationIsTheFirstScreenWhereTheMacSupportsIt() {
        XCTAssertEqual(AgentScreen.defaultScreen(supported: true), .conversation)
        XCTAssertEqual(AgentScreen.defaultScreen(supported: false), .details)
        XCTAssertEqual(AgentScreen.defaultScreen, .conversation)
    }

    func testChipsHostedAndUnhosted() {
        XCTAssertEqual(AgentScreen.chips(hosted: false),
                       [.conversation, .details])
        XCTAssertEqual(AgentScreen.chips(hosted: true).last, .terminal)
    }

    func testPaneWithoutAHostedTerminalFallsToDetails() {
        XCTAssertEqual(AgentScreen.pane(hosted: false, screen: .terminal),
                       .details)
        XCTAssertEqual(AgentScreen.pane(hosted: true, screen: .terminal),
                       .terminal)
    }

    func testDetailTabMapsConversationToDetails() {
        XCTAssertEqual(AgentScreen.detailTab(.terminal), .terminal)
        XCTAssertEqual(AgentScreen.detailTab(.conversation), .details)
        XCTAssertEqual(AgentScreen.detailTab(.details), .details)
    }

    func testClockHidesZeroAndFormatsTheRest() {
        XCTAssertEqual(ConversationRows.clock(0), "")
        XCTAssertFalse(ConversationRows.clock(1_800_000_000).isEmpty)
    }

    func testToolAndResultLines() {
        let tool = ConversationTurn(seq: 0, kind: "tool", tool: "Bash",
                                    brief: "ls")
        XCTAssertEqual(ConversationRows.toolLine(tool), "⚙ Bash · ls")
        let result = ConversationTurn(seq: 1, kind: "result", text: "ok",
                                      resultBytes: 12)
        XCTAssertEqual(ConversationRows.resultLine(result), "↳ ok · 12 bytes")
    }
}
