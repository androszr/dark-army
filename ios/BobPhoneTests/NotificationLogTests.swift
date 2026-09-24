import XCTest
import UserNotifications
@testable import BobPhone

/// The notification log's file discipline and merge rule over a temporary
/// folder — `HeldPictureTests`' shape.
@MainActor
final class NotificationLogTests: XCTestCase {
    private func store() -> (NotificationLogStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return (NotificationLogStore(directory: dir), dir)
    }

    /// A moment inside the log's age window: `seenAt` here is an offset in
    /// seconds from this anchor, so a fixture is never pruned as thirty
    /// days old by accident.
    private let anchor = Date().timeIntervalSince1970 - 100_000

    private func entry(_ receipt: String, seenAt: Double, tapped: Bool = false,
                       title: String = "Vex needs you", session: String = "s-1",
                       card: String = "") -> NotificationLogEntry {
        NotificationLogEntry(id: receipt, receiptId: receipt, title: title, subtitle: "Rename the strip",
                             sessionId: session, cardId: card, kind: "", seenAt: anchor + seenAt, tapped: tapped)
    }

    func testAbsorbThenLoadRoundTrips() async throws {
        let (store, dir) = store()
        store.absorb([entry("r-1", seenAt: 100), entry("r-2", seenAt: 200, card: "card-9")], token: "mac")
        await store.settle()
        let again = NotificationLogStore(directory: dir)
        again.load()
        XCTAssertEqual(again.entries.map(\.receiptId), ["r-2", "r-1"])
        XCTAssertEqual(again.entry(for: "r-2")?.cardId, "card-9")
        XCTAssertEqual(again.entry(for: "R-2")?.cardId, "card-9")
        XCTAssertNil(again.entry(for: ""))
    }

    func testMergeByReceiptKeepsTheEarliestSeenAtAndOrsTapped() {
        let (store, _) = store()
        store.absorb([entry("r-1", seenAt: 300, tapped: false, title: "")], token: "mac")
        store.absorb([entry("r-1", seenAt: 100, tapped: true)], token: "mac")
        store.absorb([entry("r-1", seenAt: 500, tapped: false)], token: "mac")
        let held = store.entries
        XCTAssertEqual(held.count, 1)
        XCTAssertEqual(held[0].seenAt, anchor + 100)
        XCTAssertTrue(held[0].tapped)
        XCTAssertEqual(held[0].title, "Vex needs you")
    }

    func testTheTwoHundredAndFirstEntryEvictsTheOldest() {
        let (store, _) = store()
        let now = Date()
        store.absorb((0..<NotificationLogStore.maxEntries).map { entry("r-\($0)", seenAt: Double($0)) },
                     token: "mac", now: now)
        XCTAssertEqual(store.entries.count, NotificationLogStore.maxEntries)
        store.absorb([entry("newest", seenAt: 10_000)], token: "mac", now: now)
        XCTAssertEqual(store.entries.count, NotificationLogStore.maxEntries)
        XCTAssertEqual(store.entries.first?.receiptId, "newest")
        XCTAssertNil(store.entry(for: "r-0"))
        XCTAssertNotNil(store.entry(for: "r-1"))
    }

    func testAnEntryOlderThanMaxAgeIsPrunedOnAbsorb() {
        let (store, _) = store()
        let now = Date()
        let stale = now.timeIntervalSince1970 - NotificationLogStore.maxAge - 1 - anchor
        let fresh = now.timeIntervalSince1970 - 60 - anchor
        store.absorb([entry("old", seenAt: stale), entry("new", seenAt: fresh)], token: "mac", now: now)
        XCTAssertEqual(store.entries.map(\.receiptId), ["new"])
        // Aging happens on the next absorb, whatever it carries.
        store.absorb([], token: "mac", now: now.addingTimeInterval(NotificationLogStore.maxAge))
        XCTAssertTrue(store.entries.isEmpty)
    }

