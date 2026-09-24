import Foundation

/// Pure arithmetic for the Lock Screen circular tile: tick angles and
/// weights, the centre number/caption, the spoken sentence. Foundation
/// only so the Mac host can compile and run it under `swiftc`.
enum TickRing {
    struct Tick {
        let degrees: Double
        let heavy: Bool
    }

    struct Centre {
        let text: String
        let caption: String
        let waiting: Bool
    }

    static let comfortSlots = 12
    static let heavyLength = 9.0
    static let hairLength = 5.5

    /// Comfortable 30° step up to twelve live agents; above that the
    /// circle is divided by the real count, never truncated.
    static func step(live: Int) -> Double {
        360.0 / Double(max(comfortSlots, max(1, live)))
    }

    static func ticks(needsYou: Int, working: Int) -> [Tick] {
        let needs = max(0, needsYou)
        let work = max(0, working)
        let live = needs + work
        let delta = step(live: live)
        return (0..<live).map { i in
            Tick(degrees: Double(i) * delta, heavy: i < needs)
        }
    }

    static func thinning(live: Int) -> Double {
        min(1.0, Double(comfortSlots) / Double(max(1, live)))
    }

    static func heavyWidth(live: Int) -> Double {
        max(1.6, 3.4 * thinning(live: live))
    }

    static func hairWidth(live: Int) -> Double {
        max(0.6, 1.2 * thinning(live: live))
    }

    static func centre(needsYou: Int, working: Int, stale: Bool) -> Centre {
        let needs = max(0, needsYou)
        let work = max(0, working)
        let live = needs + work
        let text: String
        var caption: String
        let waiting: Bool
        if live == 0 {
            text = "–"
            caption = "IDLE"
            waiting = false
        } else if needs > 0 {
            text = "\(needs)"
            caption = "NEEDS"
            waiting = true
        } else {
            text = "\(work)"
            caption = "WORK"
            waiting = false
        }
        if stale {
            caption.append("?")
        }
        return Centre(text: text, caption: caption, waiting: waiting)
    }

    static func spoken(needsYou: Int, working: Int, stale: Bool) -> String {
        let needs = max(0, needsYou)
        let work = max(0, working)
        let live = needs + work
        let sentence: String
        if live == 0 {
            sentence = "no agents running"
        } else if needs > 0 {
            sentence = "\(needs) need you, \(work) working"
        } else {
            sentence = "nobody needs you, \(work) working"
        }
        if stale {
            return sentence + ", last heard from the Mac a while ago"
        }
        return sentence
    }
}
