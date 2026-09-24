import CryptoKit
import XCTest
@testable import BobPhone

/// The phone's half of the typed-address pair (`SRP.swift`, `BigUInt.swift`)
/// against RFC 5054's Appendix B vector, the Mac's cross-language vector
/// (`host/tests/fixtures/pake/cross-vector.json`, written by the Mac's
/// `srp.py` and pinned there byte for byte) and the three refusals.
final class SRPTests: XCTestCase {
    // MARK: - RFC 5054 Appendix B: SHA-1, the 1024-bit group

    private static let n1024 = BigUInt(hex:
        "EEAF0AB9ADB38DD69C33F80AFA8FC5E86072618775FF3C0B9EA2314C9C256576D674DF74"
        + "96EA81D3383B4813D692C6E0E0D5D8E250B98BE48E495C1D6089DAD15DC7D7B46154D6B6"
        + "CE8EF4AD69B15D4982559B297BCF1885C529F566660E57EC68EDBC3C05726CC02FD4CBF4"
        + "976EAA9AFD5138FE8376435B9FC61D2FC0EB06E3") ?? BigUInt()

    private static let rfc = SRP.Params(N: n1024, g: BigUInt(2), hash: .sha1)

    private static let rfcSalt = "beb25379d1a8581eb5a727673a2441ee"
    private static let rfcA = "60975527035cf2ad1989806f0407210bc81edc04e2762a56afd529ddda2d4393"
    private static let rfcB = "e487cb59d31ac550471e81f00f6928e01dda08e974a004f49e61f5d105284d20"
    private static let rfcPublicA =
        "61d5e490f6f1b79547b0704c436f523dd0e560f0c64115bb72557ec44352e8903211c046"
        + "92272d8b2d1a5358a2cf1b6e0bfcf99f921530ec8e39356179eae45e42ba92aeaced8251"
        + "71e1e8b9af6d9c03e1327f44be087ef06530e69f66615261eef54073ca11cf5858f0edfd"
        + "fe15efeab349ef5d76988a3672fac47b0769447b"
    private static let rfcPublicB =
        "bd0c61512c692c0cb6d041fa01bb152d4916a1e77af46ae105393011baf38964dc46a067"
        + "0dd125b95a981652236f99d9b681cbf87837ec996c6da04453728610d0c6ddb58b318885"
        + "d7d82c7f8deb75ce7bd4fbaa37089e6f9c6059f388838e7a00030b331eb76840910440b1"
        + "b27aaeaeeb4012b7d7665238a8e3fb004b117b58"
    private static let rfcU = "ce38b9593487da98554ed47d70a7ae5f462ef019"
    private static let rfcS =
        "b0dc82babcf30674ae450c0287745e7990a3381f63b387aaf271a10d233861e359b48220"
        + "f7c4693c9ae12b0a6f67809f0876e2d013800d6c41bb59b6d5979b5c00a172b4a2a5903a"
        + "0bdcaf8a709585eb2afafa8f3499b200210dcc1f10eb33943cd67fc88a2f39a4be5bec4e"
        + "c0a3212dc346d7e474b29ede8a469ffeca686e5a"

    func testRFC5054AppendixBVector() throws {
        let params = Self.rfc
        XCTAssertEqual(params.byteLength, 128)
        XCTAssertEqual(SRP.k(params).hex(paddedTo: 20), "7556aa045aef2cdd07abaf0f665c3e818913186f")
        let salt = try XCTUnwrap(SRP.bytes(hex: Self.rfcSalt))
        let x = SRP.x(params, identity: "alice", password: "password123", salt: salt)
        XCTAssertEqual(x.hex(paddedTo: 20), "94b7555aabe9127cc58ccf4993db6cf84d16c124")
        let a = try XCTUnwrap(BigUInt(hex: Self.rfcA))
        let b = try XCTUnwrap(BigUInt(hex: Self.rfcB))
        let A = SRP.clientPublic(params, a: a)
        XCTAssertEqual(A.hex(paddedTo: 128), Self.rfcPublicA)
        // B = (k·v + g^b) mod N, computed here as the server would.
        let v = params.g.modPow(x, params.N)
        let B = (SRP.k(params) * v + params.g.modPow(b, params.N)) % params.N
        XCTAssertEqual(B.hex(paddedTo: 128), Self.rfcPublicB)
        let u = SRP.u(params, A: A, B: B)
        XCTAssertEqual(u.hex(paddedTo: 20), Self.rfcU)
        let S = try XCTUnwrap(SRP.clientSecret(params, B: B, a: a, x: x, u: u))
        XCTAssertEqual(S.hex(paddedTo: 128), Self.rfcS)
        // And through the client half, as the pairing screen drives it.
        var client = try XCTUnwrap(SRPClient(aHex: Self.rfcA, params: params))
        XCTAssertEqual(client.start(), Self.rfcPublicA)
        let outcome = client.finish(identity: "alice", saltHex: Self.rfcSalt,
                                    bHex: Self.rfcPublicB, password: "password123")
        guard case .success(let proof) = outcome else {
            return XCTFail("the RFC exchange was refused: \(outcome)")
        }
        XCTAssertEqual(proof.k, SRP.sessionKey(params, S: S))
    }

