import XCTest
@testable import BobPanel

/// The rail's Table | Map choice lives in the panel's own position file
/// (`BoardFoldTests`' temp-root shape): absent is the table, only `"map"` is
/// stored, and every other key in the file survives the write.
final class FleetRailViewTests: XCTestCase {
    private var originalRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalRoot = PanelPlacement.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-fleetrail-tests-\(UUID().uuidString)",
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
        try JSONSerialization.data(withJSONObject: body).write(to: PanelPlacement.path)
    }

    private func readRaw() throws -> [String: Any] {
        // An emptied body may remove the file; that reads as no keys.
        guard let data = try? Data(contentsOf: PanelPlacement.path) else { return [:] }
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    func testAbsentKeyIsTheTable() {
        XCTAssertNil(PanelPlacement.fleetRailView())
        XCTAssertEqual(FleetRailView.fromStored(PanelPlacement.fleetRailView()), .table)
    }

    func testMapRoundTrips() {
        PanelPlacement.saveFleetRailView("map")
        XCTAssertEqual(FleetRailView.fromStored(PanelPlacement.fleetRailView()), .map)
    }

    func testSavingTableRemovesTheKey() throws {
        PanelPlacement.saveFleetRailView("map")
        PanelPlacement.saveFleetRailView("table")
        XCTAssertNil(try readRaw()["fleet_rail_view"])
        XCTAssertEqual(FleetRailView.fromStored(PanelPlacement.fleetRailView()), .table)
    }

    func testAnUnknownValueDecodesAsTable() {
        XCTAssertEqual(FleetRailView.fromStored("radar"), .table)
        XCTAssertEqual(FleetRailView.fromStored(""), .table)
        XCTAssertEqual(FleetRailView.fromStored(nil), .table)
    }

    func testAWrongTypeReadsAsAbsent() throws {
        try writeRaw(["fleet_rail_view": 7])
        XCTAssertNil(PanelPlacement.fleetRailView())
    }

    func testEveryOtherKeySurvivesTheWrite() throws {
        try writeRaw(["board_row_flips": ["r1"], "frames": ["x": 1], "board_projects": ["p"]])
        PanelPlacement.saveFleetRailView("map")
        var body = try readRaw()
        XCTAssertEqual(body["board_row_flips"] as? [String], ["r1"])
        XCTAssertEqual(body["board_projects"] as? [String], ["p"])
        XCTAssertNotNil(body["frames"])
        PanelPlacement.saveFleetRailView("table")
        body = try readRaw()
        XCTAssertEqual(body["board_row_flips"] as? [String], ["r1"])
        XCTAssertNotNil(body["frames"])
    }
}
