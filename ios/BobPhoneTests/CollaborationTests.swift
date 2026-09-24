import XCTest
@testable import BobPhone

final class CollaborationTests: XCTestCase {
    static let fixture = """
    {"version":1,"available":true,"partial":false,"nodes":[
      {"id":"root","kind":"session","provider":"claude","session_id":"root","owner_session_id":"root","label":"Parent","resolution":"resolved","presence":"present","lifecycle":"live","inbox_observed":true,"cards":[{"id":"task","root":"/project","title":"Task","kind":"implementation"},{"id":"second","root":"/project","title":"Second","kind":"refinement"}]},
      {"id":"helper","kind":"helper","provider":"claude","session_id":"root","owner_session_id":"root","helper_id":"helper","label":"Verifier","resolution":"resolved","presence":"present","lifecycle":"live","inbox_observed":false,"cards":[{"id":"task","root":"/project","title":"Task","kind":"inherited-from-parent"}]},
      {"id":"ended","kind":"session","provider":"claude","session_id":"ended","owner_session_id":"ended","label":"Ended","resolution":"resolved","presence":"retained","lifecycle":"ended"},
      {"id":"ambiguous","kind":"recipient","label":"shared address","resolution":"ambiguous","presence":"unknown","lifecycle":"unknown"},
      {"id":"missing","kind":"recipient","label":"a long address with spaces and brackets [literal]","resolution":"unresolved","presence":"unknown","lifecycle":"unknown"},
      {"id":"quiet","kind":"session","provider":"claude","session_id":"quiet","owner_session_id":"quiet","label":"Quiet","resolution":"resolved","presence":"present","lifecycle":"live","inbox_observed":false},
      {"id":"peer","kind":"session","provider":"claude","session_id":"peer","owner_session_id":"peer","resolution":"resolved","presence":"present","lifecycle":"live"},
      {"id":"foreign","kind":"session","provider":"claude","session_id":"foreign","owner_session_id":"foreign","resolution":"resolved","presence":"present","lifecycle":"live"}
    ],"edges":[
      {"id":"parent","kind":"parent","source":"root","target":"helper"},
      {"id":"1","kind":"message","source":"root","target":"ended","count":2,"last":"2026-09-12T10:00:00Z"},
      {"id":"2","kind":"message","source":"root","target":"ambiguous","count":3,"last":"invalid"},
      {"id":"3","kind":"message","source":"root","target":"missing","count":4},
      {"id":"4","kind":"message","source":"quiet","target":"helper","count":5},
      {"id":"5","kind":"message","source":"helper","target":"peer","count":6},
      {"id":"foreign-traffic","kind":"message","source":"peer","target":"foreign","count":99}
    ]}
    """

    private func fixture() throws -> Collaboration {
        try JSONDecoder().decode(Collaboration.self, from: Data(Self.fixture.utf8))
    }

    func testSuccessStatesAreDistinctAndCallsNeverClaimDelivery() throws {
        let evidence = try fixture()
        let subjects = evidence.nodes.filter { ["helper", "ended", "ambiguous", "missing", "quiet"].contains($0.id) }
        XCTAssertEqual(Set(subjects.map(\.stateLabel)).count, 5)
        XCTAssertTrue(subjects.first { $0.id == "ended" }!.stateLabel.contains("ended"))
        XCTAssertTrue(subjects.first { $0.id == "missing" }!.stateLabel.contains("Unknown recipient"))
        let message = evidence.edges.first { $0.id == "1" }!
        XCTAssertEqual(message.observation, "Observed SendMessage calls: 2 · session-level total")
        XCTAssertNotEqual(message.lastObserved, "Last observed: undated")
        XCTAssertEqual(evidence.edges.first { $0.id == "2" }!.lastObserved, "Last observed: undated")
    }

