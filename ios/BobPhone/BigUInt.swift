import Foundation

/// An unsigned big integer for one job: the three modular exponentiations
/// of the typed-address pair (`SRP.swift`). Foundation only, `[UInt32]`
/// limbs little-endian and normalised (no trailing zero limbs), schoolbook
/// multiplication, Knuth's Algorithm D for the remainder, square-and-multiply
/// for `modPow`. Correctness over speed — the whole cost is bounded by one
/// pairing — and no force unwrap anywhere: `init?(hex:)` is failable and
/// every conversion goes through `truncatingIfNeeded`. Not used anywhere but
/// `SRP.swift`.
struct BigUInt: Equatable, Comparable {
    /// Little-endian limbs, normalised: the last limb is never zero, and
    /// zero itself is the empty array.
    private(set) var limbs: [UInt32]

    init() {
        limbs = []
    }

    init(_ value: UInt32) {
        limbs = value == 0 ? [] : [value]
    }

    /// Big-endian bytes, as a group element arrives on the wire.
    init(data: Data) {
        var out: [UInt32] = []
        out.reserveCapacity(data.count / 4 + 1)
        var acc: UInt32 = 0
        var shift: UInt32 = 0
        for byte in data.reversed() {
            acc |= UInt32(byte) << shift
            shift += 8
            if shift == 32 {
                out.append(acc)
                acc = 0
                shift = 0
            }
        }
        if shift > 0 { out.append(acc) }
        limbs = out
        normalise()
    }

    /// Hex in either case, any length; `nil` for an empty string or a
    /// character that is not a hex digit. The shape a caller wants (length,
    /// lowercase) is the caller's check, before this.
    init?(hex: String) {
        guard !hex.isEmpty else { return nil }
        var bytes: [UInt8] = []
        bytes.reserveCapacity(hex.count / 2 + 1)
        let digits = Array(hex.utf8)
        var index = digits.count
        while index > 0 {
            let low = digits[index - 1]
            let high: UInt8 = index >= 2 ? digits[index - 2] : 0x30
            guard let lo = Self.nibble(low), let hi = Self.nibble(high) else { return nil }
            bytes.append(hi << 4 | lo)
            index -= 2
        }
        self.init(data: Data(bytes.reversed()))
    }

    private static func nibble(_ c: UInt8) -> UInt8? {
        switch c {
        case 0x30...0x39: return c - 0x30
        case 0x61...0x66: return c - 0x61 + 10
        case 0x41...0x46: return c - 0x41 + 10
        default: return nil
        }
    }

    private mutating func normalise() {
        while let last = limbs.last, last == 0 {
            limbs.removeLast()
        }
    }

    var isZero: Bool { limbs.isEmpty }

    var bitWidth: Int {
        guard let top = limbs.last else { return 0 }
        return (limbs.count - 1) * 32 + (32 - top.leadingZeroBitCount)
    }

    /// Big-endian bytes, left-padded with zeros to `paddedTo`; a value
    /// longer than that is returned whole, never truncated.
    func data(paddedTo: Int) -> Data {
        var bytes: [UInt8] = []
        bytes.reserveCapacity(max(paddedTo, limbs.count * 4))
        for limb in limbs {
            bytes.append(UInt8(truncatingIfNeeded: limb))
            bytes.append(UInt8(truncatingIfNeeded: limb >> 8))
            bytes.append(UInt8(truncatingIfNeeded: limb >> 16))
            bytes.append(UInt8(truncatingIfNeeded: limb >> 24))
        }
        while let last = bytes.last, last == 0 {
            bytes.removeLast()
        }
        while bytes.count < paddedTo {
            bytes.append(0)
        }
        return Data(bytes.reversed())
    }

    /// Lowercase hex, padded as `data(paddedTo:)` pads.
    func hex(paddedTo: Int) -> String {
        data(paddedTo: paddedTo).map { String(format: "%02x", $0) }.joined()
    }

