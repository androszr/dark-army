import XCTest
@testable import BobPanel

final class BoardOutcomeTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(text.utf8))
    }
    func testAbsentOutcomeFieldsDecodeToDefaults() throws {
        let card = try decode(BoardCard.self, "{}")
        XCTAssertEqual(card.outcomeRevision, 0)
        XCTAssertEqual(card.outcomeStatus, "unaccepted")
        XCTAssertFalse(try decode(OutcomeReport.self, "{}").available)
    }
    func testZeroAndUnknownAreDifferent() throws {
        let zero = try decode(OutcomeCost.self, #"{"totals":{"USD":0},"coverage":"complete"}"#)
        XCTAssertEqual(zero.label, "Observed cost: USD 0.00 · complete")
        XCTAssertEqual(try decode(OutcomeCost.self, "{}").label, "Cost unknown")
        let missing = try decode(OutcomeCard.self, #"{"wait_seconds":null}"#)
        XCTAssertEqual(missing.waitLabel, "Time awaiting a person: unknown")
    }
    func testRaggedUnknownValuesTolerate() throws {
        let report = try decode(OutcomeReport.self, #"{"supported":true,"available":true,"card":{"card_id":"a","revision":2,"wait_coverage":"future"},"events":[{"kind":"future","evidence":"literal https://example.test"}]}"#)
        XCTAssertEqual(report.card?.statusLabel, "Not accepted")
        XCTAssertEqual(report.events.first?.label, "Outcome event")
        XCTAssertEqual(report.events.first?.evidence, "literal https://example.test")
    }
    func testLateCardOrRevisionResponseDoesNotMatch() throws {
        let report = try decode(OutcomeReport.self, #"{"card":{"card_id":"a","revision":2}}"#)
        XCTAssertTrue(report.matches(cardId: "a", revision: 2))
        XCTAssertFalse(report.matches(cardId: "b", revision: 2))
        XCTAssertFalse(report.matches(cardId: "a", revision: 3))
    }
    func testAcceptedIsSeparateFromDoneAndReviewed() throws {
        let card = try decode(BoardCard.self, #"{"column_name":"done","reviewed_at":123,"outcome_status":"unaccepted"}"#)
        XCTAssertEqual(card.column, "done")
        XCTAssertEqual(card.outcomeStatus, "unaccepted")
        XCTAssertEqual(try decode(OutcomeCard.self, #"{"accepted":true}"#).statusLabel, "Accepted outcome")
    }
    func testLaterCheckDateIsReminder() throws {
        let objective = try decode(OutcomeObjective.self, #"{"outcome_check_on":"2026-01-01"}"#)
        XCTAssertTrue(objective.checkLabel(now: Date(timeIntervalSince1970: 1788600000)).contains("overdue"))
        XCTAssertEqual(OutcomeObjective().checkLabel(), "")
    }
    @MainActor func testPendingSaveHeldAndDraftRetainedOnStaleReply() throws {
        let state = BoardState()
        state.editing = "a"
        state.draft.title = "Keep title"
        state.draft.outcome.successCriterion = "Keep criterion"
        state.draft.outcomeRevision = 4
        state.outcomeSaving = true
        XCTAssertTrue(state.saveHeld(preparing: false))
        let refusal = try decode(OutcomeActionReply.self, #"{"ok":false,"detail":"the outcome changed"}"#)
        XCTAssertFalse(refusal.ok)
        XCTAssertNil(refusal.revision)
        XCTAssertTrue(state.applyOutcomeDecision(refusal, cardId: "a", expectedRevision: 4))
        XCTAssertEqual(state.refusals["a"], "the outcome changed")
        // The actual editor response handler grants no revision on refusal.
        XCTAssertEqual(state.draft.outcome.successCriterion, "Keep criterion")
        XCTAssertEqual(state.draft.outcomeRevision, 4)
    }
    func testCoveredOutcomeDenominator() throws {
        let report = try decode(OutcomeReport.self, #"{"summary":{"accepted_outcomes":2,"cost_per_outcome":{"USD":{"per_accepted_outcome":4,"covered":1,"outcomes":2}},"rework_rate":null}}"#)
        XCTAssertEqual(report.summary?.cost["USD"]?.perOutcome, 4)
        XCTAssertEqual(report.summary?.cost["USD"]?.covered, 1)
        XCTAssertEqual(report.summary?.cost["USD"]?.outcomes, 2)
        XCTAssertNil(report.summary?.reworkRate)
    }
    func testObservedCostAndReasonRideTheSummary() throws {
        let report = try decode(OutcomeReport.self, #"{"summary":{"accepted_outcomes":2,"awaiting_acceptance":3,"observed_cost_per_outcome":{"USD":{"per_accepted_outcome":3,"covered":2,"outcomes":2}},"awaiting_cost_totals":{"USD":7.5},"awaiting_cost_covered":1,"cost_reason":"","observed_cost_note":"Observed on this Mac."}}"#)
        XCTAssertEqual(report.summary?.observedCost["USD"]?.perOutcome, 3)
        XCTAssertEqual(report.summary?.observedCost["USD"]?.covered, 2)
        XCTAssertEqual(report.summary?.observedCost["USD"]?.outcomes, 2)
        XCTAssertEqual(report.summary?.awaitingCost["USD"], 7.5)
        XCTAssertEqual(report.summary?.awaitingCovered, 1)
        XCTAssertEqual(report.summary?.observedCostNote, "Observed on this Mac.")
        XCTAssertEqual(report.summary?.costReason, "")
        XCTAssertTrue(report.summary?.cost.isEmpty == true)
    }
    func testOlderDaemonSummaryLeavesTheCostBlockExactlyAsItWas() throws {
        let report = try decode(OutcomeReport.self, #"{"summary":{"accepted_outcomes":0,"cost_per_outcome":{},"partial_cost_totals":{}}}"#)
        XCTAssertTrue(report.summary?.observedCost.isEmpty == true)
        XCTAssertTrue(report.summary?.awaitingCost.isEmpty == true)
        XCTAssertEqual(report.summary?.awaitingCovered, 0)
        XCTAssertEqual(report.summary?.costReason, "")
        XCTAssertEqual(report.summary?.observedCostNote, "")
    }
    @MainActor func testReportHandlerRejectsSwitchedCardAndPreservesEditedDraft() throws {
        let state = BoardState()
        state.editing = "a"
        let initial = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":4,"objective":{"success_criterion":"Original"}}}"#)
        XCTAssertTrue(state.applyOutcomeReport(initial, cardId: "a", expectedRevision: 4))
        XCTAssertEqual(state.draft.outcome.successCriterion, "Original")
        state.draft.outcome.successCriterion = "My unsaved criterion"
        XCTAssertTrue(state.applyOutcomeReport(initial, cardId: "a", expectedRevision: 4))
        XCTAssertEqual(state.draft.outcome.successCriterion, "My unsaved criterion")
        state.editing = "b"
        state.draft = BoardDraft()
        state.draft.outcome.successCriterion = "Other card draft"
        XCTAssertFalse(state.applyOutcomeReport(initial, cardId: "a", expectedRevision: 4))
        XCTAssertEqual(state.draft.outcome.successCriterion, "Other card draft")
        XCTAssertFalse(state.draft.outcomeLoaded)
    }
    /// A card saved with the "Benefit and success" fold closed — a model
    /// change, a retitle — never loaded its objective, and must send none:
    /// neither a refusal that only opening the fold could clear, nor the
    /// draft's empty defaults in the objective's place.
    func testSaveCarriesObjectiveOnlyOnceLoaded() {
        var draft = BoardDraft()
        draft.outcomeRevision = 4
        XCTAssertEqual(draft.objectiveFields(), [:])
        draft.outcomeLoaded = true
        draft.outcome.successCriterion = "Typed"
        let sent = draft.objectiveFields()
        XCTAssertEqual(sent["success_criterion"], "Typed")
        XCTAssertEqual(sent["expected_outcome_revision"], "4")
        XCTAssertEqual(sent["confirm_outcome_scope_change"], "false")
    }
    @MainActor func testDecisionHandlerOnlyAppliesMatchingReturnedRevision() throws {
        let state = BoardState()
        state.editing = "a"
        state.draft.outcomeRevision = 4
        let accepted = try decode(OutcomeActionReply.self, #"{"ok":true,"outcome_revision":5}"#)
        XCTAssertFalse(state.applyOutcomeDecision(accepted, cardId: "a", expectedRevision: 3))
        XCTAssertEqual(state.draft.outcomeRevision, 4)
        state.editing = "b"
        XCTAssertFalse(state.applyOutcomeDecision(accepted, cardId: "a", expectedRevision: 4))
        XCTAssertEqual(state.draft.outcomeRevision, 4)
        state.editing = "a"
        XCTAssertTrue(state.applyOutcomeDecision(accepted, cardId: "a", expectedRevision: 4))
        XCTAssertEqual(state.draft.outcomeRevision, 5)
    }

    @MainActor func testOlderAsyncReportCannotReplaceAcceptedRevision() async throws {
        let state = BoardState()
        state.editing = "a"
        state.draft.outcomeRevision = 4
        let old = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":4},"events":[{"evidence":"old"}]}"#)
        let fresh = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":5,"accepted":true},"events":[{"evidence":"accepted proof"}]}"#)
        var requests = OutcomeRequestState()
        let oldRequest = requests.begin(cardId: "a", revision: 4)
        let inFlight = Task { await Task.yield(); return old }
        let reply = try decode(OutcomeActionReply.self, #"{"ok":true,"outcome_revision":5}"#)
        XCTAssertTrue(state.applyOutcomeDecision(reply, cardId: "a", expectedRevision: 4))
        requests.invalidate(cardId: "a", revision: 5)
        let afterWrite = requests.begin(cardId: "a", revision: 5)
        XCTAssertTrue(requests.apply(fresh, request: afterWrite, currentCardId: "a", currentRevision: 5))
        let late = await inFlight.value
        XCTAssertFalse(state.applyOutcomeReport(late, cardId: "a", expectedRevision: 4))
        XCTAssertFalse(requests.apply(late, request: oldRequest, currentCardId: "a", currentRevision: 5))
        XCTAssertEqual(requests.report?.events.first?.evidence, "accepted proof")
        XCTAssertEqual(requests.report?.card?.accepted, true)
    }

    func testGenerationOrdersBaseAndPaginationAtSameRevision() throws {
        let base = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":4},"events":[{"evidence":"base"}],"next_offset":1}"#)
        let page = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":4},"events":[{"evidence":"page"}]}"#)
        var requests = OutcomeRequestState()
        let initial = requests.begin(cardId: "a", revision: 4)
        XCTAssertTrue(requests.apply(base, request: initial, currentCardId: "a", currentRevision: 4))
        let oldPage = requests.begin(cardId: "a", revision: 4, offset: 1)
        let refreshed = requests.begin(cardId: "a", revision: 4)
        XCTAssertTrue(requests.apply(base, request: refreshed, currentCardId: "a", currentRevision: 4))
        XCTAssertFalse(requests.apply(page, request: oldPage, currentCardId: "a", currentRevision: 4))
        let oldBase = requests.begin(cardId: "a", revision: 4)
        let newPage = requests.begin(cardId: "a", revision: 4, offset: 1)
        XCTAssertTrue(requests.apply(page, request: newPage, currentCardId: "a", currentRevision: 4))
        XCTAssertFalse(requests.apply(base, request: oldBase, currentCardId: "a", currentRevision: 4))
        XCTAssertEqual(requests.report?.events.map(\.evidence), ["base", "page"])
    }

    @MainActor func testCurrentUnavailableResponseWithdrawsReportAndPreservesDraft() throws {
        let healthy = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":4},"events":[{"evidence":"proof"}],"next_offset":1}"#)
        let outage = try decode(OutcomeReport.self, #"{"supported":true,"available":false}"#)
        let state = BoardState()
        state.editing = "a"
        state.draft.outcomeRevision = 4
        state.draft.outcomeLoaded = true
        state.draft.outcome.successCriterion = "Keep my unsaved criterion"
        for offset in [0, 1] {
            var requests = OutcomeRequestState()
            let first = requests.begin(cardId: "a", revision: 4)
            XCTAssertTrue(requests.apply(healthy, request: first, currentCardId: "a", currentRevision: 4))
            let request = requests.begin(cardId: "a", revision: 4, offset: offset)
            XCTAssertTrue(requests.apply(outage, request: request, currentCardId: "a", currentRevision: 4))
            XCTAssertTrue(state.applyOutcomeReport(outage, cardId: "a", expectedRevision: 4))
            XCTAssertEqual(requests.report?.available, false)
            XCTAssertTrue(requests.report?.events.isEmpty == true)
            XCTAssertEqual(state.draft.outcome.successCriterion, "Keep my unsaved criterion")
            XCTAssertFalse(requests.apply(healthy, request: first, currentCardId: "a", currentRevision: 4))
        }
    }

    func testCurrentSnapshotRevisionAndCardOverrideCapturedPhoneRequest() throws {
        let old = try decode(OutcomeReport.self, #"{"available":true,"card":{"card_id":"a","revision":4}}"#)
        var requests = OutcomeRequestState()
        let request = requests.begin(cardId: "a", revision: 4)
        XCTAssertFalse(requests.apply(old, request: request, currentCardId: "a", currentRevision: 5))
        XCTAssertFalse(requests.apply(old, request: request, currentCardId: "b", currentRevision: 4))
        let other = requests.begin(cardId: "b", revision: 4)
        XCTAssertFalse(requests.apply(old, request: request, currentCardId: "a", currentRevision: 4))
        XCTAssertEqual(other.cardId, "b")
    }

}