    // MARK: - The Mac's vector

    private static let crossI = "7a1b2c3d4e5f6071"
    private static let crossP = "K7PQ2XM9"
    private static let crossS = "00112233445566778899aabbccddeeff"
    private static let crossA = "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
    private static let crossPublicA =
        "630acdff5d334462d92a29e0b7fa6e20020f3333292f6d3a640f1c7a76ad9d317531c57979952e5736c88db118d060dc0539a812b9b0af3b4002380a9f28ae4a"
        + "7c45a896542de05fbcf76a4e7e0739b9a55d5d6c7aba4f1e1b58729a79bc084d5ff513eaec33ce978f5bad87e579b5a95fc773198e22697b2eadab9eb94f84cd"
        + "cf1fe94ff09f88d4ca46e968bba443ff71167571f19feb052869bd28d7dabf963b7fe399a1f70e7e08d00e1a3778ed1dddc3325dd09e05d31e774d1fd295c4ab"
        + "fbc613446232004d67cb03d6a034d2ce6ca0a544a0ff5b434b4b4267fa6c6d72acbbda2efc1ef1d1fe36d35382b089abe556862aec35b29d3d0cdf359a9cfed3"
    private static let crossPublicB =
        "7287baa59f09a08255d68ac6bd3f013b583672eb5404c2f46254e457dabb38827b5f27825b0150a2e11fd4f8211752164ac78020ac5d00d07f6660515b8c0997"
        + "a4c8714aeed27244122148a774c46d36dda5cec5a49cd1e3856d8b34c2dc6369fe21f98e29fa0e0bc30a8f0c4dd255f159b7d7fe1c67fb2d0591104e8942ab03"
        + "87a5247c535c0c59db9dd2b1681beb6aba4ae16fce1a0143cbf9c02bb2d4403a1f4291db95da8614fa589af98db62baeba6128676557cea8772e2cc610f8db34"
        + "9de1d0426645fc7d1c41d6712df327f231b471d9cd89c6903cb8c468602b4dae529725b8d5c2bfc4a9878d6fd1ebd3c89f3ee5f0e84bdeed03e383d40d0e0519"
    private static let crossK = "0ec9e22c37505dc26d53baaa6613b4c1e29e13e32f60a32679180baec31da6e0"
    private static let crossM1 = "d5669f8bb6c32a18ee48471339d5cba351193f941ccf9cf063f900b2d1713e92"
    private static let crossM2 = "a5ceb4c570e8f20e306b8c78ef5fd89ce3620b8cb7171d594cb600ef46082969"
    private static let crossHomeKey = "37a759fcb7f0771d04d43d112b33b9a397a9c7e68b76b3d796f1ebdf6f9a6143"

    private func hex(_ data: Data) -> String {
        data.map { String(format: "%02x", $0) }.joined()
    }

    func testCrossVectorMatchesTheMac() throws {
        var client = try XCTUnwrap(SRPClient(aHex: Self.crossA))
        XCTAssertEqual(client.start(), Self.crossPublicA)
        let outcome = client.finish(identity: Self.crossI, saltHex: Self.crossS,
                                    bHex: Self.crossPublicB, password: Self.crossP)
        guard case .success(let proof) = outcome else {
            return XCTFail("the Mac's exchange was refused: \(outcome)")
        }
        XCTAssertEqual(proof.m1, Self.crossM1)
        XCTAssertEqual(hex(proof.k), Self.crossK)
        XCTAssertEqual(hex(client.homeKey), Self.crossHomeKey)
        XCTAssertTrue(client.verify(m2Hex: Self.crossM2))
    }

