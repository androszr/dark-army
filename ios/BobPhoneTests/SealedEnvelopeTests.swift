import XCTest
@testable import BobPhone

/// `RelayTransport` — the phone's half of the sealed envelope, exercised
/// rather than grepped.
///
/// `host/tests/test_phone_remote.py` pins that the four HKDF info literals
/// are *present*. Every refusal in this file is a `guard` whose inversion
/// still compiles and changes no literal: `ctr > lastCtr` → `>=` reopens
/// replays, merging `SealNamespace.home` and `.relay` lets a home frame open
/// on the mailbox path, dropping the `written >= cap` rung accepts a
/// truncated inflate, and dropping the `https` test builds a plaintext relay
/// URL. None of those is visible to a grep; all of them are visible here.
///
/// Seam: two synthetic 32-byte keys. No network, no key store, no pairing.
///
/// **The `skewSeconds` rung is deliberately not tested.** `seal` stamps
/// `Date()` with no injection seam, so a test for it would have to hand-build
/// a frame, duplicating the sealing code it is meant to check — a test that
/// can only agree with itself.
@MainActor
final class SealedEnvelopeTests: XCTestCase {

    private let keyA = Data(repeating: 7, count: 32)
    private let keyB = Data(repeating: 9, count: 32)

    private func sealed(ctr: Int = 5, kind: String = "state",
                        body: [String: Any] = ["hello": "friend"],
                        frameId: String = "frame-1",
                        direction: RelayDirection = .phoneToMac,
                        ns: SealNamespace = .relay,
                        key: Data? = nil,
                        file: StaticString = #filePath, line: UInt = #line) -> String {
        let wire = RelayTransport.seal(
            key: key ?? keyA, direction: direction, ctr: ctr, kind: kind,
            body: body, frameId: frameId, ns: ns)
        // Fail here rather than returning "". A refusal test handed an empty
        // wire would pass for the wrong reason — every `open` refuses one —
        // so a seal that stopped working would look like a guard that works.
        XCTAssertNotNil(wire, "seal returned nil", file: file, line: line)
        return wire ?? ""
    }

    // MARK: - seal / open

