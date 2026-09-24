import XCTest
@testable import BobPanel

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

import AppKit
import SwiftUI

extension CollaborationTests {
    @MainActor
    func testSyntheticJourneyHost() throws {
        _ = NSApplication.shared
        let client = DaemonClient()
        client.snapshot = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"collaboration":\(Self.fixture),
         "agents":{"running":[{"session_id":"root","provider":"claude","nickname":"Parent"}],
                   "finished":[{"session_id":"ended","provider":"claude","nickname":"Ended"}]},
         "board":{"available":true,"cards":[{"id":"task","root":"/project","title":"Task"},
                                            {"id":"second","root":"/project","title":"Second"}]}}
        """.utf8))
        let host = NSHostingView(rootView: CollaborationFixtureHost(client: client))
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 800, height: 600),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "Collaboration synthetic fixture"
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        defer { window.close() }
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        host.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(host.fittingSize.height, 100)
        XCTAssertEqual(client.snapshot.collaboration?.nodes.count, 8)
        // Opt-in manual test host, isolated from the running daemon and fleet.
        if ProcessInfo.processInfo.environment["BOB_COLLABORATION_MANUAL"] == "1" {
            let until = Date().addingTimeInterval(600)
            while window.isVisible && Date() < until {
                RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            }
        }
    }
}

private struct CollaborationFixtureHost: View {
    @ObservedObject var client: DaemonClient
    @State private var focus = CollaborationFocus.card("task")
    @State private var trail: [CollaborationFocus] = []
    @State private var helper = ""
    var body: some View {
        VStack(alignment: .leading) {
            Text("Synthetic task evidence — no connection to the daemon")
            Button("Back") {
                if let previous = trail.popLast() { focus = previous; helper = "" }
            }.disabled(trail.isEmpty)
            CollaborationView(client: client, focus: focus, helperFocus: helper, initiallyExpanded: true,
                onSession: { provider, id, child in
                    trail.append(focus); focus = .session(provider: provider, id: id); helper = child
                },
                onCard: { card in trail.append(focus); focus = .card(card.id); helper = "" })
        }.padding()
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
    func testProductionMapControlsUseCurrentTargetsAndSendNoRequests() async throws {
        XCTAssertTrue(URLProtocol.registerClass(CollaborationRequestSpy.self))
        defer { URLProtocol.unregisterClass(CollaborationRequestSpy.self) }
        // Calibrate the request spy against the URLSession used by the real client.
        _ = try await URLSession.shared.data(from: URL(string: "http://127.0.0.1:19874/fixture-spy")!)
        XCTAssertFalse(CollaborationRequestSpy.seen.isEmpty)
        CollaborationRequestSpy.reset()
        let client = DaemonClient()
        let snapshot = try controlSnapshot()
        client.snapshot = snapshot
        let helper = snapshot.collaboration!.nodes.first { $0.id == "helper" }!
        let cardLink = helper.cards[0]
        var sessions: [String] = [], cards: [String] = []
        let openSession: (String, String, String) -> Void = {
            sessions.append("\($0)/\($1)/\($2)")
        }
        let openCard: (BoardCard) -> Void = { cards.append("\($0.id)/\($0.root)/\($0.title)") }
        XCTAssertNil(CollaborationControls.openAgent(helper.id, client: client, onSession: openSession))
        XCTAssertNil(CollaborationControls.openAgent("ended", client: client, onSession: openSession))
        XCTAssertNil(CollaborationControls.openCard(cardLink, client: client, onCard: openCard))
        XCTAssertEqual(sessions, ["claude/root/helper", "claude/ended/"])
        XCTAssertEqual(cards, ["task//project/Current task"])
        for (id, reason) in [("ambiguous", "Ambiguous address — several agents match"),
                             ("missing", "Unknown recipient — no matching identity"),
                             ("removed", "Agent unavailable in this view")] {
            XCTAssertEqual(CollaborationControls.openAgent(id, client: client, onSession: openSession),
                           reason)
        }
        // The rendered node/link still exist in these local variables; the client changes before press.
        client.snapshot.agents.waiting = []
        XCTAssertNotNil(CollaborationControls.openAgent(helper.id, client: client, onSession: openSession))
        client.snapshot = snapshot
        client.snapshot.agents.running.append(snapshot.agents.waiting[0])
        XCTAssertNotNil(CollaborationControls.openAgent(helper.id, client: client, onSession: openSession))
        client.snapshot = snapshot
        client.snapshot.collaboration = nil
        XCTAssertNotNil(CollaborationControls.openAgent(helper.id, client: client, onSession: openSession))
        client.snapshot = snapshot
        client.snapshot.board.cards[0].root = "/other"
        XCTAssertNotNil(CollaborationControls.openCard(cardLink, client: client, onCard: openCard))
        client.snapshot.board.cards = []
        XCTAssertNotNil(CollaborationControls.openCard(cardLink, client: client, onCard: openCard))
        XCTAssertEqual(sessions.count, 2)
        XCTAssertEqual(cards.count, 1)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(CollaborationRequestSpy.seen, [], "Map controls must issue zero client requests")
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

extension CollaborationTests {
    @MainActor
    func testSavedCardMapRevealsBoardAboveSelectedAgentAndHoldsThroughOpen() throws {
        _ = NSApplication.shared
        XCTAssertTrue(URLProtocol.registerClass(CollaborationRequestSpy.self))
        defer { URLProtocol.unregisterClass(CollaborationRequestSpy.self) }
        CollaborationRequestSpy.reset()
        let client = DaemonClient()
        client.snapshot = try controlSnapshot()
        let board = BoardState()
        let keys = KeyRouter()
        let focus = FocusRouter()
        let host = NSHostingView(rootView: PanelView(client: client, keys: keys, focus: focus, board: board))
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1100, height: 700),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        defer { window.close() }
        func settle() { RunLoop.main.run(until: Date().addingTimeInterval(0.15)) }
        settle()
        NotificationCenter.default.post(name: .panelShowSession, object: "root",
                                        userInfo: ["provider": "claude"])
        settle()
        XCTAssertTrue(keys.detailOpen, "Precondition: a selected fleet agent covers the board")
        let target = client.snapshot.board.cards[0]
        board.editing = target.id
        let sheet = BoardCardSheet(client: client, state: board, card: target, isComposer: false)
        // Occlusion preserves the selected row but the next deliberate open aims again.
        window.orderOut(nil)
        client.visible = false
        settle()
        var fronts = 0
        let observer = NotificationCenter.default.addObserver(forName: .panelOpenBoard, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated {
                fronts += 1
                window.makeKeyAndOrderFront(nil)
                client.visible = true
                focus.noteShown()
            }
        }
        defer { NotificationCenter.default.removeObserver(observer) }
        let link = client.snapshot.collaboration!.nodes.first { $0.id == "helper" }!.cards[0]
        // Execute both production handlers: the map control and its saved-card callback,
        // delivered to the mounted PanelView's canonical showCard path.
        XCTAssertNil(CollaborationControls.openCard(link, client: client, onCard: sheet.showCollaborationCard))
        settle()
        XCTAssertNil(board.editing)
        XCTAssertEqual(board.revealRequest?.id, target.id)
        XCTAssertEqual(fronts, 1)
        XCTAssertTrue(window.isVisible)
        XCTAssertFalse(keys.detailOpen, "The destination board must replace the selected agent")
        client.snapshot.generatedAt += 1
        settle()
        XCTAssertFalse(keys.detailOpen, "The open's next snapshot must not re-aim over the card")
        // Neither a stale project root nor a removed destination can close the editor or front the panel.
        let sequence = board.revealRequest?.seq
        board.editing = target.id
        client.snapshot.board.cards[0].root = "/other"
        sheet.showCollaborationCard(target)
        NotificationCenter.default.post(name: .panelShowCard, object: target)
        settle()
        XCTAssertEqual(board.editing, target.id)
        XCTAssertEqual(board.revealRequest?.seq, sequence)
        XCTAssertEqual(fronts, 1)
        client.snapshot.board.cards = []
        sheet.showCollaborationCard(target)
        settle()
        XCTAssertEqual(board.editing, target.id)
        XCTAssertEqual(fronts, 1)
        XCTAssertFalse(CollaborationRequestSpy.seen.contains { $0.hasPrefix("POST ") },
                       "Navigation through real detail surfaces must never send an action")
    }
}