    private func bit(_ index: Int) -> Bool {
        let limb = index / 32
        guard limb < limbs.count else { return false }
        return (limbs[limb] >> UInt32(index % 32)) & 1 == 1
    }

    // MARK: - Comparison

    static func < (lhs: BigUInt, rhs: BigUInt) -> Bool {
        if lhs.limbs.count != rhs.limbs.count {
            return lhs.limbs.count < rhs.limbs.count
        }
        var index = lhs.limbs.count
        while index > 0 {
            index -= 1
            if lhs.limbs[index] != rhs.limbs[index] {
                return lhs.limbs[index] < rhs.limbs[index]
            }
        }
        return false
    }

    // MARK: - Arithmetic

    static func + (lhs: BigUInt, rhs: BigUInt) -> BigUInt {
        let count = max(lhs.limbs.count, rhs.limbs.count)
        var out = [UInt32](repeating: 0, count: count + 1)
        var carry: UInt64 = 0
        for i in 0..<count {
            let a = i < lhs.limbs.count ? UInt64(lhs.limbs[i]) : 0
            let b = i < rhs.limbs.count ? UInt64(rhs.limbs[i]) : 0
            let sum = a + b + carry
            out[i] = UInt32(truncatingIfNeeded: sum)
            carry = sum >> 32
        }
        out[count] = UInt32(truncatingIfNeeded: carry)
        var result = BigUInt()
        result.limbs = out
        result.normalise()
        return result
    }

    /// `lhs - rhs`; `lhs >= rhs` is a precondition, as the callers here
    /// guarantee (`S = (B − k·g^x)` is taken modulo `N` by adding `N` first).
    static func - (lhs: BigUInt, rhs: BigUInt) -> BigUInt {
        precondition(lhs >= rhs, "BigUInt subtraction would go negative")
        var out = [UInt32](repeating: 0, count: lhs.limbs.count)
        var borrow: Int64 = 0
        for i in 0..<lhs.limbs.count {
            let a = Int64(lhs.limbs[i])
            let b = i < rhs.limbs.count ? Int64(rhs.limbs[i]) : 0
            var diff = a - b - borrow
            if diff < 0 {
                diff += 1 << 32
                borrow = 1
            } else {
                borrow = 0
            }
            out[i] = UInt32(truncatingIfNeeded: diff)
        }
        var result = BigUInt()
        result.limbs = out
        result.normalise()
        return result
    }

    static func * (lhs: BigUInt, rhs: BigUInt) -> BigUInt {
        if lhs.isZero || rhs.isZero { return BigUInt() }
        var out = [UInt32](repeating: 0, count: lhs.limbs.count + rhs.limbs.count)
        for i in 0..<lhs.limbs.count {
            let a = UInt64(lhs.limbs[i])
            if a == 0 { continue }
            var carry: UInt64 = 0
            for j in 0..<rhs.limbs.count {
                let t = a * UInt64(rhs.limbs[j]) + UInt64(out[i + j]) + carry
                out[i + j] = UInt32(truncatingIfNeeded: t)
                carry = t >> 32
            }
            out[i + rhs.limbs.count] = UInt32(truncatingIfNeeded: carry)
        }
        var result = BigUInt()
        result.limbs = out
        result.normalise()
        return result
    }

    /// `lhs mod rhs`; a zero modulus is a precondition failure.
    static func % (lhs: BigUInt, rhs: BigUInt) -> BigUInt {
        precondition(!rhs.isZero, "BigUInt modulo by zero")
        if lhs < rhs { return lhs }
        if rhs.limbs.count == 1 {
            let d = UInt64(rhs.limbs[0])
            var rem: UInt64 = 0
            var index = lhs.limbs.count
            while index > 0 {
                index -= 1
                rem = ((rem << 32) | UInt64(lhs.limbs[index])) % d
            }
            return BigUInt(UInt32(truncatingIfNeeded: rem))
        }
        return Self.remainder(lhs.limbs, rhs.limbs)
    }

