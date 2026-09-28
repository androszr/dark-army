import XCTest
@testable import BobPhone

/// The saved place's file discipline and rules over a temporary folder —
/// `docs/phone-contract.md`, *The phone comes back where you left it*.
@MainActor
final class PhonePlaceTests: XCTestCase {
    private let identity = String(repeating: "ab", count: 32)

    private func folder() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func fixture() -> PhonePlace {
        PhonePlace(tab: "fleet", menuSection: "", push: "",
                   trail: [PhonePlace.Entry(kind: "agent", key: "s1",
                                            agentScreen: "Conversation",
                                            replyText: "half a reply")])
    }

    private func file(_ dir: URL) -> URL {
        dir.appendingPathComponent(PhonePlaceStore.fileName)
    }

    // --- the file ------------------------------------------------------------

    func testAFlushedPlaceRoundTripsThroughTheFile() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        let saved = fixture()
        store.compose = { saved }
        store.flushNow()
        XCTAssertTrue(FileManager.default.fileExists(atPath: file(dir).path))

        let again = PhonePlaceStore(directory: dir)
        again.load()
        again.adopt(identity: identity)
        again.arm()
        let taken = again.take()
        XCTAssertEqual(taken?.tab, "fleet")
        XCTAssertEqual(taken?.pairing, identity)
        XCTAssertEqual(taken?.trail.first?.replyText, "half a reply")
        XCTAssertEqual(taken?.trail.first?.agentScreen, "Conversation")
        // Once per unlock.
        XCTAssertNil(again.take())
    }

    func testFlushWritesNothingWithoutACompose() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        store.flushNow()
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(dir).path))
    }

    func testFlushWritesNothingBeforeAPairingIsAdopted() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        let saved = fixture()
        store.compose = { saved }
        store.flushNow()
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(dir).path))
    }

    func testAdoptingAnotherPairingDeletesTheFile() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        let saved = fixture()
        store.compose = { saved }
        store.flushNow()

        let again = PhonePlaceStore(directory: dir)
        again.load()
        again.adopt(identity: String(repeating: "cd", count: 32))
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(dir).path))
        again.arm()
        XCTAssertNil(again.take())
    }

    func testForgetDeletesTheFileAndRefusesLaterWrites() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        let saved = fixture()
        store.compose = { saved }
        store.flushNow()
        store.forget()
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(dir).path))
        store.flushNow()
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(dir).path))
        store.arm()
        XCTAssertNil(store.take())
    }

    func testAProtectedReadFailureIsRetriedAtTheNextLoad() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        let saved = fixture()
        store.compose = { saved }
        store.flushNow()

        var locked = true
        let again = PhonePlaceStore(directory: dir, reader: { url in
            if locked { throw CocoaError(.fileReadNoPermission) }
            return try Data(contentsOf: url)
        })
        again.load()
        XCTAssertFalse(again.loaded)
        locked = false
        again.load()
        XCTAssertTrue(again.loaded)
        again.adopt(identity: identity)
        again.arm()
        XCTAssertEqual(again.take()?.tab, "fleet")
    }

    func testAnOlderFileMissingKeysDecodes() throws {
        let data = Data(#"{"version": 1, "pairing": "p", "tab": "board"}"#.utf8)
        let place = try JSONDecoder().decode(PhonePlace.self, from: data)
        XCTAssertEqual(place.tab, "board")
        XCTAssertEqual(place.push, "")
        XCTAssertEqual(place.trail, [])
        XCTAssertEqual(place.menuSection, "")
    }

    func testANewerFileWithUnknownKeysDecodes() throws {
        let data = Data(#"""
        {"version": 9, "pairing": "p", "tab": "menu", "menuSection": "plans",
         "scroll": 120, "trail": [{"kind": "card", "key": "c1", "shape": 2,
         "cardDraft": {"title": "t", "for": "c1", "touched": true, "baseRevision": 4,
                       "colour": "red"}}]}
        """#.utf8)
        let place = try JSONDecoder().decode(PhonePlace.self, from: data)
        XCTAssertEqual(place.version, 9)
        XCTAssertEqual(place.menuSection, "plans")
        XCTAssertEqual(place.trail.first?.cardDraft?.title, "t")
        XCTAssertEqual(place.trail.first?.cardDraft?.baseRevision, 4)
        XCTAssertEqual(place.trail.first?.cardDraft?.draftFor, "c1")
        XCTAssertEqual(place.trail.first?.cardDraft?.touched, true)
    }

    // --- the rules -----------------------------------------------------------

    private func entry(_ kind: String, _ key: String) -> PhonePlace.Entry {
        PhonePlace.Entry(kind: kind, key: key)
    }

    func testAnAgentGoneCutsTheTrailThere() {
        let trail = [entry("card", "c1"), entry("agent", "gone"), entry("card", "c2")]
        let cut = PhonePlaceRules.cut(trail, agents: ["s1"], cards: ["c1", "c2"])
        XCTAssertEqual(cut.kept.map(\.key), ["c1"])
        XCTAssertEqual(cut.dropped.first?.1, "agent gone")
        XCTAssertEqual(cut.dropped.count, 2)
    }

    func testADecisionRungStopsTheWalk() {
        let trail = [entry("decision", "d1"), entry("agent", "s1")]
        let cut = PhonePlaceRules.cut(trail, agents: ["s1"], cards: [])
        XCTAssertEqual(cut.kept, [])
        XCTAssertEqual(cut.dropped.first?.1, "kind not restorable")
    }

    func testTheTrailIsCappedAtThree() {
        let trail = [entry("agent", "s1"), entry("card", "c1"),
                     entry("agent", "s2"), entry("card", "c2")]
        let cut = PhonePlaceRules.cut(trail, agents: ["s1", "s2"], cards: ["c1", "c2"])
        XCTAssertEqual(cut.kept.count, 3)
        XCTAssertEqual(cut.dropped.first?.1, "trail depth")
    }

    func testCatchUpAllIsKeptAndAGroupIsNot() {
        XCTAssertEqual(PhonePlaceRules.cut([entry("catchUp", "all")], agents: [], cards: []).kept.count, 1)
        XCTAssertEqual(PhonePlaceRules.cut([entry("catchUp", "r1/r2")], agents: [], cards: []).kept.count, 0)
        XCTAssertEqual(PhonePlaceRules.cut([entry("card", "gone")], agents: [], cards: []).dropped.first?.1,
                       "card gone")
    }

    func testAnythingWaitingWinsOverThePlace() {
        for tab in [false, true] {
            for receipt in [false, true] {
                for composer in [false, true] {
                    XCTAssertEqual(PhonePlaceRules.wins(pendingTab: tab, pendingReceipt: receipt,
                                                        composerResume: composer),
                                   tab || receipt || composer)
                }
            }
        }
    }

    func testTerminalComesBackAsDetails() {
        XCTAssertEqual(PhonePlaceRules.restoredScreen(AgentScreen.terminal.rawValue),
                       AgentScreen.details.rawValue)
        for screen in [AgentScreen.main, .conversation, .details] {
            XCTAssertEqual(PhonePlaceRules.restoredScreen(screen.rawValue), screen.rawValue)
        }
        XCTAssertEqual(PhonePlaceRules.restoredScreen("bogus"), AgentScreen.main.rawValue)
    }

    // --- precedence and displaced text --------------------------------------

    func testSetAsideKeepsTheTypedTextForOneLaterOpen() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        let saved = fixture()
        store.compose = { saved }
        store.flushNow()
        store.arm()
        store.setAside()
        XCTAssertNil(store.take())
        let orphan = store.takeOrphanDraft(for: "agent/s1")
        XCTAssertEqual(orphan?.replyText, "half a reply")
        // A later open is a fresh open: Main, not the saved page.
        XCTAssertEqual(orphan?.agentScreen, "")
        XCTAssertNil(store.takeOrphanDraft(for: "agent/s1"))
    }

    func testAProfilePushIsHandedToItsOwnTabOnce() {
        let dir = folder()
        let store = PhonePlaceStore(directory: dir)
        store.adopt(identity: identity)
        store.compose = { PhonePlace(tab: "board", push: "profile") }
        store.flushNow()
        store.arm()
        XCTAssertEqual(store.take()?.push, "profile")
        XCTAssertEqual(store.takePush(for: "fleet"), "")
        XCTAssertEqual(store.takePush(for: "board"), "profile")
        XCTAssertEqual(store.takePush(for: "board"), "")
    }

    // --- the trail as data ---------------------------------------------------

    func testARestoredRungCarriesItsPageAndText() {
        let saved = PhonePlace.Entry(
            kind: "card", key: "c1",
            cardDraft: PhonePlace.CardDraft(title: "New title", draftFor: "c1",
                                            touched: true, baseRevision: 3, editing: true))
        let state = PhoneSheetEntryState(restoring: saved)
        let draft = state.cardDraft(for: "c1")
        // The status line is the Mac's words and never rides the place.
        XCTAssertEqual(draft.note, "")
        XCTAssertEqual(draft.draftTitle, "New title")
        XCTAssertEqual(draft.draftFor, "c1")
        XCTAssertTrue(draft.draftTouched)
        XCTAssertTrue(draft.editing)

        let agent = PhoneSheetEntryState(restoring: PhonePlace.Entry(
            kind: "agent", key: "s1", agentScreen: "Terminal", replyText: "hi"))
        XCTAssertEqual(agent.agentScreen, .details)
        XCTAssertEqual(agent.replyDraft(for: "s1").text, "hi")

        XCTAssertEqual(PhoneSheetEntryState().agentScreen, AgentScreen.defaultScreen)
    }

    /// The audit's case: a touched edit typed against revision 3, restored
    /// after the Mac moved the card to revision 7. The rung carries 3 — what
    /// `CardDetailView.shownRevision` quotes while touched — so Save is
    /// refused with the Mac's copy rather than overwriting revision 7.
    func testARestoredTouchedDraftKeepsTheRevisionItWasTypedAgainst() {
        let held = PhoneCardDraftState()
        held.draftTitle = "typed on 3"
        held.draftFor = "c1"
        held.draftTouched = true
        held.editRevision = 3
        held.note = "queued"
        let saved = PhonePlace.CardDraft(held: held)
        XCTAssertEqual(saved?.baseRevision, 3)
        XCTAssertEqual(saved?.title, "typed on 3")

        // Through the file and back, then onto a fresh rung.
        let data = try! JSONEncoder().encode(PhonePlace.Entry(kind: "card", key: "c1",
                                                               cardDraft: saved))
        let entry = try! JSONDecoder().decode(PhonePlace.Entry.self, from: data)
        let restored = PhoneSheetEntryState(restoring: entry).cardDraft(for: "c1")
        XCTAssertEqual(restored.editRevision, 3)
        XCTAssertTrue(restored.draftTouched)
        XCTAssertEqual(restored.draftTitle, "typed on 3")
        XCTAssertEqual(restored.note, "")
        XCTAssertFalse(String(data: data, encoding: .utf8)!.contains("queued"))
    }

    /// Touched text with no revision to guard it (an older file) is not
    /// restored: an unguarded Save could overwrite whatever the Mac holds.
    func testTouchedTextWithoutARevisionIsNotRestored() {
        let entry = PhonePlace.Entry(kind: "card", key: "c1", cardDraft: PhonePlace.CardDraft(
            title: "typed", draftFor: "c1", touched: true, messageText: "hello"))
        let restored = PhoneSheetEntryState(restoring: entry).cardDraft(for: "c1")
        XCTAssertFalse(restored.draftTouched)
        XCTAssertEqual(restored.draftTitle, "")
        XCTAssertNil(restored.editRevision)
        XCTAssertEqual(restored.messageText, "hello")

        // And a status note alone is nothing worth keeping.
        let held = PhoneCardDraftState()
        held.note = "refused"
        XCTAssertNil(PhonePlace.CardDraft(held: held))
    }

    /// An editor saved open with nothing typed comes back closed and
    /// untouched: reopened, its fresh seed would mark it edited and a Save
    /// could send the frame's cut-off preview over the full instructions.
    func testAnOpenEditorWithNothingTypedComesBackClosedAndUntouched() {
        let held = PhoneCardDraftState()
        held.editing = true
        let saved = PhonePlace.CardDraft(held: held)
        XCTAssertEqual(saved?.editing, true)
        XCTAssertNil(saved?.baseRevision)
        let restored = PhoneSheetEntryState(restoring: PhonePlace.Entry(
            kind: "card", key: "c1", cardDraft: saved)).cardDraft(for: "c1")
        XCTAssertFalse(restored.editing)
        XCTAssertFalse(restored.draftTouched)
        XCTAssertEqual(restored.draftFor, "")

        // Nor does an older file's touched text with no revision reopen it.
        let older = PhoneSheetEntryState(restoring: PhonePlace.Entry(
            kind: "card", key: "c2", cardDraft: PhonePlace.CardDraft(
                title: "typed", draftFor: "c2", touched: true, editing: true)))
            .cardDraft(for: "c2")
        XCTAssertFalse(older.editing)
        XCTAssertFalse(older.draftTouched)
    }
}
