import XCTest
@testable import BobPanel

/// The shared Claude / Codex / Grok display name, and the rail mark size.
final class ProviderMarkTests: XCTestCase {

    func testEmptyAndClaudeAreClaude() {
        XCTAssertEqual(ProviderMark.displayName(""), "Claude")
        XCTAssertEqual(ProviderMark.displayName("claude"), "Claude")
    }

    func testGrokAndCodexAreTitleCase() {
        XCTAssertEqual(ProviderMark.displayName("grok"), "Grok")
        XCTAssertEqual(ProviderMark.displayName("codex"), "Codex")
    }

    func testUnknownMatchesTheDefaultMark() {
        XCTAssertEqual(ProviderMark.displayName("cursor"), "Claude")
    }

    func testRailMarkSizeIsTen() {
        XCTAssertEqual(ProcessRow.providerMarkSize, 10)
    }
}
