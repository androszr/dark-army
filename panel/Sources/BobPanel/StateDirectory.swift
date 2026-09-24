import Foundation

/// Where the panel's own files live — `~/.dark-army` in production, and a
/// throwaway temp folder while a test bundle is loaded. (Until 22 Sep 2026 it
/// was `~/.bob-companion`; the menu-bar app renames that folder in place
/// before it starts this panel, so the panel only ever names the new one.)
///
/// **Why this exists rather than a `setUp` in each test file.** Three stores
/// keep a `static var root` / `dir` seam (`CardDrafts`, `CardAttachments`,
/// `PanelPlacement`) and every one of them defaults to the *real* state
/// directory. A test that redirects the seam is safe; a test that merely
/// exercises a view model which happens to write — `state.closeEditor()` banks
/// a composer draft — writes into the user's live panel. That is exactly how a
/// run of `swift test` filled the real Drafts sheet with rows titled
/// "half typed" and "(untitled)". A per-file `setUp` fixes the files that have
/// one and leaves the next one to be written unprotected, so the default
/// itself is what moves: under XCTest there is no route to the real folder at
/// all.
///
/// The detection is the presence of the XCTest runtime in this process.
/// `BobPanel` never links XCTest, so the shipped binary always answers false
/// and takes the home-directory branch; env vars alone are not enough because
/// `swift test` and `xcodebuild` set different ones.
enum PanelStateDirectory {

    /// True while this process is a test bundle.
    static let isTesting: Bool = {
        if NSClassFromString("XCTestCase") != nil { return true }
        let env = ProcessInfo.processInfo.environment
        return env["XCTestConfigurationFilePath"] != nil
            || env["XCTestSessionIdentifier"] != nil
            || env["XCTestBundlePath"] != nil
    }()

    /// The real one. Never used as a default while `isTesting`.
    static let home: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".dark-army", isDirectory: true)

    /// One folder per test process, created eagerly so a store that only
    /// writes (and never reads back) still lands somewhere real.
    private static let sandbox: URL = {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "bob-panel-tests-\(ProcessInfo.processInfo.processIdentifier)-\(UUID().uuidString)",
                isDirectory: true)
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        return dir
    }()

    /// What every store's default reads.
    static var root: URL { isTesting ? sandbox : home }
}
