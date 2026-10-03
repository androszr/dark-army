import XCTest
@testable import BobPhone

/// Rebuild & restart on the phone: the words for each state — the held
/// picture, the stale stamp and the away door included — and the tolerant
/// decode of the `rebuild` section and an agent's `rebuild_offered`.
@MainActor
final class RebuildRulesTests: XCTestCase {
    private let utc = TimeZone(identifier: "UTC")!

    private func section(_ json: String) throws -> RebuildSection {
        try JSONDecoder().decode(RebuildSection.self, from: Data(json.utf8))
    }

    private func line(_ s: RebuildSection, connected: Bool = true, away: Bool = false,
                      awayAllowed: Bool = false, pressedAt: Double? = nil) -> String {
        RebuildRules.line(section: s, connected: connected, away: away,
                          awayAllowed: awayAllowed, pressedAt: pressedAt,
                          timeZone: utc)
    }

    // MARK: the words

    func testIdleShowsTheMacsLabel() {
        let s = RebuildSection(available: true, label: "Rebuild & Reload")
        XCTAssertEqual(line(s), "Rebuild & Reload")
    }

    func testIdleWithNoLabelFallsBackToTheTiles() {
        XCTAssertEqual(line(RebuildSection(available: true)), "Rebuild & restart")
    }

    func testAMacWithNothingToRebuildSaysSo() {
        XCTAssertEqual(line(RebuildSection()), RebuildRules.unavailableLine)
    }

    func testAwayOffersNothing() {
        let s = RebuildSection(available: true)
        XCTAssertEqual(line(s, away: true), RebuildRules.awayLine)
        XCTAssertFalse(RebuildRules.canPress(section: s, away: true))
    }

    func testAwayWithTheMarkerOffersThePress() {
        let s = RebuildSection(available: true, label: "Rebuild & Reload")
        XCTAssertEqual(line(s, away: true, awayAllowed: true), "Rebuild & Reload")
        XCTAssertTrue(RebuildRules.canPress(section: s, away: true, awayAllowed: true))
    }

    func testAwayWithTheMarkerStillRefusesWhileRebuilding() {
        let s = RebuildSection(available: true, rebuilding: true)
        XCTAssertFalse(RebuildRules.canPress(section: s, away: true, awayAllowed: true))
        XCTAssertEqual(line(s, away: true, awayAllowed: true), RebuildRules.rebuildingLine)
    }

    func testOfferedTable() {
        for available in [false, true] {
            for away in [false, true] {
                for allowed in [false, true] {
                    let s = RebuildSection(available: available)
                    XCTAssertEqual(
                        RebuildRules.offered(section: s, away: away, awayAllowed: allowed),
                        available && (!away || allowed))
                }
            }
        }
    }

    func testAwayArmedWarningSaysTheLinkDrops() {
        XCTAssertTrue(RebuildRules.awayArmedWarning.contains("You are away"))
        XCTAssertTrue(RebuildRules.awayArmedWarning.contains("drops off the link"))
    }

    func testRebuildingWarnsTheMacWillDrop() {
        let s = RebuildSection(available: true, rebuilding: true)
        XCTAssertEqual(line(s), RebuildRules.rebuildingLine)
        XCTAssertTrue(line(s).contains("Rebuilding…"))
        XCTAssertFalse(RebuildRules.canPress(section: s, away: false))
    }

    func testTheHeldPictureNeverClaimsRebuildingWhileTheLinkIsDown() {
        let s = RebuildSection(available: true, rebuilding: true)
        XCTAssertTrue(line(s, connected: false).contains("waiting for the Mac"))
        XCTAssertTrue(line(RebuildSection(available: true), connected: false,
                           pressedAt: 100).contains("waiting for the Mac"))
    }

    func testARestartingMacSaysWaitingAndOffersNoPress() {
        let s = RebuildSection(available: true, restarting: true)
        XCTAssertEqual(line(s), RebuildRules.waitingLine)
        XCTAssertFalse(RebuildRules.canPress(section: s, away: false))
    }

    func testAFreshSuccessSaysWhen() {
        // 14:32 UTC on 3 Oct 2026.
        let finished = 1_791_037_920.0
        let s = RebuildSection(available: true, lastOutcome: "ok",
                               lastFinishedAt: finished)
        XCTAssertEqual(line(s, pressedAt: finished - 60), "Rebuilt at 14:32")
    }

    func testAStaleStampIsNotThisPress() {
        let s = RebuildSection(available: true, lastOutcome: "ok",
                               lastFinishedAt: 1_000)
        let said = line(s, pressedAt: 2_000)
        XCTAssertFalse(said.contains("Rebuilt at"))
        XCTAssertEqual(said, RebuildRules.askedLine)
    }

    func testAStaleFailureIsNotThisPressEither() {
        let s = RebuildSection(available: true, lastOutcome: "failed",
                               lastFinishedAt: 1_000, lastError: "old")
        XCTAssertEqual(line(s, pressedAt: 2_000), RebuildRules.askedLine)
        XCTAssertFalse(RebuildRules.isFailure(section: s, pressedAt: 2_000))
    }

