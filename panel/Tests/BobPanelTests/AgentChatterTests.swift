import SwiftUI
import XCTest
@testable import BobPanel

/// The pure half of the typed loading line. None of this is a view: the
/// arithmetic takes `reduceMotion` and `skipped` as parameters rather than
/// reading the environment, which is why reduce motion and the skip are
/// ordinary assertions here and not manual checks.
final class AgentChatterTests: XCTestCase {

    private var allLines: [(AgentChatter.Wait, String)] {
        AgentChatter.Wait.allCases.flatMap { wait in
            AgentChatter.pool(for: wait).map { (wait, $0) }
        }
    }

    // MARK: - The pool

    func testEveryWaitHasANonEmptyPool() {
        for wait in AgentChatter.Wait.allCases {
            XCTAssertFalse(AgentChatter.pool(for: wait).isEmpty, "\(wait) has nothing to say")
        }
        XCTAssertEqual(AgentChatter.Wait.allCases.count, 10)
    }

    /// The refresh line and the "opening a notification" line are two
    /// different waits and must never say the same thing: a pull that reads
    /// "pulling the thread" would claim a buzz is being opened.
    func testRefreshingPoolIsDistinctFromOpening() {
        let refreshing = Set(AgentChatter.pool(for: .refreshing))
        let opening = Set(AgentChatter.pool(for: .opening))
        XCTAssertFalse(refreshing.isEmpty)
        XCTAssertTrue(refreshing.isDisjoint(with: opening),
                      "a refresh line is shared with .opening")
    }

    func testEveryLineIsShortAndClean() {
        for (wait, line) in allLines {
            XCTAssertFalse(line.isEmpty, "\(wait) ships an empty line")
            XCTAssertLessThanOrEqual(line.count, AgentChatter.maxLineChars,
                                     "\(wait): \(line) is over the cap")
            XCTAssertEqual(line, line.trimmingCharacters(in: .whitespacesAndNewlines),
                           "\(wait): \(line) carries stray whitespace")
            XCTAssertFalse(line.hasSuffix("…") || line.hasSuffix("."),
                           "\(wait): \(line) ends in punctuation the caret replaces")
        }
    }

    // MARK: - Choosing a line

    func testIndexIsStableAcrossRepeatedCalls() {
        for step in 0..<6 {
            let a = AgentChatter.index(seed: "card-42", step: step, count: 4)
            let b = AgentChatter.index(seed: "card-42", step: step, count: 4)
            XCTAssertEqual(a, b)
            XCTAssertTrue((0..<4).contains(a))
        }
    }

    func testIndexDiffersAcrossSeeds() {
        let seeds = ["a", "b", "c", "card-1", "card-2", "connecting", "clear-done"]
        let picks = Set(seeds.map { AgentChatter.index(seed: $0, step: 0, count: 4) })
        XCTAssertGreaterThan(picks.count, 1, "every seed landed on the same line")
    }

    func testIndexWalksThePoolAsStepsAdvance() {
        let count = 4
        let walk = (0..<count).map { AgentChatter.index(seed: "x", step: $0, count: count) }
        XCTAssertEqual(Set(walk).count, count, "a step should visit every line once")
    }

    func testLineIsTheSameInsideOneWindow() {
        let first = AgentChatter.line(.starting, seed: "s", elapsed: 0, reduceMotion: false)
        let duration = AgentChatter.lineDuration(first)
        for elapsed in stride(from: 0.0, to: duration, by: 0.3) {
            XCTAssertEqual(AgentChatter.line(.starting, seed: "s", elapsed: elapsed,
                                             reduceMotion: false), first)
        }
    }

    func testLineAdvancesAcrossTheDurationBoundary() {
        let first = AgentChatter.line(.starting, seed: "s", elapsed: 0, reduceMotion: false)
        let duration = AgentChatter.lineDuration(first)
        let second = AgentChatter.line(.starting, seed: "s", elapsed: duration + 0.01,
                                       reduceMotion: false)
        XCTAssertNotEqual(first, second)
        let frame = AgentChatter.frame(.starting, seed: "s", elapsed: duration + 0.01,
                                       reduceMotion: false)
        XCTAssertEqual(frame.step, 1)
        XCTAssertEqual(frame.elapsed, 0.01, accuracy: 1e-6)
    }