    /// Knuth, TAOCP vol. 2, Algorithm D (as in Hacker's Delight's
    /// `divmnu`): normalise so the divisor's top limb has its high bit
    /// set, estimate each quotient digit from the top two limbs, correct
    /// it at most twice, multiply-subtract, add back on a rare overshoot.
    /// Only the remainder is kept.
    private static func remainder(_ dividend: [UInt32], _ divisor: [UInt32]) -> BigUInt {
        let n = divisor.count
        let m = dividend.count - n
        let shift = UInt32(divisor[n - 1].leadingZeroBitCount)
        let v = shiftLeft(divisor, by: shift, extra: false)
        var u = shiftLeft(dividend, by: shift, extra: true)
        let base: UInt64 = 1 << 32
        var j = m
        while j >= 0 {
            let top = (UInt64(u[j + n]) << 32) | UInt64(u[j + n - 1])
            var qhat = top / UInt64(v[n - 1])
            var rhat = top % UInt64(v[n - 1])
            while qhat >= base || qhat * UInt64(v[n - 2]) > ((rhat << 32) | UInt64(u[j + n - 2])) {
                qhat -= 1
                rhat += UInt64(v[n - 1])
                if rhat >= base { break }
            }
            var k: Int64 = 0
            var t: Int64 = 0
            for i in 0..<n {
                let p = qhat * UInt64(v[i])
                t = Int64(u[i + j]) - k - Int64(p & 0xFFFF_FFFF)
                u[i + j] = UInt32(truncatingIfNeeded: t)
                k = Int64(p >> 32) - (t >> 32)
            }
            t = Int64(u[j + n]) - k
            u[j + n] = UInt32(truncatingIfNeeded: t)
            if t < 0 {
                var carry: UInt64 = 0
                for i in 0..<n {
                    let s = UInt64(u[i + j]) + UInt64(v[i]) + carry
                    u[i + j] = UInt32(truncatingIfNeeded: s)
                    carry = s >> 32
                }
                u[j + n] = UInt32(truncatingIfNeeded: UInt64(u[j + n]) + carry)
            }
            j -= 1
        }
        var result = BigUInt()
        result.limbs = shiftRight(Array(u[0..<n]), by: shift)
        result.normalise()
        return result
    }

    private static func shiftLeft(_ limbs: [UInt32], by shift: UInt32, extra: Bool) -> [UInt32] {
        var out = [UInt32](repeating: 0, count: limbs.count + (extra ? 1 : 0))
        if shift == 0 {
            for i in 0..<limbs.count { out[i] = limbs[i] }
            return out
        }
        var carry: UInt32 = 0
        for i in 0..<limbs.count {
            out[i] = (limbs[i] << shift) | carry
            carry = limbs[i] >> (32 - shift)
        }
        if extra { out[limbs.count] = carry }
        return out
    }

    private static func shiftRight(_ limbs: [UInt32], by shift: UInt32) -> [UInt32] {
        if shift == 0 { return limbs }
        var out = [UInt32](repeating: 0, count: limbs.count)
        var index = limbs.count
        var carry: UInt32 = 0
        while index > 0 {
            index -= 1
            out[index] = (limbs[index] >> shift) | carry
            carry = limbs[index] << (32 - shift)
        }
        return out
    }

    /// `self^exponent mod modulus`, square-and-multiply from the top bit,
    /// reduced after every step. A modulus of 1 is 0; a zero modulus is a
    /// precondition failure.
    func modPow(_ exponent: BigUInt, _ modulus: BigUInt) -> BigUInt {
        precondition(!modulus.isZero, "BigUInt modPow with a zero modulus")
        if modulus == BigUInt(1) { return BigUInt() }
        var result = BigUInt(1)
        let base = self % modulus
        var index = exponent.bitWidth
        while index > 0 {
            index -= 1
            result = (result * result) % modulus
            if exponent.bit(index) {
                result = (result * base) % modulus
            }
        }
        return result
    }
}
