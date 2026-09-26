import XCTest
@testable import BobPhone

@MainActor
final class ConversationCacheTests: XCTestCase {
    private func store() -> (ConversationCacheStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return (ConversationCacheStore(directory: dir), dir)
    }

    private func page(turns: [ConversationTurn], nextSeq: Int, total: Int,
                      key: String = "1:1", reset: Bool = false,
                      more: Bool = false) -> ConversationPage {
        ConversationPage(available: true, provider: "claude", key: key,
                         reset: reset, turns: turns, nextSeq: nextSeq,
                         total: total, more: more)
    }

    private func turn(_ seq: Int, text: String = "t") -> ConversationTurn {
        ConversationTurn(seq: seq, kind: "user", ts: 1, text: text)
    }

    func testApplyThenLoadRoundTrip() async throws {
        let (store, dir) = store()
        store.adopt("tok")
        store.apply(page(turns: [turn(0, text: "hello")], nextSeq: 1, total: 1),
                    session: "s1")
        await store.settle()
        let again = ConversationCacheStore(directory: dir)
        again.load()
        XCTAssertEqual(again.turns("s1").map(\.text), ["hello"])
        XCTAssertEqual(again.nextSeq("s1"), 1)
    }

    func testAReopenedConversationReadsFromDiskWithoutAFetch() async throws {
        let (store, dir) = store()
        store.adopt("tok")
        store.apply(page(turns: [turn(0), turn(1)], nextSeq: 2, total: 2),
                    session: "s1")
        await store.settle()
        let started = Date()
        let again = ConversationCacheStore(directory: dir)
        again.load()
        XCTAssertEqual(again.turns("s1").count, 2)
        XCTAssertEqual(again.nextSeq("s1"), 2)
        XCTAssertLessThan(Date().timeIntervalSince(started), 2)
    }

