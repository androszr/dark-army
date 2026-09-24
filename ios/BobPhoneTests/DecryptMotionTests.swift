import XCTest
@testable import BobPhone

final class DecryptMotionTests: XCTestCase {
    func testProjectionBoundariesAndDeterminism() {
        for kind in [DecryptMotion.Kind.screen, .button, .refresh] {
            let episode = DecryptMotion.Episode(kind: kind, surface: "public", began: 10, generation: 3)
            XCTAssertEqual(episode.frame(at: 9), episode.frame(at: 10))
            XCTAssertEqual(episode.frame(at: 10.4), episode.frame(at: 10.4))
            XCTAssertEqual(episode.frame(at: episode.deadline - 0.19), kind.caption)
            XCTAssertNil(episode.frame(at: episode.deadline))
            XCTAssertNil(episode.frame(at: episode.deadline + 10))
            XCTAssertEqual(episode.frame(at: 10, reduced: true), kind.caption)
        }
        XCTAssertEqual(DecryptMotion.buttonDuration, 1.2)
        XCTAssertEqual(DecryptMotion.screenDuration, 1.5)
        XCTAssertEqual(DecryptMotion.refreshDuration, 1.5)
        XCTAssertEqual(DecryptMotion.frame("", elapsed: 0, duration: 1.5, seed: 1), "")
        XCTAssertEqual(DecryptMotion.frame(String(repeating: "X", count: 500), elapsed: 2, duration: 1.5, seed: 1).count, 24)
        XCTAssertEqual(Array(DecryptMotion.frame("A B", elapsed: 0, duration: 1.5, seed: 1))[1], " ")
    }
    func testResolutionNeverReScramblesSettledPrefix() {
        let caption = "REFRESH"
        for step in 0...25 {
            let time = Double(step) * 0.05
            let count = Int(Double(caption.count) * min(1, max(0, (time - 0.1) / 1.2)))
            let frame = DecryptMotion.frame(caption, elapsed: time, duration: 1.5, seed: 99)
            XCTAssertEqual(String(frame.prefix(count)), String(caption.prefix(count)))
        }
    }
    func testSupersessionDeadlineAndAutomaticSilence() {
        var state = DecryptMotion.State()
        state.automatic(.poll); state.automatic(.snapshot); state.automatic(.receipt)
        XCTAssertEqual(state.generation, 0)
        state.arrive("a", at: 0)
        state.arrive("a", at: 0.1)
        XCTAssertEqual(state.generation, 1)
        state.begin(.button, surface: "a", at: 0.2)
        state.arrive("b", at: 0.3)
        XCTAssertNil(state.button)
        XCTAssertEqual(state.screen?.surface, "b")
        state.cancel(surface: "a") // stale outgoing callback cannot cancel b
        XCTAssertEqual(state.surface, "b")
        state.begin(.button, surface: "a", at: 0.4)
        XCTAssertNil(state.button)
        state.tick(at: 2)
        XCTAssertNil(state.nextTick(at: 2, reduced: false))
        state.cancel()
        XCTAssertNil(state.surface)
    }
    func testArrivalIsOpenAndLeavesButtonIdle() {
        // Button and refresh stay in the reducer. A screen arrival does not
        // start them; the phone UI no longer begins those kinds either.
        var state = DecryptMotion.State()
        state.arrive("sheet", at: 0)
        XCTAssertEqual(state.screen?.kind, .screen)
        XCTAssertEqual(state.screen?.frame(at: 0, reduced: true), "OPEN")
        XCTAssertNil(state.button)
    }
    func testBoundedSchedulerAndReducedMotionDeadlineOnly() {
        var state = DecryptMotion.State(); state.arrive("a", at: 0)
        XCTAssertEqual(state.nextTick(at: 0, reduced: true), 1.5)
        var time = 0.0, ticks = 0
        while let next = state.nextTick(at: time, reduced: false) {
            XCTAssertLessThanOrEqual(next - time, 0.050001)
            time = next; ticks += 1; state.tick(at: time)
        }
        XCTAssertLessThanOrEqual(ticks, 31)
        XCTAssertEqual(time, 1.5)
    }
}
