import XCTest
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
}
