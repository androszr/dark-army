import SwiftUI
import XCTest
@testable import BobPanel

/// The fleet map's rules, each a table. What the map draws is the daemon's
/// word (stations, calls, ends) and the table's own figures (bucket, CTX).
final class FleetMapRulesTests: XCTestCase {
    private func call(_ seq: Int, _ station: String, _ end: FleetCallEnd,
                      tool: String = "Read") -> FleetCall {
        FleetCall(seq: seq, station: station, tool: tool, end: end)
    }

    private func agent(_ id: String, origin: String = "") throws -> Agent {
        var a = try JSONDecoder().decode(
            Agent.self, from: Data(#"{"session_id":"\#(id)"}"#.utf8))
        a.originCard = origin
        return a
    }

    private func metrics(_ json: String) throws -> Metrics {
        try JSONDecoder().decode(Metrics.self, from: Data(json.utf8))
    }

    func testStationCountSumsAcrossAgents() {
        let calls = ["a": [call(1, "files", .running)],
                     "b": [call(2, "files", .running)]]
        XCTAssertEqual(FleetMap.stationCounts(calls: calls, in: ["a", "b"])["files"], 2)
    }

    func testOnlyRunningCallsCountAndOnlyTheTabsSessions() {
        let calls = ["a": [call(1, "files", .done), call(2, "shell", .running)],
                     "z": [call(3, "shell", .running)]]
        let counts = FleetMap.stationCounts(calls: calls, in: ["a"])
        XCTAssertEqual(counts["shell"], 1)
        XCTAssertNil(counts["files"])
    }

    func testWaitingAgentParksOnYouWithNoPulse() {
        XCTAssertTrue(FleetMap.waitingLine(category: .waiting))
        for other in [Category.running, .sleeping, .finished, .abandoned] {
            XCTAssertFalse(FleetMap.waitingLine(category: other))
        }
        let calls = [call(1, "you", .running)]
        XCTAssertTrue(FleetMap.pulsingCalls(category: .waiting, calls: calls).isEmpty)
        XCTAssertEqual(FleetMap.pulsingCalls(category: .running, calls: calls), calls)
    }

    func testCtxRingUsesTheRowsThresholds() throws {
        XCTAssertEqual(ProcessRow.ctxColor(try metrics(#"{"ctx_used_pct":74}"#)), Theme.dim)
        XCTAssertEqual(ProcessRow.ctxColor(try metrics(#"{"ctx_used_pct":75}"#)), .orange)
        XCTAssertEqual(ProcessRow.ctxColor(try metrics(#"{"ctx_used_pct":84}"#)), .orange)
        XCTAssertEqual(ProcessRow.ctxColor(try metrics(#"{"ctx_used_pct":85}"#)), .red)
        XCTAssertEqual(ProcessRow.ctxColor(
            try metrics(#"{"ctx_used_pct":10,"exceeds_200k":true}"#)), .red)
        XCTAssertEqual(ProcessRow.ctxColor(try metrics("{}")), Theme.faint)
    }

    func testColourSlotIsTheIndexInTheTabsOrderAndWraps() {
        let ids = (0..<10).map { "s\($0)" }
        XCTAssertEqual(FleetMap.colorIndex(sessionId: "s0", in: ids), 0)
        XCTAssertEqual(FleetMap.colorIndex(sessionId: "s3", in: ids), 3)
        XCTAssertEqual(FleetMap.colorIndex(sessionId: "s9", in: ids), 1)
        XCTAssertEqual(FleetMap.colorIndex(sessionId: "nobody", in: ids), 0)
    }

    func testTiesGroupSessionsOnOneCardAndDropSingles() throws {
        var board = Board()
        board.available = true
        var card = BoardCard()
        card.id = "c1"
        card.sessionId = "a"
        board.cards = [card]
        let rows = [try agent("a"), try agent("b", origin: "c1"),
                    try agent("c", origin: "other")]
        // "a" is bound to c1 through the board; "b" names c1 as its origin.
        XCTAssertEqual(FleetMap.ties(rows: rows, board: board), [["a", "b"]])
        XCTAssertTrue(FleetMap.ties(rows: [rows[2]], board: board).isEmpty)
    }

    func testFeedFoldsRepeatedFinishedCallsAtOneStation() {
        let calls = [call(1, "files", .done), call(2, "files", .done),
                     call(3, "files", .done), call(4, "shell", .done, tool: "Bash")]
        let rows = FleetMap.feedRows(sessions: ["a": calls], names: ["a": "Cipher"], in: ["a"])
        XCTAssertEqual(rows.map(\.station), ["shell", "files"])
        XCTAssertEqual(rows.map(\.extra), [0, 2])
        XCTAssertEqual(rows[1].name, "Cipher")
    }

    func testAnotherAgentsCallDoesNotBreakAFold() {
        let rows = FleetMap.feedRows(
            sessions: ["a": [call(1, "files", .done), call(3, "files", .done)],
                       "b": [call(2, "shell", .done, tool: "Bash")]],
            names: [:], in: ["a", "b"])
        XCTAssertEqual(rows.filter { $0.sessionId == "a" }.first?.extra, 1)
        XCTAssertEqual(rows.count, 2)
    }

    func testFeedMarksRunningDoneAndFailedWithThreeSymbols() {
        let rows = FleetMap.feedRows(
            sessions: ["a": [call(1, "files", .done), call(2, "shell", .failed),
                             call(3, "web", .running)]], names: [:], in: ["a"])
        XCTAssertEqual(rows.map(\.symbol), ["●", "✕", "·"])
        XCTAssertEqual(Set(rows.map(\.symbol)).count, 3)
    }

    func testFeedKeepsTheCurrentActionAndSixteenFinishedRows() {
        var calls = (1...40).map { call($0, $0 % 2 == 0 ? "files" : "shell", .done) }
        calls.append(call(41, "web", .running))
        let rows = FleetMap.feedRows(sessions: ["a": calls], names: [:], in: ["a"])
        XCTAssertEqual(rows.count, 17)
        XCTAssertEqual(rows.first?.end, .running)
    }

    func testPulseProgressIsAPureFunctionOfTheStamp() {
        XCTAssertEqual(FleetMap.pulseProgress(seenAt: 100, now: 100), 0)
        XCTAssertEqual(FleetMap.pulseProgress(seenAt: 100, now: 100.3), 0.5, accuracy: 1e-9)
        XCTAssertEqual(FleetMap.pulseProgress(seenAt: 100, now: 200), 1)
        XCTAssertEqual(FleetMap.pulseProgress(seenAt: 100, now: 50), 0)
        // Seen before the pane was watching: already landed.
        XCTAssertEqual(FleetMap.pulseProgress(seenAt: 0, now: 100), 1)
    }

    func testThicknessIsLogScaledAndClamped() {
        XCTAssertEqual(FleetMap.thickness(count: 0), 1)
        XCTAssertLessThan(FleetMap.thickness(count: 1), FleetMap.thickness(count: 10))
        XCTAssertLessThan(FleetMap.thickness(count: 10), FleetMap.thickness(count: 1000))
        XCTAssertEqual(FleetMap.thickness(count: 10_000_000), 4)
    }

    func testTurnEndedRingFiresOnlyWhenRunningStops() {
        XCTAssertTrue(FleetMap.turnEndedRing(previous: .running, current: .sleeping))
        XCTAssertTrue(FleetMap.turnEndedRing(previous: .running, current: .waiting))
        XCTAssertFalse(FleetMap.turnEndedRing(previous: .running, current: .running))
        XCTAssertFalse(FleetMap.turnEndedRing(previous: nil, current: .sleeping))
        XCTAssertFalse(FleetMap.turnEndedRing(previous: .sleeping, current: .running))
    }

    func testTheFeedIsScopedToTheSelectedTabsSessions() {
        // Project A's tab holds "a"; project B's "b" and a gone session "z"
        // have calls in the section but must not appear on A's feed.
        let sessions = ["a": [call(1, "files", .done)],
                        "b": [call(2, "shell", .running, tool: "Bash")],
                        "z": [call(3, "web", .done)]]
        let rows = FleetMap.feedRows(sessions: sessions, names: ["a": "Cipher"],
                                     in: ["a"])
        XCTAssertEqual(rows.map(\.sessionId), ["a"])
        let other = FleetMap.feedRows(sessions: sessions, names: ["b": "Vex"],
                                      in: ["b"])
        XCTAssertEqual(other.map(\.name), ["Vex"])
    }

    func testAFailureOutlivesTheCallWindow() {
        // Eight newer calls pushed the failed entry out of `calls`, but the
        // daemon still states it per station until the next prompt.
        let gone = FleetSessionActivity(calls: (10...17).map { call($0, "files", .done) },
                                        stationCounts: [:], errors: 1,
                                        errorsByStation: ["shell": 1])
        XCTAssertEqual(FleetMap.failedCounts(sessions: ["a": gone], in: ["a"]),
                       ["shell": 1])
        let cleared = FleetSessionActivity(calls: gone.calls, stationCounts: [:],
                                           errors: 0, errorsByStation: [:])
        XCTAssertTrue(FleetMap.failedCounts(sessions: ["a": cleared], in: ["a"]).isEmpty)
    }

    func testFailedCountsStandUntilTheErrorsClear() {
        let sessions = ["a": FleetSessionActivity(calls: [call(1, "shell", .failed)],
                                                  stationCounts: [:], errors: 1,
                                                  errorsByStation: ["shell": 1]),
                        "b": FleetSessionActivity(calls: [call(2, "web", .failed)],
                                                  stationCounts: [:], errors: 0,
                                                  errorsByStation: ["web": 1])]
        XCTAssertEqual(FleetMap.failedCounts(sessions: sessions, in: ["a", "b"]),
                       ["shell": 1])
    }

    func testGeometryStaysInsideTheSquare() {
        let side = 496.0
        for i in 0..<7 {
            let p = FleetMap.stationPoint(index: i, count: 7, side: side)
            XCTAssertTrue(p.x > 0 && p.x < side && p.y > 0 && p.y < side)
        }
        XCTAssertEqual(FleetMap.portraitCenters(count: 1, side: side),
                       [CGPoint(x: 248, y: 248)])
        XCTAssertEqual(FleetMap.portraitCenters(count: 5, side: side).count, 5)
        XCTAssertTrue(FleetMap.portraitCenters(count: 0, side: side).isEmpty)
    }
}