    func testReduceMotionPinsTheLine() {
        let pinned = AgentChatter.line(.connecting, seed: "c", elapsed: 0, reduceMotion: true)
        for elapsed in [0.0, 1.0, 10.0, 100.0, 3600.0] {
            let frame = AgentChatter.frame(.connecting, seed: "c", elapsed: elapsed,
                                           reduceMotion: true)
            XCTAssertEqual(frame.line, pinned)
            XCTAssertEqual(frame.step, 0)
            XCTAssertEqual(frame.elapsed, 0)
        }
    }

    // MARK: - Revealing it

    func testRevealedIsEmptyAtZeroAndMonotonic() {
        let line = "the door is there. finding the handle"
        XCTAssertEqual(AgentChatter.revealed(line, elapsed: 0, skipped: false,
                                             reduceMotion: false), "")
        var last = 0
        for elapsed in stride(from: 0.0, through: 3.0, by: 0.01) {
            let n = AgentChatter.revealed(line, elapsed: elapsed, skipped: false,
                                          reduceMotion: false).count
            XCTAssertGreaterThanOrEqual(n, last)
            XCTAssertLessThanOrEqual(n, line.count)
            last = n
        }
    }

    func testRevealedCompletesAndNeverOvershoots() {
        let line = "on its way"
        let done = Double(line.count) * AgentChatter.charInterval
        XCTAssertEqual(AgentChatter.revealed(line, elapsed: done, skipped: false,
                                             reduceMotion: false), line)
        XCTAssertEqual(AgentChatter.revealed(line, elapsed: done * 50, skipped: false,
                                             reduceMotion: false), line)
        XCTAssertEqual(AgentChatter.revealed(line, elapsed: -5, skipped: false,
                                             reduceMotion: false), "")
    }

    func testRevealedIsGraphemeSafe() {
        // A combining accent and a flag emoji: a UTF-16 refactor splits both.
        let line = "cafe\u{0301} 🇬🇧 done"
        for elapsed in stride(from: 0.0, through: 1.0, by: 0.005) {
            let shown = AgentChatter.revealed(line, elapsed: elapsed, skipped: false,
                                              reduceMotion: false)
            XCTAssertTrue(line.hasPrefix(shown))
            XCTAssertEqual(shown, String(line.prefix(shown.count)))
            // A split would leave a Character that *starts* with the accent.
            XCTAssertFalse(shown.contains { $0.unicodeScalars.first?.value == 0x301 },
                           "the accent was split from its base")
            XCTAssertFalse(shown.unicodeScalars.contains { $0.value == 0x1F1EC }
                           && !shown.unicodeScalars.contains { $0.value == 0x1F1E7 },
                           "the flag was split in half")
        }
        let all = AgentChatter.revealed(line, elapsed: 10, skipped: false, reduceMotion: false)
        XCTAssertEqual(all, line)
    }

    func testSkipAndReduceMotionRevealTheWholeLineAtOnce() {
        let line = "burning the finished pile"
        XCTAssertEqual(AgentChatter.revealed(line, elapsed: 0, skipped: true,
                                             reduceMotion: false), line)
        XCTAssertEqual(AgentChatter.revealed(line, elapsed: 0, skipped: false,
                                             reduceMotion: true), line)
    }

    func testCaretIsSteadyUnderReduceMotionAndBlinksOtherwise() {
        for elapsed in stride(from: 0.0, through: 5.0, by: 0.1) {
            XCTAssertTrue(AgentChatter.caretVisible(elapsed: elapsed, reduceMotion: true))
        }
        XCTAssertTrue(AgentChatter.caretVisible(elapsed: 0.1, reduceMotion: false))
        XCTAssertFalse(AgentChatter.caretVisible(elapsed: 0.6, reduceMotion: false))
        XCTAssertTrue(AgentChatter.caretVisible(elapsed: 1.1, reduceMotion: false))
    }

    func testHashIsCaseInsensitiveLikeCast() {
        XCTAssertEqual(AgentChatter.hash("Card-ABC"), AgentChatter.hash("card-abc"))
        XCTAssertNotEqual(AgentChatter.hash("card-abc"), AgentChatter.hash("card-abd"))
    }

    // MARK: - The clock

    func testClockSnapsToTheMillisecondAndFloorsAtZero() {
        XCTAssertEqual(AgentChatter.clock(0.4999996), 0.5)
        XCTAssertEqual(AgentChatter.clock(-1), 0)
        XCTAssertEqual(AgentChatter.clock(.nan), 0)
        XCTAssertEqual(AgentChatter.clock(.infinity), 0)
        XCTAssertEqual(AgentChatter.clock(1.2344), 1.234)
    }

