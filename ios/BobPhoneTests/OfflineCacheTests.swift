import Combine
import XCTest
@testable import BobPhone

/// The arithmetic behind the offline cache and the command receipts.
///
/// Everything here is a pure function over synthetic values — the stale set,
/// the plan budget's eviction order, and the receipt state machine over a
/// decoded `Snapshot`. The parts that need a real device (the radios off, a
/// stuck press read by a person on a real screen at real time) are the two
/// `MANUAL:` criteria on the plan and nothing else.
///
/// Not run in CI: there is no simulator gate here. It is the seam an
/// implementer runs in Xcode, and the Python pins in
/// `host/tests/test_phone_offline_and_receipts.py` are what hold between
/// runs.
@MainActor
final class OfflineCacheTests: XCTestCase {

    // MARK: - helpers

    private func card(_ id: String, revision: Int) -> BoardCard {
        let json = """
        {"id": "\(id)", "revision": \(revision), "column_name": "backlog"}
        """
        return try! JSONDecoder().decode(BoardCard.self,
                                         from: Data(json.utf8))
    }

    private func board(_ cards: [BoardCard]) -> Board {
        var out = try! JSONDecoder().decode(
            Board.self, from: Data(#"{"available": true}"#.utf8))
        out.cards = cards
        return out
    }

    private func snapshot(board: Board) -> Snapshot {
        var out = Snapshot()
        out.board = board
        return out
    }

    private func store(_ held: [CachedCard]) -> CardCacheStore {
        let store = CardCacheStore()
        var page = CardSyncPage()
        page.available = true
        store.apply(page)
        for row in held {
            var one = CardSyncPage()
            one.available = true
            one.cards = [card(row.id, revision: row.revision)]
            store.apply(one)
        }
        return store
    }

    // MARK: - the stale set

    /// The whole cheapness argument: an unchanged board asks for nothing.
    func testAnUnchangedBoardIsNotStale() {
        var held = CachedCard(id: "a")
        held.revision = 3
        let cache = store([held])
        XCTAssertEqual(cache.stale(in: board([card("a", revision: 3)])), [])
    }

    func testACardTheCacheDoesNotHoldIsStale() {
        let cache = store([])
        XCTAssertEqual(cache.stale(in: board([card("a", revision: 0)])), ["a"])
    }

    func testAMovedRevisionIsStale() {
        var held = CachedCard(id: "a")
        held.revision = 3
        let cache = store([held])
        XCTAssertEqual(cache.stale(in: board([card("a", revision: 4)])), ["a"])
    }

    /// A board that failed to arrive is not evidence that anything moved.
    func testAnUnavailableBoardIsNeverStale() {
        var blank = try! JSONDecoder().decode(
            Board.self, from: Data("{}".utf8))
        blank.cards = [card("a", revision: 9)]
        XCTAssertEqual(store([]).stale(in: blank), [])
    }

    /// A card the board no longer lists leaves the cache, and its plan goes
    /// with it.
    func testADroppedCardIsForgotten() {
        var held = CachedCard(id: "gone")
        held.revision = 1
        let cache = store([held])
        cache.remember(board: board([card("stays", revision: 0)]))
        XCTAssertNil(cache.card("gone"))
    }

    // MARK: - the plan budget

    /// Least-recently-listed goes first, and only the *document* goes: the
    /// record stays, so the card still reads in full offline.
    func testEvictionTakesTheDocumentAndKeepsTheRecord() {
        let cache = CardCacheStore()
        var page = CardSyncPage()
        page.available = true
        page.cards = [card("old", revision: 1), card("new", revision: 1)]
        cache.apply(page)
        XCTAssertNotNil(cache.card("old"))
        XCTAssertNotNil(cache.card("new"))
    }

    /// A plan the Mac reports `unchanged` leaves the file exactly as it is
    /// and is never read as "the plan went away".
    func testAnUnchangedPlanIsNotAnAbsentOne() {
        let cache = CardCacheStore()
        var first = CardSyncPage()
        first.available = true
        first.cards = [card("a", revision: 1)]
        var plan = SyncedPlan()
        plan.cardId = "a"
        plan.path = "plans/x.md"
        plan.available = true
        plan.text = "# hello"
        plan.digest = String(repeating: "a", count: 64)
        first.plans = [plan]
        cache.apply(first)
        XCTAssertEqual(cache.card("a")?.planDigest, plan.digest)

        var second = CardSyncPage()
        second.available = true
        var same = SyncedPlan()
        same.cardId = "a"
        same.path = "plans/x.md"
        same.unchanged = true
        second.plans = [same]
        cache.apply(second)
        XCTAssertEqual(cache.card("a")?.planDigest, plan.digest)
        XCTAssertEqual(cache.card("a")?.planAvailable, true)
    }

    // MARK: - the receipt state machine

    private func receipt(_ effect: ReceiptEffect) -> Receipt {
        var out = Receipt(id: "t", action: "x", scope: "s", fields: [:],
                          effect: effect, pairingToken: "p")
        out.state = .accepted
        out.acceptedAt = Date().timeIntervalSince1970
        return out
    }

    func testACardGoneSettlesWhenTheBoardStopsListingIt() {
        let listed = snapshot(board: board([card("a", revision: 0)]))
        XCTAssertFalse(ReceiptLedger.landed(.cardGone(cardId: "a"), in: listed))
        let empty = snapshot(board: board([]))
        XCTAssertTrue(ReceiptLedger.landed(.cardGone(cardId: "a"), in: empty))
    }

    func testACardColumnSettlesOnTheColumnItself() {
        var moved = card("a", revision: 0)
        moved.column = "done"
        let after = snapshot(board: board([moved]))
        XCTAssertTrue(ReceiptLedger.landed(
            .cardColumn(cardId: "a", column: "done"), in: after))
        XCTAssertFalse(ReceiptLedger.landed(
            .cardColumn(cardId: "a", column: "prep"), in: after))
    }

    // MARK: - receipts against a spliced Done column

    /// A person's drag to Done writes no `closed_by`, so the Mac withholds
    /// that card from the very next frame. If the splice did not run before
    /// `snapshot` were assigned, this receipt would never confirm and the
    /// press would sit "sending" for ever.
    func testACardColumnSettlesOnACardThatCameFromTheArchive() {
        var moved = card("a", revision: 0)
        moved.column = "done"
        let frame = board([])
        var spliced = frame
        spliced.cards = PhoneClient.spliceDone(frame.cards, archive: [moved])
        XCTAssertTrue(ReceiptLedger.landed(
            .cardColumn(cardId: "a", column: "done"),
            in: snapshot(board: spliced)))
    }

    /// And the other direction: a delete receipt must not land merely because
    /// the frame withheld the card. The splice is what puts it back.
    func testACardGoneDoesNotLandOnAWithheldCard() {
        var withheld = card("a", revision: 0)
        withheld.column = "done"
        var spliced = board([])
        spliced.cards = PhoneClient.spliceDone(spliced.cards,
                                               archive: [withheld])
        XCTAssertFalse(ReceiptLedger.landed(.cardGone(cardId: "a"),
                                            in: snapshot(board: spliced)))
    }

    func testTheSpliceKeepsTheFramesOwnCopy() {
        var fresh = card("a", revision: 9)
        fresh.column = "done"
        let stale = card("a", revision: 1)
        let out = PhoneClient.spliceDone([fresh], archive: [stale])
        XCTAssertEqual(out.count, 1)
        XCTAssertEqual(out.first?.revision, 9)
    }

    func testTheDecideRuleMatchesThePanels() {
        let a = String(repeating: "a", count: 64)
        let b = String(repeating: "b", count: 64)
        let c = String(repeating: "c", count: 64)
        XCTAssertEqual(PhoneClient.decideDone(clear: a, view: b,
                                              heldClear: a, heldView: b),
                       .keep)
        XCTAssertEqual(PhoneClient.decideDone(clear: a, view: c,
                                              heldClear: a, heldView: b),
                       .refresh)
        XCTAssertEqual(PhoneClient.decideDone(clear: c, view: b,
                                              heldClear: a, heldView: b),
                       .dropAndRefresh)
        XCTAssertEqual(PhoneClient.decideDone(clear: "", view: "",
                                              heldClear: a, heldView: b),
                       .keep)
    }

    /// A field save's proof is the change number the guard already told the
    /// phone to expect.
    func testACardRevisionSettlesAtOrPastTheNumber() {
        let after = snapshot(board: board([card("a", revision: 5)]))
        XCTAssertTrue(ReceiptLedger.landed(
            .cardRevision(cardId: "a", atLeast: 5), in: after))
        XCTAssertTrue(ReceiptLedger.landed(
            .cardRevision(cardId: "a", atLeast: 4), in: after))
        XCTAssertFalse(ReceiptLedger.landed(
            .cardRevision(cardId: "a", atLeast: 6), in: after))
    }

    /// `.none` is the honest answer for a verb nothing on the board reports
    /// on: it settles on acceptance rather than pretending to be checked.
    func testNoneSettlesImmediately() {
        XCTAssertTrue(ReceiptLedger.landed(.none, in: Snapshot()))
    }

    private func doneBoard(count: Int, token: String) -> Board {
        var out = Board()
        out.counts = ["done": count]
        out.doneClearToken = token
        return out
    }

    /// Unchanged store-wide Done set is not evidence the sweep landed.
    func testDoneScopeChangedDoesNotSettleOnAnUnchangedScope() {
        let token = String(repeating: "a", count: 64)
        let effect = ReceiptEffect.doneScopeChanged(count: 2, token: token)
        XCTAssertFalse(ReceiptLedger.landed(
            effect, in: snapshot(board: doneBoard(count: 2, token: token))))
    }

    /// Equal count, different membership: the token exists to catch this.
    func testDoneScopeChangedSettlesWhenTheTokenMovesAtEqualCount() {
        let before = String(repeating: "a", count: 64)
        let after = String(repeating: "b", count: 64)
        let effect = ReceiptEffect.doneScopeChanged(count: 2, token: before)
        XCTAssertTrue(ReceiptLedger.landed(
            effect, in: snapshot(board: doneBoard(count: 2, token: after))))
    }

    func testDoneScopeChangedSettlesWhenTheCountMoves() {
        let token = String(repeating: "a", count: 64)
        let effect = ReceiptEffect.doneScopeChanged(count: 2, token: token)
        XCTAssertTrue(ReceiptLedger.landed(
            effect, in: snapshot(board: doneBoard(count: 0, token: ""))))
    }

    /// The whole point of `stuck`: an accepted press whose effect never
    /// arrives **stays listed**.
    func testAnAcceptedPressPastTheDeadlineGoesStuckAndStaysListed() {
        let ledger = ReceiptLedger()
        let opened = ledger.open(action: PhoneActions.boardDelete,
                                 scope: "card", fields: ["card_id": "a"],
                                 effect: .cardGone(cardId: "a"),
                                 pairingToken: "p")
        ledger.accept(opened.id)
        XCTAssertEqual(ledger.outstanding.count, 1)

        // Still listed on the board, and long past the deadline.
        let listed = snapshot(board: board([card("a", revision: 0)]))
        ledger.settled(against: listed)
        XCTAssertEqual(ledger.receipts.first?.state, .accepted)
        XCTAssertEqual(ledger.outstanding.count, 1)
    }

    /// A press whose effect shows is done, and leaves the list.
    func testASeenEffectClosesTheReceipt() {
        let ledger = ReceiptLedger()
        let opened = ledger.open(action: PhoneActions.boardDelete,
                                 scope: "card", fields: ["card_id": "a"],
                                 effect: .cardGone(cardId: "a"),
                                 pairingToken: "p")
        ledger.accept(opened.id)
        ledger.settled(against: snapshot(board: board([])))
        XCTAssertEqual(ledger.receipts.first?.state, .done)
        XCTAssertTrue(ledger.outstanding.isEmpty)
    }

    /// A transport failure leaves the record in `sent` — the press may have
    /// landed with its reply lost — and it is the only state that re-sends.
    func testOnlySentIsResendable() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .none, pairingToken: "p")
        XCTAssertEqual(ledger.resendable(pairingToken: "p").map(\.id), [one.id])
        ledger.accept(one.id)
        XCTAssertTrue(ledger.resendable(pairingToken: "p").isEmpty)
    }

