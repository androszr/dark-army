import XCTest
@testable import BobPhone

/// The held-first open: the pure resolver, and the model that composes the
/// held view without asking the Mac for anything.
@MainActor
final class HeldDestinationTests: XCTestCase {
    private func entry(receipt: String = "r-1", session: String = "s-1", card: String = "") -> NotificationLogEntry {
        NotificationLogEntry(id: receipt, receiptId: receipt, title: "Vex needs you", subtitle: "Rename the strip",
                             sessionId: session, cardId: card, kind: "",
                             seenAt: Date().timeIntervalSince1970 - 100, tapped: true)
    }

    private func card(_ id: String) -> BoardCard {
        var card = BoardCard(); card.id = id; card.title = "Rename the strip"
        return card
    }

    private func agent(_ session: String) throws -> Agent {
        var agent = try JSONDecoder().decode(Agent.self, from: Data("{}".utf8))
        agent.sessionId = session
        return agent
    }

    private func route(_ receipt: String = "r-1") -> PendingReceipt {
        PendingReceipt(receiptId: receipt, sequence: 1, generation: "g")
    }

    func testResolveGivesTheCardWhenThePictureHasIt() throws {
        var snapshot = Snapshot()
        snapshot.board.cards = [card("card-9")]
        snapshot.agents.waiting = [try agent("s-1")]
        // The card wins over the agent when both are in the picture.
        XCTAssertEqual(HeldDestination.resolve(entry: entry(card: "card-9"), snapshot: snapshot), .card(card("card-9")))
    }

    func testResolveGivesTheAgentWithItsCategoryWhenOnlyTheSessionIs() throws {
        var snapshot = Snapshot()
        snapshot.agents.sleeping = [try agent("s-1")]
        let held = HeldDestination.resolve(entry: entry(card: "card-gone"), snapshot: snapshot)
        XCTAssertEqual(held, .agent(try agent("s-1"), .sleeping))
        guard case .agent(let found, let category)? = held else { return XCTFail("not an agent") }
        XCTAssertEqual(found.sessionId, "s-1")
        XCTAssertEqual(category, .sleeping)
    }

    func testResolveGivesTheWordsWhenNeitherIsInThePicture() {
        let seen = entry(card: "card-gone")
        XCTAssertEqual(HeldDestination.resolve(entry: seen, snapshot: Snapshot()), .words(seen))
        // A restored picture that never decoded is an empty one: still words.
        let bare = entry(session: "", card: "")
        XCTAssertEqual(HeldDestination.resolve(entry: bare, snapshot: Snapshot()), .words(bare))
    }

    func testResolveGivesNilWithNoEntry() {
        var snapshot = Snapshot()
        snapshot.board.cards = [card("card-9")]
        XCTAssertNil(HeldDestination.resolve(entry: nil, snapshot: snapshot))
    }

    /// The success criterion: a tapped receipt composes from the held
    /// picture within two seconds and without the app refetching anything —
    /// the injected refresh and fetch counters read zero at `open()`, and
    /// fetch stays zero while the stub says the Mac is not live.
    func testATappedReceiptComposesFromTheHeldPictureWithoutAFetch() async {
        let log = NotificationLogStore(directory: FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true))
        log.absorb([entry(card: "card-9")], token: "mac")
        var snapshot = Snapshot()
        snapshot.board.cards = [card("card-9")]
        var refreshCount = 0
        var fetchCount = 0
        let model = NotificationDestinationModel(
            route: route(), log: log,
            snapshot: { snapshot },
            isLive: { false },
            accepts: { true },
            refresh: { refreshCount += 1 },
            fetch: { _ in fetchCount += 1; return nil })
        let t0 = Date()
        model.open()
        XCTAssertEqual(model.held, .card(card("card-9")))
        XCTAssertEqual(model.entry?.receiptId, "r-1")
        XCTAssertEqual(refreshCount, 0)
        XCTAssertEqual(fetchCount, 0)
        XCTAssertLessThan(Date().timeIntervalSince(t0), 2)
        XCTAssertNil(model.page)
        XCTAssertFalse(model.offline)
        let page = await model.resolve()
        XCTAssertNil(page)
        XCTAssertEqual(refreshCount, 1)
        XCTAssertEqual(fetchCount, 0)
        XCTAssertTrue(model.offline)
        XCTAssertEqual(model.held, .card(card("card-9")))
        XCTAssertNil(model.page)
    }

    func testALiveMacLandsThePageAndTheHeldViewIsStillSet() async {
        let log = NotificationLogStore(directory: FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true))
        log.absorb([entry(card: "card-9")], token: "mac")
        var snapshot = Snapshot()
        snapshot.board.cards = [card("card-9")]
        var refreshCount = 0
        var fetched: [String] = []
        var item = DecisionItem(); item.id = "d-1"; item.title = "Choose"
        var page = CatchUpPage(); page.available = true; page.items = [item]
        let model = NotificationDestinationModel(
            route: route(), log: log,
            snapshot: { snapshot },
            isLive: { true },
            accepts: { true },
            refresh: { refreshCount += 1 },
            fetch: { query in fetched.append(query); return page })
        model.open()
        XCTAssertEqual(model.held, .card(card("card-9")))
        let landed = await model.resolve()
        XCTAssertEqual(landed?.items.first?.id, "d-1")
        XCTAssertEqual(model.page?.items.first?.id, "d-1")
        XCTAssertEqual(refreshCount, 1)
        XCTAssertEqual(fetched, ["receipt_id=r-1"])
        XCTAssertFalse(model.offline)
        XCTAssertEqual(model.held, .card(card("card-9")))
    }

    func testAReceiptTheRouterNoLongerAcceptsNeitherRefreshesNorFetches() async {
        let log = NotificationLogStore(directory: FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true))
        var refreshCount = 0
        var fetchCount = 0
        let model = NotificationDestinationModel(
            route: route(), log: log,
            snapshot: { Snapshot() },
            isLive: { true },
            accepts: { false },
            refresh: { refreshCount += 1 },
            fetch: { _ in fetchCount += 1; return nil })
        model.open()
        XCTAssertNil(model.held)
        let landed = await model.resolve()
        XCTAssertNil(landed)
        XCTAssertEqual(refreshCount, 0)
        XCTAssertEqual(fetchCount, 0)
        XCTAssertFalse(model.offline)
    }
}
