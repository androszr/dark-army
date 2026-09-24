import XCTest
@testable import BobPanel

/// The new-card form's two phases, tabled.
///
/// `plans/2026-09-19-progressive-card-composer.md`. Every rule behind the
/// short form is a pure static over `(flag, fields, reveal)` — when phase
/// two is drawn, which caption sits above it, the fill-in-myself label,
/// and the words beside a held Save. Both composers read these and
/// re-derive none of them.
final class ComposerPhaseTests: XCTestCase {
    private var originalPlacementRoot: URL!
    private var originalDraftRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalPlacementRoot = PanelPlacement.root
        originalDraftRoot = CardDrafts.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-composer-phase-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot,
                                                withIntermediateDirectories: true)
        PanelPlacement.root = tempRoot
        CardDrafts.root = tempRoot
    }

    override func tearDownWithError() throws {
        PanelPlacement.root = originalPlacementRoot
        CardDrafts.root = originalDraftRoot
        try? FileManager.default.removeItem(at: tempRoot)
    }

    private func board(names: [String]) -> Board {
        var board = Board()
        board.projects = names.map { name in
            var project = BoardProject()
            project.name = name
            project.root = "/tmp/\(name)"
            return project
        }
        return board
    }

    // MARK: - expanded

    func testExpandedIsFalseWhenTheFlagIsOffAndEveryFieldIsEmpty() {
        XCTAssertFalse(ComposerPhase.expanded(flag: false, fields: []))
        XCTAssertFalse(ComposerPhase.expanded(flag: false, fields: ["", "", ""]))
    }

    func testExpandedIsTrueWhenTheFlagIsOn() {
        XCTAssertTrue(ComposerPhase.expanded(flag: true, fields: []))
        XCTAssertTrue(ComposerPhase.expanded(flag: true, fields: ["", ""]))
    }

    func testExpandedIsTrueWhenOneFieldIsNonBlank() {
        XCTAssertTrue(ComposerPhase.expanded(flag: false, fields: ["a title"]))
        XCTAssertTrue(ComposerPhase.expanded(flag: false, fields: ["", "plain words", ""]))
    }

    func testExpandedTreatsWhitespaceOnlyFieldsAsEmpty() {
        XCTAssertFalse(ComposerPhase.expanded(flag: false, fields: ["  ", "\n", "\t"]))
    }

    // MARK: - caption and labels

    func testCaptionForPreparedNamesTheDraft() {
        XCTAssertEqual(
            ComposerPhase.caption(for: .prepared),
            "Dark Army drafted these from your idea — every box is yours to correct")
    }

    func testCaptionForManualInvitesPrepare() {
        XCTAssertEqual(
            ComposerPhase.caption(for: .manual),
            "Every box is yours to fill — Prepare can still draft them from your idea")
    }

    func testCaptionForResumedIsNil() {
        XCTAssertNil(ComposerPhase.caption(for: .resumed))
    }

    func testFillMyselfLabel() {
        XCTAssertEqual(ComposerPhase.fillMyselfLabel, "fill in myself")
    }

    func testHoldReason() {
        XCTAssertEqual(ComposerPhase.holdReason,
                       "Prepare first, or fill it in yourself")
    }

    // MARK: - BoardState

    @MainActor
    func testFreshComposerIsHeldWithThePhaseOneReason() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: ["claude"])
        XCTAssertEqual(state.saveHoldReason(preparing: false),
                       ComposerPhase.holdReason)
    }

    @MainActor
    func testRevealPhaseTwoHoldsOnNameItFirst() {
        let state = BoardState()
        state.openComposer(defaultProject: "proj", defaultRoot: "/tmp/proj",
                           offeredTools: ["claude"])
        state.revealPhaseTwo()
        XCTAssertEqual(state.saveHoldReason(preparing: false), "Name it first")
    }

    @MainActor
    func testOpenEditorIsNeverHeldByThePhase() {
        let state = BoardState()
        var card = BoardCard()
        card.id = "c1"
        state.openEditor(card, client: DaemonClient())
        XCTAssertTrue(state.draft.expanded)
        XCTAssertNotEqual(state.saveHoldReason(preparing: false),
                          ComposerPhase.holdReason)
        XCTAssertEqual(state.saveHoldReason(preparing: false), "Name it first")
    }

    @MainActor
    func testFilingRootUsesTheLastRootOnlyWithAnEmptyFilterAndAListedRoot() {
        PanelPlacement.saveComposerRoot("/tmp/beta")
        let known = board(names: ["alpha", "beta"])
        let state = BoardState()

        state.projectFilter = []
        XCTAssertEqual(state.filingProject(board: known, agents: Agents()),
                       "beta")
        XCTAssertEqual(state.filingRoot(board: known, agents: Agents()),
                       "/tmp/beta")

        state.projectFilter = ["alpha"]
        XCTAssertEqual(state.filingProject(board: known, agents: Agents()),
                       "alpha")
        XCTAssertEqual(state.filingRoot(board: known, agents: Agents()),
                       "/tmp/alpha")
    }

    @MainActor
    func testFilingRootIgnoresALastRootThatIsNotListed() {
        PanelPlacement.saveComposerRoot("/tmp/gone")
        let known = board(names: ["alpha"])
        let state = BoardState()
        state.projectFilter = []
        XCTAssertEqual(state.filingProject(board: known, agents: Agents()),
                       "alpha")
        XCTAssertEqual(state.filingRoot(board: known, agents: Agents()),
                       "/tmp/alpha")
    }
}
