import XCTest
@testable import BobPanel

/// Real read decoders and asynchronous fetchers, with no socket or live state.
/// These are fixture-readiness checks, never participant accessibility evidence.
final class DesktopUsabilityFixtureTests: XCTestCase {
    private func variants() throws -> [String: [String: Any]] {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/r24-desktop-usability.json")
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        return try XCTUnwrap(root["variants"] as? [String: [String: Any]])
    }

    private func decode<T: Decodable>(_ value: Any, as type: T.Type) throws -> T {
        try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: value))
    }

    func testActualModelsPreserveAllTwentyIdentitiesAndFullEvidence() throws {
        for variant in try variants().values {
            let frames = try XCTUnwrap(variant["frames"] as? [String: Any])
            let snapshot = try decode(try XCTUnwrap(frames["baseline"]), as: Snapshot.self)
            let rows = snapshot.agents.running + snapshot.agents.waiting + snapshot.agents.sleeping
            XCTAssertEqual(rows.count, 20)
            XCTAssertEqual(Set(rows.map(\.sessionId)).count, 20)
            XCTAssertEqual([snapshot.agents.running.count, snapshot.agents.waiting.count,
                            snapshot.agents.sleeping.count], [12, 4, 4])
            XCTAssertEqual(Set(rows.map(\.project)).count, 3)
            XCTAssertFalse(snapshot.board.dispatchEnabled)
            XCTAssertTrue(rows.allSatisfy { $0.pid == nil && !$0.canStop && !$0.canType
                && !$0.canClose && !$0.canJump && !$0.ownTerminal && !$0.channel })
            let answers = try XCTUnwrap(variant["answers"] as? [String: Any])
            let targetID = try XCTUnwrap(answers["target_card"] as? String)
            // The fixture still carries the retired `blockers` array (it is a
            // recorded dataset); the model ignores it like any unknown key,
            // and the long Polish titles it names are checked as cards.
            let names = try XCTUnwrap(answers["blocker_names"] as? [String])
            let target = try XCTUnwrap(snapshot.board.cards.first { $0.id == targetID })
            XCTAssertFalse(target.id.isEmpty)
            XCTAssertTrue(names.contains { $0.count == 200 })
            XCTAssertTrue(names.allSatisfy { $0.contains("Żółć") && $0.contains("Łódź") })
            XCTAssertNotEqual(names[0], names[1])
            XCTAssertEqual(String(names[0].prefix(170)), String(names[1].prefix(170)))
            for name in names {
                XCTAssertNotNil(snapshot.board.cards.first { $0.title == name })
            }
            let full = try XCTUnwrap(variant["full_cards"] as? [String: Any])
            for (id, value) in full {
                let response = try decode(value, as: BoardReport.self)
                XCTAssertTrue(response.available)
                XCTAssertEqual(response.cards.count, 1)
                XCTAssertEqual(response.cards.first?.id, id)
                XCTAssertEqual(response.cards.first?.title, snapshot.board.cards.first { $0.id == id }?.title)
            }
            let targetReport = try decode(try XCTUnwrap(full[targetID]), as: BoardReport.self)
            let embedded = try XCTUnwrap(targetReport.cards.first?.workRecordFull)
            let work = try XCTUnwrap(variant["work_records"] as? [String: Any])
            let fallback = try decode(try XCTUnwrap(work[targetID]), as: WorkRecordReport.self)
            XCTAssertTrue(fallback.available)
            XCTAssertEqual(embedded, fallback.record)
            XCTAssertTrue(embedded.report.contains(try XCTUnwrap(answers["limitation"] as? String)))
            XCTAssertEqual(embedded.files.first?.path, answers["changed_file"] as? String)
            XCTAssertNil(target.workRecordFull, "Full evidence must not ride a state frame")
            let unavailableID = try XCTUnwrap(answers["unavailable_card"] as? String)
            let unavailable = try decode(try XCTUnwrap(work[unavailableID]), as: WorkRecordReport.self)
            XCTAssertFalse(unavailable.available)
            XCTAssertNil(unavailable.record)
            XCTAssertFalse(unavailable.reason.isEmpty)
            let practice = try XCTUnwrap(answers["practice_card"] as? String)
            let absent = try decode(try XCTUnwrap(work[practice]), as: WorkRecordReport.self)
            XCTAssertTrue(absent.available)
            XCTAssertNil(absent.record)
            let diffs = try XCTUnwrap(variant["diffs"] as? [String: [Any]])
            let diff = try decode(try XCTUnwrap(diffs[targetID]?.first), as: WorkRecordDiff.self)
            XCTAssertTrue(diff.available)
            XCTAssertFalse(diff.truncated)
            XCTAssertEqual(diff.path, embedded.files.first?.path)
            XCTAssertTrue(diff.text.contains("Live warehouse untested"))
            let inbox = Inbox.items(rows: [], prompts: [], cards: snapshot.board.cards)
            // A ready plan and a closed card awaiting review are the
            // Backlog tab's and the Done column's, not the inbox's.
            XCTAssertNil(inbox.first { $0.cardId == targetID })
            XCTAssertTrue(inbox.contains { $0.wire == .manualCheck })
            XCTAssertTrue(inbox.allSatisfy { $0.wire == .manualCheck || $0.wire == .endedWork })
        }
    }

    /// The History tab left the desk (20 Sep 2026); the fixture's `history`
    /// variants are the phone's to decode now. The session record the card
    /// window fetches is still read here.
    func testEveryOptionalReadDecodesWithoutInventingRecords() throws {
        for variant in try variants().values {
            let records = try XCTUnwrap(variant["session_records"] as? [String: Any])
            for (id, value) in records {
                let report = try decode(value, as: SessionRecordReport.self)
                XCTAssertTrue(report.available)
                XCTAssertEqual(report.session?.sessionId, id)
                XCTAssertEqual(report.live, id.contains("-live-"))
            }
            XCTAssertTrue(try decode(try XCTUnwrap(variant["done"]), as: BoardReport.self).available)
            XCTAssertTrue(try decode(try XCTUnwrap(variant["usage"]), as: UsageReport.self).bars.isEmpty)
        }
    }

    func testDeterministicFramesExerciseStableSelectionAndExistingFallbackRule() throws {
        for variant in try variants().values {
            let frames = try XCTUnwrap(variant["frames"] as? [String: Any])
            let answers = try XCTUnwrap(variant["answers"] as? [String: Any])
            let selected = try XCTUnwrap(answers["selected_row"] as? String)
            let baseline = try decode(try XCTUnwrap(frames["baseline"]), as: Snapshot.self)
            let status = try decode(try XCTUnwrap(frames["status"]), as: Snapshot.self)
            let removed = try decode(try XCTUnwrap(frames["removed"]), as: Snapshot.self)
            func IDs(_ snapshot: Snapshot) -> Set<String> {
                Set((snapshot.agents.running + snapshot.agents.waiting + snapshot.agents.sleeping).map(\.id))
            }
            XCTAssertEqual(IDs(baseline), IDs(status))
            XCTAssertFalse(RailLayout.reaimAfterListChange(selected: selected, listed: IDs(status)))
            XCTAssertTrue(RailLayout.reaimAfterListChange(selected: selected, listed: IDs(removed)))
            XCTAssertFalse(RailLayout.reaimAfterListChange(selected: nil, listed: IDs(removed)))
            XCTAssertEqual(removed.agents.waiting.first?.id, answers["expected_fallback"] as? String)
            XCTAssertEqual(RailLayout.escapeRung(editing: true, filterActive: true,
                                                detailOpen: true, drilledIn: true), .endEditing)
            XCTAssertEqual(RailLayout.leftPane(tab: .inbox, selected: selected), .board)
            XCTAssertEqual(RailLayout.leftPane(tab: .agents, selected: selected), .detail(selected))
        }
    }

    @MainActor
    func testRealAsyncFetchesUseOnlyFixtureResponses() async throws {
        XCTAssertTrue(PanelStateDirectory.isTesting)
        let variant = try XCTUnwrap(try variants()["A"])
        let answers = try XCTUnwrap(variant["answers"] as? [String: Any])
        let id = try XCTUnwrap(answers["target_card"] as? String)
        let historical = try XCTUnwrap(answers["history_session"] as? String)
        let full = try XCTUnwrap(variant["full_cards"] as? [String: Any])
        let work = try XCTUnwrap(variant["work_records"] as? [String: Any])
        let diffs = try XCTUnwrap(variant["diffs"] as? [String: [Any]])
        let records = try XCTUnwrap(variant["session_records"] as? [String: Any])
        let unavailableID = try XCTUnwrap(answers["unavailable_card"] as? String)
        let routes: [String: Any] = [
            "/api/board?card=\(id)": try XCTUnwrap(full[id]),
            "/api/work-record?card=\(id)": try XCTUnwrap(work[id]),
            "/api/work-record?card=\(id)&file=0": try XCTUnwrap(diffs[id]?.first),
            "/api/work-record?card=\(unavailableID)": try XCTUnwrap(work[unavailableID]),
            "/api/history?session=\(historical)": try XCTUnwrap(records[historical]),
            "/api/usage": try XCTUnwrap(variant["usage"]),
        ]
        let canonicalRoutes = Dictionary(uniqueKeysWithValues: routes.map { ($0.key.removingPercentEncoding!, $0.value) })
        R24ReadProtocol.responses = try canonicalRoutes.mapValues { try JSONSerialization.data(withJSONObject: $0) }
        R24ReadProtocol.unexpected = []
        R24ReadProtocol.seen = []
        URLProtocol.registerClass(R24ReadProtocol.self)
        defer { URLProtocol.unregisterClass(R24ReadProtocol.self) }
        let client = DaemonClient()
        let card = await client.boardCard(id)
        XCTAssertEqual(card?.id, id)
        let report = await client.workRecordReport(id)
        XCTAssertEqual(report?.record, card?.workRecordFull)
        let diff = await client.workRecordDiff(id, file: 0)
        XCTAssertEqual(diff?.path, answers["changed_file"] as? String)
        let absent = await client.workRecordReport(unavailableID)
        XCTAssertEqual(absent?.available, false)
        XCTAssertNil(absent?.record)
        let session = await client.sessionRecord(historical)
        XCTAssertEqual(session?.session?.title, answers["history_title"] as? String)
        await client.refreshUsage()
        XCTAssertTrue(R24ReadProtocol.unexpected.isEmpty, R24ReadProtocol.unexpected.joined(separator: ", "))
        XCTAssertEqual(Set(R24ReadProtocol.seen), Set(canonicalRoutes.keys))
    }
}

/// Intercept EVERY request while registered. An unexpected one fails locally;
/// returning false here would let a faulty test reach the real loopback daemon.
private final class R24ReadProtocol: URLProtocol {
    static var responses: [String: Data] = [:]
    static var unexpected: [String] = []
    static var seen: [String] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let path = (url.path + (url.query.map { "?" + $0 } ?? "")).removingPercentEncoding!
        Self.seen.append(path)
        guard request.httpMethod == "GET", let body = Self.responses[path] else {
            Self.unexpected.append(path)
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
