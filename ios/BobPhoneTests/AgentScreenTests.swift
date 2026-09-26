import XCTest
@testable import BobPhone

final class AgentScreenTests: XCTestCase {
    func testMainIsTheFirstScreenWhateverTheMacServes() {
        XCTAssertEqual(AgentScreen.defaultScreen(supported: true), .main)
        XCTAssertEqual(AgentScreen.defaultScreen(supported: false), .main)
        XCTAssertEqual(AgentScreen.defaultScreen, .main)
    }

    func testChipsHostedAndUnhosted() {
        XCTAssertEqual(AgentScreen.chips(hosted: false),
                       [.main, .conversation, .details])
        XCTAssertEqual(AgentScreen.chips(hosted: true),
                       [.main, .conversation, .details, .terminal])
    }

    func testOnlyMainDrawsTheWholeLead() {
        XCTAssertFalse(AgentScreen.titleOnly(.main))
        XCTAssertTrue(AgentScreen.titleOnly(.conversation))
        XCTAssertTrue(AgentScreen.titleOnly(.details))
        XCTAssertTrue(AgentScreen.titleOnly(.terminal))
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
        XCTAssertEqual(AgentScreen.detailTab(.main), .details)
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
