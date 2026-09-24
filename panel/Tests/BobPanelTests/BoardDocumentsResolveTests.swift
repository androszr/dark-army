import XCTest
@testable import BobPanel

/// Containment for the card sheet's document reader: the panel reads the file
/// itself, so `resolve` is the only thing between an agent-written prompt and
/// an arbitrary path on the user's disk. Nothing may escape the card's own
/// project root — not an absolute path, not a `../` walk, not a symlink
/// written inside the root that points out of it.
final class BoardDocumentsResolveTests: XCTestCase {
    private var root: URL!
    private var outside: URL!

    override func setUpWithError() throws {
        let base = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-docs-\(UUID().uuidString)")
        root = base.appendingPathComponent("project")
        outside = base.appendingPathComponent("outside")
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: outside,
                                                withIntermediateDirectories: true)
        try "inside".write(to: root.appendingPathComponent("plan.md"),
                           atomically: true, encoding: .utf8)
        try "secret".write(to: outside.appendingPathComponent("secret.md"),
                           atomically: true, encoding: .utf8)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root.deletingLastPathComponent())
    }

    func testAPlainRelativePathInsideResolves() {
        let resolved = BoardDocuments.resolve("plan.md", root: root.path)
        XCTAssertNotNil(resolved)
        // The answer is the real (symlink-resolved) location of the file.
        XCTAssertEqual(resolved,
                       root.appendingPathComponent("plan.md")
                           .resolvingSymlinksInPath().path)
    }

    func testAnAbsolutePathOutsideTheRootIsRefused() {
        let target = outside.appendingPathComponent("secret.md").path
        XCTAssertNil(BoardDocuments.resolve(target, root: root.path))
    }

    func testADotDotWalkOutOfTheRootIsRefused() {
        XCTAssertNil(BoardDocuments.resolve("../outside/secret.md",
                                            root: root.path))
        // Deeper walks are no better.
        XCTAssertNil(BoardDocuments.resolve("a/../../outside/secret.md",
                                            root: root.path))
    }

    func testASymlinkOutOfTheRootIsRefused() throws {
        // The link lives *inside* the root, so a purely lexical prefix test
        // would pass it — the resolved target is what must be judged.
        let link = root.appendingPathComponent("looks-local.md")
        try FileManager.default.createSymbolicLink(
            at: link,
            withDestinationURL: outside.appendingPathComponent("secret.md"))
        XCTAssertNil(BoardDocuments.resolve("looks-local.md", root: root.path))
    }

    func testADirectoryIsNotADocument() {
        XCTAssertNil(BoardDocuments.resolve(".", root: root.path))
    }
}
