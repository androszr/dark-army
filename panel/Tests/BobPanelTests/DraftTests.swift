import XCTest
@testable import BobPanel

/// The draft store, the composer lifecycle around it, and the Clear all gate.
///
/// Every case runs against temp-dir roots for both `CardDrafts` and
/// `CardAttachments`, so nothing here reads or writes the real
/// `~/.dark-army`. The timer is never waited on: `autosaveDraftIfNeeded()`
/// is called directly and the interval is asserted as a constant.
final class DraftTests: XCTestCase {
    private var originalDraftRoot: URL!
    private var originalAttachDir: URL!
    private var tempRoot: URL!
    private var tempAttachments: URL!

    override func setUpWithError() throws {
        originalDraftRoot = CardDrafts.root
        originalAttachDir = CardAttachments.dir
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-drafts-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        tempAttachments = tempRoot
            .appendingPathComponent("attachments", isDirectory: true)
        try FileManager.default.createDirectory(at: tempAttachments,
                                                withIntermediateDirectories: true)
        CardDrafts.root = tempRoot
        CardAttachments.dir = tempAttachments
    }

    override func tearDownWithError() throws {
        CardDrafts.root = originalDraftRoot
        CardAttachments.dir = originalAttachDir
        try? FileManager.default.removeItem(at: tempRoot)
    }

    // MARK: - Helpers

    /// What the daemon offers with dispatch on: `list(dispatch._EXECUTABLES)`.
    private let allTools = ["claude", "codex", "grok"]

    private func draft(_ id: String, title: String = "t",
                       updatedAt: Double = 1) -> CardDraft {
        CardDraft(id: id, project: "proj", root: "/tmp/proj", title: title,
                  updatedAt: updatedAt)
    }