    // MARK: - The refusals

    func testRefusesADegenerateB() throws {
        let zero = String(repeating: "00", count: 256)
        let modulus = SRP.production.N.hex(paddedTo: 256)
        for bad in [zero, modulus] {
            var client = try XCTUnwrap(SRPClient(aHex: Self.crossA))
            let outcome = client.finish(identity: Self.crossI, saltHex: Self.crossS,
                                        bHex: bad, password: Self.crossP)
            XCTAssertEqual(outcome.failure, .degenerateB)
            XCTAssertTrue(client.homeKey.isEmpty)
            XCTAssertFalse(client.verify(m2Hex: Self.crossM2))
        }
        // Out of shape: too short, upper-case, not hex.
        for bad in [String(repeating: "ab", count: 255), String(repeating: "AB", count: 256),
                    String(repeating: "zz", count: 256), ""] {
            var client = try XCTUnwrap(SRPClient(aHex: Self.crossA))
            let outcome = client.finish(identity: Self.crossI, saltHex: Self.crossS,
                                        bHex: bad, password: Self.crossP)
            XCTAssertEqual(outcome.failure, .shape, bad)
        }
    }

    func testRefusesAZeroU() throws {
        // `u` is a whole digest, so a zero one cannot be reached by choosing
        // `B`; the rule is asserted on the composition directly.
        let params = SRP.production
        let a = try XCTUnwrap(BigUInt(hex: Self.crossA))
        let x = SRP.x(params, identity: Self.crossI, password: Self.crossP,
                      salt: try XCTUnwrap(SRP.bytes(hex: Self.crossS)))
        let B = try XCTUnwrap(BigUInt(hex: Self.crossPublicB))
        XCTAssertNil(SRP.clientSecret(params, B: B, a: a, x: x, u: BigUInt()))
        XCTAssertNotNil(SRP.clientSecret(params, B: B, a: a, x: x, u: BigUInt(7)))
        XCTAssertNil(SRP.clientSecret(params, B: BigUInt(), a: a, x: x, u: BigUInt(7)))
    }

    func testRejectsAWrongM2() throws {
        var client = try XCTUnwrap(SRPClient(aHex: Self.crossA))
        _ = client.start()
        _ = client.finish(identity: Self.crossI, saltHex: Self.crossS,
                          bHex: Self.crossPublicB, password: Self.crossP)
        XCTAssertTrue(client.verify(m2Hex: Self.crossM2))
        var flipped = Array(Self.crossM2)
        flipped[0] = flipped[0] == "a" ? "b" : "a"
        XCTAssertFalse(client.verify(m2Hex: String(flipped)))
        XCTAssertFalse(client.verify(m2Hex: String(Self.crossM2.dropLast(2))))
        XCTAssertFalse(client.verify(m2Hex: ""))
        XCTAssertFalse(client.verify(m2Hex: Self.crossM2.uppercased() + "00"))
        // Before a finish, nothing verifies.
        let fresh = SRPClient()
        XCTAssertFalse(fresh.verify(m2Hex: Self.crossM2))
        XCTAssertTrue(fresh.homeKey.isEmpty)
        // A wrong code makes a different proof, and the right M2 no longer fits.
        var wrong = try XCTUnwrap(SRPClient(aHex: Self.crossA))
        _ = wrong.finish(identity: Self.crossI, saltHex: Self.crossS,
                         bHex: Self.crossPublicB, password: "WRONG123")
        XCTAssertFalse(wrong.verify(m2Hex: Self.crossM2))
    }

    func testHomeKeyInfoIsTheMacs() {
        XCTAssertEqual(SRP.homeKeyInfo, Data("bob-home v1 pake".utf8))
        XCTAssertEqual(SRP.production.byteLength, 256)
        XCTAssertEqual(SRP.production.N.bitWidth, 2048)
        XCTAssertEqual(SRP.production.g, BigUInt(2))
        // The derivation is RelayTransport.derive's: HKDF-SHA256, empty salt.
        let K = Data(repeating: 7, count: 32)
        let expected = HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: K), salt: Data(),
            info: SRP.homeKeyInfo, outputByteCount: 32).withUnsafeBytes { Data($0) }
        XCTAssertEqual(SRP.homeKey(K: K), expected)
    }
}

private extension Result {
    var failure: Failure? {
        if case .failure(let why) = self { return why }
        return nil
    }
}