    func testASealedFrameOpensWithEveryFieldIntact() {
        let wire = sealed(ctr: 5, kind: "state", body: ["hello": "friend"],
                          frameId: "frame-1")
        XCTAssertFalse(wire.isEmpty)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: wire, lastCtr: 4)
        XCTAssertEqual(refusal, "")
        XCTAssertEqual(frame?["ctr"] as? Int, 5)
        XCTAssertEqual(frame?["kind"] as? String, "state")
        XCTAssertEqual(frame?["id"] as? String, "frame-1")
        XCTAssertEqual(frame?["v"] as? Int, 1)
        XCTAssertEqual((frame?["body"] as? [String: Any])?["hello"] as? String,
                       "friend")
    }

    func testTheSameWireIsRefusedAsAReplay() {
        // The counter guard. `ctr > lastCtr` inverted to `>=` reopens every
        // frame the phone has already accepted.
        let wire = sealed(ctr: 5)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: wire, lastCtr: 5)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "ctr")
    }

    func testTheSameWireIsRefusedUnderTheOtherDirection() {
        let wire = sealed(ctr: 5)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .macToPhone, wire: wire, lastCtr: 4)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "seal")
    }

    func testARelaySealDoesNotOpenUnderTheHomeNamespace() {
        // The two families must never be merged: a home frame opening on the
        // mailbox path is the failure the file's own comment names.
        let wire = sealed(ctr: 5, ns: .relay)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: wire, lastCtr: 4, ns: .home)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "seal")
    }

    func testAHomeSealDoesNotOpenUnderTheRelayNamespace() {
        let wire = sealed(ctr: 5, ns: .home)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: wire, lastCtr: 4, ns: .relay)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "seal")
        // …and it does open under its own.
        let (good, none) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: wire, lastCtr: 4, ns: .home)
        XCTAssertEqual(none, "")
        XCTAssertNotNil(good)
    }

    func testTheSameWireIsRefusedUnderADifferentKey() {
        let wire = sealed(ctr: 5)
        let (frame, refusal) = RelayTransport.open(
            key: keyB, direction: .phoneToMac, wire: wire, lastCtr: 4)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "seal")
    }

    func testOneAlteredBase64CharacterIsRefused() {
        let wire = sealed(ctr: 5)
        var characters = Array(wire)
        let index = characters.count / 2
        characters[index] = characters[index] == "A" ? "B" : "A"
        let doctored = String(characters)
        XCTAssertNotEqual(doctored, wire)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: doctored, lastCtr: 4)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "seal")
    }

    func testSomethingThatIsNotBase64AtAllIsRefused() {
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: "not base64 !!!", lastCtr: 0)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "seal")
    }

    func testAnOversizeWireIsRefusedBeforeAnyCrypto() {
        let wire = String(repeating: "A",
                          count: RelayTransport.frameMaxBytes + 1)
        let (frame, refusal) = RelayTransport.open(
            key: keyA, direction: .phoneToMac, wire: wire, lastCtr: 0)
        XCTAssertNil(frame)
        XCTAssertEqual(refusal, "oversize")
    }

    // MARK: - the channel id

    func testTheChannelIdIsThirtyTwoLowercaseHexCharactersAndStable() {
        let id = RelayTransport.channelId(key: keyA)
        XCTAssertEqual(id.count, 32)
        XCTAssertTrue(id.allSatisfy { "0123456789abcdef".contains($0) }, id)
        XCTAssertEqual(id, RelayTransport.channelId(key: keyA))
    }

    func testADifferentKeyIsADifferentChannel() {
        XCTAssertNotEqual(RelayTransport.channelId(key: keyA),
                          RelayTransport.channelId(key: keyB))
    }

    func testOneKeyHasADifferentChannelPerNamespace() {
        XCTAssertNotEqual(RelayTransport.channelId(key: keyA, ns: .relay),
                          RelayTransport.channelId(key: keyA, ns: .home))
    }

    // MARK: - blobs

    func testABlobRoundTrips() {
        let plain = Data("a photograph, more or less".utf8)
        let raw = RelayTransport.sealBlob(
            key: keyA, direction: .phoneToMac, frameId: "f1", data: plain)
        XCTAssertNotNil(raw)
        let back = RelayTransport.openBlob(
            key: keyA, direction: .phoneToMac, frameId: "f1", raw: raw ?? Data())
        XCTAssertEqual(back, plain)
    }

    func testABlobIsBoundToItsFrameIdAndDirection() {
        let plain = Data("a photograph, more or less".utf8)
        let raw = RelayTransport.sealBlob(
            key: keyA, direction: .phoneToMac, frameId: "f1",
            data: plain) ?? Data()
        XCTAssertNil(RelayTransport.openBlob(
            key: keyA, direction: .phoneToMac, frameId: "f2", raw: raw))
        XCTAssertNil(RelayTransport.openBlob(
            key: keyA, direction: .macToPhone, frameId: "f1", raw: raw))
        XCTAssertNil(RelayTransport.openBlob(
            key: keyB, direction: .phoneToMac, frameId: "f1", raw: raw))
    }

    // Honest about what this pins: a 27-byte blob is refused whether or not
    // `openBlob` keeps its own `raw.count >= 28` rung, because CryptoKit's
    // `SealedBox(combined:)` throws on it first. Mutation-tested 7 Sep 2026 —
    // deleting that rung does not fail this test. It is a property test, not
    // coverage of the guard.
    func testABlobShorterThanANonceAndTagIsRefused() {
        XCTAssertNil(RelayTransport.openBlob(
            key: keyA, direction: .phoneToMac, frameId: "f1",
            raw: Data(repeating: 0, count: 27)))
    }

    // MARK: - raw DEFLATE

    func testDeflateAndInflateRoundTripAGenerousPayload() {
        let plain = Data(String(repeating: "hello, friend. ", count: 3_500).utf8)
        XCTAssertGreaterThan(plain.count, 50_000)
        let squeezed = RelayTransport.deflate(plain)
        XCTAssertNotNil(squeezed)
        let back = RelayTransport.inflate(squeezed ?? Data(),
                                          cap: plain.count + 1_024)
        XCTAssertEqual(back, plain)
    }

    func testInflateRefusesRatherThanTruncatingAtASmallCap() {
        // The bounded-inflate refusal: a decode that filled the whole cap may
        // have been cut off, so it is nil rather than a short answer.
        let plain = Data(String(repeating: "hello, friend. ", count: 3_500).utf8)
        let squeezed = RelayTransport.deflate(plain) ?? Data()
        XCTAssertNil(RelayTransport.inflate(squeezed, cap: 128))
    }

    func testInflateOfSomethingThatIsNotDeflateIsNil() {
        XCTAssertNil(RelayTransport.inflate(Data(repeating: 0xFF, count: 64),
                                            cap: 4_096))
    }

    // MARK: - the mailbox URL

    func testBoxURLIsBuiltOverHTTPS() {
        let url = RelayTransport.boxURL(base: "https://relay.example",
                                        channel: "abc", direction: "p2m",
                                        wait: false)
        XCTAssertEqual(url?.absoluteString,
                       "https://relay.example/api/box?ch=abc&dir=p2m")
    }

    func testBoxURLAppendsWaitOnlyWhenAsked() {
        let url = RelayTransport.boxURL(base: "https://relay.example",
                                        channel: "abc", direction: "m2p",
                                        wait: true)
        XCTAssertEqual(url?.absoluteString,
                       "https://relay.example/api/box?ch=abc&dir=m2p&wait=1")
    }

    func testBoxURLDoesNotDoubleAppendItsOwnPath() {
        let url = RelayTransport.boxURL(base: "https://relay.example/api/box",
                                        channel: "abc", direction: "p2m",
                                        wait: false)
        XCTAssertEqual(url?.absoluteString,
                       "https://relay.example/api/box?ch=abc&dir=p2m")
    }

    func testBoxURLRefusesAPlaintextOrSchemelessBase() {
        XCTAssertNil(RelayTransport.boxURL(base: "http://relay.example",
                                           channel: "abc", direction: "p2m",
                                           wait: false))
        XCTAssertNil(RelayTransport.boxURL(base: "relay.example",
                                           channel: "abc", direction: "p2m",
                                           wait: false))
    }
}
