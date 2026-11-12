import XCTest
@testable import BobPanel

/// `fleet_activity` decodes tolerantly: an older daemon sends none, a newer
/// one may send words this build does not know, and neither blanks the panel.
final class FleetActivityDecodeTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    func testAnEmptyObjectIsNotAvailable() throws {
        let section = try decode(FleetActivitySection.self, "{}")
        XCTAssertFalse(section.available)
        XCTAssertTrue(section.stations.isEmpty)
        XCTAssertTrue(section.sessions.isEmpty)
    }

    func testAMissingKeyIsCarriedNotBlanked() throws {
        let snapshot = try decode(Snapshot.self, #"{"generated_at":1}"#)
        XCTAssertTrue(snapshot.carried.contains(.fleetActivity))
        XCTAssertFalse(snapshot.fleetActivity.available)
        var held = Snapshot()
        held.fleetActivity = FleetActivitySection(available: true)
        var next = held
        DaemonClient.copy(.fleetActivity, from: held, into: &next)
        XCTAssertTrue(next.fleetActivity.available)
    }

    func testAPresentKeyIsDecodedAndNotCarried() throws {
        let snapshot = try decode(Snapshot.self, #"""
        {"fleet_activity":{"available":true,
         "stations":[{"id":"files","label":"Files"}],
         "sessions":{"s1":{"calls":[{"seq":4,"station":"files","tool":"Read","end":"running"}],
                           "station_counts":{"files":3},"errors":1}}}}
        """#)
        XCTAssertFalse(snapshot.carried.contains(.fleetActivity))
        let s1 = snapshot.fleetActivity.sessions["s1"]
        XCTAssertEqual(s1?.calls.first, FleetCall(seq: 4, station: "files",
                                                  tool: "Read", end: .running))
        XCTAssertEqual(s1?.stationCounts["files"], 3)
        XCTAssertEqual(s1?.errors, 1)
    }

    func testAnUnknownEndWordDecodesAsDone() throws {
        let call = try decode(FleetCall.self,
                              #"{"seq":1,"station":"web","tool":"x","end":"exploded"}"#)
        XCTAssertEqual(call.end, .done)
    }

    func testACallWithNoStationDecodesAsTools() throws {
        XCTAssertEqual(try decode(FleetCall.self, #"{"seq":1,"tool":"x"}"#).station, "tools")
        XCTAssertEqual(try decode(FleetCall.self, #"{"seq":1,"station":""}"#).station, "tools")
    }

    func testWrongTypedFieldsFallBackWithoutThrowing() throws {
        let s = try decode(FleetSessionActivity.self,
                           #"{"calls":"nope","station_counts":[],"errors":"x"}"#)
        XCTAssertTrue(s.calls.isEmpty)
        XCTAssertTrue(s.stationCounts.isEmpty)
        XCTAssertEqual(s.errors, 0)
        XCTAssertTrue(s.errorsByStation.isEmpty)
    }

    func testErrorsByStationDecodesAndAbsentIsEmpty() throws {
        let s = try decode(FleetSessionActivity.self,
                           #"{"errors":2,"errors_by_station":{"shell":2}}"#)
        XCTAssertEqual(s.errorsByStation, ["shell": 2])
        XCTAssertTrue(try decode(FleetSessionActivity.self, "{}").errorsByStation.isEmpty)
    }
}