    func testASecondPageAppendsAndAReplayAddsNothing() async {
        let (store, _) = store()
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 2), session: "s1")
        store.apply(page(turns: [turn(1, text: "next")], nextSeq: 2, total: 2),
                    session: "s1")
        XCTAssertEqual(store.turns("s1").map(\.text), ["t", "next"])
        store.apply(page(turns: [turn(1, text: "next")], nextSeq: 2, total: 2),
                    session: "s1")
        XCTAssertEqual(store.turns("s1").count, 2)
    }

    func testResetReplacesTurns() {
        let (store, _) = store()
        store.apply(page(turns: [turn(0), turn(1)], nextSeq: 2, total: 2),
                    session: "s1")
        store.apply(page(turns: [turn(0, text: "fresh")], nextSeq: 1, total: 1,
                         reset: true), session: "s1")
        XCTAssertEqual(store.turns("s1").map(\.text), ["fresh"])
    }

    func testANewKeyAfterResetIsKept() {
        let (store, _) = store()
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1, key: "1:1"),
                    session: "s1")
        store.apply(page(turns: [turn(0, text: "b")], nextSeq: 1, total: 1,
                         key: "2:2", reset: true), session: "s1")
        XCTAssertEqual(store.key("s1"), "2:2")
    }

    func testThe5001stTurnDropsTheFirst() {
        let (store, _) = store()
        let turns = (0..<5001).map { turn($0, text: "t\($0)") }
        store.apply(page(turns: turns, nextSeq: 5001, total: 5001, reset: true),
                    session: "s1")
        let held = store.turns("s1")
        XCTAssertEqual(held.count, 5000)
        XCTAssertEqual(held.first?.seq, 1)
        XCTAssertEqual(store.nextSeq("s1"), 5001)
    }

    func testUpdatedAtMovesOnlyWhenATurnLanded() async {
        let (store, _) = store()
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        let first = store.conversations["s1"]?.updatedAt ?? 0
        XCTAssertGreaterThan(first, 0)
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        XCTAssertEqual(store.conversations["s1"]?.updatedAt, first)
    }

    func testAnEntryOlderThanMaxAgeIsGoneAfterLoadAndItsFileIsDeleted() async throws {
        let (store, dir) = store()
        store.adopt("tok")
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        await store.settle()
        var row = store.conversations["s1"]!
        row.updatedAt = Date().timeIntervalSince1970 - (5 * 86400) - 10
        let sessionURL = dir.appendingPathComponent("conversations/s1.json")
        try JSONEncoder().encode(row).write(to: sessionURL)
        let indexURL = dir.appendingPathComponent(ConversationCacheStore.fileName)
        try JSONEncoder().encode(["s1": row.updatedAt]).write(to: indexURL)
        let again = ConversationCacheStore(directory: dir)
        again.load()
        XCTAssertTrue(again.turns("s1").isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: sessionURL.path))
    }

    func testTwoPagesInQuickSuccessionLeaveTheNewerNextSeqOnDisk() async throws {
        let (store, dir) = store()
        store.adopt("tok")
        // A large first page, a small second: unchained, the second's
        // write could finish before the first's and be overwritten.
        let bulk = (0..<2000).map { turn($0, text: String(repeating: "x", count: 200)) }
        store.apply(page(turns: bulk, nextSeq: 2000, total: 2001), session: "s1")
        store.apply(page(turns: [turn(2000, text: "last")], nextSeq: 2001, total: 2001),
                    session: "s1")
        await store.settle()
        let again = ConversationCacheStore(directory: dir)
        again.load()
        XCTAssertEqual(again.nextSeq("s1"), 2001)
        XCTAssertEqual(again.turns("s1").last?.text, "last")
    }

    func testAPageWithMoreIsWrittenOnceAtTheEndOrOnFlush() async throws {
        let (store, dir) = store()
        store.adopt("tok")
        let sessionURL = dir.appendingPathComponent("conversations/s1.json")
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 3, more: true), session: "s1")
        await store.pendingValue()
        XCTAssertFalse(FileManager.default.fileExists(atPath: sessionURL.path))
        store.apply(page(turns: [turn(1)], nextSeq: 2, total: 3, more: true), session: "s1")
        store.flush()
        await store.pendingValue()
        XCTAssertTrue(FileManager.default.fileExists(atPath: sessionURL.path))
        store.apply(page(turns: [turn(2)], nextSeq: 3, total: 3), session: "s1")
        await store.settle()
        let again = ConversationCacheStore(directory: dir)
        again.load()
        XCTAssertEqual(again.nextSeq("s1"), 3)
    }

    func testAPageThatBringsNothingDoesNotPublish() {
        let (store, _) = store()
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        var published = 0
        let sink = store.objectWillChange.sink { _ in published += 1 }
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        XCTAssertEqual(published, 0)
        store.apply(page(turns: [turn(1)], nextSeq: 2, total: 2), session: "s1")
        XCTAssertEqual(published, 1)
        sink.cancel()
    }

    func testTheCursorJumpsToTheTailOnlyWhenTheHopsLeftCannotReachIt() {
        // Away: three hops, the first page brought 150 of 5 000 turns.
        XCTAssertEqual(ConversationCatchUp.jump(nextSeq: 150, total: 5000, hopsLeft: 2), 4850)
        // Within reach of the two hops left: page on in order.
        XCTAssertNil(ConversationCatchUp.jump(nextSeq: 150, total: 450, hopsLeft: 2))
        XCTAssertNil(ConversationCatchUp.jump(nextSeq: 150, total: 5000, hopsLeft: 39))
        // No hop left to spend the jump on.
        XCTAssertNil(ConversationCatchUp.jump(nextSeq: 150, total: 5000, hopsLeft: 0))
        XCTAssertEqual(ConversationCatchUp.jump(nextSeq: 0, total: 100, hopsLeft: 0), nil)
    }

    func testSkipAheadMakesTheNextPageAResetAtTheTail() {
        let (store, _) = store()
        store.apply(page(turns: [turn(0), turn(1)], nextSeq: 2, total: 1000, more: true),
                    session: "s1")
        store.skipAhead(to: 850, session: "s1")
        XCTAssertEqual(store.nextSeq("s1"), 850)
        store.apply(page(turns: [turn(850, text: "tail"), turn(851)], nextSeq: 852,
                         total: 1000), session: "s1")
        XCTAssertEqual(store.turns("s1").map(\.seq), [850, 851])
        XCTAssertEqual(store.turns("s1").first?.text, "tail")
        // The reset was spent: the page after appends as before.
        store.apply(page(turns: [turn(852)], nextSeq: 853, total: 1000), session: "s1")
        XCTAssertEqual(store.turns("s1").count, 3)
    }

    func testAdoptWithAnotherTokenEmpties() async {
        let (store, _) = store()
        store.adopt("macA")
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        store.adopt("macB")
        XCTAssertTrue(store.turns("s1").isEmpty)
    }

    func testForgetDeletesEveryFile() async throws {
        let (store, dir) = store()
        store.adopt("tok")
        store.apply(page(turns: [turn(0)], nextSeq: 1, total: 1), session: "s1")
        await store.settle()
        store.forget()
        XCTAssertTrue(store.turns("s1").isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: dir.appendingPathComponent(ConversationCacheStore.fileName).path))
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: dir.appendingPathComponent("conversations").path))
    }
}

final class ImageLinksTests: XCTestCase {
    func testPathsAreFoundInOrderWithTheirFolders() {
        let text = "Her morning clip is now `girl/thinking/morning/01.jpg` through `05.jpg`. "
            + "I also opened [his frame](sleeping/night/03.PNG) and /Users/me/proj/a.gif."
        XCTAssertEqual(ImageLinks.paths(in: text), [
            "girl/thinking/morning/01.jpg", "girl/thinking/morning/05.jpg",
            "sleeping/night/03.PNG", "/Users/me/proj/a.gif",
        ])
    }