    private func readRaw() throws -> [String: Any] {
        let data = try Data(contentsOf: CardDrafts.path)
        return try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    @discardableResult
    private func stageFile(_ stagingId: String, named: String) throws -> String {
        let folder = tempAttachments.appendingPathComponent(stagingId,
                                                            isDirectory: true)
        try FileManager.default.createDirectory(at: folder,
                                                withIntermediateDirectories: true)
        try Data("hello".utf8).write(to: folder.appendingPathComponent(named))
        return "\(stagingId)/\(named)"
    }

    private func folderExists(_ stagingId: String) -> Bool {
        FileManager.default.fileExists(
            atPath: tempAttachments.appendingPathComponent(stagingId).path)
    }

    // MARK: - Store

    func testUpsertRoundTrips() {
        CardDrafts.upsert(draft("a", title: "First"))

        let all = CardDrafts.all()
        XCTAssertEqual(all.count, 1)
        XCTAssertEqual(all.first?.id, "a")
        XCTAssertEqual(all.first?.title, "First")
        XCTAssertEqual(all.first?.project, "proj")
    }

    /// Same id twice is one row, not two — the draft id is the composer's
    /// identity, so autosave overwrites rather than breeding a row per tick.
    func testUpsertSameIdOverwritesOneRow() {
        CardDrafts.upsert(draft("a", title: "First", updatedAt: 1))
        CardDrafts.upsert(draft("a", title: "Second", updatedAt: 2))

        XCTAssertEqual(CardDrafts.all().count, 1)
        XCTAssertEqual(CardDrafts.all().first?.title, "Second")
    }

    func testAllIsNewestFirst() {
        CardDrafts.upsert(draft("old", updatedAt: 10))
        CardDrafts.upsert(draft("new", updatedAt: 20))

        XCTAssertEqual(CardDrafts.all().map(\.id), ["new", "old"])
    }

    func testRemoveDeletesOnlyThatRow() {
        CardDrafts.upsert(draft("a"))
        CardDrafts.upsert(draft("b"))

        CardDrafts.remove("a")

        XCTAssertEqual(CardDrafts.all().map(\.id), ["b"])
    }

    func testGarbageFileReadsAsEmpty() throws {
        try Data("not json at all".utf8).write(to: CardDrafts.path)

        XCTAssertTrue(CardDrafts.all().isEmpty)
    }

    /// A malformed entry drops itself and leaves its siblings alone.
    func testMalformedEntryDropsOnlyItself() throws {
        let body: [String: Any] = [
            "drafts": [
                "good": ["title": "kept", "updated_at": 5],
                "bad": "this is not a dictionary",
            ],
        ]
        try JSONSerialization.data(withJSONObject: body)
            .write(to: CardDrafts.path)

        let all = CardDrafts.all()
        XCTAssertEqual(all.map(\.id), ["good"])
        XCTAssertEqual(all.first?.title, "kept")
    }

    /// Read-merge-write: an upsert may not drop a top-level key it has never
    /// heard of, which is what a newer panel's addition looks like from here.
    func testUpsertKeepsUnknownTopLevelKeys() throws {
        try JSONSerialization.data(withJSONObject: ["future_key": "kept"])
            .write(to: CardDrafts.path)

        CardDrafts.upsert(draft("a"))

        XCTAssertEqual(try readRaw()["future_key"] as? String, "kept")
        XCTAssertEqual(CardDrafts.all().count, 1)
    }

    /// `.atomic` replaces the inode, so the chmod has to run after **every**
    /// write. Asserted after the second one, which is where a
    /// once-at-creation chmod fails.
    func testPermissionsAre0600AfterASecondWrite() throws {
        CardDrafts.upsert(draft("a"))
        CardDrafts.upsert(draft("b"))

        let attrs = try FileManager.default
            .attributesOfItem(atPath: CardDrafts.path.path)
        XCTAssertEqual((attrs[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }

    func testAutosaveIntervalIsTenSeconds() {
        XCTAssertEqual(CardDrafts.autosaveInterval, 10)
    }

    // MARK: - worthKeeping

    func testWorthKeepingIsFalseForASeededComposer() {
        var seeded = BoardDraft()
        seeded.project = "proj"
        seeded.root = "/tmp/proj"
        seeded.tool = "claude"
        seeded.model = "haiku"

        XCTAssertFalse(CardDrafts.worthKeeping(draft: seeded, staged: []))
    }

    func testWorthKeepingIsTrueForAnyTypedFieldAlone() {
        for field in ["title", "summary", "prompt", "workflow", "idea"] {
            var d = BoardDraft()
            switch field {
            case "title": d.title = "x"
            case "summary": d.summary = "x"
            case "prompt": d.prompt = "x"
            case "workflow": d.workflow = "x"
            default: d.idea = "x"
            }
            XCTAssertTrue(CardDrafts.worthKeeping(draft: d, staged: []),
                          "\(field) alone should keep the draft")
        }
    }

    func testWorthKeepingIsTrueForAStagedFileWithNoText() {
        XCTAssertTrue(CardDrafts.worthKeeping(draft: BoardDraft(),
                                              staged: ["abc/notes.txt"]))
    }

    func testWorthKeepingIgnoresWhitespaceOnlyText() {
        var d = BoardDraft()
        d.title = "   \n "
        XCTAssertFalse(CardDrafts.worthKeeping(draft: d, staged: []))
    }

    // MARK: - Default assistant

    @MainActor
    func testFreshComposerDefaultsToClaude() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        XCTAssertEqual(state.draft.tool, "claude")
        XCTAssertEqual(state.draft.model, "")
    }

    /// Dispatch off or an older daemon offers nothing; a daemon that offers
    /// only another assistant does not offer Claude. Both open unassigned.
    @MainActor
    func testFreshComposerStaysUnassignedWhenClaudeIsNotOffered() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: [])
        XCTAssertEqual(state.draft.tool, "")

        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: ["codex"])
        XCTAssertEqual(state.draft.tool, "")
    }

