import XCTest
@testable import BobPanel

final class AttachmentTests: XCTestCase {

    func testSanitizeStripsAPathWalkToTheBasename() {
        XCTAssertEqual(CardAttachments.sanitize("../../etc/passwd"), "passwd")
    }

    func testSanitizeCapsALongNameKeepingTheExtension() {
        let name = String(repeating: "x", count: 200) + ".png"
        let out = CardAttachments.sanitize(name)
        XCTAssertEqual(out?.hasSuffix(".png"), true)
        XCTAssertEqual(out?.count, CardAttachments.maxNameChars)
    }

    func testSanitizeReducesSpacesAndQuotesToTheCharset() {
        let out = CardAttachments.sanitize("  'foo bar'.txt")
        XCTAssertNotNil(out)
        XCTAssertFalse(out!.contains(" "))
        XCTAssertFalse(out!.contains("'"))
        XCTAssertTrue(out!.hasSuffix(".txt"))
        XCTAssertTrue(out!.unicodeScalars.allSatisfy {
            CharacterSet(charactersIn:
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
                .contains($0)
        })
    }

    func testSanitizeStripsLeadingDots() {
        XCTAssertEqual(CardAttachments.sanitize(".hidden.png"), "hidden.png")
        XCTAssertEqual(CardAttachments.sanitize("..secret.txt"), "secret.txt")
    }

    func testSanitizeKeepsDoubleDotInTheStem() {
        XCTAssertEqual(CardAttachments.sanitize("photo..png"), "photo..png")
        XCTAssertEqual(CardAttachments.sanitize("v1..2.pdf"), "v1..2.pdf")
    }

    func testResolveRefusesAPathOutsideTheAttachmentsDir() throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        XCTAssertNil(CardAttachments.resolve("/etc/passwd", root: root))
        XCTAssertNil(CardAttachments.resolve("../secret.png", root: root))
    }