    func testTaskFocusDoesNotExpandPeersOtherTask() throws {
        let evidence = try fixture()
        let rows = CollaborationFocus.card("task").edges(in: evidence)
        XCTAssertEqual(rows.count, 6)
        XCTAssertEqual(rows.first?.kind, "parent")
        XCTAssertFalse(rows.contains { $0.id == "foreign-traffic" })
        XCTAssertEqual(CollaborationFocus.session(provider: "claude", id: "root").edges(in: evidence).map(\.id),
                       rows.map(\.id))
        XCTAssertTrue(CollaborationFocus.card("missing-card").edges(in: evidence).isEmpty)
    }

    func testExactRoutesRevalidateCurrentIdentityAndNeverChooseAnotherCard() throws {
        let evidence = try fixture()
        let helper = evidence.nodes.first { $0.id == "helper" }!
        XCTAssertEqual(CollaborationRoute.agent(helper, current: [("claude", "root")]),
                       .session(provider: "claude", id: "root", helper: "helper"))
        XCTAssertEqual(CollaborationRoute.agent(helper, current: [("codex", "root")]),
                       .unavailable("Agent unavailable in this view"))
        XCTAssertEqual(CollaborationRoute.agent(helper, current: [("claude", "root"), ("claude", "root")]),
                       .unavailable("Agent unavailable in this view"))
        let link = helper.cards[0]
        XCTAssertEqual(CollaborationRoute.card(link, current: [("task", "/project")]), .card(id: "task", root: "/project"))
        XCTAssertEqual(CollaborationRoute.card(link, current: [("task", "/other"), ("same-title", "/project")]),
                       .unavailable("Card unavailable in this view"))
        for node in evidence.nodes where ["ambiguous", "missing"].contains(node.id) {
            guard case .unavailable = CollaborationRoute.agent(node, current: [("claude", "root")]) else {
                XCTFail("Uncertain recipient offered navigation"); continue
            }
        }
    }

    func testOldUnknownAndRaggedPicturesStayUnavailable() throws {
        for raw in ["{}", #"{"version":99,"available":true,"nodes":[{}]}"#] {
            let evidence = try JSONDecoder().decode(Collaboration.self, from: Data(raw.utf8))
            XCTAssertFalse(evidence.supported)
            XCTAssertTrue(evidence.nodes.isEmpty)
            XCTAssertTrue(evidence.coverage.contains("unavailable"))
        }
        let node = try JSONDecoder().decode(CollaborationNode.self, from: Data("""
        {"id":"a","kind":"future","resolution":"future","inbox_observed":"wrong"}
        """.utf8))
        XCTAssertFalse(node.known)
        XCTAssertNil(node.inboxObserved)
    }

    func testDuplicateWireIdsCannotCrashOrNavigate() throws {
        let evidence = try JSONDecoder().decode(Collaboration.self, from: Data("""
        {"version":1,"available":true,"partial":false,"nodes":[{"id":"a"},{"id":"a"}],"edges":[]}
        """.utf8))
        XCTAssertTrue(evidence.nodes.isEmpty)
        XCTAssertTrue(evidence.partial)
    }
}

import UIKit
import SwiftUI

