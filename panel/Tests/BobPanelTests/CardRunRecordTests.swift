import XCTest
@testable import BobPanel

/// The card's "What ran" record: tolerant decode of the `?session=` wrapper
/// and the pure formatting helpers the sheet draws.
final class CardRunRecordTests: XCTestCase {

    private func record(_ json: String) throws -> SessionRecord {
        try JSONDecoder().decode(SessionRecord.self, from: Data(json.utf8))
    }

    private func report(_ json: String) throws -> SessionRecordReport {
        try JSONDecoder().decode(SessionRecordReport.self, from: Data(json.utf8))
    }

    func testFullRecordDecodes() throws {
        let rec = try record("""
            {"session_id":"s1","project":"p","title":"Rewrite the pipeline",
             "kind":"interactive","provider":"claude",
             "primary_model":"claude-opus-4-1","first_seen":1000,
             "last_seen":1120,"ended_at":1120,"end_reason":"complete",
             "cost_usd":1.5,"cost_source":"measured","turns":6,"tokens":54,
             "subagents":2,"tools":[["Bash",41],["Read",23]],
             "attribution":["reviewer"],"compactions":1}
            """)
        XCTAssertEqual(rec.sessionId, "s1")
        XCTAssertEqual(rec.project, "p")
        XCTAssertEqual(rec.title, "Rewrite the pipeline")
        XCTAssertEqual(rec.kind, "interactive")
        XCTAssertEqual(rec.provider, "claude")
        XCTAssertEqual(rec.primaryModel, "claude-opus-4-1")
        XCTAssertEqual(rec.firstSeen, 1000)
        XCTAssertEqual(rec.lastSeen, 1120)
        XCTAssertEqual(rec.endedAt, 1120)
        XCTAssertEqual(rec.endReason, "complete")
        XCTAssertEqual(rec.costUsd, 1.5)
        XCTAssertEqual(rec.costSource, "measured")
        XCTAssertEqual(rec.turns, 6)
        XCTAssertEqual(rec.tokens, 54)
        XCTAssertEqual(rec.subagents, 2)
        XCTAssertEqual(rec.tools.map(\.name), ["Bash", "Read"])
        XCTAssertEqual(rec.tools.map(\.count), [41, 23])
        XCTAssertEqual(rec.attribution, ["reviewer"])
        XCTAssertEqual(rec.compactions, 1)
    }

    /// A ragged `{}` must not throw, and must not invent zeros for the
    /// numbers Dark Army may not know — Models.swift's documented trap.
    func testEmptyObjectLeavesOptionalsNil() throws {
        let rec = try record("{}")
        XCTAssertNil(rec.costUsd)
        XCTAssertNil(rec.tokens)
        XCTAssertNil(rec.firstSeen)
        XCTAssertNil(rec.lastSeen)
        XCTAssertNil(rec.endedAt)
        XCTAssertEqual(rec.sessionId, "")
        XCTAssertEqual(rec.turns, 0)
        XCTAssertTrue(rec.tools.isEmpty)
    }

    func testUnavailableWrapperIsDistinctFromNoRecord() throws {
        let closed = try report(
            #"{"available":false,"reason":"history database is not open"}"#)
        XCTAssertFalse(closed.available)
        XCTAssertNil(closed.session)
        XCTAssertEqual(closed.reason, "history database is not open")

        let missing = try report(
            #"{"available":true,"live":false,"session":null}"#)
        XCTAssertTrue(missing.available)
        XCTAssertFalse(missing.live)
        XCTAssertNil(missing.session)
        XCTAssertEqual(missing.reason, "")
    }

    func testNullCostDoesNotBecomeZero() throws {
        let rec = try record(#"{"session_id":"s1","cost_usd":null}"#)
        XCTAssertNil(rec.costUsd)
    }

    func testToolsLine() {
        XCTAssertEqual(
            SessionRecordFormat.toolsLine([
                SessionTool(name: "Bash", count: 41),
                SessionTool(name: "Read", count: 23),
            ]),
            "Bash ×41 · Read ×23")
        XCTAssertEqual(SessionRecordFormat.toolsLine([]), "")
        XCTAssertEqual(
            SessionRecordFormat.toolsLine([SessionTool(name: "", count: 3)]),
            "")
    }

    func testDurationText() {
        XCTAssertEqual(
            SessionRecordFormat.durationText(
                firstSeen: 1000, endedAt: 1060, lastSeen: 1050),
            Format.duration(60))
        XCTAssertEqual(
            SessionRecordFormat.durationText(
                firstSeen: 1000, endedAt: nil, lastSeen: 1030),
            Format.duration(30))
        XCTAssertEqual(
            SessionRecordFormat.durationText(
                firstSeen: nil, endedAt: 10, lastSeen: 10),
            "—")
        XCTAssertEqual(
            SessionRecordFormat.durationText(
                firstSeen: 1000, endedAt: nil, lastSeen: nil),
            "—")
        XCTAssertEqual(
            SessionRecordFormat.durationText(
                firstSeen: 1000, endedAt: 999, lastSeen: 1100),
            "—")
    }

    func testAbsenceLineCoversLoadingAndTheThreeAbsences() throws {
        XCTAssertEqual(
            SessionRecordFormat.absenceLine(report: nil, fetchFailed: false),
            "Loading this run's record…")
        XCTAssertEqual(
            SessionRecordFormat.absenceLine(report: nil, fetchFailed: true),
            "Couldn't reach Dark Army for this run's record.")
        let closed = try report(
            #"{"available":false,"reason":"history database is not open"}"#)
        XCTAssertEqual(
            SessionRecordFormat.absenceLine(report: closed, fetchFailed: false),
            "Dark Army's history database isn't available.")
        let missing = try report(
            #"{"available":true,"live":false,"session":null}"#)
        XCTAssertEqual(
            SessionRecordFormat.absenceLine(report: missing, fetchFailed: false),
            "Dark Army has no durable record of this session.")
        let found = try report(
            #"{"available":true,"live":false,"session":{"session_id":"s1"}}"#)
        XCTAssertNil(
            SessionRecordFormat.absenceLine(report: found, fetchFailed: false))
        XCTAssertEqual(
            SessionRecordFormat.absenceLine(report: found, fetchFailed: true),
            "Couldn't reach Dark Army for this run's record.")
    }

    func testCostTextUsesTildeForEstimatesAndDashForUnknown() {
        XCTAssertEqual(
            SessionRecordFormat.costText(usd: 1.5, source: "measured"),
            Format.usd(1.5))
        XCTAssertEqual(
            SessionRecordFormat.costText(usd: 1.5, source: "estimated"),
            "~\(Format.usd(1.5))")
        XCTAssertEqual(
            SessionRecordFormat.costText(usd: nil, source: "measured"),
            "—")
    }
}
