import CryptoKit
import Foundation

/// The phone's half of the typed-address pair: SRP-6a over the pairing
/// code, `host/dark_army_daemon/srp.py`'s twin byte for byte on the
/// padding rule — every hashed group element (`g` in `k`, `A` and `B` in
/// `u` and the proofs, `S` before `K`) is left-padded to the group's byte
/// length. RFC 5054's 2048-bit group with SHA-256 in production; SHA-1 and
/// the 1024-bit group exist so RFC 5054's Appendix B vector pins the
/// arithmetic in `SRPTests`. `host/tests/fixtures/pake/cross-vector.json`
/// is the fixed exchange both ends must reproduce.
///
///     k  = H(N ‖ pad(g))
///     x  = H(s ‖ H(I ‖ ":" ‖ P))
///     A  = g^a mod N
///     u  = H(pad(A) ‖ pad(B))
///     S  = (B − k·g^x)^(a + u·x) mod N
///     K  = H(pad(S))
///     M1 = H((H(N) ⊕ H(g)) ‖ H(I) ‖ s ‖ pad(A) ‖ pad(B) ‖ K)
///     M2 = H(pad(A) ‖ M1 ‖ K)
///     home key = HKDF-SHA256(K, salt: empty, info: homeKeyInfo)
///
/// The client refuses `B mod N == 0` and `u == 0` — the two values a
/// hostile Mac could choose to make `S` independent of the code — and
/// compares `M2` in constant time. The password is handed in as typed by
/// the caller (`PairingStore` trims and upper-cases the code as the Mac's
/// redeem does); nothing here normalises it, so the RFC vector runs as
/// written.
enum SRP {
    /// The home key's HKDF label. `srp.INFO_PAKE_HOME` on the Mac.
    static let homeKeyInfo = Data("bob-home v1 pake".utf8)

    enum Hash {
        case sha256
        case sha1

        func digest(_ parts: [Data]) -> Data {
            switch self {
            case .sha256:
                var h = SHA256()
                for part in parts { h.update(data: part) }
                return Data(h.finalize())
            case .sha1:
                var h = Insecure.SHA1()
                for part in parts { h.update(data: part) }
                return Data(h.finalize())
            }
        }
    }

    struct Params {
        let N: BigUInt
        let g: BigUInt
        let hash: Hash

        var byteLength: Int { (N.bitWidth + 7) / 8 }
    }

    /// RFC 5054, Appendix A: the 2048-bit group, generator 2 — the same
    /// hex as `srp.GROUP_2048`.
    static let group2048: (N: BigUInt, g: BigUInt) = (
        N: BigUInt(hex:
            "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
            + "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
            + "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
            + "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
            + "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
            + "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6"
            + "AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
            + "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73")
            ?? BigUInt(),
        g: BigUInt(2))

    static let production = Params(N: group2048.N, g: group2048.g, hash: .sha256)

    // MARK: - The compositions

    static func pad(_ value: BigUInt, _ params: Params) -> Data {
        value.data(paddedTo: params.byteLength)
    }

    static func k(_ params: Params) -> BigUInt {
        BigUInt(data: params.hash.digest([pad(params.N, params), pad(params.g, params)]))
    }

    static func x(_ params: Params, identity: String, password: String, salt: Data) -> BigUInt {
        let inner = params.hash.digest([Data(identity.utf8), Data(":".utf8), Data(password.utf8)])
        return BigUInt(data: params.hash.digest([salt, inner]))
    }

    static func clientPublic(_ params: Params, a: BigUInt) -> BigUInt {
        params.g.modPow(a, params.N)
    }

    static func u(_ params: Params, A: BigUInt, B: BigUInt) -> BigUInt {
        BigUInt(data: params.hash.digest([pad(A, params), pad(B, params)]))
    }

    /// `S = (B − k·g^x)^(a + u·x) mod N`, or `nil` for a degenerate `B` or
    /// a zero `u` — the caller says which in words.
    static func clientSecret(_ params: Params, B: BigUInt, a: BigUInt, x: BigUInt,
                             u: BigUInt) -> BigUInt? {
        guard !(B % params.N).isZero, !u.isZero else { return nil }
        let kgx = (k(params) * params.g.modPow(x, params.N)) % params.N
        // (B − k·g^x) mod N with unsigned arithmetic: add N before taking
        // the difference, then reduce.
        let base = ((B % params.N) + params.N - kgx) % params.N
        return base.modPow(a + u * x, params.N)
    }

    static func sessionKey(_ params: Params, S: BigUInt) -> Data {
        params.hash.digest([pad(S, params)])
    }

    static func clientProof(_ params: Params, identity: String, salt: Data,
                            A: BigUInt, B: BigUInt, K: Data) -> Data {
        let hn = params.hash.digest([pad(params.N, params)])
        let hg = params.hash.digest([pad(params.g, params)])
        var mixed = Data(count: hn.count)
        for i in 0..<hn.count {
            mixed[i] = hn[hn.startIndex + i] ^ hg[hg.startIndex + i]
        }
        return params.hash.digest([
            mixed, params.hash.digest([Data(identity.utf8)]), salt,
            pad(A, params), pad(B, params), K,
        ])
    }

    static func serverProof(_ params: Params, A: BigUInt, M1: Data, K: Data) -> Data {
        params.hash.digest([pad(A, params), M1, K])
    }

