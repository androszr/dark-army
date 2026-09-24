import XCTest
@testable import BobPanel

/// The agent efficiency report, panel side
/// (`plans/2026-09-09-agent-efficiency-reporting.md`).
///
/// Two ways this breaks on screen, and both are pinned here. **Ragged
/// decode**: an older daemon serves none of the new sections, and Swift's
/// synthesized `Decodable` throws on a missing key even where the property
/// has a default — one absent section must never blank the whole History tab.
/// And **a dash is not a zero**: every figure Dark Army cannot vouch for
/// arrives as `null` and has to read as "unknown", because a `0` in a cost or
/// a rate column is a measurement.
final class AgentReportTests: XCTestCase {

    private func report(_ json: String) throws -> HistoryReport {
        try JSONDecoder().decode(HistoryReport.self, from: Data(json.utf8))
    }

    // MARK: - Ragged decode

    func testAReportWithNoneOfTheNewSectionsStillDecodes() throws {
        let decoded = try report(#"{"available": true, "range": "30d"}"#)
        XCTAssertTrue(decoded.available)
        XCTAssertTrue(decoded.byAgent.isEmpty)
        XCTAssertTrue(decoded.topDispatches.isEmpty)
        XCTAssertTrue(decoded.byModel.isEmpty)
        XCTAssertTrue(decoded.hourly.isEmpty)
        XCTAssertFalse(decoded.effectiveness.available)
        XCTAssertEqual(decoded.waiting.spans, 0)
        XCTAssertTrue(decoded.limits.series.isEmpty)
        XCTAssertTrue(decoded.context.byProject.isEmpty)
    }

    func testAFullReportDecodesEverySection() throws {
        let decoded = try report(#"""
        {"available": true, "range": "30d",
         "totals": {"turns": 4, "cost_usd": 9.5},
         "daily": [{"day": "2026-09-08", "cost_usd": 2.0, "turns": 2}],
         "by_project": [{"project": "bob", "sessions": 1, "cost_usd": 2.0}],
         "sessions": [{"session_id": "s1", "project": "bob"}],
         "by_model": [{"model": "claude-sonnet-4-6", "turns": 4,
                       "output_tokens": 90, "cost_usd": 9.5}],
         "waiting": {"spans": 3, "total_seconds": 600, "median_seconds": 120,
                     "by_day": [{"day": "2026-09-08", "spans": 2,
                                 "total_seconds": 400}]},
         "limits": {"series": [{"ts": 1, "five_hour_pct": 40,
                                "seven_day_pct": 12}],
                    "burn_pct_per_hour": 3.5},
         "hourly": [{"hour": 9, "turns": 4, "output_tokens": 90}],
         "context": {"by_project": [{"project": "bob", "peak_pct": 91,
                                     "compactions": 2, "sessions": 1}],
                     "compactions": 2},
         "by_agent": [{"name": "bc-implementer", "role": "bc-implementer",
                       "dispatches": 3, "sessions": 2, "turns": 4,
                       "duration_ms": 4200, "tool_calls": 7,
                       "cost_usd": 6.25, "cache_hit_ratio": 0.5,
                       "models": ["claude-sonnet-4-6"]}],
         "top_dispatches": [{"agent_id": "a1", "name": "bc-verifier",
                             "session_id": "s1", "turns": 2,
                             "duration_ms": 900, "tool_calls": 3,
                             "cost_usd": 1.5, "model": "claude-sonnet-4-6"}],
         "effectiveness": {"supported": true, "available": true,
                           "outcomes_available": true,
                           "sessions_truncated": false,
                           "summary": {"accepted_outcomes": 2,
                                       "submitted_cards": 3,
                                       "reworked_cards": 1,
                                       "rework_rate": 0.3333,
                                       "manual_checks_outstanding": 1,
                                       "joined_cards": 3,
                                       "joined_sessions": 4,
                                       "sessions_with_history": 4,
                                       "partial": false},
                           "cards": [{"card_id": "c1", "title": "Ship it",
                                      "accepted": true, "rework_count": 0,
                                      "cost_usd": 4.0, "turns": 2,
                                      "duration_ms": 3000,
                                      "partial": false}]}}
        """#)
        XCTAssertEqual(decoded.byAgent.first?.name, "bc-implementer")
        XCTAssertEqual(decoded.byAgent.first?.role, "bc-implementer")
        XCTAssertEqual(decoded.byAgent.first?.dispatches, 3)
        XCTAssertEqual(decoded.byAgent.first?.cacheHitRatio, 0.5)
        XCTAssertEqual(decoded.byAgent.first?.models, ["claude-sonnet-4-6"])
        XCTAssertEqual(decoded.topDispatches.first?.agentId, "a1")
        XCTAssertEqual(decoded.topDispatches.first?.model, "claude-sonnet-4-6")
        XCTAssertEqual(decoded.byModel.first?.costUsd, 9.5)
        XCTAssertEqual(decoded.waiting.byDay.first?.totalSeconds, 400)
        XCTAssertEqual(decoded.limits.series.first?.fiveHourPct, 40)
        XCTAssertEqual(decoded.hourly.first?.hour, 9)
        XCTAssertEqual(decoded.context.byProject.first?.peakPct, 91)
        XCTAssertEqual(decoded.effectiveness.summary.acceptedOutcomes, 2)
        XCTAssertEqual(decoded.effectiveness.cards.first?.cardId, "c1")
        XCTAssertTrue(decoded.effectiveness.cards.first?.accepted ?? false)
    }

    // MARK: - A dash is not a zero

    func testAnUnpricedHelperReadsAsUnknownRatherThanFree() throws {
        let decoded = try report(#"""
        {"by_agent": [{"name": "bc-planner", "cost_usd": null,
                       "cache_hit_ratio": null, "unpriced_turns": 4}]}
        """#)
        let row = try XCTUnwrap(decoded.byAgent.first)
        XCTAssertNil(row.costUsd)
        XCTAssertNil(row.cacheHitRatio)
        XCTAssertEqual(HistoryFormat.optionalUsd(row.costUsd), "—")
        XCTAssertEqual(HistoryFormat.percent(row.cacheHitRatio), "—")
    }

    /// `limits_report`'s series admits a bucket where only the seven-day
    /// window read (`five_hour_pct IS NOT NULL OR seven_day_pct IS NOT
    /// NULL`), so `MAX(five_hour_pct)` arrives as JSON `null`. Decoding that
    /// as `0` drew a zero-height bar whose hover said "5h 0%".
    func testAnUnmeasuredLimitSampleIsNotZeroPercent() throws {
        let decoded = try report(#"""
        {"limits": {"series": [{"ts": 1, "five_hour_pct": null,
                                "seven_day_pct": 12},
                               {"ts": 2, "five_hour_pct": 0,
                                "seven_day_pct": 12}]}}
        """#)
        let unmeasured = try XCTUnwrap(decoded.limits.series.first)
        XCTAssertNil(unmeasured.fiveHourPct)
        XCTAssertEqual(HistoryFormat.percentPoints(unmeasured.fiveHourPct), "—")
        // …and a window that genuinely read zero still says zero.
        let measured = try XCTUnwrap(decoded.limits.series.last)
        XCTAssertEqual(measured.fiveHourPct, 0)
        XCTAssertEqual(HistoryFormat.percentPoints(measured.fiveHourPct), "0%")
    }

    func testAGenuineZeroIsStillAFigure() {
        XCTAssertEqual(HistoryFormat.percent(0), "0%")
        XCTAssertEqual(HistoryFormat.optionalUsd(0), Format.usd(0))
        XCTAssertEqual(HistoryFormat.duration(ms: 0), "—")
    }

    func testAnEmptyDenominatorReworkRateIsNil() throws {
        let decoded = try report(#"""
        {"effectiveness": {"available": true,
                           "summary": {"submitted_cards": 0,
                                       "rework_rate": null}}}
        """#)
        XCTAssertNil(decoded.effectiveness.summary.reworkRate)
        XCTAssertEqual(
            HistoryFormat.percent(decoded.effectiveness.summary.reworkRate), "—")
    }

    // MARK: - Nobody asked is not the same as somebody said no

    func testAcceptanceIsAbsentRatherThanFalseWhereNobodyMeasuredIt() throws {
        // The machine-wide report: `available` true, and no acceptance in it.
        // Gating a stated line on `available` here would print "0 accepted ·
        // 0 submitted · rework —" for a period nobody measured.
        let decoded = try report(#"""
        {"available": true,
         "effectiveness": {"supported": true, "available": true,
                           "outcomes_available": false,
                           "agent_scope": "machine",
                           "outcomes_reason": "Acceptance is reported per project; choose one to see it.",
                           "cards": [{"card_id": "c1", "title": "Ship it"}]}}
        """#)
        XCTAssertTrue(decoded.effectiveness.available)
        XCTAssertFalse(decoded.effectiveness.outcomesAvailable)
        XCTAssertEqual(decoded.effectiveness.agentScope, "machine")
        XCTAssertNil(decoded.effectiveness.cards.first?.accepted)
        XCTAssertNil(decoded.effectiveness.cards.first?.reworkCount)
        XCTAssertFalse(decoded.effectiveness.outcomesReason.isEmpty)
    }

    func testARejectionStillReadsAsARejection() throws {
        let decoded = try report(#"""
        {"effectiveness": {"available": true, "outcomes_available": true,
                           "agent_scope": "project", "root": "/p",
                           "cards": [{"card_id": "c1", "accepted": false,
                                      "rework_count": 2}]}}
        """#)
        XCTAssertEqual(decoded.effectiveness.cards.first?.accepted, false)
        XCTAssertEqual(decoded.effectiveness.cards.first?.reworkCount, 2)
        XCTAssertEqual(decoded.effectiveness.root, "/p")
    }

    // MARK: - One question, one answer, on both surfaces

    /// The helper table is gone. The payload still decodes: the narrowed
    /// list lives on `effectiveness.agents`, and the machine-wide list on
    /// `byAgent`, and neither is drawn.
    func testAProjectScopedReportKeepsTheNarrowedHelpers() throws {
        let decoded = try report(#"""
        {"available": true,
         "by_agent": [{"name": "bc-implementer"}, {"name": "bc-verifier"},
                      {"name": "bc-planner"}],
         "effectiveness": {"available": true, "agent_scope": "project",
                           "root": "/p",
                           "agents": [{"name": "bc-verifier",
                                       "dispatches": 2}]}}
        """#)
        XCTAssertEqual(decoded.effectiveness.agents.map(\.name), ["bc-verifier"])
        XCTAssertEqual(decoded.effectiveness.agents.first?.dispatches, 2)
        XCTAssertEqual(decoded.byAgent.map(\.name).count, 3)
    }

    func testAMachineWideReportStillDecodesTheMachineWideList() throws {
        let decoded = try report(#"""
        {"available": true,
         "by_agent": [{"name": "bc-implementer"}, {"name": "bc-verifier"}],
         "effectiveness": {"available": true, "agent_scope": "machine",
                           "agents": []}}
        """#)
        XCTAssertEqual(decoded.byAgent.map(\.name),
                       ["bc-implementer", "bc-verifier"])
        XCTAssertEqual(decoded.effectiveness.agentScope, "machine")
    }

    /// A failed effectiveness half decodes as unavailable and still speaks
    /// its reason. It does not become a table of the machine-wide helpers.
    func testAFailedReportDoesNotBecomeAHelperTable() throws {
        let decoded = try report(#"""
        {"available": true,
         "by_agent": [{"name": "bc-implementer"}, {"name": "bc-verifier"}],
         "effectiveness": {"supported": true, "available": false,
                           "reason": "the agent report could not be computed"}}
        """#)
        XCTAssertEqual(decoded.effectiveness.agentScope, "")
        XCTAssertFalse(decoded.effectiveness.available)
        XCTAssertTrue(decoded.effectiveness.agents.isEmpty)
        XCTAssertEqual(decoded.byAgent.map(\.name),
                       ["bc-implementer", "bc-verifier"])
        XCTAssertTrue(decoded.effectiveness.speaks)
    }

    /// Acceptance is not a row of a helper table. An empty helper list does
    /// not hide a summary that was actually computed.
    func testTheAcceptanceHalfSpeaksWithNoHelperRowsAtAll() throws {
        let decoded = try report(#"""
        {"available": true, "by_agent": [],
         "effectiveness": {"available": true, "agent_scope": "project",
                           "outcomes_available": true, "agents": [],
                           "summary": {"accepted_outcomes": 2}}}
        """#)
        XCTAssertTrue(decoded.effectiveness.agents.isEmpty)
        XCTAssertEqual(decoded.effectiveness.summary.acceptedOutcomes, 2)
        XCTAssertTrue(decoded.effectiveness.speaks)
    }

    func testAnEmptyEffectivenessHalfSaysNothing() throws {
        let decoded = try report(#"{"available": true}"#)
        XCTAssertFalse(decoded.effectiveness.speaks)
    }
}


/// The History tab's fetch, and the one way it wedges.
///
/// `loadHistory`'s re-entry guard used to compare against `historyRange` /
/// `historyRoot`, which the range buttons and the project picker write
/// **before** `.task(id:)` re-fires. The guard then found its own state
/// already matching, saw `.loading` from the request `.task(id:)` had just
/// cancelled, and returned — leaving `historyLoad` at `.loading` for the life
/// of the panel, with every later pick re-entering the same early return.
final class HistoryFetchTests: XCTestCase {

    override func setUp() {
        super.setUp()
        StubHistory.delay = 0
        URLProtocol.registerClass(StubHistory.self)
    }

    override func tearDown() {
        URLProtocol.unregisterClass(StubHistory.self)
        StubHistory.delay = 0
        super.tearDown()
    }

    @MainActor
    func testPickingAProjectDuringTheFirstLoadStillEndsLoaded() async {
        let client = DaemonClient()
        StubHistory.delay = 0.4
        // The first fetch, still in flight…
        let first = Task { await client.loadHistory(range: "30d", root: "") }
        try? await Task.sleep(for: .milliseconds(80))
        guard case .loading = client.historyLoad else {
            return XCTFail("the first fetch should be in flight")
        }
        // …and the picker writes its selection before `.task(id:)` re-fires,
        // which is the whole shape of the bug.
        client.historyRoot = "/p"
        StubHistory.delay = 0
        await client.loadHistory(range: "30d", root: "/p")
        guard case .loaded(let report) = client.historyLoad else {
            return XCTFail("the History tab wedged in .loading")
        }
        XCTAssertTrue(report.available)
        _ = await first.value
        // The late answer to the question nobody is asking any more is still
        // dropped, so the second answer survives it.
        guard case .loaded = client.historyLoad else {
            return XCTFail("a late answer overwrote the current one")
        }
    }

    @MainActor
    func testTheSameFetchTwiceOverIsStillOnlyOneRequest() async {
        let client = DaemonClient()
        StubHistory.delay = 0.3
        StubHistory.requests = 0
        let a = Task { await client.loadHistory(range: "7d", root: "") }
        try? await Task.sleep(for: .milliseconds(60))
        await client.loadHistory(range: "7d", root: "")
        _ = await a.value
        XCTAssertEqual(StubHistory.requests, 1)
    }
}

/// Answers `/api/history` in-process. Registered on `URLSession.shared`, so
/// nothing here opens a socket or needs a daemon.
final class StubHistory: URLProtocol {
    static var delay: TimeInterval = 0
    static var requests = 0

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.path == "/api/history"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.requests += 1
        let url = request.url!
        let body = Data(#"{"available": true, "range": "30d"}"#.utf8)
        let response = HTTPURLResponse(url: url, statusCode: 200,
                                       httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        let client = self.client
        DispatchQueue.global().asyncAfter(deadline: .now() + Self.delay) { [weak self] in
            guard let self else { return }
            client?.urlProtocol(self, didReceive: response,
                                cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}
}
