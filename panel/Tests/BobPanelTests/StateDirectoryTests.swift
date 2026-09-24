import XCTest
@testable import BobPanel

/// The guard that keeps a test run out of the user's live `~/.dark-army`.
///
/// A run of the suite once filled the real Drafts sheet with rows titled
/// "half typed" and "(untitled)" — view-model tests calling `closeEditor()`,
/// which banks a composer draft through `CardDrafts`, whose default root was
/// the real folder. These cases pin the default, not any one test's `setUp`.
final class StateDirectoryTests: XCTestCase {

    func testTheProcessIsRecognisedAsATestBundle() {
        XCTAssertTrue(PanelStateDirectory.isTesting)
    }

    func testTheSandboxIsNotTheRealStateDirectory() {
        XCTAssertNotEqual(PanelStateDirectory.root.standardizedFileURL,
                          PanelStateDirectory.home.standardizedFileURL)
        XCTAssertTrue(FileManager.default
            .fileExists(atPath: PanelStateDirectory.root.path))
    }

    /// Every store that writes files defaults through the sandbox. A new one
    /// added below the home directory would fail here.
    func testEveryPanelStoreDefaultsInsideTheSandbox() {
        let home = PanelStateDirectory.home.standardizedFileURL.path
        for path in [CardDrafts.path.path,
                     CardAttachments.dir.path,
                     PanelPlacement.path.path,
                     PanelLock.path.path] {
            XCTAssertFalse(path.hasPrefix(home),
                           "\(path) writes into the live state directory")
        }
    }

    /// The one that actually bit: banking a draft with no redirection at all
    /// must not reach the real file.
    @MainActor
    func testBankingADraftUnderTestNeverTouchesTheRealFile() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        state.draft.title = "half typed"
        state.closeEditor()
        let real = PanelStateDirectory.home
            .appendingPathComponent("card-drafts.json")
        let banked = (try? Data(contentsOf: CardDrafts.path)).map {
            String(decoding: $0, as: UTF8.self)
        } ?? ""
        XCTAssertTrue(banked.contains("half typed"),
                      "the draft still banks — into the sandbox")
        XCTAssertNotEqual(CardDrafts.path.standardizedFileURL, real)
    }
}