extension CollaborationTests {
    @MainActor
    func testSyntheticJourneyHostAtLargestAccessibilitySizeAndBack() throws {
        let client = PhoneClient()
        client.snapshot = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"collaboration":\(Self.fixture),
         "agents":{"running":[{"session_id":"root","provider":"claude","nickname":"Parent"}],
                   "finished":[{"session_id":"ended","provider":"claude","nickname":"Ended"}]},
         "board":{"available":true,"cards":[{"id":"task","root":"/project","title":"Task"},
                                            {"id":"second","root":"/project","title":"Second"}]}}
        """.utf8))
        let router = PhoneSheetRouter()
        let card = client.snapshot.board.cards[0]
        router.show(.card(card))
        router.show(.agent(client.snapshot.agents.running[0], .running))
        router.show(.agent(client.snapshot.agents.running[0], .running))
        XCTAssertEqual(router.stack.count, 2)
        router.back()
        XCTAssertEqual(router.top, .card(card))
        let host = UIHostingController(rootView: CollaborationPhoneFixtureHost(client: client, router: router)
            .environmentObject(router)
            .environment(\.dynamicTypeSize, .accessibility5))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 375, height: 812)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        host.view.layoutIfNeeded()
        XCTAssertGreaterThan(host.view.bounds.height, 0)
        XCTAssertEqual(router.stack.count, 1)
        // An opt-in fixture host never starts polling or sends action requests.
        if ProcessInfo.processInfo.environment["BOB_COLLABORATION_MANUAL"] == "1" {
            let until = Date().addingTimeInterval(600)
            while Date() < until && !router.stack.isEmpty {
                RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            }
        }
    }
}

private struct CollaborationPhoneFixtureHost: View {
    @ObservedObject var client: PhoneClient
    @ObservedObject var router: PhoneSheetRouter
    var focus: CollaborationFocus {
        guard let subject = router.top?.subject else { return .card("task") }
        switch subject {
        case .agent(let agent, _): return .session(provider: agent.provider, id: agent.sessionId)
        case .card(let card): return .card(card.id)
        default: return .card("task")
        }
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading) {
                Text("Synthetic task evidence — no connection to the Mac")
                Button("Back") { router.back() }
                CollaborationView(client: client, focus: focus, initiallyExpanded: true)
            }.padding()
        }
    }
}

extension CollaborationTests {
    func testMapEffectsAreOnlyExactNavigationAndUnavailableReasons() throws {
        let evidence = try JSONDecoder().decode(Collaboration.self, from: Data(Self.fixture.utf8))
        var sessions: [String] = [], cards: [String] = [], explanations: [String] = []
        let helper = evidence.nodes.first { $0.id == "helper" }!
        let routes = [
            CollaborationRoute.agent(helper, current: [("claude", "root")]),
            CollaborationRoute.card(helper.cards[0], current: [("task", "/project")]),
            CollaborationRoute.agent(helper, current: []),
            CollaborationRoute.card(helper.cards[0], current: [("same-title", "/project")])]
        for route in routes {
            route.follow(session: { provider, id, child in sessions.append("\(provider)/\(id)/\(child)") },
                         card: { id, root in cards.append("\(id)/\(root)") },
                         unavailable: { explanations.append($0) })
        }
        XCTAssertEqual(sessions, ["claude/root/helper"])
        XCTAssertEqual(cards, ["task//project"])
        XCTAssertEqual(explanations, ["Agent unavailable in this view", "Card unavailable in this view"])
        // Every effect is accounted for; there is no Send/action callback or route case.
        XCTAssertEqual(sessions.count + cards.count + explanations.count, routes.count)
    }
}

extension CollaborationTests {
    @MainActor
    func testProductionMapControlsRevalidateAndBackWithoutActionRequests() async throws {
        XCTAssertTrue(URLProtocol.registerClass(CollaborationRequestSpy.self))
        defer { URLProtocol.unregisterClass(CollaborationRequestSpy.self) }
        CollaborationRequestSpy.reset()
        let client = PhoneClient()
        // A real paired client makes accidental action calls observable, even though every
        // request is intercepted. Its initial failed poll finishes before the map presses.
        client.start(record: PairingRecord(token: "collaboration-fixture", host: "127.0.0.1", port: 19874,
            deviceId: "collaboration-fixture", homeKey: Data(repeating: 42, count: 32).base64EncodedString()))
        defer { client.stop() }
        for _ in 0..<100 where client.status == .connecting {
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertFalse(CollaborationRequestSpy.seen.isEmpty, "The paired client must exercise the request spy")
        XCTAssertNotEqual(client.status, .connecting)
        CollaborationRequestSpy.reset()
        let snapshot = try controlSnapshot()
        client.snapshot = snapshot
        let helper = snapshot.collaboration!.nodes.first { $0.id == "helper" }!
        let cardLink = helper.cards[0]
        let sheets = PhoneSheetRouter()
        let task = snapshot.board.cards[0]
        sheets.show(.card(task))
        let receiptIDs = client.receipts.receipts.map(\.id)
        XCTAssertNil(CollaborationControls.openAgent(helper.id, client: client, sheets: sheets))
        XCTAssertEqual(sheets.top, .agent(snapshot.agents.waiting[0], .waiting))
        XCTAssertNil(CollaborationControls.openAgent(helper.id, client: client, sheets: sheets))
        XCTAssertEqual(sheets.stack.count, 2, "Repeated presses reuse the current detail")
        XCTAssertNil(CollaborationControls.openCard(cardLink, client: client, sheets: sheets))
        XCTAssertEqual(sheets.top, .card(task))
        XCTAssertEqual(sheets.stack.count, 3)
        sheets.back()
        XCTAssertEqual(sheets.top, .agent(snapshot.agents.waiting[0], .waiting))
        sheets.back()
        XCTAssertEqual(sheets.top, .card(task))
        XCTAssertNil(CollaborationControls.openAgent("ended", client: client, sheets: sheets))
        XCTAssertEqual(sheets.top, .agent(snapshot.agents.finished[0], .finished))
        sheets.back()
        for id in ["ambiguous", "missing", "removed"] {
            XCTAssertNotNil(CollaborationControls.openAgent(id, client: client, sheets: sheets))
        }
        client.snapshot.agents.waiting = []
        XCTAssertNotNil(CollaborationControls.openAgent(helper.id, client: client, sheets: sheets))
        client.snapshot = snapshot
        client.snapshot.agents.running.append(snapshot.agents.waiting[0])
        XCTAssertNotNil(CollaborationControls.openAgent(helper.id, client: client, sheets: sheets))
        client.snapshot = snapshot
        client.snapshot.collaboration = nil
        XCTAssertNotNil(CollaborationControls.openAgent(helper.id, client: client, sheets: sheets))
        client.snapshot = snapshot
        client.snapshot.board.cards[0].root = "/other"
        XCTAssertNotNil(CollaborationControls.openCard(cardLink, client: client, sheets: sheets))
        client.snapshot.board.cards = []
        XCTAssertNotNil(CollaborationControls.openCard(cardLink, client: client, sheets: sheets))
        XCTAssertEqual(sheets.stack.count, 1)
        XCTAssertEqual(sheets.top, .card(task))
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(CollaborationRequestSpy.seen, [], "Map controls must issue zero action or read requests")
        XCTAssertTrue(client.writesInFlight.isEmpty)
        XCTAssertEqual(client.receipts.receipts.map(\.id), receiptIDs, "No action receipt may be minted")
    }

    private func controlSnapshot() throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"collaboration":\(Self.fixture),
         "agents":{"running":[{"session_id":"root","provider":"codex","nickname":"Wrong provider"}],
                   "waiting":[{"session_id":"root","provider":"claude","nickname":"Current parent"}],
                   "finished":[{"session_id":"ended","provider":"claude","nickname":"Ended"}]},
         "board":{"available":true,"cards":[{"id":"task","root":"/project","title":"Current task"},
                                            {"id":"second","root":"/project","title":"Second"}]}}
        """.utf8))
    }
}

private final class CollaborationRequestSpy: URLProtocol {
    private static let lock = NSLock()
    private static var requests: [String] = []
    static var seen: [String] { lock.lock(); defer { lock.unlock() }; return requests }
    static func reset() { lock.lock(); defer { lock.unlock() }; requests = [] }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.requests.append((request.httpMethod ?? "GET") + " " + (request.url?.path ?? ""))
        Self.lock.unlock()
        // Never let this test reach a daemon, paired Mac, or relay.
        let response = HTTPURLResponse(url: request.url!, statusCode: 503,
                                       httpVersion: "HTTP/1.1", headerFields: [:])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
