import XCTest
@testable import BobPhone

/// The phone's link-timing arithmetic and its ring on disk.
@MainActor
final class LinkTimingTests: XCTestCase {
    private func sample(route: String = LinkTimingRoute.away, kind: String = "state",
                        total: Double, post: Double = 0.3,
                        mac: [String: Double] = [:], at: Double = 1_700_000_000) -> LinkTimingSample {
        LinkTimingSample(at: at, route: route, kind: kind, status: 200,
                         totalSeconds: total, postSeconds: post, macTiming: mac)
    }

    func testPercentilesAreNearestRank() {
        XCTAssertEqual(LinkTimingSummary.percentile([], 50), 0)
        XCTAssertEqual(LinkTimingSummary.percentile([2.5], 50), 2.5)
        XCTAssertEqual(LinkTimingSummary.percentile([2.5], 90), 2.5)
        XCTAssertEqual(LinkTimingSummary.percentile([1, 3], 50), 1)
        XCTAssertEqual(LinkTimingSummary.percentile([1, 3], 90), 3)
        let ten = (1...10).map(Double.init)
        XCTAssertEqual(LinkTimingSummary.percentile(ten, 50), 5)
        XCTAssertEqual(LinkTimingSummary.percentile(ten, 90), 9)
        XCTAssertEqual(LinkTimingSummary.percentile([4, 4, 4], 90), 4)
    }

    func testLinesGroupByRouteThenKindAwayFirst() {
        let samples = [
            sample(route: LinkTimingRoute.home, kind: "state", total: 0.2),
            sample(route: LinkTimingRoute.away, kind: "usage", total: 4.0),
            sample(route: LinkTimingRoute.away, kind: "state", total: 2.9),
            sample(route: LinkTimingRoute.away, kind: "state", total: 5.1),
        ]
        let lines = LinkTimingSummary.lines(samples)
        XCTAssertEqual(lines, [
            "away · state · 2 trips · 2.9 s typical · 5.1 s slow",
            "away · usage · 1 trip · 4.0 s typical · 4.0 s slow",
            "home · state · 1 trip · 0.20 s typical · 0.20 s slow",
        ])
        XCTAssertEqual(LinkTimingSummary.lines([]), [])
    }

    func testTheSocketRouteSitsBesideAwayAndItsPushRowSaysTheAge() {
        let samples = [
            sample(route: LinkTimingRoute.home, kind: "state", total: 0.2),
            sample(route: LinkTimingRoute.socket, kind: "push", total: 0.4, post: 0),
            sample(route: LinkTimingRoute.away, kind: "state", total: 2.9),
            sample(route: LinkTimingRoute.socket, kind: "state", total: 0.3),
        ]
        XCTAssertEqual(LinkTimingSummary.lines(samples), [
            "away · state · 1 trip · 2.9 s typical · 2.9 s slow",
            "ws · push · 1 trip · 0.40 s typical · 0.40 s slow",
            "ws · state · 1 trip · 0.30 s typical · 0.30 s slow",
            "home · state · 1 trip · 0.20 s typical · 0.20 s slow",
        ])
        XCTAssertEqual(LinkTimingRoute.socket, "ws")
        let push = LinkTimingSummary.format(
            sample(route: LinkTimingRoute.socket, kind: "push", total: 0.4, post: 0))
        XCTAssertTrue(push.hasSuffix("· ws · push · 0.40 s — age 0.40 on arrival"), push)
        XCTAssertFalse(push.contains("post"), push)
        let trip = LinkTimingSummary.format(
            sample(route: LinkTimingRoute.socket, kind: "state", total: 0.3, post: 0.1,
                   mac: ["open": 0.001, "run": 0.005]))
        XCTAssertTrue(trip.contains("· ws · state · 0.30 s — post 0.10 · Mac 0.005 · back"), trip)
        XCTAssertFalse(trip.contains("pickup"), trip)
    }

    func testBackEstimateClampsAtZero() {
        let fast = sample(total: 1.0, post: 0.3, mac: ["dwell": 0.5, "run": 0.4])
        XCTAssertEqual(fast.backEstimate, 0)
        let slow = sample(total: 3.0, post: 0.3, mac: ["dwell": 1.8, "run": 0.04])
        XCTAssertEqual(slow.backEstimate, 0.86, accuracy: 0.0001)
        XCTAssertEqual(slow.macHold, 0)
    }