    // MARK: - The cadence

    /// A wait whose first line is long enough to walk inside.
    private let wait = AgentChatter.Wait.starting
    private let seed = "card-cadence"

    private var firstLine: String {
        AgentChatter.frame(wait, seed: seed, elapsed: 0, reduceMotion: false).line
    }

    private func typingTime(_ line: String) -> TimeInterval {
        Double(line.count) * AgentChatter.charInterval
    }

    private func cadence(_ style: AgentChatterView.Style, elapsed: TimeInterval,
                         skipped: Bool = false, reduceMotion: Bool = false,
                         running: Bool = true) -> AgentChatter.Cadence {
        let frame = AgentChatter.frame(wait, seed: seed, elapsed: elapsed,
                                       reduceMotion: reduceMotion)
        return AgentChatter.cadence(style: style, frame: frame, skipped: skipped,
                                    reduceMotion: reduceMotion, running: running)
    }

    func testCadenceIsTypingWhileCharactersArrive() {
        XCTAssertEqual(cadence(.line, elapsed: typingTime(firstLine) / 2), .typing)
        XCTAssertEqual(cadence(.line, elapsed: 0), .typing)
    }

    func testCadenceDropsToCaretOnceTheLineIsTyped() {
        XCTAssertEqual(cadence(.line, elapsed: typingTime(firstLine)), .caret)
        XCTAssertEqual(cadence(.line, elapsed: typingTime(firstLine) + 1.0), .caret)
        // The instant the cadence calls it typed is the instant the whole
        // line is on screen — never one character short.
        let frame = AgentChatter.frame(wait, seed: seed, elapsed: typingTime(firstLine),
                                       reduceMotion: false)
        XCTAssertEqual(AgentChatter.revealed(frame.line, elapsed: frame.elapsed,
                                             skipped: false, reduceMotion: false),
                       frame.line)
    }

    func testCadenceIsCaretForTheCaretStyle() {
        XCTAssertEqual(cadence(.caret, elapsed: 0), .caret)
    }

    func testSkipDropsToCaret() {
        XCTAssertEqual(cadence(.line, elapsed: 0, skipped: true), .caret)
    }

    /// The success criterion's third leg: a hidden panel, or reduce motion,
    /// asks for no redraw at all.
    func testCadenceIsStillWhenHiddenOrUnderReduceMotion() {
        for style in [AgentChatterView.Style.line, .caret] {
            XCTAssertEqual(cadence(style, elapsed: 0, running: false), .still)
            XCTAssertEqual(cadence(style, elapsed: 0.3, running: false), .still)
            XCTAssertEqual(cadence(style, elapsed: 0, reduceMotion: true), .still)
            XCTAssertEqual(cadence(style, elapsed: 30, reduceMotion: true), .still)
        }
        XCTAssertNil(AgentChatter.nextTick(after: 0.3, cadence: .still, lineEnd: 5))
    }

    // MARK: - The schedule

    private let t = Date(timeIntervalSinceReferenceDate: 800_000_000.123)

    private func schedule(_ style: AgentChatterView.Style, began: Date?,
                          skipped: Bool = false, reduceMotion: Bool = false,
                          running: Bool = true) -> AgentChatter.Schedule {
        AgentChatter.Schedule(style: style, wait: wait, seed: seed, began: began,
                              skipped: skipped, reduceMotion: reduceMotion,
                              running: running)
    }

    /// The wait-clock instants of the first `count` entries.
    private func walk(_ schedule: AgentChatter.Schedule, from start: Date,
                      count: Int, mode: TimelineScheduleMode = .normal) -> [TimeInterval] {
        let origin = schedule.began ?? start
        return Array(schedule.entries(from: start, mode: mode).prefix(count))
            .map { $0.timeIntervalSince(origin) }
    }

    private func gaps(_ instants: [TimeInterval]) -> [TimeInterval] {
        zip(instants.dropFirst(), instants).map { $0 - $1 }
    }

    func testScheduleFirstEntryIsTheInstantAsked() {
        for style in [AgentChatterView.Style.line, .caret] {
            for began in [nil, t.addingTimeInterval(-3600)] {
                let first = schedule(style, began: began).entries(from: t, mode: .normal)
                    .makeIterator().next()
                XCTAssertEqual(first, t, "\(style), began \(String(describing: began))")
            }
        }
    }