    func testWebAddressesHiddenFoldersAndOtherFilesAreNotPictures() {
        let text = "See https://example.com/a.png and .claude/x.png, notes.md, "
            + "a.png.txt, archive.pngx and image_edit ×4"
        XCTAssertEqual(ImageLinks.paths(in: text), [])
    }

    func testRelativeHomeAndEveryExtension() {
        let text = "./a.svg ../b.webp ~/c.heic d.jpeg e.JPG"
        XCTAssertEqual(ImageLinks.paths(in: text),
                       ["./a.svg", "../b.webp", "~/c.heic", "d.jpeg", "e.JPG"])
    }

    func testABareNameOfAnotherKindIsNotMovedIntoTheFolder() {
        XCTAssertEqual(ImageLinks.paths(in: "art/01.jpg then logo.png"),
                       ["art/01.jpg", "logo.png"])
    }

    func testDuplicatesOnceAndAtMostTheLimit() {
        let many = (1...20).map { "f\($0).png" }.joined(separator: " ")
        XCTAssertEqual(ImageLinks.paths(in: many).count, ImageLinks.limit)
        XCTAssertEqual(ImageLinks.paths(in: "a.png a.png"), ["a.png"])
        XCTAssertEqual(ImageLinks.name("girl/01.jpg"), "01.jpg")
    }

    func testThePreviewDecodesTolerantly() throws {
        let json = #"{"available":true,"path":"a/b.png","name":"b.png","format":"png","#
            + #""source_format":"png","data":"iVBORw0KGgo=","bytes":4300000,"modified":0,"#
            + #""width":3000,"height":2000,"shown_width":2048,"shown_height":1365,"#
            + #""animated":false,"frames":1,"reduced":true,"future":1}"#
        let p = try JSONDecoder().decode(ImagePreview.self, from: Data(json.utf8))
        XCTAssertTrue(p.available)
        XCTAssertNotNil(p.pictureData)
        XCTAssertEqual(ImagePreviewWords.meta(p), "3000 × 2000 · PNG · 4.1 MB")
        XCTAssertEqual(ImagePreviewWords.reducedLine(p),
                       "shown at 2048 × 1365 — the Mac's copy is larger")
        let empty = try JSONDecoder().decode(ImagePreview.self, from: Data("{}".utf8))
        XCTAssertFalse(empty.available)
        XCTAssertNil(empty.pictureData)
    }

    func testAnAnimationShownAsOneFrameSaysSo() {
        var p = ImagePreview()
        p.available = true
        p.animated = true
        p.frames = 48
        p.format = "jpeg"
        XCTAssertTrue(ImagePreviewWords.reducedLine(p).hasPrefix("animated, 48 frames"))
        p.format = "gif"
        XCTAssertEqual(ImagePreviewWords.reducedLine(p), "")
    }
}

final class ImageLinksBorrowTests: XCTestCase {
    func testAFarAwayBareNameKeepsItsOwnSpelling() {
        let text = "I wrote `assets/hero.png` and then, after a long while of other work, also `logo.png`."
        XCTAssertEqual(ImageLinks.paths(in: text), ["assets/hero.png", "logo.png"])
    }

    func testARunOfFramesKeepsBorrowing() {
        XCTAssertEqual(ImageLinks.paths(in: "a/01.jpg, 02.jpg, 03.jpg"),
                       ["a/01.jpg", "a/02.jpg", "a/03.jpg"])
    }
}

final class ImageMemoTests: XCTestCase {
    func testAPictureIsDroppedAfterADay() {
        var memo = ImageMemo()
        var p = ImagePreview()
        p.available = true
        let start = Date(timeIntervalSince1970: 1_000_000)
        memo.put(p, session: "s", path: "a.png", now: start)
        XCTAssertNotNil(memo.get(session: "s", path: "a.png",
                                 now: start.addingTimeInterval(ImageMemo.lifetime - 1)))
        XCTAssertNil(memo.get(session: "s", path: "a.png",
                              now: start.addingTimeInterval(ImageMemo.lifetime)))
        XCTAssertEqual(memo.count, 0)
        XCTAssertEqual(ImageMemo.lifetime, 86_400)
    }

    func testAtMostTheLimitAndKeyedBySession() {
        var memo = ImageMemo()
        for i in 0..<(ImageMemo.limit + 4) { memo.put(ImagePreview(), session: "s", path: "\(i).png") }
        XCTAssertEqual(memo.count, ImageMemo.limit)
        XCTAssertNil(memo.get(session: "s", path: "0.png"))
        XCTAssertNil(memo.get(session: "other", path: "\(ImageMemo.limit).png"))
        XCTAssertNotNil(memo.get(session: "s", path: "\(ImageMemo.limit).png"))
    }
}
