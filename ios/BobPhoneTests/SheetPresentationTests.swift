import Combine
import XCTest
@testable import BobPhone

@MainActor
final class SheetPresentationTests: XCTestCase {
    private func card(_ id: String, title: String = "Card") -> PhoneSheet {
        var card = BoardCard(); card.id = id; card.title = title
        return .card(card)
    }
    private func receipt(_ sequence: Int = 1) -> PendingReceipt {
        PendingReceipt(receiptId: "receipt", sequence: sequence, generation: "pairing")
    }

    func testTerminalPresentedDoesNotPublish() {
        let router = PhoneSheetRouter()
        var fired = 0
        let sub = router.objectWillChange.sink { fired += 1 }
        router.terminalPresented = true
        router.terminalPresented = false
        XCTAssertTrue(router.terminalPresented == false)
        XCTAssertEqual(fired, 0)
        _ = sub
    }

    func testShowAppendsAndBackPopsOne() {
        let router = PhoneSheetRouter()
        router.show(card("one")); router.show(card("two"))
        XCTAssertEqual(router.stack.map(\.id), ["card/one", "card/two"])
        router.back()
        XCTAssertEqual(router.top?.id, "card/one")
    }

    func testEqualTopRefreshesPayloadWithoutGrowing() {
        let router = PhoneSheetRouter()
        router.show(card("one", title: "Old"))
        router.show(card("one", title: "New"))
        XCTAssertEqual(router.stack.count, 1)
        XCTAssertEqual(router.top?.title, "New")
    }

    func testCloseEmptiesEveryRungAndPresenter() {
        let router = PhoneSheetRouter()
        router.show(card("one")); router.show(card("two"))
        XCTAssertNotNil(router.presentation)
        router.close()
        XCTAssertTrue(router.stack.isEmpty)
        XCTAssertNil(router.top)
        XCTAssertNil(router.presentation)
        router.back(); router.close()
        XCTAssertTrue(router.stack.isEmpty)
    }

    func testDeepLinkResetsAnOpenTrail() {
        let router = PhoneSheetRouter()
        router.show(card("one")); router.show(card("two"))
        router.route(receipt())
        XCTAssertEqual(router.stack.count, 1)
        XCTAssertEqual(router.top, .notification(receipt()))
    }

    func testFourthShowReplacesTopAndKeepsWayBack() {
        let router = PhoneSheetRouter()
        for id in ["one", "two", "three", "four"] { router.show(card(id)) }
        XCTAssertEqual(router.stack.count, PhoneSheetRouter.MAX_DEPTH)
        XCTAssertEqual(router.stack.map(\.id), ["card/one", "card/two", "card/four"])
        router.back()
        XCTAssertEqual(router.top?.id, "card/two")
    }

    func testSubjectChangesKeepTheSamePresentedItem() {
        let router = PhoneSheetRouter()
        router.show(card("one"))
        let presentation = router.presentation?.id
        router.show(card("two"))
        XCTAssertEqual(router.presentation?.id, presentation)
        router.back()
        XCTAssertEqual(router.presentation?.id, presentation)
        router.route(receipt())
        XCTAssertEqual(router.presentation?.id, presentation)
        XCTAssertEqual(router.stack.count, 1)
    }

    func testRepeatedNotificationTapGetsFreshContentIdentity() {
        let router = PhoneSheetRouter()
        router.route(receipt(1))
        let first = router.top?.id
        let presentation = router.presentation?.id
        router.route(receipt(2))
        XCTAssertNotEqual(router.top?.id, first)
        XCTAssertEqual(router.presentation?.id, presentation)
        XCTAssertEqual(router.stack.count, 1)
    }