    func testResolveRefusesASymlinkWalkOutOfTheDir() throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        let secret = tmp.appendingPathComponent("secret.png")
        try Data("no".utf8).write(to: secret)
        let link = root.appendingPathComponent("shot.png")
        try FileManager.default.createSymbolicLink(at: link,
                                                   withDestinationURL: secret)
        XCTAssertNil(CardAttachments.resolve("shot.png", root: root))
    }

    func testPrepareGateNamesEveryEmptyField() {
        let all = PrepareGate.missing(summaryEmpty: true, toolEmpty: true,
                                      rootEmpty: true)
        XCTAssertEqual(all,
                       "Prepare needs your idea (or a description), an assistant and a project.")
    }

    func testPrepareGateNamesOnlyTheEmptyField() {
        let one = PrepareGate.missing(summaryEmpty: true, toolEmpty: false,
                                      rootEmpty: false)
        XCTAssertEqual(one, "Prepare needs your idea (or a description).")
        let two = PrepareGate.missing(summaryEmpty: false, toolEmpty: true,
                                      rootEmpty: true)
        XCTAssertEqual(two, "Prepare needs an assistant and a project.")
    }

    func testPrepareGateIsNilWhenNothingIsMissing() {
        XCTAssertNil(PrepareGate.missing(summaryEmpty: false, toolEmpty: false,
                                         rootEmpty: false))
    }

    func testStagingIdMatchesTheStoreFolderPattern() {
        let id = CardAttachments.mintStagingId()
        let regex = try! NSRegularExpression(pattern: "^[a-z0-9-]{8,40}$")
        let range = NSRange(id.startIndex..., in: id)
        XCTAssertEqual(regex.numberOfMatches(in: id, range: range), 1)
    }

    func testStageCopiesASymlinkAsARegularFile() async throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        let real = tmp.appendingPathComponent("photo.png")
        try Data("hello".utf8).write(to: real)
        let link = tmp.appendingPathComponent("shot.png")
        try FileManager.default.createSymbolicLink(at: link,
                                                   withDestinationURL: real)
        let id = CardAttachments.mintStagingId()
        let (staged, refused) = await CardAttachments.stage(
            urls: [link], into: id, root: root)
        XCTAssertTrue(refused.isEmpty, "\(refused)")
        XCTAssertEqual(staged, ["\(id)/shot.png"])
        let dest = root.appendingPathComponent(id)
            .appendingPathComponent("shot.png")
        let attrs = try FileManager.default.attributesOfItem(atPath: dest.path)
        XCTAssertEqual(attrs[.type] as? FileAttributeType, .typeRegular)
        let perms = (attrs[.posixPermissions] as? NSNumber)?.intValue ?? 0
        XCTAssertEqual(perms & 0o777, 0o600)
        XCTAssertEqual(try String(contentsOf: dest, encoding: .utf8), "hello")
    }

    func testStageRefusesADirectoryEvenWithAnAllowedExtension() async throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        let folder = tmp.appendingPathComponent("shots.png", isDirectory: true)
        try FileManager.default.createDirectory(at: folder,
                                                withIntermediateDirectories: true)
        let id = CardAttachments.mintStagingId()
        let (staged, refused) = await CardAttachments.stage(
            urls: [folder], into: id, root: root)
        XCTAssertTrue(staged.isEmpty)
        XCTAssertFalse(refused.isEmpty)
        let dest = root.appendingPathComponent(id)
            .appendingPathComponent("shots.png")
        XCTAssertFalse(FileManager.default.fileExists(atPath: dest.path))
    }

    func testKeepIfOwnedDeletesOnlyThisCallsFilesWhenNotOwned() throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        let id = CardAttachments.mintStagingId()
        let folder = root.appendingPathComponent(id, isDirectory: true)
        try FileManager.default.createDirectory(at: folder,
                                                withIntermediateDirectories: true)
        let kept = folder.appendingPathComponent("kept.png")
        let late = folder.appendingPathComponent("late.png")
        try Data("kept".utf8).write(to: kept)
        try Data("late".utf8).write(to: late)

        let accepted = CardAttachments.keepIfOwned(
            ["\(id)/late.png"], owned: false, root: root)

        XCTAssertTrue(accepted.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: kept.path),
                      "a successful save's copies must survive a late stage")
        XCTAssertFalse(FileManager.default.fileExists(atPath: late.path))
        var isDirectory: ObjCBool = false
        XCTAssertTrue(FileManager.default.fileExists(atPath: folder.path,
                                                     isDirectory: &isDirectory))
        XCTAssertTrue(isDirectory.boolValue,
                      "mismatch must not discard the staging folder")
    }

    func testKeepIfOwnedReturnsThePathsWhenOwnedAndDeletesNothing() throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        let id = CardAttachments.mintStagingId()
        let folder = root.appendingPathComponent(id, isDirectory: true)
        try FileManager.default.createDirectory(at: folder,
                                                withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("shot.png")
        try Data("ok".utf8).write(to: file)

        let accepted = CardAttachments.keepIfOwned(
            ["\(id)/shot.png"], owned: true, root: root)

        XCTAssertEqual(accepted, ["\(id)/shot.png"])
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path))
    }

    func testKeepIfOwnedRefusesAPathWalkWhenNotOwned() throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        let secret = tmp.appendingPathComponent("secret.png")
        try Data("no".utf8).write(to: secret)

        _ = CardAttachments.keepIfOwned(
            ["../secret.png"], owned: false, root: root)

        XCTAssertTrue(FileManager.default.fileExists(atPath: secret.path))
    }

    func testStageSuffixesAMaxLengthNameWithoutColliding() async throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
        let longName = maxLengthPngName()
        let firstDir = tmp.appendingPathComponent("a", isDirectory: true)
        let secondDir = tmp.appendingPathComponent("b", isDirectory: true)
        try FileManager.default.createDirectory(at: firstDir,
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: secondDir,
                                                withIntermediateDirectories: true)
        let first = firstDir.appendingPathComponent(longName)
        let second = secondDir.appendingPathComponent(longName)
        try Data("first".utf8).write(to: first)
        try Data("second".utf8).write(to: second)
        let id = CardAttachments.mintStagingId()

        let (staged1, refused1) = await CardAttachments.stage(
            urls: [first], into: id, root: root)
        XCTAssertTrue(refused1.isEmpty, "\(refused1)")
        XCTAssertEqual(staged1, ["\(id)/\(longName)"])

        let (staged2, refused2) = await CardAttachments.stage(
            urls: [second], into: id, root: root)
        XCTAssertTrue(refused2.isEmpty, "\(refused2)")
        XCTAssertEqual(staged2.count, 1)
        let minted = (staged2[0] as NSString).lastPathComponent
        XCTAssertNotEqual(minted, longName)
        XCTAssertLessThanOrEqual(minted.count, CardAttachments.maxNameChars)
        XCTAssertTrue(minted.hasSuffix(".png"))

        let original = root.appendingPathComponent(id)
            .appendingPathComponent(longName)
        XCTAssertEqual(try String(contentsOf: original, encoding: .utf8), "first")
        let mintedPath = CardAttachments.resolve(staged2[0], root: root)
        XCTAssertNotNil(mintedPath)
        XCTAssertEqual(try String(contentsOf: URL(fileURLWithPath: mintedPath!),
                                  encoding: .utf8), "second")
    }

    func testStageDoesNotReplaceAnExistingDestWithTheSameSanitisedName() async throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        let id = CardAttachments.mintStagingId()
        let folder = root.appendingPathComponent(id, isDirectory: true)
        try FileManager.default.createDirectory(at: folder,
                                                withIntermediateDirectories: true)
        let longName = maxLengthPngName()
        let dest = folder.appendingPathComponent(longName)
        try Data("kept".utf8).write(to: dest)

        let srcDir = tmp.appendingPathComponent("src", isDirectory: true)
        try FileManager.default.createDirectory(at: srcDir,
                                                withIntermediateDirectories: true)
        let incoming = srcDir.appendingPathComponent(longName)
        try Data("late".utf8).write(to: incoming)

        let (staged, refused) = await CardAttachments.stage(
            urls: [incoming], into: id, root: root)
        XCTAssertEqual(try String(contentsOf: dest, encoding: .utf8), "kept")
        for rel in staged {
            XCTAssertNotEqual((rel as NSString).lastPathComponent, longName)
        }
        if staged.isEmpty {
            XCTAssertFalse(refused.isEmpty)
        }
    }

    @MainActor
    func testSaveHeldIncludesStagingInFlight() {
        let state = BoardState()
        state.draft.title = "A card"
        XCTAssertFalse(state.saveHeld(preparing: false),
                       "a titled draft with no copy in flight must be saveable")
        state.stagingId = "abcd-efgh"
        state.beginStaging(for: "abcd-efgh")
        XCTAssertTrue(state.saveHeld(preparing: false))
        state.endStaging(for: "abcd-efgh")
        XCTAssertFalse(state.saveHeld(preparing: false))
    }

    @MainActor
    func testSaveHeldIncludesComposerSaving() {
        let state = BoardState()
        state.draft.title = "A card"
        state.composerSaving = true
        XCTAssertTrue(state.saveHeld(preparing: false))
    }

    @MainActor
    func testFailedComposerCreateKeepsStagingAndShowsTheRefusal() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        let id = state.stagingId
        XCTAssertFalse(id.isEmpty)
        state.stagedAttachments = ["\(id)/shot.png"]
        state.draft.title = "A card"
        state.composerSaving = true
        state.finishComposerCreate(ok: false,
                                   detail: "the board is full (500 cards)")
        XCTAssertEqual(state.stagingId, id)
        XCTAssertEqual(state.stagedAttachments, ["\(id)/shot.png"])
        XCTAssertEqual(state.editing, BoardState.newCard)
        XCTAssertEqual(state.refusals[BoardState.newCard],
                       "the board is full (500 cards)")
        XCTAssertFalse(state.composerSaving)
        XCTAssertFalse(state.saveHeld(preparing: false),
                       "a refused create must not keep Save held")
    }

    @MainActor
    func testSuccessfulComposerCreateClearsStagingAndCloses() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        let id = state.stagingId
        state.stagedAttachments = ["\(id)/shot.png"]
        state.composerSaving = true
        state.finishComposerCreate(ok: true, detail: "")
        XCTAssertEqual(state.stagingId, "")
        XCTAssertTrue(state.stagedAttachments.isEmpty)
        XCTAssertNil(state.editing)
        XCTAssertFalse(state.composerSaving)
    }

    @MainActor
    func testCloseWithoutSaveClearsStagingId() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        XCTAssertFalse(state.stagingId.isEmpty)
        state.stagedAttachments = ["x/shot.png"]
        state.closeEditor()
        XCTAssertEqual(state.stagingId, "")
        XCTAssertTrue(state.stagedAttachments.isEmpty)
        XCTAssertNil(state.editing)
    }

    @MainActor
    func testCloseDuringComposerSaveDoesNotDiscard() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        let id = state.stagingId
        state.stagedAttachments = ["\(id)/shot.png"]
        state.composerSaving = true
        state.closeEditor()
        XCTAssertEqual(state.stagingId, id)
        XCTAssertEqual(state.stagedAttachments, ["\(id)/shot.png"])
        XCTAssertEqual(state.editing, BoardState.newCard)
    }

    @MainActor
    func testOpenEditorDuringComposerSaveIsANoOp() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        let id = state.stagingId
        state.stagedAttachments = ["\(id)/shot.png"]
        state.composerSaving = true
        var card = BoardCard()
        card.id = "existing-card"
        card.title = "Existing"
        state.openEditor(card, client: DaemonClient())
        XCTAssertEqual(state.editing, BoardState.newCard)
        XCTAssertEqual(state.stagingId, id)
        XCTAssertEqual(state.stagedAttachments, ["\(id)/shot.png"])
        XCTAssertEqual(state.draft.project, "p")
        XCTAssertTrue(state.composerSaving)
    }

    @MainActor
    func testUnstageDuringComposerSaveKeepsThePath() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        let rel = "\(state.stagingId)/shot.png"
        state.stagedAttachments = [rel]
        state.composerSaving = true
        state.unstageAttachment(rel)
        XCTAssertEqual(state.stagedAttachments, [rel])
    }

    @MainActor
    func testSuccessfulCreateKeepsAttachmentsWhenEditingMoved() {
        let state = BoardState()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        let id = state.stagingId
        state.stagedAttachments = ["\(id)/shot.png"]
        state.composerSaving = true
        state.editing = "other-card"
        state.finishComposerCreate(ok: true, detail: "", stillComposer: false)
        XCTAssertEqual(state.stagingId, "")
        XCTAssertTrue(state.stagedAttachments.isEmpty)
        XCTAssertEqual(state.editing, "other-card")
        XCTAssertFalse(state.composerSaving)
    }

    @MainActor
    func testEndStagingIgnoresAClosedComposer() {
        let state = BoardState()
        state.stagingId = "live-id-01"
        state.beginStaging(for: "live-id-01")
        state.endStaging(for: "old-id-01")
        XCTAssertEqual(state.stagingInFlight, 1)
        state.endStaging(for: "live-id-01")
        XCTAssertEqual(state.stagingInFlight, 0)
    }

    @MainActor
    func testWaitForStagingReturnsImmediatelyWhenIdle() async {
        let state = BoardState()
        await state.waitForStaging()
        XCTAssertEqual(state.stagingInFlight, 0)
    }

    @MainActor
    func testWaitForStagingResumesWhenTheCopyLands() async {
        let state = BoardState()
        state.stagingId = "abcd-efgh"
        state.beginStaging(for: "abcd-efgh")
        let waiter = Task { @MainActor in
            await state.waitForStaging()
        }
        await Task.yield()
        state.endStaging(for: "abcd-efgh")
        await waiter.value
        XCTAssertEqual(state.stagingInFlight, 0)
    }

    func testKeepIfOwnedOfALateCollidingStageLeavesTheKeptFile() async throws {
        let tmp = try scratchDir()
        defer { try? FileManager.default.removeItem(at: tmp) }
        let root = tmp.appendingPathComponent("attach", isDirectory: true)
        let id = CardAttachments.mintStagingId()
        let folder = root.appendingPathComponent(id, isDirectory: true)
        try FileManager.default.createDirectory(at: folder,
                                                withIntermediateDirectories: true)
        let longName = maxLengthPngName()
        let kept = folder.appendingPathComponent(longName)
        try Data("kept".utf8).write(to: kept)

        let srcDir = tmp.appendingPathComponent("src", isDirectory: true)
        try FileManager.default.createDirectory(at: srcDir,
                                                withIntermediateDirectories: true)
        let incoming = srcDir.appendingPathComponent(longName)
        try Data("late".utf8).write(to: incoming)

        let (staged, _) = await CardAttachments.stage(
            urls: [incoming], into: id, root: root)
        let accepted = CardAttachments.keepIfOwned(
            staged, owned: false, root: root)

        XCTAssertTrue(accepted.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: kept.path))
        XCTAssertEqual(try String(contentsOf: kept, encoding: .utf8), "kept")
        for rel in staged {
            XCTAssertNil(CardAttachments.resolve(rel, root: root),
                         "a mismatch must tidy only the file this stage wrote")
        }
    }

    private func maxLengthPngName() -> String {
        let suffix = ".png"
        return String(repeating: "x",
                      count: CardAttachments.maxNameChars - suffix.count)
            + suffix
    }

    private func scratchDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-attach-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: url,
                                                withIntermediateDirectories: true)
        return url
    }
}