    /// The restore path copies the banked value verbatim — a draft banked
    /// with nobody chosen comes back with nobody chosen, never Claude.
    @MainActor
    func testResumedDraftKeepsItsOwnTool() {
        let state = BoardState()
        let unassigned = CardDraft(id: "d-none", project: "proj",
                                   root: "/tmp/proj", title: "Nobody",
                                   tool: "", updatedAt: 1)
        CardDrafts.upsert(unassigned)
        state.resumeDraft(unassigned)
        XCTAssertEqual(state.draft.tool, "")

        let grok = CardDraft(id: "d-grok", project: "proj",
                             root: "/tmp/proj", title: "Grok's",
                             tool: "grok", updatedAt: 2)
        CardDrafts.upsert(grok)
        state.resumeDraft(grok)
        XCTAssertEqual(state.draft.tool, "grok")
    }

    /// The pre-selected assistant is seed state, not something typed: an
    /// opened-and-closed fresh composer still leaves nothing in Drafts.
    @MainActor
    func testDefaultedComposerIsNotWorthKeeping() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        XCTAssertEqual(state.draft.tool, "claude")
        XCTAssertFalse(CardDrafts.worthKeeping(draft: state.draft, staged: []))

        state.closeEditor()
        XCTAssertTrue(CardDrafts.all().isEmpty)
    }

    /// The default lives in `openComposer`, never in the initialiser that
    /// `resumeDraft` and `init(_ card:)` build on.
    func testBoardDraftInitLeavesToolEmpty() {
        XCTAssertEqual(BoardDraft().tool, "")
        XCTAssertEqual(BoardDraft.defaultTool(offered: []), "")
        XCTAssertEqual(BoardDraft.defaultTool(offered: ["codex", "grok"]), "")
        XCTAssertEqual(BoardDraft.defaultTool(offered: ["grok", "claude"]),
                       "claude")
    }

    // MARK: - Lifecycle

    @MainActor
    func testClosingAContentfulComposerBanksItAndKeepsTheFolder() throws {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        let id = state.stagingId
        try stageFile(id, named: "notes.txt")
        state.draft.title = "Half typed"

        state.closeEditor()

        XCTAssertNil(state.editing)
        XCTAssertEqual(state.drafts.map(\.id), [id])
        XCTAssertEqual(state.drafts.first?.title, "Half typed")
        XCTAssertTrue(folderExists(id))
        XCTAssertEqual(state.stagingId, "")
    }

    @MainActor
    func testClosingAnEmptyComposerKeepsNothing() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)

        state.closeEditor()

        XCTAssertTrue(state.drafts.isEmpty)
        XCTAssertTrue(CardDrafts.all().isEmpty)
    }

    /// The success-path ordering: the row must go before
    /// `keepStagedAttachments()` clears the id, and the `closeEditor()` that
    /// follows must then bank nothing.
    @MainActor
    func testSuccessfulCreateRemovesTheRowAndKeepsTheFolder() throws {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        let id = state.stagingId
        try stageFile(id, named: "notes.txt")
        state.draft.title = "Real card"
        state.autosaveDraftIfNeeded()
        XCTAssertEqual(CardDrafts.all().count, 1)

        state.composerSaving = true
        state.finishComposerCreate(ok: true, detail: "")

        XCTAssertTrue(CardDrafts.all().isEmpty)
        XCTAssertTrue(state.drafts.isEmpty)
        XCTAssertTrue(folderExists(id))
        XCTAssertNil(state.editing)
    }

    @MainActor
    func testRefusedCreateKeepsTheComposerAndBanksOnTheNextClose() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        state.draft.title = "Refused"
        state.composerSaving = true

        state.finishComposerCreate(ok: false, detail: "no")

        XCTAssertEqual(state.editing, BoardState.newCard)
        XCTAssertTrue(CardDrafts.all().isEmpty)

        state.closeEditor()
        XCTAssertEqual(CardDrafts.all().count, 1)
    }

    @MainActor
    func testOpeningANewComposerBanksTheOldOne() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        let first = state.stagingId
        state.draft.title = "First"

        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)

        XCTAssertNotEqual(state.stagingId, first)
        XCTAssertEqual(state.drafts.map(\.id), [first])
    }

    @MainActor
    func testAutosaveIsANoOpWhileSaving() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: allTools)
        state.draft.title = "Held"
        state.composerSaving = true

        state.autosaveDraftIfNeeded()

        XCTAssertTrue(CardDrafts.all().isEmpty)
    }

    @MainActor
    func testResumeRestoresTheFormAndDropsAVanishedAttachment() throws {
        let state = BoardState()
        let id = CardAttachments.mintStagingId()
        let alive = try stageFile(id, named: "kept.txt")
        CardDrafts.upsert(CardDraft(id: id, project: "proj",
                                    root: "/tmp/proj", title: "Back",
                                    summary: "why", prompt: "do it",
                                    workflow: "reviewer", tool: "claude",
                                    model: "haiku",
                                    attachments: [alive, "\(id)/gone.txt"],
                                    updatedAt: 5))
        state.reloadDrafts()

        state.resumeDraft(try XCTUnwrap(state.drafts.first))

        XCTAssertEqual(state.editing, BoardState.newCard)
        XCTAssertEqual(state.stagingId, id)
        XCTAssertEqual(state.draft.title, "Back")
        XCTAssertEqual(state.draft.summary, "why")
        XCTAssertEqual(state.draft.prompt, "do it")
        XCTAssertEqual(state.draft.workflow, "reviewer")
        XCTAssertEqual(state.draft.tool, "claude")
        XCTAssertEqual(state.draft.model, "haiku")
        XCTAssertEqual(state.stagedAttachments, [alive])
    }

    // MARK: - Clear all

    func testClearAllDeletesOnTheRightCountAndReturnsTheIds() {
        CardDrafts.upsert(draft("a"))
        CardDrafts.upsert(draft("b"))

        let gone = CardDrafts.clearAll(expectedCount: 2)

        XCTAssertEqual(gone?.sorted(), ["a", "b"])
        XCTAssertTrue(CardDrafts.all().isEmpty)
    }

    func testClearAllRefusesAStaleCountAndDeletesNothing() {
        CardDrafts.upsert(draft("a"))
        CardDrafts.upsert(draft("b"))

        XCTAssertNil(CardDrafts.clearAll(expectedCount: 1))
        XCTAssertEqual(CardDrafts.all().count, 2)
    }

    /// The draft open in the composer is excluded, and the count confirms
    /// against the deletable set only.
    func testClearAllExcludesTheOpenComposersDraft() {
        CardDrafts.upsert(draft("open"))
        CardDrafts.upsert(draft("a"))
        CardDrafts.upsert(draft("b"))

        XCTAssertNil(CardDrafts.clearAll(expectedCount: 3, excluding: "open"))

        let gone = CardDrafts.clearAll(expectedCount: 2, excluding: "open")
        XCTAssertEqual(gone?.sorted(), ["a", "b"])
        XCTAssertEqual(CardDrafts.all().map(\.id), ["open"])
    }

    // MARK: - The one-box idea

    func testIdeaRoundTripsThroughTheFile() throws {
        CardDrafts.upsert(CardDraft(id: "a", title: "t",
                                    idea: "the whole thought",
                                    updatedAt: 1))
        CardDrafts.root = tempRoot  // force a fresh read off disk

        XCTAssertEqual(CardDrafts.all().first?.idea, "the whole thought")
        let rows = try XCTUnwrap(try readRaw()["drafts"] as? [String: Any])
        let row = try XCTUnwrap(rows["a"] as? [String: Any])
        XCTAssertEqual(row["idea"] as? String, "the whole thought")
    }

    /// A form holding nothing but a sentence in the idea box is the whole
    /// point of the box; banking it must not depend on anything else.
    func testAnIdeaOnlyDraftIsWorthKeeping() {
        var d = BoardDraft()
        d.idea = "make the strip stop wrapping"
        XCTAssertTrue(CardDrafts.worthKeeping(draft: d, staged: []))
    }

    /// A row written by an older panel, read after an upgrade: the key is
    /// simply absent and must decode as empty rather than making the whole
    /// draft absent.
    func testAStoredDraftWithoutTheKeyDecodesWithAnEmptyIdea() throws {
        let row: [String: Any] = ["project": "proj", "root": "/tmp/proj",
                                  "title": "Back", "summary": "why",
                                  "prompt": "do it", "workflow": "reviewer",
                                  "tool": "claude", "model": "haiku",
                                  "attachments": [], "updated_at": 3.0]
        let decoded = try XCTUnwrap(CardDraft(id: "a", any: row))
        XCTAssertEqual(decoded.idea, "")
        XCTAssertEqual(decoded.title, "Back")
    }

    @MainActor
    func testResumeRestoresTheIdea() {
        let state = BoardState()
        CardDrafts.upsert(CardDraft(id: CardAttachments.mintStagingId(),
                                    project: "proj", root: "/tmp/proj",
                                    title: "Back", idea: "one whole thought",
                                    updatedAt: 5))
        state.reloadDrafts()

        state.resumeDraft(state.drafts[0])

        XCTAssertEqual(state.draft.idea, "one whole thought")
    }

    /// A card has no idea field, so opening an existing card must not put a
    /// previous composer's scratch on it.
    func testACardOpenedForEditingCarriesNoIdea() {
        var card = BoardCard()
        card.id = "c1"
        card.title = "A card"
        XCTAssertEqual(BoardDraft(card).idea, "")
    }

    // MARK: - expanded

    func testExpandedRoundTripsThroughTheFile() throws {
        CardDrafts.upsert(CardDraft(id: "a", title: "t",
                                    updatedAt: 1, expanded: true))
        CardDrafts.root = tempRoot

        XCTAssertEqual(CardDrafts.all().first?.expanded, true)
        let rows = try XCTUnwrap(try readRaw()["drafts"] as? [String: Any])
        let row = try XCTUnwrap(rows["a"] as? [String: Any])
        XCTAssertEqual(row["expanded"] as? Bool, true)
    }

    func testAStoredDraftWithoutTheKeyDecodesExpandedFalse() throws {
        let row: [String: Any] = ["project": "proj", "root": "/tmp/proj",
                                  "title": "Back", "summary": "why",
                                  "prompt": "do it", "workflow": "reviewer",
                                  "tool": "claude", "model": "haiku",
                                  "attachments": [], "updated_at": 3.0]
        let decoded = try XCTUnwrap(CardDraft(id: "a", any: row))
        XCTAssertEqual(decoded.expanded, false)
        XCTAssertEqual(decoded.title, "Back")
    }

    func testAFlagOnlyComposerIsNotWorthKeeping() {
        var d = BoardDraft()
        d.expanded = true
        XCTAssertFalse(CardDrafts.worthKeeping(draft: d, staged: []))
    }

    @MainActor
    func testResumeRestoresExpanded() {
        let state = BoardState()
        CardDrafts.upsert(CardDraft(id: CardAttachments.mintStagingId(),
                                    project: "proj", root: "/tmp/proj",
                                    title: "Back", updatedAt: 5,
                                    expanded: true))
        state.reloadDrafts()

        state.resumeDraft(state.drafts[0])

        XCTAssertTrue(state.draft.expanded)
    }

    func testACardOpenedForEditingIsExpanded() {
        var card = BoardCard()
        card.id = "c1"
        XCTAssertTrue(BoardDraft(card).expanded)
        XCTAssertFalse(BoardDraft().expanded)
    }
}