    func testSubjectKindSeparatesIdenticalIds() throws {
        let agent = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"same"}"#.utf8))
        XCTAssertNotEqual(PhoneSheet.agent(agent, .waiting).id, card("same").id)
    }

    func testCardEditsAndRevisionContextSurviveAChildAndBack() throws {
        let router = PhoneSheetRouter()
        router.show(card("one"))
        let entry = try XCTUnwrap(router.topState)
        let draft = entry.cardDraft(for: "one")
        draft.draftFor = "one"
        draft.draftTitle = "Unsaved title"
        draft.draftSummary = "Unsaved description"
        draft.draftPrompt = "Unsaved instructions"
        draft.draftPriority = "91"
        draft.draftTouched = true
        draft.editing = true
        draft.messageOpen = true
        draft.messageText = "Unsent message"
        draft.planDetailOpen = true
        draft.note = "The card changed"
        draft.conflict = CardStated(json: ["revision": 8, "title": "Mac version"])
        var full = CardFull()
        var row = BoardCard(); row.id = "one"; row.revision = 7
        full.card = row
        draft.cardFull = full
        router.show(.catchUp())
        router.back()
        let restored = try XCTUnwrap(router.topState).cardDraft(for: "one")
        XCTAssertTrue(restored === draft)
        XCTAssertEqual(restored.draftTitle, "Unsaved title")
        XCTAssertEqual(restored.draftSummary, "Unsaved description")
        XCTAssertEqual(restored.draftPrompt, "Unsaved instructions")
        XCTAssertEqual(restored.draftPriority, "91")
        XCTAssertTrue(restored.draftTouched)
        XCTAssertTrue(restored.editing)
        XCTAssertTrue(restored.messageOpen)
        XCTAssertEqual(restored.messageText, "Unsent message")
        XCTAssertTrue(restored.planDetailOpen)
        XCTAssertEqual(restored.conflict?.revision, 8)
        XCTAssertEqual(restored.cardFull?.card?.revision, 7)
        XCTAssertEqual(restored.draftFor, "one")
        XCTAssertEqual(restored.note, "The card changed")
    }

    func testTypedReplyAndSuccessfulSendClearUseTheRetainedEntry() throws {
        let router = PhoneSheetRouter()
        router.show(.catchUp())
        let reply = try XCTUnwrap(router.topState).replyDraft(for: "session")
        reply.text = "A half-written answer"
        router.show(card("child"))
        router.back()
        let restored = try XCTUnwrap(router.topState).replyDraft(for: "session")
        XCTAssertTrue(restored === reply)
        XCTAssertEqual(restored.text, "A half-written answer")
        // A reply finishing after departure clears the same object, so Back
        // must not resurrect text that was already sent.
        router.show(card("child"))
        reply.text = ""
        router.back()
        XCTAssertEqual(router.topState?.replyDraft(for: "session").text, "")
    }

    func testEqualSubjectRefreshPreservesStateButRepeatedTrailEntriesAreSeparate() throws {
        let router = PhoneSheetRouter()
        router.show(card("one"))
        let original = try XCTUnwrap(router.topState)
        original.cardDraft(for: "one").draftTitle = "Keep this"
        router.show(card("one", title: "New seed"))
        XCTAssertTrue(router.topState === original)
        router.show(card("two"))
        router.show(card("one"))
        XCTAssertFalse(router.topState === original)
        XCTAssertEqual(router.topState?.cardDraft(for: "one").draftTitle, "")
        router.back(); router.back()
        XCTAssertEqual(router.topState?.cardDraft(for: "one").draftTitle, "Keep this")
    }

    func testPoppedAndCappedEntriesReleaseTheirTransientState() {
        let router = PhoneSheetRouter()
        router.show(card("one")); router.show(card("two")); router.show(card("three"))
        weak var capped = router.topState
        router.show(card("four"))
        XCTAssertNil(capped)
        weak var popped = router.topState
        router.back()
        XCTAssertNil(popped)
        XCTAssertEqual(router.stack.count, 2)
    }

    func testCloseAndDeepLinkResetEvictDraftsAndResolvedPages() {
        let router = PhoneSheetRouter()
        router.show(card("one"))
        weak var first = router.topState
        router.close()
        XCTAssertNil(first)
        XCTAssertNil(router.topState)
        router.show(card("one"))
        router.topState?.cardDraft(for: "one").draftTitle = "Discarded by deep link"
        weak var replaced = router.topState
        router.route(receipt())
        XCTAssertNil(replaced)
        XCTAssertEqual(router.topState?.cardDraft(for: "one").draftTitle, "")
        weak var notification = router.topState
        router.close()
        XCTAssertNil(notification)
    }

    func testConsumedNotificationGroupReturnsFromDecisionWithoutNewAuthorization() throws {
        var destination = DestinationState()
        destination.pair("pairing")
        destination.tap("receipt")
        let route = try XCTUnwrap(destination.pending)
        let router = PhoneSheetRouter()
        router.route(route)
        let entry = try XCTUnwrap(router.topState)
        var page = CatchUpPage(); page.available = true
        var first = DecisionItem(); first.id = "one"
        var second = DecisionItem(); second.id = "two"
        page.items = [first, second]
        entry.rememberNotification(page, route: route, authorized: destination.accepts(route), requestGeneration: 4)
        destination.consume(route)
        XCTAssertFalse(destination.accepts(route))
        router.show(.decision(first)); router.back()
        let restored = router.topState?.resolvedNotification(for: route, unlocked: true,
                                                            generation: "pairing", requestGeneration: 4)
        XCTAssertEqual(restored?.items.map(\.id), ["one", "two"])
    }

    func testNotificationCacheRequiresPriorAuthorizationAndSameUnlockedScope() {
        let entry = PhoneSheetEntryState()
        let route = receipt()
        var page = CatchUpPage(); page.available = true
        entry.rememberNotification(page, route: route, authorized: false, requestGeneration: 4)
        XCTAssertNil(entry.resolvedNotification(for: route, unlocked: true, generation: "pairing", requestGeneration: 4))
        entry.rememberNotification(page, route: route, authorized: true, requestGeneration: 4)
        XCTAssertNotNil(entry.resolvedNotification(for: route, unlocked: true, generation: "pairing", requestGeneration: 4))
        XCTAssertNil(entry.resolvedNotification(for: receipt(2), unlocked: true, generation: "pairing", requestGeneration: 4))
        XCTAssertNil(entry.resolvedNotification(for: route, unlocked: false, generation: "pairing", requestGeneration: 4))
        XCTAssertNil(entry.resolvedNotification(for: route, unlocked: true, generation: "new pairing", requestGeneration: 4))
        XCTAssertNil(entry.resolvedNotification(for: route, unlocked: true, generation: "pairing", requestGeneration: 5))
    }

    func testCappedDuplicateSubjectBackRestoresTheEarlierMountAndDraft() throws {
        let router = PhoneSheetRouter()
        let agent = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"A"}"#.utf8))
        router.show(.agent(agent, .waiting))
        router.show(card("C"))
        let earlier = try XCTUnwrap(router.topState)
        earlier.cardDraft(for: "C").draftTitle = "Earlier unsaved draft"
        let earlierIdentity = earlier.id
        router.show(.agent(agent, .waiting))
        router.show(card("C"))
        XCTAssertEqual(router.stack.map(\.id), ["agent/A", "card/C", "card/C"])
        let later = try XCTUnwrap(router.topState)
        later.cardDraft(for: "C").draftTitle = "Later unsaved draft"
        XCTAssertNotEqual(later.id, earlierIdentity)
        let subject = router.top?.id
        router.back()
        XCTAssertEqual(router.top?.id, subject)
        XCTAssertEqual(router.topState?.id, earlierIdentity)
        XCTAssertNotEqual(router.topState?.id, later.id)
        XCTAssertEqual(router.topState?.cardDraft(for: "C").draftTitle, "Earlier unsaved draft")
        router.show(card("C", title: "Refreshed seed"))
        XCTAssertEqual(router.topState?.id, earlierIdentity)
    }

    /// The agent and a card in In progress are the two sheets answered from
    /// half height; every other card, and every accessibility size, is large.
    func testAnInProgressCardIsTheSecondAnsweringCase() {
        XCTAssertEqual(PhoneSheetKind.initialDetent(.card, column: "in_progress", accessibility: false), .medium)
        XCTAssertEqual(PhoneSheetKind.detents(.card, column: "in_progress", accessibility: false), [.medium, .large])
        for column in ["backlog", "prep", "done", nil] as [String?] {
            XCTAssertEqual(PhoneSheetKind.initialDetent(.card, column: column, accessibility: false), .large)
            XCTAssertEqual(PhoneSheetKind.detents(.card, column: column, accessibility: false), [.large])
            XCTAssertEqual(PhoneSheetKind.initialDetent(.agent, column: column, accessibility: false), .medium)
        }
        XCTAssertEqual(PhoneSheetKind.initialDetent(.agent, column: "in_progress", accessibility: false), .medium)
        XCTAssertEqual(PhoneSheetKind.initialDetent(.catchUp, column: "in_progress", accessibility: false), .large)
        XCTAssertEqual(PhoneSheetKind.initialDetent(.card, column: "in_progress", accessibility: true), .large)
        XCTAssertEqual(PhoneSheetKind.initialDetent(.agent, column: nil, accessibility: true), .large)
        XCTAssertEqual(PhoneSheetKind.onKeyboard(.medium), .large)
        var working = BoardCard(); working.id = "w"; working.column = "in_progress"
        XCTAssertEqual(PhoneSheet.card(working).cardColumn, "in_progress")
        XCTAssertNil(PhoneSheet.catchUp().cardColumn)
    }
}
