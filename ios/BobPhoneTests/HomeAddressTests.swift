import Combine
import XCTest
@testable import BobPhone

/// Where home is, and why a phone on its own Wi-Fi was drawing AWAY.
///
/// The pairing QR's `hosts` are a photograph taken once and kept for the life
/// of the pairing. A Mac that changed address afterwards — a DHCP lease
/// renewed, a different Wi-Fi, Ethernet instead of Wi-Fi — could never be
/// found at home again: every home probe knocked on addresses nobody was on,
/// the relay answered every time, and the phone lived on the relay from the
/// sofa. The Mac now publishes its live addresses in the snapshot the phone
/// already polls (the relay carries it too, sealed), and these are the rules
/// for learning them.
@MainActor
final class HomeAddressTests: XCTestCase {

    private func record(host: String, hosts: [String]) -> PairingRecord {
        PairingRecord(token: "t", host: host, hosts: hosts, port: 19875,
                      deviceId: "d", relayKey: "", relayURL: "",
                      sendCtr: 0, recvCtr: 0)
    }

    func testTheSnapshotCarriesTheMacsCurrentAddresses() throws {
        let body = #"{"devices": [], "hosts": ["10.0.0.7", "mac.local"]}"#
        let decoded = try JSONDecoder().decode(PhoneDevices.self,
                                               from: Data(body.utf8))
        XCTAssertEqual(decoded.hosts, ["10.0.0.7", "mac.local"])
    }

    func testAnOlderMacOffersNoneAndThatIsNotAnError() throws {
        // Tolerant decoding: an absent key must never throw, or one older
        // Mac blanks the whole snapshot.
        let decoded = try JSONDecoder().decode(PhoneDevices.self,
                                               from: Data(#"{}"#.utf8))
        XCTAssertEqual(decoded.hosts, [])
    }

    func testANewAddressIsLearned() {
        let store = PairingStore()
        store.record = record(host: "10.0.0.4", hosts: ["10.0.0.4"])
        store.learnHosts(["10.0.0.7", "mac.local"])
        XCTAssertEqual(store.record?.hosts,
                       ["10.0.0.4", "10.0.0.7", "mac.local"])
    }

    func testTheAddressOnFileKeepsItsPlace() {
        // Additive only: the walk still tries the working route first, so
        // learning can never demote an address that is answering.
        let store = PairingStore()
        store.record = record(host: "10.0.0.4",
                              hosts: ["10.0.0.4", "mac.local"])
        store.learnHosts(["10.0.0.7"])
        XCTAssertEqual(store.record?.host, "10.0.0.4")
        XCTAssertEqual(store.record?.hosts.first, "10.0.0.4")
    }

    func testAnAddressIsNeverDroppedOnOneReading() {
        // An interface that is down this second — a Mac on Ethernet with its
        // Wi-Fi asleep — must not cost the phone the address it needs
        // tomorrow.
        let store = PairingStore()
        store.record = record(host: "10.0.0.4",
                              hosts: ["10.0.0.4", "mac.local"])
        store.learnHosts(["10.0.0.4"])
        XCTAssertEqual(store.record?.hosts, ["10.0.0.4", "mac.local"])
    }

    func testNothingNewPublishesNothing() {
        // Publishing `record` restarts the client, and these addresses ride
        // *every* quiet poll: a store that rewrote itself each time would
        // restart the client for ever.
        let store = PairingStore()
        let before = record(host: "10.0.0.4", hosts: ["10.0.0.4", "mac.local"])
        store.record = before
        var publishes = 0
        let watch = store.objectWillChange.sink { _ in publishes += 1 }
        store.learnHosts(["mac.local", "10.0.0.4", "  "])
        watch.cancel()
        XCTAssertEqual(publishes, 0)
        XCTAssertEqual(store.record, before)
    }

    func testAnUnpairedPhoneLearnsNothing() {
        let store = PairingStore()
        store.record = nil
        store.learnHosts(["10.0.0.7"])
        XCTAssertNil(store.record)
    }
}
