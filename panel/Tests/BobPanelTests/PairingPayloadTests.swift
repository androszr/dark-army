import XCTest
@testable import BobPanel

/// What the pairing QR actually carries. The image itself is unreadable to a
/// test, so `PairingSession.qrPayload` is the seam: get the bytes, decode
/// them, and pin the two keys the phone reads.
@MainActor
final class PairingPayloadTests: XCTestCase {
    private func decode(_ data: Data?) throws -> [String: Any] {
        let data = try XCTUnwrap(data)
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testTheWholeOrderedListRidesTheCode() throws {
        let obj = try decode(PairingSession.qrPayload(
            hosts: ["192.168.1.5", "mac.local", "10.8.0.3"],
            port: 19875, code: "ABCD"))
        XCTAssertEqual(obj["hosts"] as? [String],
                       ["192.168.1.5", "mac.local", "10.8.0.3"])
        XCTAssertEqual(obj["port"] as? Int, 19875)
        XCTAssertEqual(obj["code"] as? String, "ABCD")
    }

    /// The old single-address key stays, and stays equal to the best
    /// candidate — a phone build from before the list reads only this one.
    func testHostIsTheFirstCandidate() throws {
        let obj = try decode(PairingSession.qrPayload(
            hosts: ["192.168.1.5", "mac.local", "10.8.0.3"],
            port: 19875, code: "ABCD"))
        XCTAssertEqual(obj["host"] as? String, "192.168.1.5")
    }

    func testNoCandidatesIsEmptyRatherThanACrash() throws {
        let obj = try decode(PairingSession.qrPayload(
            hosts: [], port: 19875, code: "ABCD"))
        XCTAssertEqual(obj["host"] as? String, "")
        XCTAssertEqual(obj["hosts"] as? [String], [])
    }

    /// The home key rides the square when the daemon minted one — the one
    /// place it travels, so the phone's first request is already sealed.
    func testTheHomeKeyRidesTheCodeWhenGiven() throws {
        let obj = try decode(PairingSession.qrPayload(
            hosts: ["192.168.1.5"], port: 19875, code: "ABCD",
            homeKey: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="))
        XCTAssertEqual(obj["home_key"] as? String,
                       "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
        XCTAssertEqual(obj["code"] as? String, "ABCD")
    }

    /// An older daemon sends no key, and the QR must then be byte-identical
    /// to what it always was: no `home_key` at all, never an empty one.
    func testAnEmptyHomeKeyIsAbsentFromTheCode() throws {
        let obj = try decode(PairingSession.qrPayload(
            hosts: ["192.168.1.5"], port: 19875, code: "ABCD", homeKey: ""))
        XCTAssertNil(obj["home_key"])
        XCTAssertEqual(Set(obj.keys), ["host", "hosts", "port", "code"])
        XCTAssertEqual(
            PairingSession.qrPayload(hosts: ["192.168.1.5"], port: 19875,
                                     code: "ABCD", homeKey: ""),
            PairingSession.qrPayload(hosts: ["192.168.1.5"], port: 19875,
                                     code: "ABCD"))
    }

    /// The loopback request with the tick off is exactly what every earlier
    /// build sent — one key — so an older daemon sees nothing new.
    func testBeginPayloadIsByteIdenticalWhenTypingIsOff() throws {
        let payload = PairingSession.beginPayload(allowTyped: false)
        XCTAssertEqual(Set(payload.keys), ["action"])
        XCTAssertEqual(payload["action"] as? String, "begin_pairing")
        let bytes = try JSONSerialization.data(withJSONObject: payload)
        let legacy = try JSONSerialization.data(
            withJSONObject: ["action": "begin_pairing"])
        XCTAssertEqual(bytes, legacy)
    }

    func testBeginPayloadCarriesAllowTypedTrue() {
        let payload = PairingSession.beginPayload(allowTyped: true)
        XCTAssertEqual(payload["action"] as? String, "begin_pairing")
        XCTAssertEqual(payload["allow_typed"] as? Bool, true)
        XCTAssertEqual(Set(payload.keys), ["action", "allow_typed"])
    }

    /// The tick's state is the daemon's echo, and an older reply (no
    /// `allow_typed`) draws it off.
    func testAllowsTypingIsReadOffTheResult() {
        XCTAssertFalse(session().allowsTyping)
        let armed = PairingSession(
            result: PairingResult(
                ok: true, detail: "", code: "ABCD",
                host: "192.168.1.5", hosts: ["192.168.1.5"],
                port: 19875,
                expiresAt: Date().timeIntervalSince1970 + 120,
                allowTyped: true),
            knownIds: [])
        XCTAssertTrue(armed.allowsTyping)
        XCTAssertFalse(PairingResult(ok: true, detail: "").allowTyped)
    }

    private func session() -> PairingSession {
        PairingSession(
            result: PairingResult(
                ok: true, detail: "", code: "ABCD",
                host: "192.168.1.5", hosts: ["192.168.1.5"],
                port: 19875,
                expiresAt: Date().timeIntervalSince1970 + 120),
            knownIds: [])
    }

    func testNotePairingOpenLatchesTrueThenFalse() {
        let s = session()
        XCTAssertFalse(s.notePairingOpen(false))
        XCTAssertFalse(s.notePairingOpen(true))
        XCTAssertTrue(s.notePairingOpen(false))
    }

    func testNotePairingOpenNeverClosesIfItOnlySeesFalse() {
        let s = session()
        XCTAssertFalse(s.notePairingOpen(false))
        XCTAssertFalse(s.notePairingOpen(false))
        XCTAssertFalse(s.notePairingOpen(false))
    }
}
