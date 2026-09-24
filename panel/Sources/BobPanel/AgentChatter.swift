import SwiftUI

/// What Dark Army says while it waits — a typed line in its own register, in place
/// of a spinner.
///
/// The Mac's copy; the phone's is `ios/BobPhone/AgentChatter.swift`.
/// Everything from the `enum AgentChatter {` line down is byte-identical
/// between the two, pinned by `host/tests/test_agent_chatter.py` — mirror
/// every edit onto the other side in the same commit, and never re-baseline
/// one of them. The words ship in code because the phone draws them exactly
/// when it cannot reach the Mac.
///
/// Only `import SwiftUI` above the marker. `AppKit`, `UIKit`, `help(_:)` and
/// the panel's pointer overlay (`Cursor.swift`) are all one-platform things
/// that compile here and break the phone, where no CI would catch them.
/// Type is asked for through `Theme.mono` alone, so the phone's Dynamic Type
/// contract holds.
///
/// `PanelView` sets `\.agentChatterRunning` from the daemon client's
/// visibility flag, so no chatter clock wakes while the window cannot be
/// seen.
enum AgentChatter {

    /// What is being waited on. The raw value is the seed's namespace and
    /// nothing else — no surface draws it.
    enum Wait: String, CaseIterable {
        case starting
        case refining
        case deleting
        case clearingDone
        case readingHistory
        case connecting
        case opening
        case sending
        case closing
        case refreshing
    }

    /// The longest line the pool may hold. 48 at `Theme.mono(11)` wraps to
    /// at most two lines in any column a person would leave open; the
    /// narrowest host is a board card inside a flexible column.
    static let maxLineChars = 48

    /// Seconds per revealed character.
    static let charInterval: TimeInterval = 0.028
    /// The `TimelineView` cadence while characters arrive. Not a display
    /// link on purpose; once the line is typed the clock drops to the
    /// caret's half-period (`Cadence`).
    static let tick: TimeInterval = 0.05
    /// How long a finished line sits before the next one is chosen.
    static let hold: TimeInterval = 2.2
    /// The cursor drawn after the revealed prefix.
    static let caret = "_"
    /// One blink, on and off, in seconds.
    static let caretPeriod: TimeInterval = 1.0

    /// The shipped pool. Lowercase, terse, about Dark Army — never an apology and
    /// never a promise about how long is left. No trailing punctuation: the
    /// caret is drawn separately. Total over `Wait`: every case answers.
    static func pool(for wait: Wait) -> [String] {
        switch wait {
        case .starting:
            return ["waking somebody up",
                    "handing over the plan",
                    "opening a terminal in the dark",
                    "the session is coming. give it a second"]
        case .refining:
            return ["reading your idea back to itself",
                    "asking the questions you skipped",
                    "turning a sentence into a plan",
                    "nobody ships without a plan. not here"]
        case .deleting:
            return ["unmaking it",
                    "the card was never here",
                    "deleting is easy. deciding was the hard part"]
        case .clearingDone:
            return ["burning the finished pile",
                    "done is a place, not a memory",
                    "clearing the ledger"]
        case .readingHistory:
            return ["walking back through everything",
                    "the archive remembers more than you do",
                    "reading the record. all of it"]
        case .connecting:
            return ["hello, friend",
                    "knocking on your mac",
                    "the door is there. finding the handle",
                    "somewhere out there is a machine that knows you"]
        case .opening:
            return ["pulling the thread",
                    "finding what buzzed",
                    "opening the file on this one"]
        case .sending:
            return ["on its way",
                    "over the wire",
                    "sent. now we wait"]
        case .closing:
            return ["closing the tab. the work stays",
                    "ending it cleanly",
                    "tidying up after ourselves"]
        case .refreshing:
            return ["asking the mac again",
                    "one more look",
                    "checking nothing moved",
                    "the picture, refreshed"]
        }
    }

    /// `Cast.character(for:)`'s djb2 over the lowercased string — copied,
    /// not imported, because this file may depend on neither tree's `Cast`.
    static func hash(_ seed: String) -> Int {
        var hash = 5381
        for byte in seed.lowercased().utf8 { hash = (hash &* 33) &+ Int(byte) }
        return abs(hash)
    }

    /// Which line of a pool a wait is on. Pure: the same seed and step give
    /// the same index for ever, so a repaint underneath cannot reshuffle it.
    static func index(seed: String, step: Int, count: Int) -> Int {
        guard count > 0 else { return 0 }
        let base = hash(seed) % count
        return ((base + step) % count + count) % count
    }

    /// How long one line owns the screen: its typing time plus the hold.
    static func lineDuration(_ line: String) -> TimeInterval {
        Double(line.count) * charInterval + hold
    }

    /// One drawn moment of a wait: the line on screen and how far into that
    /// line's own window the clock is. A wait long enough to outlive one line
    /// advances to the next; under reduce motion the step is pinned to 0 so
    /// the text never changes under a reader.
    struct Frame: Equatable {
        let line: String
        let step: Int
        /// Seconds since this line started, never negative.
        let elapsed: TimeInterval
    }