    func testFormatDrawsTheLegsAndSaysTheBackIsEstimated() {
        let row = LinkTimingSummary.format(
            sample(total: 2.9, post: 0.3, mac: ["dwell": 1.8, "run": 0.04]))
        XCTAssertTrue(row.contains("· away · state · 2.9 s — post 0.30 · pickup 1.8 · Mac 0.040 · back 0.76 (est.)"), row)
        let home = LinkTimingSummary.format(
            sample(route: LinkTimingRoute.home, total: 0.2, post: 0.2, mac: ["run": 0.03]))
        XCTAssertFalse(home.contains("pickup"), home)
        XCTAssertTrue(home.contains("(est.)"), home)
    }

    func testARefusalStatusIsDrawn() {
        var refused = sample(total: 1.0)
        refused.status = 409
        XCTAssertTrue(LinkTimingSummary.format(refused).contains("· 409 ·"))
        XCTAssertFalse(LinkTimingSummary.format(sample(total: 1.0)).contains("200"))
    }

    private func folder() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    func testTheRingDropsTheOldest() async {
        let dir = folder()
        let store = LinkTimingStore(directory: dir, minWriteInterval: 0)
        store.adopt("mac")
        for i in 0..<(LinkTimingStore.capacity + 7) {
            store.record(sample(total: Double(i)))
        }
        XCTAssertEqual(store.samples.count, LinkTimingStore.capacity)
        XCTAssertEqual(store.samples.first?.totalSeconds, 7)
        await store.settle()
        let url = dir.appendingPathComponent(LinkTimingStore.fileName)
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        let reopened = LinkTimingStore(directory: dir, minWriteInterval: 0)
        reopened.adopt("mac")
        XCTAssertEqual(reopened.samples.count, LinkTimingStore.capacity)
        store.forget()
        XCTAssertTrue(store.samples.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func testAdoptDropsAnotherMacsRing() async {
        let dir = folder()
        let store = LinkTimingStore(directory: dir, minWriteInterval: 0)
        store.adopt("macA")
        store.record(sample(total: 1))
        await store.settle()
        let reopened = LinkTimingStore(directory: dir, minWriteInterval: 0)
        reopened.adopt("macB")
        XCTAssertTrue(reopened.samples.isEmpty)
        let url = dir.appendingPathComponent(LinkTimingStore.fileName)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func testWritesAreFlooredAndForgetNeverPutsTheFileBack() async {
        let dir = folder()
        let url = dir.appendingPathComponent(LinkTimingStore.fileName)
        let store = LinkTimingStore(directory: dir, minWriteInterval: 60)
        store.adopt("mac")
        store.record(sample(total: 1))
        await store.settle()
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        // Inside the floor: the ring changes in memory, a flush is booked,
        // and forgetting cancels it — the file stays gone.
        store.record(sample(total: 2))
        store.record(sample(total: 3))
        XCTAssertNotNil(store.pendingWrite)
        store.forget()
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        try? await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        XCTAssertTrue(store.samples.isEmpty)
    }

    func testADecodedSampleWithAMissingMacLegReadsZero() throws {
        let json = Data(#"{"at": 5, "route": "away", "kind": "state", "totalSeconds": 2.5}"#.utf8)
        let decoded = try JSONDecoder().decode(LinkTimingSample.self, from: json)
        XCTAssertEqual(decoded.totalSeconds, 2.5)
        XCTAssertEqual(decoded.macDwell, 0)
        XCTAssertEqual(decoded.macRun, 0)
        XCTAssertEqual(decoded.status, 0)
        XCTAssertEqual(decoded.backEstimate, 2.5)
    }

    func testSecondsWording() {
        XCTAssertEqual(LinkTimingSummary.seconds(0.0001), "<0.001 s")
        XCTAssertEqual(LinkTimingSummary.seconds(0.04), "0.040 s")
        XCTAssertEqual(LinkTimingSummary.seconds(0.3), "0.30 s")
        XCTAssertEqual(LinkTimingSummary.seconds(2.94), "2.9 s")
        XCTAssertEqual(LinkTimingSummary.seconds(45.2), "45 s")
        XCTAssertEqual(LinkTimingSummary.seconds(123.4), "123 s")
    }
}
