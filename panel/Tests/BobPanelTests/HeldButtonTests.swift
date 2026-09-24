import XCTest
@testable import BobPanel

/// The sentence beside a held Save button, and the promise that it and the
/// gate are one computation rather than two that can drift apart.
final class HeldButtonTests: XCTestCase {

    @MainActor
    func testBlankTitleSaysNameItFirst() {
        let state = BoardState()
        XCTAssertEqual(state.saveHoldReason(preparing: false), "Name it first")
    }

    @MainActor
    func testTitleOnlyDraftIsNotHeld() {
        let state = BoardState()
        state.draft.title = "A card"
        XCTAssertNil(state.saveHoldReason(preparing: false))
    }

    @MainActor
    func testTruncatedPromptSaysWaitingForTheFullInstructions() {
        let state = BoardState()
        state.draft.title = "A card"
        state.draft.promptTruncated = true
        XCTAssertEqual(state.saveHoldReason(preparing: false),
                       "Waiting for the full instructions")
    }

    @MainActor
    func testTruncatedSummarySaysWaitingForTheFullDescription() {
        let state = BoardState()
        state.draft.title = "A card"
        state.draft.summaryTruncated = true
        XCTAssertEqual(state.saveHoldReason(preparing: false),
                       "Waiting for the full description")
        state.draft.summaryTruncated = false
        XCTAssertNil(state.saveHoldReason(preparing: false))
    }

    /// Only the truncated editor is disabled. A fetch that wrote both fields
    /// would drop keystrokes in the live sibling.
    @MainActor
    func testFetchedCardDoesNotClobberAnUntruncatedSibling() {
        let state = BoardState()
        state.draft.prompt = "typed while waiting"
        state.draft.promptTruncated = false
        state.draft.summary = "preview"
        state.draft.summaryTruncated = true
        var full = BoardCard()
        full.prompt = "server prompt"
        full.summary = "full description"
        state.applyFetchedCard(full)
        XCTAssertEqual(state.draft.prompt, "typed while waiting")
        XCTAssertFalse(state.draft.promptTruncated)
        XCTAssertEqual(state.draft.summary, "full description")
        XCTAssertFalse(state.draft.summaryTruncated)
    }

    @MainActor
    func testFetchedCardDoesNotClobberATypedSummary() {
        let state = BoardState()
        state.draft.prompt = "preview"
        state.draft.promptTruncated = true
        state.draft.summary = "typed while waiting"
        state.draft.summaryTruncated = false
        var full = BoardCard()
        full.prompt = "full instructions"
        full.summary = "server summary"
        state.applyFetchedCard(full)
        XCTAssertEqual(state.draft.prompt, "full instructions")
        XCTAssertFalse(state.draft.promptTruncated)
        XCTAssertEqual(state.draft.summary, "typed while waiting")
        XCTAssertFalse(state.draft.summaryTruncated)
    }

    @MainActor
    func testPreparingSaysBobIsStillWriting() {
        let state = BoardState()
        state.draft.title = "A card"
        XCTAssertEqual(state.saveHoldReason(preparing: true),
                       "Dark Army is still writing the instructions")
    }

    @MainActor
    func testStagingInFlightSaysStillCopyingFiles() {
        let state = BoardState()
        state.draft.title = "A card"
        state.stagingId = "abcd-efgh"
        state.beginStaging(for: "abcd-efgh")
        XCTAssertEqual(state.saveHoldReason(preparing: false), "Still copying files")
        state.endStaging(for: "abcd-efgh")
        XCTAssertNil(state.saveHoldReason(preparing: false))
    }

    @MainActor
    func testComposerSavingSaysAddingTheCard() {
        let state = BoardState()
        state.draft.title = "A card"
        state.composerSaving = true
        XCTAssertEqual(state.saveHoldReason(preparing: false), "Adding the card…")
    }

    /// Precedence, and the reason it is a decision rather than an `||` order:
    /// a drop onto an unnamed composer makes both true at once, and naming
    /// the copy would tell the person to wait for something that will not
    /// unhold the button.
    @MainActor
    func testBlankTitleOutranksACopyInFlight() {
        let state = BoardState()
        state.stagingId = "abcd-efgh"
        state.beginStaging(for: "abcd-efgh")
        XCTAssertEqual(state.saveHoldReason(preparing: false), "Name it first")
    }

    /// The test that fails if somebody re-adds a term to one and not the other.
    @MainActor
    func testHeldAndReasonCannotDrift() {
        let configurations: [(String, Bool, (BoardState) -> Void)] = [
            ("blank title", false, { _ in }),
            ("titled draft", false, { $0.draft.title = "A card" }),
            ("truncated prompt", false, {
                $0.draft.title = "A card"
                $0.draft.promptTruncated = true
            }),
            ("truncated summary", false, {
                $0.draft.title = "A card"
                $0.draft.summaryTruncated = true
            }),
            ("preparing", true, { $0.draft.title = "A card" }),
            ("copy in flight", false, {
                $0.draft.title = "A card"
                $0.stagingId = "abcd-efgh"
                $0.beginStaging(for: "abcd-efgh")
            }),
            ("composer saving", false, {
                $0.draft.title = "A card"
                $0.composerSaving = true
            }),
        ]
        for (label, preparing, arrange) in configurations {
            let state = BoardState()
            arrange(state)
            XCTAssertEqual(state.saveHeld(preparing: preparing),
                           state.saveHoldReason(preparing: preparing) != nil,
                           "gate and sentence disagree for \(label)")
        }
    }
}
