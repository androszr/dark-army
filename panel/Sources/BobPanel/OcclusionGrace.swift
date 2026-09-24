import Foundation

/// How long a covered panel keeps its live stream before the occlusion
/// writer closes the gate — a pure rule, no AppKit and no timer, so every
/// edge is a test case with an injected clock (`OcclusionGraceTests`).
///
/// Sliding a window over the panel and off again used to run `stop()` /
/// `start()` on the stream every time: a fresh `/api/events` attach, a full
/// fleet decode and an `sse open` / `sse dropped` pair in the log every few
/// seconds. The grace is on the **covered edge of the occlusion writer
/// only**: an uncovered edge opens at once, and the explicit hide, close
/// and quit paths never ask this (they call `cancel()` and write the gate
/// themselves). It is delegate state, never a second flag on
/// `DaemonClient` (`plans/2026-08-23-panel-as-a-normal-window.md`).
///
/// Four rules: `observe(seen: true)` is `.open` and clears any armed
/// deadline; `observe(seen: false)` arms `now + seconds` only when nothing
/// is armed — **a cover never extends itself** — and is `.unchanged` while
/// one is; `expired()` is true exactly once per armed wait; `cancel()`
/// disarms. **The timer's own fire is authoritative**, never a wall-clock
/// comparison: the owner schedules it by interval (uptime) and invalidates
/// it on every open and cancel, so any fire that runs is the current wait —
/// comparing against `Date()` let a clock step back or a hair-early fire
/// leave a deadline armed with no timer, and every later cover `.unchanged`
/// with the gate never closing. The fire's owner recomputes what is on
/// screen rather than trusting the edge that armed it.
struct OcclusionGrace: Equatable {
    static let seconds: TimeInterval = 4

    enum Verdict: Equatable {
        /// Something of ours can be seen: write the gate open now.
        case open
        /// Covered, and the wait starts: `seconds` from now (the date is
        /// informational; the owner's timer runs on an interval).
        case armed(Date)
        /// Covered, and a wait is already running: do nothing.
        case unchanged
    }

    private(set) var deadline: Date?

    mutating func observe(seen: Bool, now: Date) -> Verdict {
        if seen {
            deadline = nil
            return .open
        }
        if deadline != nil { return .unchanged }
        let due = now.addingTimeInterval(Self.seconds)
        deadline = due
        return .armed(due)
    }

    /// The wait's timer fired: true, and disarmed, when a wait was armed;
    /// false for a fire with nothing armed (the owner cancelled it).
    mutating func expired() -> Bool {
        guard deadline != nil else { return false }
        deadline = nil
        return true
    }

    mutating func cancel() {
        deadline = nil
    }
}