    /// Every clock this file reads, snapped: `0` for a non-finite or
    /// negative value, otherwise rounded to the millisecond. A schedule entry
    /// minted at `began + 0.5` can read back as `0.4999…`; unsnapped, that
    /// draws the caret one half-period late and lets a line boundary land a
    /// hair before itself.
    static func clock(_ elapsed: TimeInterval) -> TimeInterval {
        guard elapsed.isFinite, elapsed > 0 else { return 0 }
        return (elapsed * 1000).rounded() / 1000
    }

    /// A snapped clock in whole milliseconds, as an integral `Double`, so
    /// the comparisons and divisions below are exact.
    private static func millis(_ seconds: TimeInterval) -> Double {
        (clock(seconds) * 1000).rounded()
    }

    static func frame(_ wait: Wait, seed: String, elapsed: TimeInterval,
                      reduceMotion: Bool) -> Frame {
        let lines = pool(for: wait)
        var step = 0
        var remaining = millis(elapsed)
        if !reduceMotion {
            while true {
                let candidate = lines[index(seed: seed, step: step, count: lines.count)]
                let duration = millis(lineDuration(candidate))
                if remaining < duration { break }
                remaining -= duration
                step += 1
            }
        }
        let line = lines[index(seed: seed, step: step, count: lines.count)]
        return Frame(line: line, step: step, elapsed: reduceMotion ? 0 : remaining / 1000)
    }

    /// The line a wait shows at a given instant. `reduceMotion` pins it to
    /// the first choice for good.
    static func line(_ wait: Wait, seed: String, elapsed: TimeInterval,
                     reduceMotion: Bool) -> String {
        frame(wait, seed: seed, elapsed: elapsed, reduceMotion: reduceMotion).line
    }

    /// The prefix on screen `elapsed` seconds into a line. `prefix` over
    /// `Character`s, never UTF-16 offsets, so no grapheme cluster can split.
    static func revealed(_ line: String, elapsed: TimeInterval, skipped: Bool,
                         reduceMotion: Bool) -> String {
        if skipped || reduceMotion { return line }
        let count = min(line.count, Int(millis(elapsed) / millis(charInterval)))
        return String(line.prefix(max(0, count)))
    }

    /// Whether the caret is drawn this instant. Steady under reduce motion.
    static func caretVisible(elapsed: TimeInterval, reduceMotion: Bool) -> Bool {
        if reduceMotion { return true }
        return clock(elapsed).truncatingRemainder(dividingBy: caretPeriod) < caretPeriod / 2
    }

    // MARK: - The clock's cadence

    /// How often a wait's clock has to wake. `typing` while characters
    /// arrive (`tick`), `caret` once only the cursor changes
    /// (`caretPeriod / 2`), `still` when nothing on screen can change.
    enum Cadence: Equatable { case typing, caret, still }

    /// The cadence for one drawn moment. Hidden (`running` false) and reduce
    /// motion are still: under reduce motion the line is whole, the caret
    /// steady and the pool pinned at step 0, so nothing ever changes.
    static func cadence(style: AgentChatterView.Style, frame: Frame, skipped: Bool,
                        reduceMotion: Bool, running: Bool) -> Cadence {
        if !running || reduceMotion { return .still }
        if style == .caret || skipped { return .caret }
        let typed = millis(frame.elapsed) >= millis(Double(frame.line.count) * charInterval)
        return typed ? .caret : .typing
    }

    /// The wait-clock instant of the next redraw, strictly after `elapsed`:
    /// the next multiple of `tick` while typing, of `caretPeriod / 2` while
    /// blinking, pulled in to `lineEnd` when that comes first so the next
    /// line starts typing on time. `nil` when still. Whole milliseconds
    /// throughout, so a sequence of these can never repeat a date.
    static func nextTick(after elapsed: TimeInterval, cadence: Cadence,
                         lineEnd: TimeInterval) -> TimeInterval? {
        let step: TimeInterval
        switch cadence {
        case .still: return nil
        case .typing: step = tick
        case .caret: step = caretPeriod / 2
        }
        let now = millis(elapsed)
        let stepMillis = millis(step)
        var next = ((now / stepMillis).rounded(.down) + 1) * stepMillis
        let end = millis(lineEnd)
        if end > now && end < next { next = end }
        return next / 1000
    }

    /// The chatter's `TimelineView` schedule: pure in its inputs, it yields
    /// the instant asked for first, then only the instants at which a
    /// character arrives, the caret flips or the next line begins — and
    /// nothing at all while the surface is hidden. Under reduce motion it
    /// yields the one instant asked for and stops.
    struct Schedule: TimelineSchedule {
        let style: AgentChatterView.Style
        let wait: Wait
        let seed: String
        let began: Date?
        let skipped: Bool
        let reduceMotion: Bool
        let running: Bool