    func testAdoptWithAnotherTokenRemovesTheFile() async throws {
        let (store, dir) = store()
        store.absorb([entry("r-1", seenAt: 100)], token: "macA")
        await store.settle()
        let url = dir.appendingPathComponent(NotificationLogStore.fileName)
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        store.adopt("macB")
        XCTAssertTrue(store.entries.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        // The same token keeps it; an absorb under another token drops it too.
        store.absorb([entry("r-2", seenAt: 100)], token: "macB")
        store.adopt("macB")
        XCTAssertEqual(store.entries.count, 1)
        store.absorb([entry("r-3", seenAt: 100)], token: "macC")
        XCTAssertEqual(store.entries.map(\.receiptId), ["r-3"])
        // No token, nothing kept.
        store.absorb([entry("r-4", seenAt: 100)], token: "")
        XCTAssertNil(store.entry(for: "r-4"))
    }

    /// The gate re-runs `load()` at every unlock while writes are chained
    /// detached tasks: a second `load()` with a write still pending must
    /// not replace the record with the older file and lose what the bank
    /// already handed over.
    func testASecondLoadDoesNotDropAnAbsorbedEntry() async throws {
        let (store, _) = store()
        store.load()
        store.absorb([entry("r-1", seenAt: 100)], token: "mac")
        await store.settle()
        store.absorb([entry("r-2", seenAt: 200)], token: "mac")
        // No settle: the second write is still in flight, as it is when
        // Face ID succeeds mid-write.
        store.load()
        XCTAssertEqual(store.entries.map(\.receiptId), ["r-2", "r-1"])
        await store.settle()
        store.load()
        XCTAssertEqual(store.entries.map(\.receiptId), ["r-2", "r-1"])
    }

    /// `forget()` removes the file rather than replacing it, so a re-pair
    /// has no older file to read back: a `load()` after it stays empty and
    /// the next absorb under the new token starts from nothing.
    func testALoadAfterForgetOrAdoptBringsNothingBack() async throws {
        let (store, _) = store()
        store.load()
        store.absorb([entry("r-1", seenAt: 100)], token: "macA")
        await store.settle()
        store.forget()
        store.load()
        XCTAssertTrue(store.entries.isEmpty)
        store.absorb([entry("r-2", seenAt: 200)], token: "macB")
        store.adopt("macC")
        store.load()
        XCTAssertTrue(store.entries.isEmpty)
        store.absorb([entry("r-3", seenAt: 300)], token: "macC")
        store.load()
        XCTAssertEqual(store.entries.map(\.receiptId), ["r-3"])
    }

    func testForgetRemovesTheFile() async throws {
        let (store, dir) = store()
        store.absorb([entry("r-1", seenAt: 100)], token: "mac")
        await store.settle()
        store.forget()
        XCTAssertTrue(store.entries.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: dir.appendingPathComponent(NotificationLogStore.fileName).path))
    }

    func testEnrichFillsOnlyEmptyFields() async throws {
        let (store, dir) = store()
        store.absorb([entry("r-1", seenAt: 100, title: "", session: "")], token: "mac")
        var item = DecisionItem(); item.kind = "question"; item.title = "Choose"; item.sessionId = "s-9"; item.cardId = "card-9"
        var page = CatchUpPage(); page.available = true; page.items = [item]
        store.enrich(receiptId: "r-1", page: page)
        let filled = store.entry(for: "r-1")!
        XCTAssertEqual(filled.kind, "question")
        XCTAssertEqual(filled.title, "Choose")
        XCTAssertEqual(filled.sessionId, "s-9")
        XCTAssertEqual(filled.cardId, "card-9")
        XCTAssertEqual(filled.subtitle, "Rename the strip")
        // A second page never overwrites what is already there.
        var other = DecisionItem(); other.kind = "permission"; other.title = "Other"; other.sessionId = "s-2"; other.cardId = "card-2"
        page.items = [other]
        store.enrich(receiptId: "r-1", page: page)
        XCTAssertEqual(store.entry(for: "r-1")?.kind, "question")
        XCTAssertEqual(store.entry(for: "r-1")?.cardId, "card-9")
        // An unknown receipt or an unavailable page changes nothing.
        store.enrich(receiptId: "nobody", page: page)
        var unavailable = CatchUpPage(); unavailable.items = [other]
        store.enrich(receiptId: "r-1", page: unavailable)
        await store.settle()
        let again = NotificationLogStore(directory: dir)
        again.load()
        XCTAssertEqual(again.entry(for: "r-1")?.kind, "question")
    }

