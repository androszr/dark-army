import XCTest
@testable import BobPhone

/// The bot's two grants on the profile screen: the decode, which must never
/// throw on a phone's own row, and the three sentences `BotAccessRules`
/// draws — the same three as the Mac's `botGrantWords`.
final class BotAccessTests: XCTestCase {

    private let now: Double = 1_700_000_000

    private func row(_ json: String) throws -> PhoneDeviceRow {
        try JSONDecoder().decode(PhoneDeviceRow.self, from: Data(json.utf8))
    }

    // MARK: - decode

    func testTheBotsRowDecodesBothSides() throws {
        let bot = try row("""
        {"id":"b","name":"Grok","bot_access":{"read":{"mode":"forever","until":0},"write":{"mode":"1h","until":1700003600}}}
        """)
        XCTAssertEqual(bot.id, "b")
        XCTAssertEqual(bot.name, "Grok")
        XCTAssertEqual(bot.botAccess?.read, PhoneBotGrant(mode: "forever", until: 0))
        XCTAssertEqual(bot.botAccess?.write.mode, "1h")
        XCTAssertEqual(bot.botAccess?.write.until ?? 0, 1_700_003_600, accuracy: 0.001)
    }

    func testAPhonesRowHasNoBotAccess() throws {
        let phone = try row(#"{"id":"p"}"#)
        XCTAssertNil(phone.botAccess)
        XCTAssertEqual(phone.name, "")
        let older = try row(#"{"id":"p","lease_expires_at":0}"#)
        XCTAssertNil(older.botAccess)
        XCTAssertEqual(older.leaseExpiresAt, 0)
    }

    func testAHalfStatedGrantKeepsTheOtherSidesDefault() throws {
        let bot = try row(#"{"id":"b","bot_access":{"read":{"mode":"6h"}}}"#)
        XCTAssertEqual(bot.botAccess?.read.mode, "6h")
        XCTAssertEqual(bot.botAccess?.write, PhoneBotGrant())
    }

    func testTheDevicesSectionPicksTheRowWithTheGrants() throws {
        let devices = try JSONDecoder().decode(PhoneDevices.self, from: Data("""
        {"devices":[{"id":"p","lease_expires_at":1700086400},
                    {"id":"b","name":"Grok","bot_access":{"read":{"mode":"off","until":0},"write":{"mode":"off","until":0}}}]}
        """.utf8))
        XCTAssertEqual(devices.bot?.id, "b")
        XCTAssertEqual(devices.leaseExpiresAt(deviceId: "p"), 1_700_086_400)
        let olderMac = try JSONDecoder().decode(PhoneDevices.self, from: Data("""
        {"devices":[{"id":"p"},{"id":"b"}]}
        """.utf8))
        XCTAssertNil(olderMac.bot)
    }

    // MARK: - words

    func testTheThreeSentences() {
        XCTAssertEqual(BotAccessRules.words(mode: "forever", until: 0, now: now),
                       "on — no timer")
        XCTAssertEqual(BotAccessRules.words(mode: "off", until: 0, now: now), "off")
        XCTAssertEqual(BotAccessRules.words(mode: "1h", until: now + 3600, now: now),
                       "on until " + AwaySpan.until(now + 3600, now: now))
    }

    func testALapsedTimerAndAnUnknownWordReadOff() {
        XCTAssertEqual(BotAccessRules.words(mode: "24h", until: now - 1, now: now), "off")
        XCTAssertEqual(BotAccessRules.words(mode: "3d", until: now + 3600, now: now), "off")
    }

    func testTheMenuAndTheRestartButton() {
        XCTAssertEqual(BotAccessRules.modes, ["off", "1h", "6h", "24h", "forever"])
        XCTAssertEqual(BotAccessRules.modes.map(BotAccessRules.title),
                       ["Off", "1 hour", "6 hours", "24 hours", "No timer"])
        XCTAssertTrue(BotAccessRules.showsRestart("6h"))
        XCTAssertFalse(BotAccessRules.showsRestart("forever"))
        XCTAssertFalse(BotAccessRules.showsRestart("off"))
    }

    // MARK: - pressing

    /// A menu moved by the screen itself lands on the published mode and so
    /// never posts; only a real, offered, different choice does.
    func testOnlyARealChoicePosts() {
        XCTAssertTrue(BotAccessRules.shouldPost(chosen: "6h", published: "forever"))
        XCTAssertFalse(BotAccessRules.shouldPost(chosen: "forever", published: "forever"))
        XCTAssertFalse(BotAccessRules.shouldPost(chosen: "", published: "forever"))
        XCTAssertFalse(BotAccessRules.shouldPost(chosen: "3d", published: "off"))
    }

    func testARefusalIsShownInTheMacsWords() {
        XCTAssertEqual(BotAccessRules.refusalWords("the bot may not change its own access"),
                       "the bot may not change its own access")
        XCTAssertEqual(BotAccessRules.refusalWords("  "), "the Mac did not change it")
    }

    /// A change to the bot's access is judged by the Mac's `bot_access`,
    /// and a record left `sent` by a failed press is dropped before any
    /// re-send — never landed behind the person's back.
    @MainActor
    func testABotAccessPressIsJudgedByTheGrantAndNeverResent() throws {
        let effect = ReceiptLedger.effect(
            for: PhoneActions.setBotAccess,
            fields: ["device_id": "b", "side": "write", "mode": "6h"], scope: "")
        XCTAssertEqual(effect, .botAccess(deviceId: "b", side: "write", mode: "6h"))
        var snapshot = Snapshot()
        snapshot.devices = try JSONDecoder().decode(PhoneDevices.self, from: Data("""
        {"devices":[{"id":"b","bot_access":{"read":{"mode":"forever","until":0},"write":{"mode":"off","until":0}}}]}
        """.utf8))
        XCTAssertFalse(ReceiptLedger.landed(effect, in: snapshot))
        // Not landed, and still never re-sent.
        XCTAssertTrue(ReceiptLedger.evidenceBeforeSending(effect, in: snapshot))
        snapshot.devices = try JSONDecoder().decode(PhoneDevices.self, from: Data("""
        {"devices":[{"id":"b","bot_access":{"read":{"mode":"forever","until":0},"write":{"mode":"6h","until":1700021600}}}]}
        """.utf8))
        XCTAssertTrue(ReceiptLedger.landed(effect, in: snapshot))
        // Every other verb keeps its ordinary rule.
        XCTAssertFalse(ReceiptLedger.evidenceBeforeSending(.none, in: snapshot))
    }
}
