import XCTest
@testable import BobPanel

/// The per-screen frame store and its merge discipline. Every case runs
/// against a temp-dir `root`, so nothing here reads or writes the real
/// `~/.dark-army/panel-position.json`.
final class PlacementTests: XCTestCase {
    private var originalRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalRoot = PanelPlacement.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-placement-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot,
                                                withIntermediateDirectories: true)
        PanelPlacement.root = tempRoot
    }

    override func tearDownWithError() throws {
        PanelPlacement.root = originalRoot
        try? FileManager.default.removeItem(at: tempRoot)
    }

    private func writeRaw(_ body: [String: Any]) throws {
        let data = try JSONSerialization.data(withJSONObject: body)
        try data.write(to: PanelPlacement.path)
    }

    private func readRaw() throws -> [String: Any] {
        let data = try Data(contentsOf: PanelPlacement.path)
        return try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Merge discipline

    /// A frame save must never drop an unknown key — or a key it has never
    /// heard of, which is what a newer build's addition looks like from here.
    func testSaveFrameKeepsUnrelatedAndUnknownKeys() throws {
        try writeRaw(["legacy_dock_display": 42, "legacy_dock_locked": true,
                      "future_key": "kept"])

        PanelPlacement.saveFrame(NSRect(x: 10, y: 20, width: 300, height: 400),
                                 for: 7)

        let body = try readRaw()
        XCTAssertEqual(body["legacy_dock_display"] as? Int, 42)
        XCTAssertEqual(body["legacy_dock_locked"] as? Bool, true)
        XCTAssertEqual(body["future_key"] as? String, "kept")
        XCTAssertNotNil(body["frames"])
    }

    /// A second display's save must not clobber the first's frame.
    func testFramesArePerDisplay() {
        let a = NSRect(x: 0, y: 0, width: 800, height: 600)
        let b = NSRect(x: 2000, y: 100, width: 1200, height: 900)
        PanelPlacement.saveFrame(a, for: 1)
        PanelPlacement.saveFrame(b, for: 2)

        XCTAssertEqual(PanelPlacement.frame(for: 1), a)
        XCTAssertEqual(PanelPlacement.frame(for: 2), b)
    }

    // MARK: - Round trip and wrong states

    func testFrameRoundTrip() {
        let frame = NSRect(x: -120.5, y: 44, width: 990, height: 712)
        PanelPlacement.saveFrame(frame, for: 5)
        XCTAssertEqual(PanelPlacement.frame(for: 5), frame)
    }

    /// A display the file has never seen — the unplugged-screen case.
    func testMissingDisplayIsNil() {
        PanelPlacement.saveFrame(NSRect(x: 0, y: 0, width: 100, height: 100),
                                 for: 1)
        XCTAssertNil(PanelPlacement.frame(for: 999))
    }

    /// Garbage under `frames` decodes as absent, never as a frame — the
    /// tolerant-read rule every key in this file lives under.
    func testGarbageFramesValueIsNil() throws {
        try writeRaw(["frames": "junk"])
        XCTAssertNil(PanelPlacement.frame(for: 1))

        try writeRaw(["frames": ["1": "nope",
                                 "2": [1, 2],
                                 "3": [0, 0, 0, 0]]])
        XCTAssertNil(PanelPlacement.frame(for: 1))   // wrong shape
        XCTAssertNil(PanelPlacement.frame(for: 2))   // too short
        XCTAssertNil(PanelPlacement.frame(for: 3))   // zero size
    }

    /// An absent file is an empty store, not an error.
    func testAbsentFileIsEmpty() {
        XCTAssertNil(PanelPlacement.frame(for: 1))
        XCTAssertNil(PanelPlacement.boardProjects())
    }

    // MARK: - The pure helpers

    func testDefaultFrameIsCentredAndProportioned() {
        let visible = NSRect(x: 100, y: 50, width: 1000, height: 800)
        let frame = PanelPlacement.defaultFrame(in: visible)

        XCTAssertEqual(frame.width, 800)     // 0.8 of 1000
        XCTAssertEqual(frame.height, 640)    // 0.8 of 800
        XCTAssertEqual(frame.midX, visible.midX, accuracy: 1)
        XCTAssertEqual(frame.midY, visible.midY, accuracy: 1)
    }

    func testClampedPullsOffscreenFrameOnScreen() {
        let visible = NSRect(x: 0, y: 0, width: 1600, height: 1000)
        // Fully off the right and above the top — a frame stored on a screen
        // that has since been unplugged.
        let stranded = NSRect(x: 3000, y: 2000, width: 600, height: 400)
        let frame = PanelPlacement.clamped(stranded, to: visible)

        XCTAssertTrue(visible.contains(frame),
                      "clamped frame \(frame) not inside \(visible)")
        XCTAssertEqual(frame.size, stranded.size)
    }

    func testClampedCapsOversizeFrame() {
        let visible = NSRect(x: 0, y: 25, width: 1440, height: 875)
        let oversize = NSRect(x: -100, y: -100, width: 5000, height: 3000)
        let frame = PanelPlacement.clamped(oversize, to: visible)

        XCTAssertEqual(frame.width, visible.width)
        XCTAssertEqual(frame.height, visible.height)
        XCTAssertTrue(visible.contains(frame))
    }

    /// A stored sliver is floored back to a usable size — `frame(for:)` only
    /// refuses non-positive sizes, so the clamp is what stops a garbage-small
    /// frame round-tripping forever.
    func testClampedFloorsSliverFrame() {
        let visible = NSRect(x: 0, y: 0, width: 1600, height: 1000)
        let sliver = NSRect(x: 100, y: 100, width: 40, height: 30)
        let frame = PanelPlacement.clamped(sliver, to: visible)

        XCTAssertEqual(frame.width, PanelPlacement.minWidth)
        XCTAssertEqual(frame.height, PanelPlacement.minHeight)
        XCTAssertTrue(visible.contains(frame))
    }

    /// The floor yields to a screen smaller than it — the cap wins, so the
    /// window can never exceed the screen it must stay reachable on.
    func testFloorYieldsToATinyScreen() {
        let visible = NSRect(x: 0, y: 0, width: 400, height: 300)
        let frame = PanelPlacement.clamped(
            NSRect(x: 50, y: 50, width: 40, height: 30), to: visible)

        XCTAssertEqual(frame.width, visible.width)
        XCTAssertEqual(frame.height, visible.height)
        XCTAssertTrue(visible.contains(frame))
    }

    /// An already-good frame comes back untouched — clamping runs on every
    /// show, so it must be a no-op for the common case.
    func testClampedLeavesGoodFrameAlone() {
        let visible = NSRect(x: 0, y: 0, width: 1600, height: 1000)
        let good = NSRect(x: 200, y: 150, width: 900, height: 700)
        XCTAssertEqual(PanelPlacement.clamped(good, to: visible), good)
    }
}
