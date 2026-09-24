import XCTest
@testable import BobPanel

/// The devices section of the snapshot, and the one distinction the whole
/// surface rests on: **absent is not empty**.
final class DevicesDecodeTests: XCTestCase {

    private func decode(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    func testAnAbsentSectionIsNotAnEmptyOne() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {}}
        """)
        XCTAssertFalse(snap.devices.available)
        XCTAssertTrue(snap.devices.devices.isEmpty)
        XCTAssertFalse(snap.devices.lanEnabled)
    }

    func testAPresentSectionWithUnknownKeysDecodes() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "lan_enabled": true, "port": 19875,
                     "devices": [{"id": "abc", "name": "Kitchen",
                                  "paired_at": 1, "last_seen": 2}],
                     "a_future_field": 7}}
        """)
        XCTAssertTrue(snap.devices.available)
        XCTAssertTrue(snap.devices.lanEnabled)
        XCTAssertEqual(snap.devices.port, 19875)
        XCTAssertEqual(snap.devices.devices.count, 1)
        XCTAssertEqual(snap.devices.devices.first?.id, "abc")
        XCTAssertEqual(snap.devices.devices.first?.name, "Kitchen")
        XCTAssertEqual(snap.devices.devices.first?.lastSeen ?? 0, 2, accuracy: 0.001)
    }

    func testADeviceRowMissingOptionalFieldsDecodes() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "devices": [{"id": "xyz"}]}}
        """)
        XCTAssertEqual(snap.devices.devices.count, 1)
        XCTAssertEqual(snap.devices.devices[0].id, "xyz")
        XCTAssertEqual(snap.devices.devices[0].name, "")
        XCTAssertEqual(snap.devices.devices[0].pairedAt, 0)
        XCTAssertEqual(snap.devices.devices[0].lastSeen, 0)
        XCTAssertEqual(snap.devices.devices[0].lastFrameAt, 0)
    }

    /// The away-health fields. Absent must stay distinguishable from zero:
    /// an older daemon publishes neither, and the Devices menu must then
    /// draw no health line at all rather than a healthy-looking one.
    func testAwayHealthAndLastFrameDecodeWhenStated() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "remote_enabled": true,
                     "relay_health": {"state": "failing", "status": 429,
                                      "failures": 12, "failing_for": 300,
                                      "last_ok_at": 1700},
                     "devices": [{"id": "abc", "last_frame_at": 1690}]}}
        """)
        XCTAssertTrue(snap.devices.relayHealth.stated)
        XCTAssertTrue(snap.devices.relayHealth.isFailing)
        XCTAssertEqual(snap.devices.relayHealth.status, 429)
        XCTAssertEqual(snap.devices.relayHealth.failures, 12)
        XCTAssertEqual(snap.devices.relayHealth.failingFor, 300, accuracy: 0.001)
        XCTAssertEqual(snap.devices.relayHealth.lastOkAt, 1700, accuracy: 0.001)
        XCTAssertEqual(snap.devices.devices[0].lastFrameAt, 1690, accuracy: 0.001)
    }

    func testAnOlderDaemonStatesNoAwayHealth() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "remote_enabled": true,
                     "devices": [{"id": "abc"}]}}
        """)
        XCTAssertFalse(snap.devices.relayHealth.stated)
        XCTAssertFalse(snap.devices.relayHealth.isFailing)
        XCTAssertEqual(snap.devices.relayHealth.lastOkAt, 0)
        XCTAssertEqual(snap.devices.devices[0].lastFrameAt, 0)
    }

    func testAbsentPairingOpenDecodesFalse() throws {
        let snap = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "devices": []}}
        """)
        XCTAssertFalse(snap.devices.pairingOpen)
    }

    func testPairingOpenRoundTrips() throws {
        let open = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "pairing_open": true, "devices": []}}
        """)
        XCTAssertTrue(open.devices.pairingOpen)
        let closed = try decode("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "pairing_open": false, "devices": []}}
        """)
        XCTAssertFalse(closed.devices.pairingOpen)
    }
}