    /// The success criterion's first two legs: twenty times a second while
    /// the line types, twice a second once it is typed.
    func testScheduleTicksAtTwentyHertzWhileTypingThenHalfSeconds() {
        let typed = typingTime(firstLine)
        let end = AgentChatter.lineDuration(firstLine)
        let instants = walk(schedule(.line, began: t), from: t, count: 400)
            .filter { $0 < end + 0.001 }
        var sawTyping = false
        var sawCaret = false
        var handedOver = false
        for (at, gap) in zip(instants, gaps(instants)) {
            if at < typed - 0.001 {
                XCTAssertEqual(gap, AgentChatter.tick, accuracy: 0.001, "typing at \(at)")
                sawTyping = true
            } else if !handedOver {
                // The one hand-over gap: the first blink lands on the wait
                // clock's next half-period, so it is never longer than one.
                XCTAssertLessThanOrEqual(gap, AgentChatter.caretPeriod / 2 + 0.001)
                handedOver = true
            } else if at + gap < end - 0.001 {
                XCTAssertEqual(gap, AgentChatter.caretPeriod / 2, accuracy: 0.001,
                               "blinking at \(at)")
                sawCaret = true
            }
        }
        XCTAssertTrue(sawTyping)
        XCTAssertTrue(sawCaret)
    }

    func testScheduleReturnsToTypingAtTheNextLine() {
        let end = AgentChatter.lineDuration(firstLine)
        let instants = walk(schedule(.line, began: t), from: t, count: 400)
        guard let i = instants.firstIndex(where: { abs($0 - end) < 0.001 }) else {
            return XCTFail("no entry at the first line's end, \(end)")
        }
        let after = Array(instants[i...].prefix(6))
        // The first gap after the boundary lands on the wait clock's next
        // `tick`; every one after that is a whole `tick`.
        XCTAssertLessThanOrEqual(after[1] - after[0], AgentChatter.tick + 0.001)
        for gap in gaps(Array(after.dropFirst())) {
            XCTAssertEqual(gap, AgentChatter.tick, accuracy: 0.001)
        }
        let second = AgentChatter.frame(wait, seed: seed, elapsed: after[1], reduceMotion: false)
        XCTAssertEqual(second.step, 1)
    }

    func testCaretStyleTicksOnlyAtHalfPeriods() {
        let instants = walk(schedule(.caret, began: t), from: t, count: 60)
        XCTAssertEqual(instants.count, 60)
        for gap in gaps(instants) {
            XCTAssertEqual(gap, AgentChatter.caretPeriod / 2, accuracy: 0.001)
        }
    }

    /// The success criterion's hidden leg, at the schedule itself.
    func testScheduleIsEmptyWhileHiddenOrUnderReduceMotion() {
        for style in [AgentChatterView.Style.line, .caret] {
            let hidden = schedule(style, began: t, running: false)
            XCTAssertTrue(Array(hidden.entries(from: t, mode: .normal).prefix(5)).isEmpty)
            let reduced = schedule(style, began: t, reduceMotion: true)
            XCTAssertEqual(Array(reduced.entries(from: t, mode: .normal).prefix(5)), [t])
        }
    }

    func testScheduleEntriesStrictlyIncrease() {
        for style in [AgentChatterView.Style.line, .caret] {
            for skipped in [false, true] {
                let dates = Array(schedule(style, began: t, skipped: skipped)
                    .entries(from: t.addingTimeInterval(0.0371), mode: .normal).prefix(400))
                XCTAssertEqual(dates.count, 400)
                for (later, earlier) in zip(dates.dropFirst(), dates) {
                    XCTAssertGreaterThan(later, earlier)
                }
                let last = dates.last!.timeIntervalSince(t)
                let crossed = AgentChatter.frame(wait, seed: seed, elapsed: last,
                                                 reduceMotion: false).step
                XCTAssertGreaterThanOrEqual(crossed, 2, "\(style) walked too short a span")
            }
        }
    }

    func testScheduleIsTheSameForBothModes() {
        for style in [AgentChatterView.Style.line, .caret] {
            let s = schedule(style, began: t)
            XCTAssertEqual(walk(s, from: t, count: 50, mode: .normal),
                           walk(s, from: t, count: 50, mode: .lowFrequency))
        }
    }
}