        func entries(from start: Date, mode: TimelineScheduleMode) -> AnyIterator<Date> {
            guard running else { return AnyIterator { nil } }
            let origin = began ?? start
            var pending: Date? = start
            return AnyIterator {
                guard let now = pending else { return nil }
                let elapsed = AgentChatter.clock(now.timeIntervalSince(origin))
                let frame = AgentChatter.frame(wait, seed: seed, elapsed: elapsed,
                                               reduceMotion: reduceMotion)
                let cadence = AgentChatter.cadence(style: style, frame: frame,
                                                   skipped: skipped,
                                                   reduceMotion: reduceMotion,
                                                   running: running)
                // The caret alone draws no words, so a line boundary is no
                // reason to wake it.
                let lineEnd = style == .line
                    ? elapsed - frame.elapsed + AgentChatter.lineDuration(frame.line)
                    : elapsed
                pending = AgentChatter.nextTick(after: elapsed, cadence: cadence,
                                                lineEnd: lineEnd)
                    .map { origin.addingTimeInterval($0) }
                return now
            }
        }
    }
}

/// Whether the chatter's clock may run where it is drawn. The Mac's
/// `PanelView` sets it from the window's visibility; the default is on.
struct AgentChatterRunningKey: EnvironmentKey {
    static let defaultValue = true
}

extension EnvironmentValues {
    var agentChatterRunning: Bool {
        get { self[AgentChatterRunningKey.self] }
        set { self[AgentChatterRunningKey.self] = newValue }
    }
}

/// The furniture. Two shapes: `.line` types the sentence where there is room
/// for prose; `.caret` is the blinking cursor alone, for a wait that sits in
/// a row of controls where a sentence would squeeze what a person is using.
///
/// The reveal is a pure function of `now - began`, ticked by a `TimelineView`
/// on `AgentChatter.Schedule` that redraws only its own subtree, so a
/// snapshot arriving mid-wait recomputes the same frame rather than
/// restarting it. The schedule wakes only when something drawn changes, and
/// not at all while `agentChatterRunning` is off. `began` is minted
/// once per wait, in a task keyed on the seed; the call site pins `.id(seed)`
/// too.
/// A tap finishes the line at once. Nothing is remembered.
///
/// `spoken:` is required — one element, one fixed sentence — so no call
/// site can be silent and a screen reader never hears the half-typed
/// decoration.
struct AgentChatterView: View {
    enum Style { case line, caret }

    let style: Style
    let wait: AgentChatter.Wait
    let seed: String
    let spoken: String

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.agentChatterRunning) private var running
    @State private var began: Date?
    @State private var skipped = false

    init(_ style: Style = .line, wait: AgentChatter.Wait, seed: String,
         spoken: String) {
        self.style = style
        self.wait = wait
        self.seed = seed
        self.spoken = spoken
    }

    private static let type: CGFloat = 11
    /// One reserved monospace cell, so the blink cannot nudge a control row.
    private static let caretCell: CGFloat = 9

    var body: some View {
        TimelineView(AgentChatter.Schedule(style: style, wait: wait, seed: seed,
                                           began: began, skipped: skipped,
                                           reduceMotion: reduceMotion,
                                           running: running)) { context in
            let elapsed = began.map { context.date.timeIntervalSince($0) } ?? 0
            content(elapsed: max(0, elapsed))
        }
        .contentShape(Rectangle())
        .onTapGesture { skipped = true }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(spoken))
        .task(id: seed) {
            began = .now
            skipped = false
        }
    }

    @ViewBuilder
    private func content(elapsed: TimeInterval) -> some View {
        let frame = AgentChatter.frame(wait, seed: seed, elapsed: elapsed,
                                       reduceMotion: reduceMotion)
        // The blink rides the wait's own clock, not the line's: the per-line
        // clock snaps to zero at every line boundary, which would hold the
        // caret lit for a whole extra half-period once per line.
        let caretOn = AgentChatter.caretVisible(elapsed: elapsed,
                                                reduceMotion: reduceMotion)
        switch style {
        case .caret:
            Text(caretOn ? AgentChatter.caret : " ")
                .font(Theme.mono(Self.type))
                .foregroundStyle(Theme.phosphor)
                .frame(width: Self.caretCell, alignment: .leading)
        case .line:
            let shown = AgentChatter.revealed(frame.line, elapsed: frame.elapsed,
                                              skipped: skipped,
                                              reduceMotion: reduceMotion)
            // The hidden, fully revealed copy is the sizing layer: the frame
            // is the finished line's from the first tick, so nothing around
            // it reflows as characters arrive, and a prefix never wraps to
            // more lines than its own full string, so the overlay always fits.
            // It ends in the caret glyph rather than a space, so it wraps
            // exactly where the finished overlay wraps: a trailing space hangs
            // past a wrap boundary where the caret does not.
            Text(frame.line + AgentChatter.caret)
                .font(Theme.mono(Self.type))
                .opacity(0)
                .overlay(alignment: .topLeading) {
                    Text(Self.styled(shown, caret: caretOn ? AgentChatter.caret : " "))
                        .font(Theme.mono(Self.type))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The revealed prefix in `Theme.dim` with the caret in `Theme.phosphor`,
    /// as one run so the two wrap together.
    private static func styled(_ shown: String, caret: String) -> AttributedString {
        var text = AttributedString(shown)
        text.foregroundColor = Theme.dim
        var tail = AttributedString(caret)
        tail.foregroundColor = Theme.phosphor
        text.append(tail)
        return text
    }
}
