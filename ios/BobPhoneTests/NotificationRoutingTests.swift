import XCTest
import UserNotifications
@testable import BobPhone

final class NotificationRoutingTests: XCTestCase {
    @MainActor
    func testProductionRouterRestoresAfterUnlockAndRejectsLockedResponse() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        let router = PhoneRouter(storageURL: url)
        router.pair(token: "a")
        router.tap(receipt: "00000000-0000-4000-8000-000000000001")
        let pending = router.pendingReceipt!
        router.lock()
        XCTAssertFalse(router.accepts(pending))
        XCTAssertEqual(router.pendingReceipt, pending)
        let restored = PhoneRouter(storageURL: url)
        let before = restored.signal
        restored.pair(token: "a")
        XCTAssertGreaterThan(restored.signal, before)
        XCTAssertEqual(restored.pendingReceipt, pending)
        XCTAssertTrue(restored.accepts(pending))
        restored.go(.board)
        XCTAssertFalse(restored.accepts(pending))
        XCTAssertNil(restored.pendingReceipt)
    }
    @MainActor
    func testProductionRouterRetriesProtectedReadAndScopesNewTapToNewPairing() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        let original = PhoneRouter(storageURL: url)
        original.pair(token: "a")
        original.tap(receipt: "00000000-0000-4000-8000-000000000001")
        var protected = true
        let fresh = PhoneRouter(storageURL: url, reader: { url in
            if protected { throw CocoaError(.fileReadNoPermission) }
            return try Data(contentsOf: url)
        })
        fresh.tap(receipt: "00000000-0000-4000-8000-000000000002")
        protected = false
        fresh.pair(token: "b")
        XCTAssertEqual(fresh.pendingReceipt?.receiptId, "00000000-0000-4000-8000-000000000002")
        XCTAssertTrue(fresh.accepts(fresh.pendingReceipt!))
        fresh.forget()
        XCTAssertNil(fresh.pendingReceipt)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }
    func testColdRestoreAndCancelUnlockRetainDestination() throws {
        var state = DestinationState()
        state.pair("pair-a")
        state.tap("receipt-a")
        let restored = try JSONDecoder().decode(DestinationState.self, from: JSONEncoder().encode(state))
        XCTAssertEqual(restored.pending, state.pending)
        XCTAssertTrue(restored.accepts(state.pending!))
    }
    func testReplacementAndDuplicateTapRejectLateResponse() {
        var state = DestinationState()
        state.pair("pair-a"); state.tap("one")
        let old = state.pending!
        state.tap("two")
        XCTAssertFalse(state.accepts(old))
        let second = state.pending!
        state.tap("two")
        XCTAssertFalse(state.accepts(second))
        state.consume(old)
        XCTAssertEqual(state.pending?.receiptId, "two")
    }
    func testPairingSwitchClearsReceipt() {
        var state = DestinationState()
        state.pair("a"); state.tap("one")
        let old = state.pending!
        state.pair("b")
        XCTAssertNil(state.pending)
        XCTAssertFalse(state.accepts(old))
    }
    func testConsumeRequiresExactPresentedRoute() {
        var state = DestinationState()
        state.tap("one"); state.pair("a")
        let current = state.pending!
        XCTAssertTrue(state.accepts(current))
        state.consume(current)
        XCTAssertNil(state.pending)
    }

    // MARK: the Lock Screen card and the banner's Open review button

    func testReviewLinkAndTheCardsChoiceOfLink() {
        XCTAssertEqual(FleetLinks.review("r-1")?.absoluteString, "bobphone://review?run=r-1")
        XCTAssertNil(FleetLinks.review(""))
        XCTAssertEqual(FleetLinks.waiter(kind: "picks", runId: "r-1", sessionId: "s-1"),
                       FleetLinks.review("r-1"))
        XCTAssertEqual(FleetLinks.waiter(kind: "question", runId: "r-1", sessionId: "s-1"),
                       FleetLinks.agent("s-1"))
        XCTAssertEqual(FleetLinks.waiter(kind: "picks", runId: "", sessionId: "s-1"),
                       FleetLinks.agent("s-1"))
    }

    @MainActor
    func testReviewLinkAndButtonFillTheRouterSlotOnce() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        let router = PhoneRouter(storageURL: url)
        router.open(try XCTUnwrap(URL(string: "bobphone://review?run=r-1")))
        XCTAssertEqual(router.take(), .menu)
        XCTAssertEqual(router.takeSection(), .review)
        XCTAssertEqual(router.takeReviewRun(), "r-1")
        XCTAssertEqual(router.takeReviewRun(), "")
        router.open(try XCTUnwrap(URL(string: "bobphone://review?run=bad%20id!")))
        XCTAssertEqual(router.takeReviewRun(), "")
        _ = router.take(); _ = router.takeSection()
        router.openReview(runId: "r-1")
        XCTAssertEqual(router.take(), .menu)
        XCTAssertEqual(router.takeSection(), .review)
        XCTAssertEqual(router.takeReviewRun(), "r-1")
        router.openReview(runId: "bad id!")
        XCTAssertEqual(router.takeReviewRun(), "")
    }

    func testOpenReviewButtonIsPureAndStrict() {
        XCTAssertEqual(LockScreenActions.opens(actionIdentifier: "bob.review.open",
                                               userInfo: ["act": "review", "run_id": "r-1"]), "r-1")
        XCTAssertNil(LockScreenActions.opens(actionIdentifier: UNNotificationDefaultActionIdentifier,
                                             userInfo: ["act": "review", "run_id": "r-1"]))
        XCTAssertNil(LockScreenActions.opens(actionIdentifier: "bob.review.open",
                                             userInfo: ["act": "acknowledge", "run_id": "r-1"]))
        XCTAssertNil(LockScreenActions.opens(actionIdentifier: "bob.review.open",
                                             userInfo: ["act": "review", "run_id": "bad id!"]))
        XCTAssertNil(LockScreenActions.press(actionIdentifier: "bob.review.open",
                                             userInfo: ["act": "review", "run_id": "r-1"]))
    }

    func testHeldReviewRunWaitsForAPictureNewerThanTheHold() {
        let held = Date(timeIntervalSince1970: 100)
        XCTAssertEqual(HeldReviewRun.decide(listed: true, pictureAsOf: nil, heldAt: held), .open)
        XCTAssertEqual(HeldReviewRun.decide(listed: false, pictureAsOf: nil, heldAt: held), .wait)
        XCTAssertEqual(HeldReviewRun.decide(listed: false, pictureAsOf: Date(timeIntervalSince1970: 50), heldAt: held), .wait)
        XCTAssertEqual(HeldReviewRun.decide(listed: false, pictureAsOf: Date(timeIntervalSince1970: 200), heldAt: held), .drop)
    }

    @MainActor
    func testAnotherLinkClearsAHeldReviewRun() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        let router = PhoneRouter(storageURL: url)
        router.openReview(runId: "r-1")
        router.open(try XCTUnwrap(URL(string: "bobphone://fleet?session=s-1")))
        XCTAssertEqual(router.takeReviewRun(), "")
    }
}
