import XCTest
@testable import BobPhone

/// The held picture's file discipline over a temporary folder.
@MainActor
final class HeldPictureTests: XCTestCase {
    private func store() -> (HeldPictureStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return (HeldPictureStore(directory: dir), dir)
    }

    func testARememberedBodyRestoresWithItsStamp() {
        let (store, _) = store()
        let body = Data("{\"generated_at\": 5}".utf8)
        store.remember(body: body, digest: "d1", token: "tok")
        let restored = store.restore(for: "tok")
        XCTAssertEqual(restored?.body, body)
        XCTAssertNotNil(restored?.savedAt)
        XCTAssertNil(store.restore(for: "other"))
    }

    func testAdoptDropsAnotherMacsPicture() async throws {
        let (store, dir) = store()
        store.remember(body: Data("x".utf8), digest: "d", token: "macA")
        await store.settle()
        let url = dir.appendingPathComponent(HeldPictureStore.fileName)
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        store.adopt("macB")
        XCTAssertNil(store.restore(for: "macA"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func testForgetRemovesTheFile() async throws {
        let (store, dir) = store()
        store.remember(body: Data("x".utf8), digest: "d", token: "mac")
        await store.settle()
        store.forget()
        XCTAssertNil(store.restore(for: "mac"))
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: dir.appendingPathComponent(HeldPictureStore.fileName).path))
    }

    func testASecondRememberWithTheSameDigestIsSkipped() {
        let (store, _) = store()
        store.remember(body: Data("one".utf8), digest: "same", token: "mac")
        store.remember(body: Data("two".utf8), digest: "same", token: "mac")
        XCTAssertEqual(store.restore(for: "mac")?.body, Data("one".utf8))
    }

    func testLoadReadsWhatWasWritten() async throws {
        let (store, dir) = store()
        store.remember(body: Data("kept".utf8), digest: "d", token: "mac")
        await store.settle()
        let again = HeldPictureStore(directory: dir)
        again.load()
        XCTAssertEqual(again.restore(for: "mac")?.body, Data("kept".utf8))
    }
}