    /// A press minted under another pairing is never replayed against this
    /// Mac.
    func testAnotherPairingsPressIsStuckAndNeverResent() {
        let ledger = ReceiptLedger()
        _ = ledger.open(action: "a", scope: "s", fields: [:],
                        effect: .cardGone(cardId: "a"), pairingToken: "old")
        ledger.rebase(to: "new")
        XCTAssertEqual(ledger.receipts.first?.state, .stuck)
        XCTAssertEqual(ledger.receipts.first?.lastError,
                       ReceiptLedger.otherPairingLine)
        XCTAssertTrue(ledger.resendable(pairingToken: "new").isEmpty)
    }

    /// RETRY mints a fresh token, which is what stops the Mac replaying the
    /// refusal it recorded against the old one.
    func testRetryMintsAFreshToken() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .none, pairingToken: "p")
        ledger.refuse(one.id, detail: "no")
        ledger.retry(one.id)
        XCTAssertNotEqual(ledger.receipts.first?.id, one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .sent)
    }

    /// A save's effect is written from the guard it sent, so the two can
    /// never disagree about which number proves the write.
    func testASaveIsJudgedByTheRevisionItGuardedAgainst() {
        let effect = ReceiptLedger.effect(
            for: PhoneActions.boardUpdate,
            fields: ["card_id": "a", "expected_revision": "7"],
            scope: "a")
        XCTAssertEqual(effect, .cardRevision(cardId: "a", atLeast: 8))
    }

    // MARK: - the resend ceiling (2026-09-06 bug audit)

    /// A press that has run out of tries stops being sent and **stays
    /// listed**. Without the ceiling a refused press was replayed on every
    /// poll for ever — and, away, raised an unprompted Face ID sheet each
    /// time.
    func testAPressPastTheCeilingIsNotResendableAndSaysSo() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .cardGone(cardId: "a"),
                              pairingToken: "p")
        for _ in 0..<ReceiptLedger.maxAttempts { ledger.noteAttempt(one.id) }
        XCTAssertTrue(ledger.resendable(pairingToken: "p").isEmpty)
        XCTAssertEqual(ledger.exhausted(pairingToken: "p").map(\.id), [one.id])
        // And `exhausted` uses its parameter, rather than looking like a
        // guard and not being one.
        XCTAssertTrue(ledger.exhausted(pairingToken: "other").isEmpty)
        ledger.gaveUp(one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .stuck)
        XCTAssertEqual(ledger.receipts.first?.lastError,
                       ReceiptLedger.gaveUpLine)
        XCTAssertEqual(ledger.outstanding.count, 1)
    }

    /// A person's RETRY is a new press, so the ceiling resets with the mark.
    func testRetryResetsTheCeiling() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .none, pairingToken: "p")
        for _ in 0..<ReceiptLedger.maxAttempts { ledger.noteAttempt(one.id) }
        ledger.gaveUp(one.id)
        ledger.retry(ledger.receipts[0].id)
        XCTAssertEqual(ledger.receipts.first?.attempts, 0)
        XCTAssertEqual(ledger.receipts.first?.state, .sent)
        XCTAssertEqual(ledger.resendable(pairingToken: "p").count, 1)
    }

    /// A refusal the person read on the screen they pressed from is
    /// terminal — never re-sent — and **not listed**: PENDING exists so a
    /// press nobody has heard about is visible, and every plan-gate refusal
    /// sitting there for a day is how the row that matters gets lost.
    func testASurfacedRefusalIsTerminalAndNotListed() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .none, pairingToken: "p")
        ledger.refuse(one.id, detail: "this card has no plan yet",
                      surfaced: true)
        XCTAssertEqual(ledger.receipts.first?.state, .refused)
        XCTAssertTrue(ledger.resendable(pairingToken: "p").isEmpty)
        XCTAssertTrue(ledger.outstanding.isEmpty)
    }

    /// A **resend** had no screen behind it, so nobody saw that answer: it
    /// stays listed, carrying the Mac's own words.
    func testAnUnsurfacedRefusalStaysListedWithTheMacsWords() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .none, pairingToken: "p")
        ledger.refuse(one.id, detail: "away access has lapsed")
        XCTAssertEqual(ledger.receipts.first?.state, .stuck)
        XCTAssertTrue(ledger.resendable(pairingToken: "p").isEmpty)
        XCTAssertEqual(ledger.outstanding.count, 1)
        XCTAssertEqual(ledger.receipts.first?.line, "away access has lapsed")
    }

    /// `.cardRevision` is **not** evidence that this press landed — only
    /// that some write did. A save that transport-failed still holds the
    /// person's typed words, and the agent's own `dark_army_close_card` moves the
    /// number past `atLeast`; dropping the receipt there would throw those
    /// words away with no line in PENDING, where re-sending gets a 409 that
    /// keeps them and shows what moved underneath.
    func testACardRevisionIsNeverEvidenceBeforeSending() {
        let after = snapshot(board: board([card("a", revision: 9)]))
        let effect = ReceiptEffect.cardRevision(cardId: "a", atLeast: 5)
        XCTAssertTrue(ReceiptLedger.landed(effect, in: after))
        XCTAssertFalse(
            ReceiptLedger.evidenceBeforeSending(effect, in: after))
        // The goal-shaped effects still are.
        XCTAssertTrue(ReceiptLedger.evidenceBeforeSending(
            .cardGone(cardId: "gone"), in: after))
        XCTAssertTrue(ReceiptLedger.evidenceBeforeSending(
            .cardColumn(cardId: "a", column: "backlog"), in: after))
        // And `.none` describes nothing, so it can never be inferred either.
        XCTAssertFalse(ReceiptLedger.evidenceBeforeSending(.none, in: after))
    }

    /// `.doneScopeChanged` is the same shape as `.cardRevision`: any other
    /// Done arrival/departure moves the token, and dropping the receipt
    /// would forget a sweep that never ran; re-sending 409s with the Mac's
    /// words. After HTTP 200, `landed` still treats any scope change as
    /// fair evidence.
    func testADoneScopeChangeIsNeverEvidenceBeforeSending() {
        let before = String(repeating: "a", count: 64)
        let after = String(repeating: "b", count: 64)
        let effect = ReceiptEffect.doneScopeChanged(count: 2, token: before)
        let moved = snapshot(board: doneBoard(count: 2, token: after))
        XCTAssertTrue(ReceiptLedger.landed(effect, in: moved))
        XCTAssertFalse(
            ReceiptLedger.evidenceBeforeSending(effect, in: moved))
    }

    /// The two give-up routes are different facts and must not share one
    /// sentence: a press tried once whose replay window closed would
    /// otherwise wear a count that never happened.
    func testTheTwoGiveUpRoutesSayDifferentThings() {
        let ledger = ReceiptLedger()
        let one = ledger.open(action: "a", scope: "s", fields: [:],
                              effect: .none, pairingToken: "p")
        ledger.gaveUp(one.id, why: ReceiptLedger.tooOldLine)
        XCTAssertEqual(ledger.receipts.first?.lastError,
                       ReceiptLedger.tooOldLine)
        XCTAssertNotEqual(ReceiptLedger.tooOldLine, ReceiptLedger.gaveUpLine)
        XCTAssertEqual(ledger.outstanding.count, 1)
    }

    /// Finished business is bounded **in session**, not only at `load()`;
    /// `.stuck` never is, because it is the press nobody has been told
    /// about and the machine dropping it is the disappearance the state
    /// exists to prevent.
    func testFinishedRecordsAreCappedAndStuckOnesAreNot() {
        let ledger = ReceiptLedger()
        for _ in 0..<(ReceiptLedger.cap + 20) {
            let one = ledger.open(action: "a", scope: "s", fields: [:],
                                  effect: .none, pairingToken: "p")
            ledger.refuse(one.id, detail: "no", surfaced: true)
        }
        XCTAssertLessThanOrEqual(ledger.receipts.count, ReceiptLedger.cap)
        XCTAssertTrue(ledger.outstanding.isEmpty)

        let stuck = ledger.open(action: "b", scope: "s", fields: [:],
                                effect: .none, pairingToken: "p")
        ledger.gaveUp(stuck.id)
        for _ in 0..<(ReceiptLedger.cap + 20) {
            let one = ledger.open(action: "a", scope: "s", fields: [:],
                                  effect: .none, pairingToken: "p")
            ledger.refuse(one.id, detail: "no", surfaced: true)
        }
        XCTAssertEqual(ledger.outstanding.map(\.id), [stuck.id])
    }

    // MARK: - the queue's arithmetic (a press is queued, not awaited)

    private func queued(_ id: String, scope: String, state: Receipt.State = .queued,
                        at created: Double, action: String = "x",
                        heldUntil: Double = 0, attempts: Int = 0,
                        pairing: String = "p", error: String = "") -> Receipt {
        var out = Receipt(id: id, action: action, scope: scope, fields: [:],
                          effect: .none, pairingToken: pairing)
        out.state = state
        out.createdAt = created
        out.heldUntil = heldUntil
        out.attempts = attempts
        out.lastError = error
        return out
    }

    /// (1) The oldest queued press goes first, whatever its scope.
    func testNextSendableIsCreationOrderAcrossScopes() {
        let rows = [queued("later", scope: "b", at: 20),
                    queued("first", scope: "a", at: 10),
                    queued("third", scope: "c", at: 30)]
        XCTAssertEqual(ReceiptLedger.nextSendable(in: rows, pairingToken: "p",
                                                  now: 1000)?.id, "first")
    }

    /// (2) Approve then Start on one card: the start waits behind an
    /// approval still in `sent` under its backoff, and another card's press
    /// goes instead.
    func testAScopeWaitsForItsOwnEarlierPress() {
        let held = queued("approve", scope: "a", state: .sent, at: 10,
                          heldUntil: 1500, attempts: 1)
        let start = queued("start", scope: "a", at: 20)
        let other = queued("other", scope: "b", at: 30)
        XCTAssertEqual(ReceiptLedger.nextSendable(in: [held, start, other],
                                                  pairingToken: "p", now: 1000)?.id,
                       "other")
        XCTAssertNil(ReceiptLedger.nextSendable(in: [held, start],
                                                pairingToken: "p", now: 1000))
        // Past the backoff the held one goes first, then the start.
        XCTAssertEqual(ReceiptLedger.nextSendable(in: [held, start],
                                                  pairingToken: "p", now: 2000)?.id,
                       "approve")
    }

    /// (3) An earlier refusal is finished business: the later press goes.
    func testAnEarlierStuckPressReleasesTheLaterOne() {
        let refused = queued("approve", scope: "a", state: .stuck, at: 10,
                             error: "this card has no plan yet")
        let start = queued("start", scope: "a", at: 20)
        XCTAssertEqual(ReceiptLedger.nextSendable(in: [refused, start],
                                                  pairingToken: "p", now: 1000)?.id,
                       "start")
    }

    /// A transmit in flight is skipped, and it holds its scope.
    func testASendingPressHoldsItsScope() {
        let out = queued("out", scope: "a", state: .sending, at: 10)
        let next = queued("next", scope: "a", at: 20)
        let elsewhere = queued("elsewhere", scope: "b", at: 30)
        XCTAssertEqual(ReceiptLedger.nextSendable(in: [out, next, elsewhere],
                                                  pairingToken: "p", now: 1000)?.id,
                       "elsewhere")
    }

    /// (4) The same scope and verb in `.sending` is a duplicate; after a
    /// refusal it is a deliberate second press.
    func testDuplicateIsTheSameScopeAndVerbWhileUnresolved() {
        for state in [Receipt.State.sending, .queued, .sent] {
            let rows = [queued("one", scope: "a", state: state, at: 10,
                               action: PhoneActions.boardDispatch)]
            XCTAssertTrue(ReceiptLedger.duplicate(action: PhoneActions.boardDispatch,
                                                  scope: "a", fields: [:], in: rows), "\(state)")
        }
        for state in [Receipt.State.refused, .done, .stuck, .accepted] {
            let rows = [queued("one", scope: "a", state: state, at: 10,
                               action: PhoneActions.boardDispatch)]
            XCTAssertFalse(ReceiptLedger.duplicate(action: PhoneActions.boardDispatch,
                                                   scope: "a", fields: [:], in: rows), "\(state)")
        }
        // A different verb on the same subject queues behind it.
        let rows = [queued("one", scope: "a", at: 10,
                           action: PhoneActions.boardApprovePlan)]
        XCTAssertFalse(ReceiptLedger.duplicate(action: PhoneActions.boardDispatch,
                                               scope: "a", fields: [:], in: rows))
    }

    /// The answer verbs stay a duplicate while they land: the Mac's 200
    /// leaves the question listed for a poll cadence, and a second copy
    /// would be typed again. Every other verb is released on the 200.
    func testAnAnswerStaysADuplicateWhileItLands() {
        for action in [PhoneActions.answerQuestion, PhoneActions.answerQuestions,
                       PhoneActions.reply] {
            XCTAssertTrue(ReceiptLedger.duplicate(
                action: action, scope: "s", fields: [:],
                in: [queued("one", scope: "s", state: .accepted, at: 10, action: action)]),
                action)
            XCTAssertFalse(ReceiptLedger.duplicate(
                action: action, scope: "s", fields: [:],
                in: [queued("one", scope: "s", state: .done, at: 10, action: action)]),
                action)
        }
        XCTAssertFalse(ReceiptLedger.duplicate(
            action: PhoneActions.boardDispatch, scope: "s", fields: [:],
            in: [queued("one", scope: "s", state: .accepted, at: 10,
                        action: PhoneActions.boardDispatch)]))
    }

    /// An answer is judged by the question leaving the row, so the 200
    /// closes it `.accepted` (mark SENT) rather than `.done`; a reply is
    /// `.none` in the table and upgraded by the client on a waiting row.
    func testTheAnswerVerbsAreJudgedByTheQuestionLeavingTheRow() {
        let fields = ["session_id": "s1", "question_id": "q1", "option_index": "0"]
        for action in [PhoneActions.answerQuestion, PhoneActions.answerQuestions] {
            XCTAssertEqual(ReceiptLedger.effect(for: action, fields: fields, scope: "s1"),
                           .questionGone(sessionId: "s1", questionId: "q1"), action)
            XCTAssertEqual(ReceiptLedger.effect(for: action, fields: ["session_id": "s1"],
                                                scope: "s1"),
                           .none, "no question id, nothing to hold against")
        }
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.reply,
                                            fields: ["session_id": "s1", "text": "hi"],
                                            scope: "s1"), .none)
        let ledger = ReceiptLedger()
        ledger.forget()
        let one = ledger.enqueue(action: PhoneActions.answerQuestion, scope: "s1",
                                 fields: fields,
                                 effect: .questionGone(sessionId: "s1", questionId: "q1"),
                                 pairingToken: "p", authorised: false)
        ledger.markSending(one.id)
        ledger.accept(one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .accepted)
        XCTAssertEqual(ReceiptLedger.mark(for: "s1", in: ledger.receipts), "SENT")
        XCTAssertTrue(ReceiptLedger.duplicate(action: PhoneActions.answerQuestion,
                                              scope: "s1", fields: fields, in: ledger.receipts),
                      "the same question, answered again, is the duplicate")
        XCTAssertFalse(ReceiptLedger.duplicate(action: PhoneActions.answerQuestion,
                                               scope: "s1", fields: ["question_id": "q2"],
                                               in: ledger.receipts),
                       "an answer to another question on the same row is a new press")
        ledger.forget()
    }

    /// A scope the sender is told to hold — its synchronous press holds
    /// `post`'s lock — is walked past like one with a press in flight, and
    /// the press behind it on the same scope waits too.
    func testAHeldScopeIsSkippedAndItsLaterPressesWait() {
        let rows = [queued("first", scope: "a", at: 10),
                    queued("second", scope: "a", at: 20),
                    queued("other", scope: "b", at: 30)]
        XCTAssertEqual(ReceiptLedger.nextSendable(in: rows, pairingToken: "p", now: 1000,
                                                  holding: ["a"])?.id, "other")
        XCTAssertNil(ReceiptLedger.nextSendable(in: Array(rows.prefix(2)),
                                                pairingToken: "p", now: 1000,
                                                holding: ["a"]))
        XCTAssertEqual(ReceiptLedger.nextSendable(in: rows, pairingToken: "p",
                                                  now: 1000)?.id, "first")
    }

    /// (5) The mark reads QUEUED / SENDING… / SENT, the newest press wins,
    /// and a closed record wears none.
    func testMarkReadsTheThreeWords() {
        XCTAssertEqual(ReceiptLedger.mark(for: "s", in: [queued("q", scope: "s", at: 1)]),
                       "QUEUED")
        XCTAssertEqual(ReceiptLedger.mark(for: "s", in: [queued("q", scope: "s",
                                                                state: .sending, at: 1)]),
                       "SENDING…")
        XCTAssertEqual(ReceiptLedger.mark(for: "s", in: [queued("q", scope: "s",
                                                                state: .sent, at: 1)]),
                       "SENT")
        XCTAssertEqual(ReceiptLedger.mark(for: "s", in: [queued("q", scope: "s",
                                                                state: .accepted, at: 1)]),
                       "SENT")
        XCTAssertNil(ReceiptLedger.mark(for: "s", in: [queued("q", scope: "s",
                                                              state: .stuck, at: 1)]))
        XCTAssertEqual(ReceiptLedger.mark(for: "s", in: [
            queued("old", scope: "s", state: .sent, at: 10),
            queued("new", scope: "s", at: 20)]), "QUEUED")
        XCTAssertNil(ReceiptLedger.mark(for: "other", in: [queued("q", scope: "s", at: 1)]))
    }

    /// The note is the Mac's words until a fresh press is queued behind it.
    func testNoteIsTheMacsWordsUntilANewerPressIsQueued() {
        let refused = queued("one", scope: "s", state: .stuck, at: 10,
                             action: PhoneActions.boardDispatch,
                             error: "this card has no plan yet")
        let note = ReceiptLedger.note(for: "s", in: [refused], now: 100)
        XCTAssertEqual(note?.text, "this card has no plan yet")
        XCTAssertEqual(note?.id, "one")
        XCTAssertEqual(note?.action, PhoneActions.boardDispatch)
        XCTAssertNil(ReceiptLedger.note(for: "s", in: [refused,
                                                       queued("two", scope: "s", at: 20)],
                                        now: 100))
    }

    /// A later press on the scope the Mac accepted, or that landed, is the
    /// person having moved on: the older refusal never comes back once
    /// that press closes. Only a newer refusal is drawn.
    func testASupersededRefusalNeverComesBack() {
        let refused = queued("one", scope: "s", state: .stuck, at: 10,
                             action: PhoneActions.boardDispatch,
                             error: "this card has no plan yet")
        for state in [Receipt.State.accepted, .done, .refused, .sent] {
            XCTAssertNil(ReceiptLedger.note(for: "s", in: [
                refused, queued("two", scope: "s", state: state, at: 20)], now: 100),
                         "\(state)")
        }
        let later = ReceiptLedger.note(for: "s", in: [
            queued("one", scope: "s", state: .done, at: 10),
            queued("two", scope: "s", state: .stuck, at: 20,
                   error: "away access has lapsed")], now: 100)
        XCTAssertEqual(later?.text, "away access has lapsed")
    }

    /// The note is bounded like the QUEUE list's refusal: a record older
    /// than `refusedShown` is not the note — every screen re-reads it on
    /// appearance, and a Start refused hours ago would otherwise re-arm
    /// "Start unplanned?" on a card that has a plan by now — and a
    /// `.stuck` record wearing one of the phone's own sentences never is:
    /// nobody refused it, and the QUEUE list is where its RETRY lives.
    func testANoteAgesOffAndIsNeverThePhonesOwnStop() {
        let refused = queued("one", scope: "s", state: .refused, at: 100,
                             action: PhoneActions.boardDispatch,
                             error: "this card has no plan yet")
        XCTAssertEqual(ReceiptLedger.note(for: "s", in: [refused], now: 100 + 599)?.text,
                       "this card has no plan yet")
        XCTAssertNil(ReceiptLedger.note(for: "s", in: [refused], now: 100 + 700),
                     "700 s old: the note is gone, the QUEUE list has aged it too")
        for line in [ReceiptLedger.stuckLine, ReceiptLedger.gaveUpLine,
                     ReceiptLedger.tooOldLine, ReceiptLedger.otherPairingLine,
                     ReceiptLedger.faceNeededLine] {
            XCTAssertNil(ReceiptLedger.note(for: "s", in: [
                queued("one", scope: "s", state: .stuck, at: 100,
                       action: PhoneActions.answerQuestion, error: line)], now: 110),
                         line)
        }
        // The Mac's own words on a `.stuck` record stay the note.
        XCTAssertEqual(ReceiptLedger.note(for: "s", in: [
            queued("one", scope: "s", state: .stuck, at: 100,
                   error: "away access has lapsed")], now: 110)?.text,
                       "away access has lapsed")
        // The split the agent screen's two readers use.
        XCTAssertTrue(QueueNote(id: "a", action: PhoneActions.answerQuestion,
                                text: "x", refused: true).isAnswer)
        XCTAssertTrue(QueueNote(id: "a", action: PhoneActions.reply,
                                text: "x", refused: true).isAnswer)
        XCTAssertFalse(QueueNote(id: "a", action: PhoneActions.stopSession,
                                 text: "x", refused: true).isAnswer)
    }

    /// RETRY is a new press and is asked for the face like one: the pass
    /// remembered at the original tap goes with the token, so a re-send
    /// made from away goes back to the sheet rather than riding a check
    /// that covered a different press hours ago.
    func testRetryDropsThePassWithTheToken() {
        let ledger = ReceiptLedger()
        defer { ledger.forget() }
        let one = ledger.enqueue(action: "x", scope: "s", fields: [:], effect: .none,
                                 pairingToken: "p", authorised: true)
        ledger.markSending(one.id)
        ledger.refuse(one.id, detail: ReceiptLedger.faceNeededLine, surfaced: false)
        XCTAssertEqual(ledger.receipts.first?.authorised, true)
        ledger.retry(one.id)
        XCTAssertNotEqual(ledger.receipts.first?.id, one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .sent)
        XCTAssertEqual(ledger.receipts.first?.authorised, false)
    }

    /// A read that changes nothing — the record is not `.stuck`, or wears
    /// the phone's own sentence — neither publishes nor rewrites the file.
    func testAReadThatChangesNothingWritesNothing() {
        let ledger = ReceiptLedger()
        ledger.forget()
        defer { ledger.forget() }
        let one = ledger.enqueue(action: "x", scope: "s", fields: [:], effect: .none,
                                 pairingToken: "p", authorised: false)
        var published = 0
        let sink = ledger.objectWillChange.sink { _ in published += 1 }
        defer { sink.cancel() }
        ledger.read(one.id)
        XCTAssertEqual(published, 0, "a queued record is not read")
        ledger.markSending(one.id)
        ledger.refuse(one.id, detail: ReceiptLedger.faceNeededLine, surfaced: false)
        let before = published
        ledger.read(one.id)
        XCTAssertEqual(published, before, "a phone-authored stop is left alone")
        ledger.refuse(one.id, detail: "away access has lapsed", surfaced: false)
        let drawn = published
        ledger.read(one.id)
        XCTAssertEqual(published, drawn + 1)
        XCTAssertEqual(ledger.receipts.first?.state, .refused)
    }

    /// The mark as a screen reader says it: QUEUED is "Queued", never
    /// "Sending".
    func testTheMarkIsSpokenAsDrawn() {
        XCTAssertEqual(Receipt.spoken(mark: "QUEUED"), "Queued")
        XCTAssertEqual(Receipt.spoken(mark: "SENDING…"), "Sending")
        XCTAssertEqual(Receipt.spoken(mark: "SENT"), "Sent")
        XCTAssertEqual(Receipt.spoken(mark: "other"), "other")
    }

    /// RETRY re-mints a token only on a press that has gone and answered
    /// or stalled: a queued press has not gone, and a press going out right
    /// now would be sent twice. A declined Face ID sheet on a replay is the
    /// phone's own stop, drawn STUCK with RETRY.
    func testRetryLeavesAQueuedOrSendingPressAlone() {
        let ledger = ReceiptLedger()
        ledger.forget()
        let one = ledger.enqueue(action: "x", scope: "s", fields: [:], effect: .none,
                                 pairingToken: "p", authorised: false)
        ledger.retry(one.id)
        XCTAssertEqual(ledger.receipts.first?.id, one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .queued)
        ledger.markSending(one.id)
        ledger.retry(one.id)
        XCTAssertEqual(ledger.receipts.first?.id, one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .sending)
        ledger.refuse(one.id, detail: ReceiptLedger.faceNeededLine, surfaced: false)
        XCTAssertEqual(ledger.receipts.first?.state, .stuck)
        XCTAssertEqual(ledger.receipts.first?.statusWord, "STUCK")
        ledger.retry(one.id)
        XCTAssertNotEqual(ledger.receipts.first?.id, one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .sent)
        ledger.forget()
    }

    /// (6) The QUEUE list is oldest first and lists a fresh refusal, not a
    /// stale one and not a finished press.
    func testQueueRowsAreOldestFirstAndListAFreshRefusal() {
        let rows = [queued("c", scope: "s", state: .sent, at: 30),
                    queued("a", scope: "s", at: 10),
                    queued("done", scope: "s", state: .done, at: 15),
                    queued("fresh", scope: "s", state: .refused, at: 900),
                    queued("old", scope: "s", state: .refused, at: 100),
                    queued("b", scope: "s", state: .stuck, at: 20)]
        XCTAssertEqual(ReceiptLedger.queueRows(in: rows, now: 1000).map(\.id),
                       ["a", "b", "c", "fresh"])
    }

    /// The status word tells a refusal in the Mac's words from a stop the
    /// phone wrote itself.
    func testStatusWordsTellARefusalFromAPhoneSideStop() {
        XCTAssertEqual(queued("q", scope: "s", at: 1).statusWord, "QUEUED")
        XCTAssertEqual(queued("q", scope: "s", state: .sending, at: 1).statusWord, "SENDING…")
        XCTAssertEqual(queued("q", scope: "s", state: .sent, at: 1).statusWord, "SENT")
        XCTAssertEqual(queued("q", scope: "s", state: .accepted, at: 1).statusWord, "LANDING")
        XCTAssertEqual(queued("q", scope: "s", state: .done, at: 1).statusWord, "DONE")
        XCTAssertEqual(queued("q", scope: "s", state: .refused, at: 1).statusWord, "REFUSED")
        XCTAssertEqual(queued("q", scope: "s", state: .stuck, at: 1,
                              error: "away access has lapsed").statusWord, "REFUSED")
        XCTAssertEqual(queued("q", scope: "s", state: .stuck, at: 1,
                              error: ReceiptLedger.gaveUpLine).statusWord, "STUCK")
    }

    /// `enqueue` writes the record in `.queued` with no attempt spent and
    /// the face check remembered; `markSending` counts the attempt; a
    /// `holdOff` on a transmit that ended without an answer goes back to
    /// `sent`, which is the one state the sender re-takes.
    func testEnqueueMarkSendingAndHoldOffMoveTheStates() {
        let ledger = ReceiptLedger()
        defer { ledger.forget() }
        let one = ledger.enqueue(action: PhoneActions.boardDispatch, scope: "a",
                                 fields: ["card_id": "a"],
                                 effect: .cardLinked(cardId: "a"),
                                 pairingToken: "p", authorised: true)
        XCTAssertEqual(one.state, .queued)
        XCTAssertEqual(one.attempts, 0)
        XCTAssertTrue(one.authorised)
        XCTAssertTrue(ledger.resendable(pairingToken: "p").isEmpty,
                      "a queued press is the sender's, never the old replay's")
        ledger.markSending(one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .sending)
        XCTAssertEqual(ledger.receipts.first?.attempts, 1)
        ledger.holdOff(one.id)
        XCTAssertEqual(ledger.receipts.first?.state, .sent)
        XCTAssertGreaterThan(ledger.receipts.first?.heldUntil ?? 0, 0)
    }

    /// A persisted `.sending` reads back as `.sent`: an interrupted transmit
    /// may have landed with its reply lost, which is what the token replay
    /// is for.
    func testLoadReadsAnInterruptedTransmitAsSent() {
        let ledger = ReceiptLedger()
        defer { ledger.forget() }
        let one = ledger.enqueue(action: "a", scope: "s", fields: [:],
                                 effect: .none, pairingToken: "p", authorised: false)
        ledger.markSending(one.id)
        let again = ReceiptLedger()
        again.load()
        XCTAssertEqual(again.receipts.first { $0.id == one.id }?.state, .sent)
        XCTAssertEqual(again.receipts.first { $0.id == one.id }?.authorised, false)
    }

    /// The two new effects: a verdict lands when the prompt is no longer
    /// listed for that session; an approval when the card carries one.
    func testPermissionGoneAndPlanApprovedLand() {
        var prompt = PermissionPrompt()
        prompt.requestId = "r1"
        prompt.sessionId = "s1"
        var listed = snapshot(board: board([]))
        listed.permissions = [prompt]
        let verdict = ReceiptEffect.permissionGone(sessionId: "s1", requestId: "r1")
        XCTAssertFalse(ReceiptLedger.landed(verdict, in: listed))
        XCTAssertTrue(ReceiptLedger.landed(verdict, in: snapshot(board: board([]))))
        // Another session's prompt with the same id is not this one.
        prompt.sessionId = "s2"
        listed.permissions = [prompt]
        XCTAssertTrue(ReceiptLedger.landed(verdict, in: listed))

        let approval = ReceiptEffect.planApproved(cardId: "a", digest: "abc")
        XCTAssertFalse(ReceiptLedger.landed(approval, in: snapshot(board: board([card("a", revision: 0)]))))
        let approved = try! JSONDecoder().decode(BoardCard.self, from: Data("""
        {"id": "a", "revision": 1, "column_name": "backlog", "plan_approved": "abc"}
        """.utf8))
        XCTAssertTrue(ReceiptLedger.landed(approval, in: snapshot(board: board([approved]))))
        XCTAssertFalse(ReceiptLedger.landed(approval, in: snapshot(board: board([]))))
        // Both are goal-achieved effects: evidence before sending.
        XCTAssertTrue(ReceiptLedger.evidenceBeforeSending(approval, in: snapshot(board: board([approved]))))
        // And the table maps the two verbs to them, the digest riding along.
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.permissionVerdict,
                                            fields: ["request_id": "r1"], scope: "s1"),
                       verdict)
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.boardApprovePlan,
                                            fields: ["card_id": "a", "plan_digest": "abc"],
                                            scope: "a"),
                       approval)
    }

    /// The card screen's two one-field toggles and Refine used to settle
    /// as `.none` on the Mac's 200, so the pressed control snapped back
    /// to the card's *old* value until the next board frame — from away,
    /// a minute later — and the press read as "nothing happened" (21 Sep
    /// 2026). Each is judged by the card showing what was asked for.
    func testToolModelAndRefineAreJudgedByTheCard() {
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.boardUpdate,
                                            fields: ["card_id": "a", "tool": "codex"],
                                            scope: "a"),
                       .cardTool(cardId: "a", tool: "codex"))
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.boardUpdate,
                                            fields: ["card_id": "a", "model": ""],
                                            scope: "a"),
                       .cardModel(cardId: "a", model: ""))
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.boardRefine,
                                            fields: ["card_id": "a"], scope: "a"),
                       .cardRefining(cardId: "a"))
        // A guarded Save still settles on the revision, tool field or not.
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.boardUpdate,
                                            fields: ["card_id": "a", "column_name": "backlog",
                                                     "tool": "codex"],
                                            scope: "a"),
                       .cardColumn(cardId: "a", column: "backlog"))

        let before = try! JSONDecoder().decode(BoardCard.self, from: Data("""
        {"id": "a", "revision": 1, "column_name": "prep", "tool": "claude", "model": "opus"}
        """.utf8))
        let tool = ReceiptEffect.cardTool(cardId: "a", tool: "codex")
        let model = ReceiptEffect.cardModel(cardId: "a", model: "")
        let refine = ReceiptEffect.cardRefining(cardId: "a")
        for effect in [tool, model, refine] {
            XCTAssertFalse(ReceiptLedger.landed(effect, in: snapshot(board: board([before]))))
            // A card the board no longer lists proves nothing.
            XCTAssertFalse(ReceiptLedger.landed(effect, in: snapshot(board: board([]))))
        }
        let switched = try! JSONDecoder().decode(BoardCard.self, from: Data("""
        {"id": "a", "revision": 2, "column_name": "prep", "tool": "codex", "model": ""}
        """.utf8))
        XCTAssertTrue(ReceiptLedger.landed(tool, in: snapshot(board: board([switched]))))
        XCTAssertTrue(ReceiptLedger.landed(model, in: snapshot(board: board([switched]))))
        XCTAssertFalse(ReceiptLedger.landed(refine, in: snapshot(board: board([switched]))))
        // Refine lands on any sign of a planner: its state, its session,
        // a plan already attached, or the card out of Prep.
        for json in [
            #"{"id": "a", "column_name": "prep", "refine_state": "dispatching"}"#,
            #"{"id": "a", "column_name": "prep", "refine_session_id": "s9"}"#,
            #"{"id": "a", "column_name": "prep", "plan_path": "plans/x.md"}"#,
            #"{"id": "a", "column_name": "backlog"}"#,
        ] {
            let card = try! JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
            XCTAssertTrue(ReceiptLedger.landed(refine, in: snapshot(board: board([card]))), json)
        }
        // All three are goal-achieved effects: evidence before sending.
        XCTAssertTrue(ReceiptLedger.evidenceBeforeSending(tool, in: snapshot(board: board([switched]))))
        XCTAssertFalse(ReceiptLedger.evidenceBeforeSending(tool, in: snapshot(board: board([before]))))
    }

    /// Approve on a *changed* plan: the card already carries the old
    /// digest, so "any approval" read as landed on the very snapshot that
    /// offered the button and the press was removed unsent (the audit's
    /// blocker, 20 Sep 2026). The effect carries the digest it asked for.
    func testApproveOnAChangedPlanIsNotAlreadyLanded() {
        let stale = try! JSONDecoder().decode(BoardCard.self, from: Data("""
        {"id": "a", "revision": 1, "column_name": "backlog", "plan_approved": "old"}
        """.utf8))
        let fresh = ReceiptEffect.planApproved(cardId: "a", digest: "new")
        XCTAssertFalse(ReceiptLedger.landed(fresh, in: snapshot(board: board([stale]))))
        XCTAssertFalse(ReceiptLedger.evidenceBeforeSending(fresh, in: snapshot(board: board([stale]))))
        let approved = try! JSONDecoder().decode(BoardCard.self, from: Data("""
        {"id": "a", "revision": 2, "column_name": "backlog", "plan_approved": "new"}
        """.utf8))
        XCTAssertTrue(ReceiptLedger.landed(fresh, in: snapshot(board: board([approved]))))
        // An older caller with no digest: any approval, as before.
        XCTAssertTrue(ReceiptLedger.landed(.planApproved(cardId: "a", digest: ""),
                                           in: snapshot(board: board([stale]))))
    }

    /// Delete on an *abandoned* row: the row is out of the live buckets by
    /// definition, so `.rowGone` read "already happened" and
    /// `delete_abandoned_agent` was removed unsent (the audit's blocker).
    /// `.rowGoneAnywhere` lands only when the row is listed nowhere.
    func testDeleteOnAnAbandonedRowIsJudgedAgainstEveryList() {
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.deleteAgent,
                                            fields: ["session_id": "s1"], scope: "s1"),
                       .rowGoneAnywhere(sessionId: "s1"))
        XCTAssertEqual(ReceiptLedger.effect(for: PhoneActions.stopSession,
                                            fields: ["session_id": "s1"], scope: "s1"),
                       .rowGone(sessionId: "s1"))
        var listed = snapshot(board: board([]))
        listed.agents = try! JSONDecoder().decode(Agents.self, from: Data("""
        {"abandoned": [{"session_id": "s1"}]}
        """.utf8))
        XCTAssertTrue(ReceiptLedger.landed(.rowGone(sessionId: "s1"), in: listed),
                      "the old judge: already gone from the live buckets")
        XCTAssertFalse(ReceiptLedger.landed(.rowGoneAnywhere(sessionId: "s1"), in: listed))
        XCTAssertFalse(ReceiptLedger.evidenceBeforeSending(.rowGoneAnywhere(sessionId: "s1"),
                                                           in: listed))
        XCTAssertTrue(ReceiptLedger.landed(.rowGoneAnywhere(sessionId: "s1"),
                                           in: snapshot(board: board([]))))
    }

    /// A queued reply carries the person's words: `.replyHold` lands on
    /// the same evidence as `.questionGone` but is never evidence before
    /// sending — the row leaving `waiting` before the sender took it
    /// (the plan's own chain: an answer, then a follow-up) is no reason
    /// to drop it unsent (the audit's blocker). And a row that keeps
    /// waiting on the same question closes it `.done` after `replyHold`,
    /// never `.stuck` at the effect deadline.
    func testAQueuedReplyIsNeverDroppedUnsentAndClosesAfterTheHold() {
        let gone = snapshot(board: board([]))
        let hold = ReceiptEffect.replyHold(sessionId: "s1", questionId: "q1")
        XCTAssertTrue(ReceiptLedger.landed(hold, in: gone))
        XCTAssertFalse(ReceiptLedger.evidenceBeforeSending(hold, in: gone))
        XCTAssertTrue(ReceiptLedger.evidenceBeforeSending(
            .questionGone(sessionId: "s1", questionId: "q1"), in: gone))
        var same = gone
        same.agents = try! JSONDecoder().decode(Agents.self, from: Data("""
        {"waiting": [{"session_id": "s1", "question": {"id": "q1"}}]}
        """.utf8))
        XCTAssertFalse(ReceiptLedger.landed(hold, in: same))

        let ledger = ReceiptLedger()
        ledger.forget()
        let reply = ledger.enqueue(action: PhoneActions.reply, scope: "s1",
                                   fields: ["session_id": "s1", "text": "hi"],
                                   effect: hold, pairingToken: "p", authorised: false)
        ledger.markSending(reply.id)
        ledger.accept(reply.id)
        XCTAssertEqual(ledger.receipts.first?.state, .accepted)
        ledger.settled(against: same)
        XCTAssertEqual(ledger.receipts.first?.state, .accepted, "held for the duplicate window")
        var expired = ledger.receipts.first!
        expired.acceptedAt = Date().timeIntervalSince1970 - ReceiptLedger.replyHold - 1
        XCTAssertTrue(ReceiptLedger.holdExpired(expired, now: Date().timeIntervalSince1970))
        var answer = expired
        answer.effect = .questionGone(sessionId: "s1", questionId: "q1")
        XCTAssertFalse(ReceiptLedger.holdExpired(answer, now: Date().timeIntervalSince1970),
                       "only a reply's hold expires; an answer waits for the evidence")
        ledger.forget()
    }

    /// The payload counts: a true double-tap carries identical fields and
    /// is refused, a genuinely different press on the same subject and verb
    /// queues behind the first. The answer verbs are keyed on
    /// `question_id` alone.
    func testDuplicateReadsThePayload() {
        func row(_ action: String, _ fields: [String: String]) -> Receipt {
            var out = queued("one", scope: "s", at: 10, action: action)
            out.fields = fields
            return out
        }
        let cases: [(String, [String: String], [String: String])] = [
            (PhoneActions.reply, ["session_id": "s", "text": "one"],
             ["session_id": "s", "text": "two"]),
            (PhoneActions.permissionVerdict, ["request_id": "r1", "behavior": "allow"],
             ["request_id": "r2", "behavior": "allow"]),
            (PhoneActions.boardUpdate, ["card_id": "c", "tool": "codex"],
             ["card_id": "c", "model": "x"]),
            (PhoneActions.boardQueueMove, ["card_id": "c", "before_id": "a"],
             ["card_id": "c", "before_id": "b"]),
            (PhoneActions.setBoardAutostart, ["enabled": "on"], ["enabled": "off"]),
        ]
        for (action, first, second) in cases {
            XCTAssertTrue(ReceiptLedger.duplicate(action: action, scope: "s", fields: first,
                                                  in: [row(action, first)]), action)
            XCTAssertFalse(ReceiptLedger.duplicate(action: action, scope: "s", fields: second,
                                                   in: [row(action, first)]), action)
        }
        for action in [PhoneActions.answerQuestion, PhoneActions.answerQuestions] {
            var held = row(action, ["question_id": "q1", "option_index": "0"])
            held.state = .accepted
            XCTAssertTrue(ReceiptLedger.duplicate(
                action: action, scope: "s",
                fields: ["question_id": "q1", "option_index": "1"], in: [held]), action)
            XCTAssertFalse(ReceiptLedger.duplicate(
                action: action, scope: "s",
                fields: ["question_id": "q2", "option_index": "0"], in: [held]), action)
        }
    }

    /// A drawn refusal is read: `.stuck` with the Mac's words goes
    /// `.refused` — listed for `refusedShown`, then aged like any finished
    /// business — and the record is **kept**, so it stays the subject's
    /// newest receipt and an older refusal never becomes the note. A
    /// phone-authored STUCK row is left alone.
    func testAReadRefusalAgesLikeFinishedBusinessAndStaysTheNewestNote() {
        let ledger = ReceiptLedger()
        ledger.forget()
        let older = ledger.enqueue(action: PhoneActions.boardDispatch, scope: "c",
                                   fields: [:], effect: .none, pairingToken: "p",
                                   authorised: false)
        ledger.refuse(older.id, detail: "older words", surfaced: false)
        let newer = ledger.enqueue(action: PhoneActions.boardDispatch, scope: "c",
                                   fields: ["skip_plan_gate": "true"], effect: .none,
                                   pairingToken: "p", authorised: false)
        ledger.refuse(newer.id, detail: "this card has no plan yet", surfaced: false)
        let now = Date().timeIntervalSince1970
        XCTAssertEqual(ReceiptLedger.note(for: "c", in: ledger.receipts, now: now)?.id,
                       newer.id)
        ledger.read(newer.id)
        XCTAssertEqual(ledger.receipts.last?.state, .refused)
        XCTAssertEqual(ledger.receipts.last?.statusWord, "REFUSED")
        XCTAssertEqual(ReceiptLedger.note(for: "c", in: ledger.receipts, now: now)?.id,
                       newer.id,
                       "read, kept, still the newest note — never the older words")
        XCTAssertTrue(ReceiptLedger.queueRows(in: ledger.receipts,
                                              now: Date().timeIntervalSince1970)
                        .contains { $0.id == newer.id })
        XCTAssertFalse(ReceiptLedger.queueRows(
            in: ledger.receipts,
            now: Date().timeIntervalSince1970 + ReceiptLedger.refusedShown + 1)
            .contains { $0.id == newer.id })
        let stuck = ledger.enqueue(action: PhoneActions.reply, scope: "s", fields: [:],
                                   effect: .none, pairingToken: "p", authorised: false)
        ledger.gaveUp(stuck.id)
        ledger.read(stuck.id)
        XCTAssertEqual(ledger.receipts.last?.state, .stuck, "nobody refused it")
        ledger.forget()
    }

    /// A poll that went through proves the transport back: every `.sent`
    /// press's backoff ends, so a fresh `.queued` press on the scope no
    /// longer waits up to `backoffCap` behind it.
    func testASuccessfulPollReleasesTheTransportBackoff() {
        let ledger = ReceiptLedger()
        ledger.forget()
        let held = ledger.open(action: PhoneActions.boardApprovePlan, scope: "c",
                               fields: [:], effect: .none, pairingToken: "p")
        ledger.holdOff(held.id)
        XCTAssertTrue(ledger.receipts.first!.heldUntil > Date().timeIntervalSince1970)
        let start = ledger.enqueue(action: PhoneActions.boardDispatch, scope: "c",
                                   fields: [:], effect: .none, pairingToken: "p",
                                   authorised: false)
        XCTAssertNil(ReceiptLedger.nextSendable(in: ledger.receipts, pairingToken: "p",
                                                now: Date().timeIntervalSince1970))
        ledger.releaseHolds()
        XCTAssertEqual(ledger.receipts.first?.heldUntil, 0)
        XCTAssertEqual(ledger.receipts.first?.attempts, 1, "the ceiling still counts")
        XCTAssertEqual(ReceiptLedger.nextSendable(in: ledger.receipts, pairingToken: "p",
                                                  now: Date().timeIntervalSince1970)?.id,
                       held.id, "the earlier press first, then \(start.id)")
        ledger.forget()
    }

    // MARK: - PENDING reads as words

    func testAPressIsNamedInWordsWithItsSubject() {
        var one = Receipt(id: "t", action: PhoneActions.boardUpdate,
                          scope: "card-1234567890",
                          fields: ["card_id": "card-1234567890"],
                          effect: .none, pairingToken: "p")
        XCTAssertEqual(one.wording, "Save a card")
        XCTAssertEqual(one.subject, "card-123")
        // A verb with no entry is still listable.
        one.action = "some_new_verb"
        XCTAssertEqual(one.wording, "some_new_verb")
        // A bare verb names nothing, and says nothing rather than "verb:x".
        var bare = Receipt(id: "t", action: PhoneActions.registerPushToken,
                           scope: "verb:register_push_token", fields: [:],
                           effect: .none, pairingToken: "p")
        XCTAssertEqual(bare.subject, "")
        bare.action = PhoneActions.stopSession
        XCTAssertEqual(bare.wording, "Stop an agent")
        // The two card acknowledgments are words too, never their wire verb.
        bare.action = PhoneActions.boardManualClear
        XCTAssertEqual(bare.wording, "Mark a check done")
        bare.action = PhoneActions.boardReview
        XCTAssertEqual(bare.wording, "Mark a result reviewed")
    }

    // MARK: - the cache is pairing-scoped

    /// `Receipt.pairingToken`'s twin: a cache filled against another Mac is
    /// never read as this one's.
    func testAdoptDropsAnotherPairingsCards() {
        let cache = CardCacheStore()
        cache.adopt("mac-a")
        var page = CardSyncPage()
        page.available = true
        page.cards = [card("a", revision: 1)]
        cache.apply(page)
        XCTAssertNotNil(cache.card("a"))
        cache.adopt("mac-b")
        XCTAssertNil(cache.card("a"))
    }

    /// An unstamped row — written before the stamp existed — is adopted
    /// rather than thrown away.
    func testAdoptKeepsAnUnstampedRow() {
        let cache = CardCacheStore()
        var page = CardSyncPage()
        page.available = true
        page.cards = [card("a", revision: 1)]
        cache.apply(page)          // no pairing set yet, so the stamp is ""
        cache.adopt("mac-a")
        XCTAssertNotNil(cache.card("a"))
        XCTAssertEqual(cache.card("a")?.pairingToken, "mac-a")
    }
}