    func testAFailureShowsTheMacsWords() {
        let s = RebuildSection(available: true, lastOutcome: "failed",
                               lastFinishedAt: 3_000, lastError: "swift: error")
        XCTAssertEqual(line(s, pressedAt: 2_000), "Rebuild failed: swift: error")
        XCTAssertTrue(RebuildRules.isFailure(section: s, pressedAt: 2_000))
        XCTAssertEqual(line(s), "Rebuild failed: swift: error")
    }

    func testAFailureWithNoTextStillSaysFailed() {
        let s = RebuildSection(available: true, lastOutcome: "failed")
        XCTAssertEqual(line(s), "Rebuild failed")
    }

    // MARK: the decode

    func testTheSectionDecodesFromNothing() throws {
        let s = try section("{}")
        XCTAssertEqual(s, RebuildSection())
        XCTAssertFalse(s.available)
        XCTAssertNil(s.startedAt)
        XCTAssertNil(s.lastFinishedAt)
    }

    func testTheSectionDecodesEveryKey() throws {
        let s = try section("""
            {"available":true,"label":"Rebuild & Deploy","rebuilding":true,
             "started_at":5.5,"last_outcome":"ok","last_finished_at":9.0,
             "last_error":"x"}
            """)
        XCTAssertTrue(s.available)
        XCTAssertEqual(s.label, "Rebuild & Deploy")
        XCTAssertTrue(s.rebuilding)
        XCTAssertEqual(s.startedAt, 5.5)
        XCTAssertEqual(s.lastOutcome, "ok")
        XCTAssertEqual(s.lastFinishedAt, 9.0)
        XCTAssertEqual(s.lastError, "x")
    }

    func testWrongTypesNeverBlankTheSection() throws {
        let s = try section(#"{"available":"yes","started_at":"soon","label":3}"#)
        XCTAssertFalse(s.available)
        XCTAssertNil(s.startedAt)
        XCTAssertEqual(s.label, "")
    }

    func testAnAgentWithNoRebuildOfferedDecodesFalse() throws {
        let agent = try JSONDecoder().decode(
            Agent.self, from: Data(#"{"session_id":"s1"}"#.utf8))
        XCTAssertFalse(agent.rebuildOffered)
        let on = try JSONDecoder().decode(
            Agent.self, from: Data(#"{"session_id":"s1","rebuild_offered":true}"#.utf8))
        XCTAssertTrue(on.rebuildOffered)
    }

    func testTheSectionIsCarriedFromTheHeldPicture() {
        var held = Snapshot()
        held.rebuild = RebuildSection(available: true, label: "held")
        var fresh = Snapshot()
        XCTAssertTrue(fresh.carry("rebuild", from: held))
        XCTAssertEqual(fresh.rebuild.label, "held")
    }

    func testALostRebuildReplyIsNeverReplayed() {
        XCTAssertTrue(ReceiptLedger.neverReplayed.contains(PhoneActions.rebuildApp))
        XCTAssertFalse(ReceiptLedger.neverReplayed.contains(PhoneActions.stopSession))
        XCTAssertTrue(ReceiptLedger.phoneAuthored.contains(ReceiptLedger.notReplayedLine))
    }

    private func rebuildReceipt(_ ledger: ReceiptLedger) -> Receipt {
        ledger.open(action: PhoneActions.rebuildApp, scope: "", fields: [:],
                    effect: .none, pairingToken: "p")
    }

    func testAnAlreadySentRebuildIsNeverReplayed() {
        let ledger = ReceiptLedger()
        let sent = rebuildReceipt(ledger)
        XCTAssertTrue(ReceiptLedger.neverReplays(sent))
    }

    func testARetryOfAStuckRebuildIsSentOnce() {
        let ledger = ReceiptLedger()
        let sent = rebuildReceipt(ledger)
        ledger.gaveUp(sent.id, why: ReceiptLedger.notReplayedLine)
        ledger.retry(sent.id)
        let again = ledger.receipts.first { $0.action == PhoneActions.rebuildApp }!
        XCTAssertNotEqual(again.id, sent.id)
        XCTAssertEqual(again.attempts, 0)
        XCTAssertFalse(ReceiptLedger.neverReplays(again))
        ledger.markSending(again.id)
        let going = ledger.receipts.first { $0.id == again.id }!
        XCTAssertTrue(ReceiptLedger.neverReplays(going),
                      "once sent, a second sweep must not send it again")
    }

    func testOtherVerbsAreNotAffected() {
        let ledger = ReceiptLedger()
        let other = ledger.open(action: PhoneActions.stopSession, scope: "s",
                                fields: [:], effect: .none, pairingToken: "p")
        XCTAssertFalse(ReceiptLedger.neverReplays(other))
    }

    func testTheMenuTileLightsOnlyWhenTheMacCanRebuild() {
        XCTAssertFalse(MenuSection.rebuild.lit(scoutReports: false))
        XCTAssertTrue(MenuSection.rebuild.lit(scoutReports: false, rebuild: true))
        let s = RebuildSection(available: true)
        XCTAssertFalse(MenuSection.rebuild.lit(
            scoutReports: false,
            rebuild: RebuildRules.offered(section: s, away: true, awayAllowed: false)))
        XCTAssertTrue(MenuSection.rebuild.lit(
            scoutReports: false,
            rebuild: RebuildRules.offered(section: s, away: true, awayAllowed: true)))
    }
}
