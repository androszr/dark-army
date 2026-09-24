import Foundation

/// Decoration only: captions cannot contain a request, identity or result.
enum DecryptMotion {
    static let screenDuration = 1.5
    static let buttonDuration = 1.2
    static let refreshDuration = 1.5
    static let glyphInterval = 0.05
    static let finalHold = 0.2
    static let maxGlyphs = 24

    enum Kind: Equatable {
        case screen, button, refresh
        var caption: String {
            switch self { case .screen: return "OPEN"; case .button: return "INPUT"; case .refresh: return "REFRESH" }
        }
        var duration: Double { self == .button ? buttonDuration : screenDuration }
    }

    struct Episode: Equatable {
        let kind: Kind
        let surface: String
        let began: Double
        let generation: UInt64
        var deadline: Double { began + kind.duration }
        func frame(at now: Double, reduced: Bool = false) -> String? {
            guard now < deadline else { return nil }
            return DecryptMotion.frame(kind.caption, elapsed: now - began,
                                       duration: kind.duration, seed: generation, reduced: reduced)
        }
    }

    static func frame(_ caption: String, elapsed: Double, duration: Double,
                      seed: UInt64, reduced: Bool = false) -> String {
        let chars = Array(caption.prefix(maxGlyphs))
        guard !chars.isEmpty else { return "" }
        let time = max(0, elapsed)
        if reduced || time >= duration - finalHold { return String(chars) }
        let progress = min(1, max(0, (time - 0.1) / max(0.01, duration - finalHold - 0.1)))
        let resolved = Int(Double(chars.count) * progress)
        let tick = UInt64(time / glyphInterval)
        let alphabet = Array("0123456789#%+=<>[]/:")
        return String(chars.enumerated().map { index, char in
            if char == " " || index < resolved { return char }
            var value = seed &+ tick &* 6364136223846793005 &+ UInt64(index) &* 1442695040888963407
            value ^= value >> 17
            return alphabet[Int(value % UInt64(alphabet.count))]
        })
    }

    /// A finite reducer, shared by the actual owner and toolchain-only tests.
    struct State {
        private(set) var surface: String?
        private(set) var screen: Episode?
        private(set) var button: Episode?
        private(set) var generation: UInt64 = 0
        mutating func arrive(_ surface: String, at now: Double) {
            guard self.surface != surface else { return }
            self.surface = surface
            button = nil // The destination owns feedback for a navigating press.
            begin(.screen, surface: surface, at: now)
        }
        mutating func begin(_ kind: Kind, surface: String, at now: Double) {
            guard self.surface == surface else { return }
            generation &+= 1
            let episode = Episode(kind: kind, surface: surface, began: now, generation: generation)
            if kind == .button { button = episode } else { screen = episode; button = nil }
        }
        mutating func returnedFromPresentation(at now: Double) {
            guard let surface else { return } // Its next completed arrival owns the return.
            if let screen, screen.kind == .screen, now - screen.began < 0.1 { return }
            begin(.screen, surface: surface, at: now)
        }
        mutating func cancel(surface: String? = nil) {
            guard surface == nil || self.surface == surface else { return }
            self.surface = nil; screen = nil; button = nil; generation &+= 1
        }
        mutating func tick(at now: Double) {
            if let screen, now >= screen.deadline { self.screen = nil }
            if let button, now >= button.deadline { self.button = nil }
        }
        func nextTick(at now: Double, reduced: Bool) -> Double? {
            let deadlines = [screen?.deadline, button?.deadline].compactMap { $0 }.filter { $0 > now }
            guard let deadline = deadlines.min() else { return nil }
            return reduced ? deadline : min(deadline, now + glyphInterval)
        }
        enum AutomaticEvent { case snapshot, poll, receipt }
        mutating func automatic(_ event: AutomaticEvent) { /* Deliberately silent. */ }
    }
}
