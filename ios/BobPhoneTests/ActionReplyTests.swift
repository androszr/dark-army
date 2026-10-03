import XCTest
@testable import BobPhone

/// What the phone makes of the Mac's answer to one press, and the addresses
/// it builds to send that press.
///
/// All of it is arithmetic-free and all of it is silently invertible: the
/// three refusals match by **prefix** and CLAUDE.md forbids either plan-gate
/// constant becoming a prefix of the other; `CardStated(json:)` must be `nil`
/// for an absent object and never a blank card; `PhonePrepareResult` must
/// apply only non-empty values. `PhoneActions.homeURL` is the only phone file
/// composing a home URL — `HostAddress` itself is tested in the panel's
/// target, the composition around it nowhere until now.
///
/// Seam: `Data` bodies plus a status integer.
@MainActor
final class ActionReplyTests: XCTestCase {

    private func result(_ json: String, _ code: Int) -> PhoneActionResult {
        PhoneActionResult.parse(data: Data(json.utf8), code: code)
    }

    // MARK: - the ordinary answers

    func testAnEmptyTwoHundredIsPlainSuccess() {
        let answer = result("", 200)
        XCTAssertTrue(answer.ok)
        XCTAssertEqual(answer.detail, "")
        XCTAssertNil(answer.revision)
        XCTAssertNil(answer.current)
    }