    /// The device's 32-byte home key from the session key —
    /// `RelayTransport.derive`'s exact call: HKDF-SHA256 with an empty salt,
    /// which is what `relay._hkdf` computes on the Mac.
    static func homeKey(K: Data) -> Data {
        HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: K),
            salt: Data(),
            info: homeKeyInfo,
            outputByteCount: 32
        ).withUnsafeBytes { Data($0) }
    }

    /// Exactly `length` lowercase hex characters, the shape every group
    /// element and proof arrives in. Checked before any arithmetic.
    static func isLowercaseHex(_ text: String, length: Int) -> Bool {
        guard text.utf8.count == length else { return false }
        return text.utf8.allSatisfy { ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x61 && $0 <= 0x66) }
    }

    /// Bytes from hex in either case, `nil` for an odd length, an empty
    /// string or a non-hex character.
    static func bytes(hex: String) -> Data? {
        guard !hex.isEmpty, hex.utf8.count % 2 == 0 else { return nil }
        guard let value = BigUInt(hex: hex) else { return nil }
        return value.data(paddedTo: hex.utf8.count / 2)
    }

    /// Constant-time equality over bytes: a byte-wise OR fold, never `==`
    /// on `Data`, and a length mismatch is simply false.
    static func constantTimeEqual(_ lhs: Data, _ rhs: Data) -> Bool {
        guard lhs.count == rhs.count else { return false }
        var acc: UInt8 = 0
        for i in 0..<lhs.count {
            acc |= lhs[lhs.startIndex + i] ^ rhs[rhs.startIndex + i]
        }
        return acc == 0
    }
}

enum SRPError: Error, Equatable {
    /// The Mac's `B` or salt was not the shape asked for.
    case shape
    /// `B mod N == 0`: a value no honest Mac sends.
    case degenerateB
    /// `u == 0`: the scrambler vanished, which no honest exchange produces.
    case zeroU

    /// The phone's own sentence for each; `PairingStore` shows `.shape` as
    /// `PhoneActions.pakeOutOfStep` and the two degenerate cases as these.
    var words: String {
        switch self {
        case .shape:
            return "The Mac's pairing answer was not the shape this phone asked for."
        case .degenerateB:
            return "The Mac's pairing answer was not a valid exchange (B) — start a new code on the Mac and try again."
        case .zeroU:
            return "The Mac's pairing answer was not a valid exchange (u) — start a new code on the Mac and try again."
        }
    }
}

/// One typed pair's client state: `a` chosen at init, `A` from `start()`,
/// `K` and `M1` after `finish`, the home key derived from `K`.
struct SRPClient {
    let params: SRP.Params
    private let a: BigUInt
    private let A: BigUInt
    private var K = Data()
    private var M1 = Data()

    /// A fresh 32-byte ephemeral from the system's generator.
    init(params: SRP.Params = SRP.production) {
        var generator = SystemRandomNumberGenerator()
        var bytes = [UInt8](repeating: 0, count: 32)
        for i in 0..<bytes.count {
            bytes[i] = UInt8.random(in: UInt8.min...UInt8.max, using: &generator)
        }
        var value = BigUInt(data: Data(bytes))
        if value.isZero { value = BigUInt(1) }
        self.init(a: value, params: params)
    }

    /// A pinned ephemeral — for the tests and the cross-language harness.
    init?(aHex: String, params: SRP.Params = SRP.production) {
        guard let value = BigUInt(hex: aHex), !value.isZero else { return nil }
        self.init(a: value, params: params)
    }

    private init(a: BigUInt, params: SRP.Params) {
        self.params = params
        self.a = a
        self.A = SRP.clientPublic(params, a: a)
    }

    /// `A`, padded lowercase hex — the first request's one field.
    func start() -> String {
        A.hex(paddedTo: params.byteLength)
    }

    /// The proof. Refuses a `B` that is not the group's length in lowercase
    /// hex (`.shape`), `B mod N == 0` (`.degenerateB`) and `u == 0`
    /// (`.zeroU`); otherwise `M1` as hex and `K`, both kept on the client
    /// for `verify` and `homeKey`.
    mutating func finish(identity: String, saltHex: String, bHex: String,
                         password: String) -> Result<(m1: String, k: Data), SRPError> {
        guard SRP.isLowercaseHex(bHex, length: params.byteLength * 2),
              let B = BigUInt(hex: bHex),
              let salt = SRP.bytes(hex: saltHex) else {
            return .failure(.shape)
        }
        guard !(B % params.N).isZero else { return .failure(.degenerateB) }
        let u = SRP.u(params, A: A, B: B)
        guard !u.isZero else { return .failure(.zeroU) }
        let x = SRP.x(params, identity: identity, password: password, salt: salt)
        guard let S = SRP.clientSecret(params, B: B, a: a, x: x, u: u) else {
            return .failure(.degenerateB)
        }
        K = SRP.sessionKey(params, S: S)
        M1 = SRP.clientProof(params, identity: identity, salt: salt, A: A, B: B, K: K)
        return .success((m1: M1.map { String(format: "%02x", $0) }.joined(), k: K))
    }

    /// Whether the Mac's `M2` is the one this exchange expects. False
    /// before `finish` succeeded, for a wrong length, and for a wrong
    /// value; compared in constant time.
    func verify(m2Hex: String) -> Bool {
        guard !K.isEmpty, !M1.isEmpty, let m2 = SRP.bytes(hex: m2Hex) else { return false }
        let expected = SRP.serverProof(params, A: A, M1: M1, K: K)
        return SRP.constantTimeEqual(expected, m2)
    }

    /// The derived home key; empty before `finish` succeeded.
    var homeKey: Data {
        K.isEmpty ? Data() : SRP.homeKey(K: K)
    }
}
