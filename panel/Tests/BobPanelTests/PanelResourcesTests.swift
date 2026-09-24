import XCTest
@testable import BobPanel

/// The installed app's own resource bundle is looked at before anything
/// SwiftPM hard-coded into the binary: an installed panel must never read
/// its faces out of the checkout that built it.
final class PanelResourcesTests: XCTestCase {
    func testInstalledResourcesComeFirst() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("prt-\(UUID().uuidString)")
        let app = dir.appendingPathComponent("BobPanel.app")
        let resources = app.appendingPathComponent("Contents/Resources")
        try FileManager.default.createDirectory(
            at: app.appendingPathComponent("Contents/MacOS"),
            withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: resources, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let main = try XCTUnwrap(Bundle(url: app))

        let order = PanelResources.candidates(main: main).map(\.path)
        XCTAssertEqual(order, [
            resources.appendingPathComponent(PanelResources.bundleName).path,
            app.appendingPathComponent(PanelResources.bundleName).path,
        ])
    }

    func testEveryPortraitResolvesFromTheBundle() {
        // The tree the checkout ships; a slug with no file draws an initial,
        // and that is the seam this resolver must not widen.
        for name in Cast.names + Cast.artOnly {
            XCTAssertNotNil(Cast.portrait(name), name)
        }
    }
}