    func testATwoHundredCarryingARevisionKeepsIt() {
        let answer = result(#"{"revision": 9}"#, 200)
        XCTAssertTrue(answer.ok)
        XCTAssertEqual(answer.revision, 9)
    }

    /// The Mac's report on a success is the only word START n TOGETHER and
    /// START PROJECT draw; blanking it left the phone saying nothing.
    func testATwoHundredKeepsTheMacsReport() {
        let answer = result(#"{"ok": true, "detail": "Started 4 cards (1 skipped)."}"#, 200)
        XCTAssertTrue(answer.ok)
        XCTAssertEqual(answer.detail, "Started 4 cards (1 skipped).")
    }

    func testAnErrorKeyOnATwoHundredIsNotAReport() {
        let answer = result(#"{"error": "stray"}"#, 200)
        XCTAssertTrue(answer.ok)
        XCTAssertEqual(answer.detail, "")
    }

    func testANonJSONBodyYieldsAnEmptyDetailRatherThanThrowing() {
        let answer = result("<html>nope</html>", 500)
        XCTAssertFalse(answer.ok)
        XCTAssertEqual(answer.detail, "")
    }

    func testErrorIsReadWhereDetailIsAbsent() {
        let answer = result(#"{"error": "no."}"#, 400)
        XCTAssertFalse(answer.ok)
        XCTAssertEqual(answer.detail, "no.")
    }

    // MARK: - the conflict the phone hears first after a gap

    func testACardChangedRefusalCarriesTheOtherSideOfTheConflict() throws {
        let body = """
        {"detail": "\(PhoneActions.cardChangedPrefix) — fetch it again.",
         "revision": 9,
         "current": {"revision": 9, "title": "t", "summary": "s",
                     "prompt": "p", "priority": "40"}}
        """
        let answer = result(body, 409)
        XCTAssertFalse(answer.ok)
        XCTAssertTrue(answer.isCardChangedRefusal)
        let current = try XCTUnwrap(answer.current)
        XCTAssertEqual(current.revision, 9)
        XCTAssertEqual(current.title, "t")
        XCTAssertEqual(current.summary, "s")
        XCTAssertEqual(current.prompt, "p")
        XCTAssertEqual(current.priority, "40")
    }

    // MARK: - the three prefixes

    func testEachRefusalPredicateFiresForItsOwnConstantAndNoOther() {
        let gate = result(#"{"detail": "\#(PhoneActions.planGatePrefix) and so on"}"#, 409)
        XCTAssertTrue(gate.isPlanGateRefusal)
        XCTAssertFalse(gate.isPlanChangedRefusal)
        XCTAssertFalse(gate.isCardChangedRefusal)

        let changed = result(#"{"detail": "\#(PhoneActions.planChangedPrefix) and so on"}"#, 409)
        XCTAssertFalse(changed.isPlanGateRefusal)
        XCTAssertTrue(changed.isPlanChangedRefusal)
        XCTAssertFalse(changed.isCardChangedRefusal)

        let card = result(#"{"detail": "\#(PhoneActions.cardChangedPrefix) and so on"}"#, 409)
        XCTAssertFalse(card.isPlanGateRefusal)
        XCTAssertFalse(card.isPlanChangedRefusal)
        XCTAssertTrue(card.isCardChangedRefusal)
    }

    func testNeitherPlanConstantIsAPrefixOfTheOther() {
        // CLAUDE.md's rule, and the one a reworded constant would break: both
        // surfaces match by prefix, so if one became a prefix of the other the
        // phone would show "start with a changed plan" for a card that has no
        // plan at all.
        XCTAssertFalse(
            PhoneActions.planGatePrefix.hasPrefix(PhoneActions.planChangedPrefix))
        XCTAssertFalse(
            PhoneActions.planChangedPrefix.hasPrefix(PhoneActions.planGatePrefix))
    }

    // MARK: - the Mac's stated card

    func testAnAbsentCurrentObjectIsNilAndNeverABlankCard() {
        XCTAssertNil(CardStated(json: nil))
    }

    func testAnEmptyCurrentObjectIsACardAtItsDefaults() throws {
        let stated = try XCTUnwrap(CardStated(json: [:]))
        XCTAssertEqual(stated.revision, 0)
        XCTAssertEqual(stated.title, "")
        XCTAssertEqual(stated.summary, "")
        XCTAssertEqual(stated.prompt, "")
        XCTAssertEqual(stated.priority, "")
    }

    // MARK: - prepare

    private func prepared(_ json: String, _ code: Int) -> PhonePrepareResult {
        PhonePrepareResult.parse(data: Data(json.utf8), code: code)
    }

    func testPrepareRefusesATwoHundredThatDoesNotSayOk() {
        let answer = prepared(#"{"prompt": "p", "detail": "no."}"#, 200)
        XCTAssertFalse(answer.ok)
        XCTAssertEqual(answer.detail, "no.")
        XCTAssertEqual(answer.prompt, "")
    }

    func testPrepareMapsEverySnakeCaseKey() {
        let answer = prepared("""
        {"ok": true, "prompt": "p", "workflow": "w", "title": "t",
         "summary": "s", "suggested_root": "/a/proj",
         "beneficiary": "b", "intended_benefit": "ib",
         "success_criterion": "sc",
         "suggested_dependencies": ["c1", "c2"]}
        """, 200)
        XCTAssertTrue(answer.ok)
        XCTAssertEqual(answer.prompt, "p")
        XCTAssertEqual(answer.workflow, "w")
        XCTAssertEqual(answer.title, "t")
        XCTAssertEqual(answer.summary, "s")
        XCTAssertEqual(answer.suggestedRoot, "/a/proj")
        XCTAssertEqual(answer.beneficiary, "b")
        XCTAssertEqual(answer.intendedBenefit, "ib")
        XCTAssertEqual(answer.successCriterion, "sc")
        XCTAssertEqual(answer.suggestedDependencies, ["c1", "c2"])
    }

    func testTheKeysAnOlderMacOmitsReadEmptyRatherThanNil() {
        // Empty is what the composer refuses to apply, so an older Mac
        // overwrites nothing somebody typed.
        let answer = prepared(#"{"ok": true, "prompt": "p", "workflow": "w"}"#, 200)
        XCTAssertTrue(answer.ok)
        XCTAssertEqual(answer.title, "")
        XCTAssertEqual(answer.summary, "")
        XCTAssertEqual(answer.suggestedRoot, "")
        XCTAssertEqual(answer.beneficiary, "")
        XCTAssertEqual(answer.intendedBenefit, "")
        XCTAssertEqual(answer.successCriterion, "")
        XCTAssertEqual(answer.suggestedDependencies, [])
    }

    // MARK: - the addresses

    private func url(_ verdict: PhoneActions.ActionURL) -> String? {
        if case .ok(let value) = verdict { return value.absoluteString }
        return nil
    }

    private func refusal(_ verdict: PhoneActions.ActionURL) -> String? {
        if case .refused(let words) = verdict { return words }
        return nil
    }

    func testAPlainHostAndPortBecomeTheHomeURL() {
        XCTAssertEqual(url(PhoneActions.homeURL(host: "192.168.1.23",
                                                port: 19875)),
                       "http://192.168.1.23:19875/api/home")
    }

    // The `https://` a person pasted comes back `http://`, and that is correct
    // rather than a downgrade to fix: the LAN door has no TLS name to present,
    // and the *seal* is the confidentiality boundary here, not the scheme.
    // (`RelayTransport.boxURL` is the opposite case — it refuses a plaintext
    // base, because the relay is off-machine. `SealedEnvelopeTests` pins that.)
    func testAPastedAddressIsStrippedAndItsEmbeddedPortWins() {
        XCTAssertEqual(
            url(PhoneActions.homeURL(host: " https://192.168.1.23:19875/ ",
                                     port: 1)),
            "http://192.168.1.23:19875/api/home")
    }

    func testAHostWithASpaceIsRefusedInWords() {
        let words = refusal(PhoneActions.homeURL(host: "a b", port: 19875))
        XCTAssertNotNil(words)
        XCTAssertTrue(words?.contains("space") == true, words ?? "")
    }

    func testPortZeroIsRefused() {
        XCTAssertNotNil(refusal(PhoneActions.homeURL(host: "192.168.1.23",
                                                     port: 0)))
    }

    func testTheUploadURLDiffersOnlyInItsPath() {
        XCTAssertEqual(url(PhoneActions.uploadURL(host: "192.168.1.23",
                                                  port: 19875)),
                       "http://192.168.1.23:19875/api/upload")
    }

    // MARK: - the four away failures, told apart

    func testTheMailboxStatusesEachGetTheirOwnSentence() {
        XCTAssertEqual(RelayChannel.Trouble.forStatus(429),
                       RelayChannel.Trouble.rateLimited)
        XCTAssertEqual(RelayChannel.Trouble.forStatus(503),
                       RelayChannel.Trouble.storageDown)
        XCTAssertNotEqual(RelayChannel.Trouble.rateLimited,
                          RelayChannel.Trouble.storageDown)
    }

    func testAnUnexpectedStatusNamesItsCode() {
        let words = RelayChannel.Trouble.forStatus(418)
        XCTAssertEqual(words, RelayChannel.Trouble.unexpected(418))
        XCTAssertTrue(words.contains("418"), words)
    }
}
