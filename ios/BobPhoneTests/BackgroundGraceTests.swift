import XCTest
@testable import BobPhone

/// The five-minute rule a return to the app is judged by.
@MainActor
final class BackgroundGraceTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    private func departed(_ secondsAgo: TimeInterval) -> Date {
        now.addingTimeInterval(-secondsAgo)
    }

    func testTheWindowIsFiveMinutesAndUnderTheRefreshFloor() {
        XCTAssertEqual(BackgroundGrace.window, 300)
        XCTAssertLessThan(BackgroundGrace.window,
                          TimeInterval(BackgroundRefresh.intervals[0] * 60))
    }

    func testNoDepartureWithALivePollerKeeps() {
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: nil, now: now, alive: true), .keep)
    }

    func testInsideTheWindowKeeps() {
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: departed(30), now: now, alive: true), .keep)
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: departed(299), now: now, alive: true), .keep)
    }

    func testExactlyTheWindowKeeps() {
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: departed(300), now: now, alive: true), .keep)
    }

    func testOneSecondPastTheWindowRestarts() {
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: departed(301), now: now, alive: true), .restart)
    }

    func testNothingRunningRestartsWhateverTheStamp() {
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: nil, now: now, alive: false), .restart)
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: departed(30), now: now, alive: false), .restart)
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: departed(301), now: now, alive: false), .restart)
    }

    func testExpiredIsFalseForNilAndInsideTrueOnlyPast() {
        XCTAssertFalse(BackgroundGrace.expired(departedAt: nil, now: now))
        XCTAssertFalse(BackgroundGrace.expired(departedAt: departed(299), now: now))
        XCTAssertFalse(BackgroundGrace.expired(departedAt: departed(300), now: now))
        XCTAssertTrue(BackgroundGrace.expired(departedAt: departed(301), now: now))
    }

    func testAClockThatMovedBackwardsKeepsAndIsNotExpired() {
        let future = departed(-60)
        XCTAssertEqual(BackgroundGrace.verdict(departedAt: future, now: now, alive: true), .keep)
        XCTAssertFalse(BackgroundGrace.expired(departedAt: future, now: now))
    }

    // MARK: the pairing-record change handler's rule

    private func record(host: String = "10.0.0.2", hosts: [String] = ["10.0.0.2"],
                        sendCtr: Int = 3, recvCtr: Int = 5,
                        homeSendCtr: Int = 7, homeRecvCtr: Int = 9) -> PairingRecord {
        PairingRecord(token: "tok", host: host, hosts: hosts, port: 19875,
                      deviceId: "dev", relayKey: "rk", relayURL: "https://relay",
                      sendCtr: sendCtr, recvCtr: recvCtr, homeKey: "hk",
                      homeSendCtr: homeSendCtr, homeRecvCtr: homeRecvCtr)
    }

    func testACountersOnlyChangeWhileThePollerRunsKeepsIt() {
        let before = record()
        let after = record(sendCtr: 4, recvCtr: 6, homeSendCtr: 8, homeRecvCtr: 10)
        XCTAssertNotEqual(before, after, "the synthesized == sees the counters")
        XCTAssertTrue(BackgroundGrace.keepsPoller(old: before.identity, new: after.identity,
                                                  polling: true))
    }

    func testAChangedAddressOrListStillRestarts() {
        let before = record()
        let promoted = record(host: "10.0.0.9", hosts: ["10.0.0.9", "10.0.0.2"])
        XCTAssertFalse(BackgroundGrace.keepsPoller(old: before.identity, new: promoted.identity,
                                                   polling: true))
        let learned = record(hosts: ["10.0.0.2", "mac.local"])
        XCTAssertFalse(BackgroundGrace.keepsPoller(old: before.identity, new: learned.identity,
                                                   polling: true))
    }

    func testNothingRunningOrNoEarlierRecordRestarts() {
        let before = record()
        let after = record(sendCtr: 4)
        XCTAssertFalse(BackgroundGrace.keepsPoller(old: before.identity, new: after.identity,
                                                   polling: false))
        XCTAssertFalse(BackgroundGrace.keepsPoller(old: nil, new: after.identity, polling: true))
    }

    func testTheIdentityIsEverythingButTheFourCounters() {
        let a = record()
        let b = record(sendCtr: 100, recvCtr: 100, homeSendCtr: 100, homeRecvCtr: 100)
        XCTAssertEqual(a.identity, b.identity)
        var c = a
        c.homeKeyCrossedInClear = true
        XCTAssertNotEqual(a.identity, c.identity)
        var d = a
        d.relayKey = "other"
        XCTAssertNotEqual(a.identity, d.identity)
    }
}
