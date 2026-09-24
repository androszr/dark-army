import XCTest
@testable import BobPanel

/// `OcclusionGrace` against an injected clock: a brief cover costs nothing,
/// a long one expires exactly once, a second cover never extends the first,
/// a cancelled wait does nothing — and the timer's own fire is
/// authoritative, so no clock step can leave a wait armed with no timer.
/// Plus `GraceCover`, what the terminal heartbeat reads during the wait.
final class OcclusionGraceTests: XCTestCase {

    private let t0 = Date(timeIntervalSinceReferenceDate: 1_000_000)

    func testTheGraceIsFourSeconds() {
        XCTAssertEqual(OcclusionGrace.seconds, 4)
    }

    func testACoverArmsFourSecondsOut() {
        var grace = OcclusionGrace()
        XCTAssertEqual(grace.observe(seen: false, now: t0),
                       .armed(t0.addingTimeInterval(4)))
        XCTAssertEqual(grace.deadline, t0.addingTimeInterval(4))
    }

    func testAnUncoverInsideTheGraceOpensAndALaterFireDoesNothing() {
        var grace = OcclusionGrace()
        _ = grace.observe(seen: false, now: t0)
        XCTAssertEqual(grace.observe(seen: true, now: t0.addingTimeInterval(2)), .open)
        XCTAssertNil(grace.deadline)
        XCTAssertFalse(grace.expired())
    }

    func testAFireIsTrueOnce() {
        var grace = OcclusionGrace()
        _ = grace.observe(seen: false, now: t0)
        XCTAssertTrue(grace.expired())
        XCTAssertFalse(grace.expired())
        XCTAssertNil(grace.deadline)
    }

    func testASecondCoverDoesNotExtendTheFirst() {
        var grace = OcclusionGrace()
        _ = grace.observe(seen: false, now: t0)
        XCTAssertEqual(grace.observe(seen: false, now: t0.addingTimeInterval(3)), .unchanged)
        XCTAssertEqual(grace.deadline, t0.addingTimeInterval(4))
        XCTAssertTrue(grace.expired())
    }

    func testCancelThenFireDoesNothing() {
        var grace = OcclusionGrace()
        _ = grace.observe(seen: false, now: t0)
        grace.cancel()
        XCTAssertNil(grace.deadline)
        XCTAssertFalse(grace.expired())
    }

    /// The audit's case: the wall clock stepped back (or the timer fired a
    /// hair early) so `now` is before the recorded deadline. The fire still
    /// ends the wait, and the next cover arms a fresh one — never
    /// `.unchanged` behind a deadline no timer will ever reach.
    func testAnEarlyOrClockSteppedFireStillEndsTheWaitAndTheNextCoverArms() {
        var grace = OcclusionGrace()
        _ = grace.observe(seen: false, now: t0)
        // Fired "early" by the wall clock: authoritative all the same.
        XCTAssertTrue(grace.expired())
        XCTAssertNil(grace.deadline)
        // A later cover, even stamped before the old deadline, arms anew.
        let stepped = t0.addingTimeInterval(-3600)
        XCTAssertEqual(grace.observe(seen: false, now: stepped),
                       .armed(stepped.addingTimeInterval(4)))
        XCTAssertTrue(grace.expired())
    }

    func testAnUncoveredEdgeWithNothingArmedIsStillOpen() {
        var grace = OcclusionGrace()
        XCTAssertEqual(grace.observe(seen: true, now: t0), .open)
        XCTAssertFalse(grace.expired())
    }

    func testACoverAfterAFireArmsAFreshDeadline() {
        var grace = OcclusionGrace()
        _ = grace.observe(seen: false, now: t0)
        XCTAssertTrue(grace.expired())
        let later = t0.addingTimeInterval(20)
        XCTAssertEqual(grace.observe(seen: false, now: later),
                       .armed(later.addingTimeInterval(4)))
    }

    // MARK: - GraceCover, what the heartbeat reads during the wait

    @MainActor
    func testTheCoverFollowsTheVerdictsAndClears() {
        let cover = GraceCover()
        XCTAssertFalse(cover.inGrace)
        cover.note(.armed(t0))
        XCTAssertTrue(cover.inGrace)
        cover.note(.unchanged)
        XCTAssertTrue(cover.inGrace, "a second cover keeps the wait")
        cover.note(.open)
        XCTAssertFalse(cover.inGrace)
        cover.note(.armed(t0))
        cover.clear()
        XCTAssertFalse(cover.inGrace, "cancel and settle both clear it")
    }

    /// The pane names its session only while seen and uncovered: a cover
    /// empties the daemon's focused set at once, as before the grace.
    func testThePaneStatesItsSessionOnlyWhileSeenAndUncovered() {
        XCTAssertEqual(GraceCover.stated(session: "s1", visible: true, inGrace: false), "s1")
        XCTAssertEqual(GraceCover.stated(session: "s1", visible: true, inGrace: true), "")
        XCTAssertEqual(GraceCover.stated(session: "s1", visible: false, inGrace: false), "")
        XCTAssertEqual(GraceCover.stated(session: "s1", visible: false, inGrace: true), "")
    }
}