    func testACollapsedPageFillsTheKindAndTitleButBindsNoSubject() {
        // "N agents need you" spans N decisions: the receipt is about none
        // of them in particular, so the entry takes no session or card from
        // the first item — a held open would otherwise aim at the wrong card.
        let (store, _) = store()
        store.absorb([entry("r-1", seenAt: 100, title: "", session: "")], token: "mac")
        var first = DecisionItem(); first.kind = "question"; first.title = "Choose"; first.sessionId = "s-9"; first.cardId = "card-9"
        var second = DecisionItem(); second.kind = "permission"; second.title = "Allow"; second.sessionId = "s-2"; second.cardId = "card-2"
        var page = CatchUpPage(); page.available = true; page.items = [first, second]
        store.enrich(receiptId: "r-1", page: page)
        let filled = store.entry(for: "r-1")!
        XCTAssertEqual(filled.kind, "question")
        XCTAssertEqual(filled.title, "Choose")
        XCTAssertEqual(filled.sessionId, "")
        XCTAssertEqual(filled.cardId, "")
        // A subject the buzz itself carried is untouched either way.
        store.absorb([entry("r-2", seenAt: 200, card: "card-5")], token: "mac")
        store.enrich(receiptId: "r-2", page: page)
        XCTAssertEqual(store.entry(for: "r-2")?.sessionId, "s-1")
        XCTAssertEqual(store.entry(for: "r-2")?.cardId, "card-5")
    }

    func testARecordMissingKeysStillLoads() async throws {
        // Synthesized Codable throws on one absent key; a log written by an
        // older or newer build must read as defaults, never blank the log.
        let (_, dir) = store()
        let json = """
        {"entries": [
          {"receiptId": "r-1", "title": "Vex needs you", "seenAt": \(anchor + 100)},
          {"id": "kept", "receiptId": "r-2", "title": "Quiet needs you", "seenAt": \(anchor + 200), "later": 1},
          {"title": "legacy"}
        ]}
        """
        try Data(json.utf8).write(to: dir.appendingPathComponent(NotificationLogStore.fileName))
        let store = NotificationLogStore(directory: dir)
        store.load()
        XCTAssertEqual(store.entries.count, 3)
        // `id` falls back to the receipt, then to a fresh UUID; a stored id stays.
        XCTAssertEqual(store.entry(for: "r-1")?.id, "r-1")
        XCTAssertEqual(store.entry(for: "r-2")?.id, "kept")
        let legacy = store.entries.first { $0.title == "legacy" }!
        XCTAssertEqual(legacy.receiptId, "")
        XCTAssertFalse(legacy.id.isEmpty)
        XCTAssertEqual(legacy.seenAt, 0)
        XCTAssertFalse(legacy.tapped)
        XCTAssertEqual(store.entry(for: "r-1")?.subtitle, "")
        XCTAssertEqual(store.entry(for: "r-1")?.kind, "")
        // The token stamp may be missing too: the record reads as unstamped,
        // so an absorb under a token keeps it rather than dropping it as
        // another Mac's. The dateless legacy row goes then, by the age rule.
        store.absorb([], token: "mac")
        XCTAssertEqual(store.entries.map(\.receiptId), ["r-2", "r-1"])
    }

    func testFromReadsTheTwoLinesAndTheThreeIdsAndNothingElse() {
        let content = UNMutableNotificationContent()
        content.title = "Vex wants to run a tool"
        content.subtitle = "Rename the strip ladder"
        content.body = "Approve running Bash"
        content.userInfo = ["receipt_id": "00000000-0000-4000-8000-00000000000A", "session_id": "s-1",
                            "card_id": "card-9", "act": "permission", "request_id": "hook-1",
                            "destination_version": 1]
        let date = Date(timeIntervalSince1970: 1_758_355_200)
        let entry = NotificationLogEntry.from(content: content, date: date, tapped: true)
        XCTAssertEqual(entry.receiptId, "00000000-0000-4000-8000-00000000000a")
        XCTAssertEqual(entry.id, entry.receiptId)
        XCTAssertEqual(entry.title, "Vex wants to run a tool")
        XCTAssertEqual(entry.subtitle, "Rename the strip ladder")
        XCTAssertEqual(entry.sessionId, "s-1")
        XCTAssertEqual(entry.cardId, "card-9")
        XCTAssertEqual(entry.kind, "")
        XCTAssertEqual(entry.seenAt, 1_758_355_200)
        XCTAssertTrue(entry.tapped)
        let encoded = String(decoding: try! JSONEncoder().encode(entry), as: UTF8.self)
        XCTAssertFalse(encoded.contains("hook-1"))
        XCTAssertFalse(encoded.contains("permission"))
        XCTAssertFalse(encoded.contains("Approve running Bash"))
        // A legacy buzz with no receipt mints its own id, so two never merge.
        let legacy = UNMutableNotificationContent(); legacy.title = "Dark Army needs you"
        let one = NotificationLogEntry.from(content: legacy, date: date, tapped: false)
        let two = NotificationLogEntry.from(content: legacy, date: date, tapped: false)
        XCTAssertEqual(one.receiptId, "")
        XCTAssertNotEqual(one.id, two.id)
    }
}
