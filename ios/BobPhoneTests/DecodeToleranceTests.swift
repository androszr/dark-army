import XCTest
@testable import BobPhone

/// A message from the Mac with a piece missing must never blank a surface.
///
/// CLAUDE.md's rule for both clients: Swift's synthesized `Decodable` throws
/// on a missing key *even when the property has a default*, so every model
/// here decodes through `KeyedDecodingContainer.value` / `.maybe`. A model
/// quietly reverting to the synthesized decoder throws on the first absent
/// key and blanks the phone, and a grep for `c.value(` cannot tell which keys
/// a body actually carries.
///
/// Seam: `JSONDecoder` over string literals.
@MainActor
final class DecodeToleranceTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    // MARK: - the ordering rule

    func testUnchangedIsAStateAnswerAndWouldBeAnEmptySnapshot() throws {
        // `PhoneClient` decodes `StateAnswer` **before** `Snapshot`, and that
        // order is load-bearing rather than tidy: `Snapshot` decodes every key
        // tolerantly, so the very same bytes decode into a perfectly valid
        // *empty* picture — blank fleet, blank board, needs-you zero. Only the
        // ordering stops an unchanged reply blanking the screen, and ordering
        // is not something a grep can see.
        let body = #"{"unchanged": true, "state_digest": "abc"}"#

        let answer = try decode(StateAnswer.self, body)
        XCTAssertTrue(answer.unchanged)
        XCTAssertEqual(answer.stateDigest, "abc")

        let snapshot = try decode(Snapshot.self, body)
        XCTAssertEqual(snapshot.counts.working, 0)
        XCTAssertEqual(snapshot.counts.attention, 0)
        XCTAssertTrue(snapshot.agents.running.isEmpty)
        XCTAssertTrue(snapshot.agents.waiting.isEmpty)
        XCTAssertFalse(snapshot.board.available)
        XCTAssertEqual(snapshot.needsYouCount, 0)
    }

    // MARK: - the delta answer

    func testADeltaAnswerNamesItsKeptSectionsAndIsNotUnchanged() throws {
        // A delta answer leaves a section out **and names it**: the marker is
        // the present list, never the absence, because `Snapshot` would read
        // an absent board as an empty one.
        let board = String(repeating: "ab", count: 32)
        let agents = String(repeating: "cd", count: 32)
        let body = """
        {"agents": {"running": []}, "sections_unchanged": ["board"],
         "section_digests": {"board": "\(board)", "agents": "\(agents)"},
         "state_digest": "\(String(repeating: "ef", count: 32))"}
        """
        let answer = try decode(StateAnswer.self, body)
        XCTAssertFalse(answer.unchanged)
        XCTAssertEqual(answer.sectionsUnchanged, ["board"])
        XCTAssertEqual(answer.sectionDigests.count, 2)
        XCTAssertEqual(answer.sectionDigests["board"], board)
        XCTAssertEqual(answer.sectionDigests["agents"], agents)
    }

    func testAnAnswerWithoutTheDeltaKeysDecodesEmptyLists() throws {
        // An older Mac sends neither key: nothing is kept, nothing is quoted.
        let answer = try decode(
            StateAnswer.self,
            #"{"generated_at": 12, "agents": {}, "board": {}, "state_digest": "abc"}"#)
        XCTAssertFalse(answer.unchanged)
        XCTAssertEqual(answer.sectionsUnchanged, [])
        XCTAssertEqual(answer.sectionDigests, [:])
        // A wrongly shaped key decodes to the default rather than throwing.
        let odd = try decode(
            StateAnswer.self,
            #"{"sections_unchanged": "board", "section_digests": ["x"]}"#)
        XCTAssertEqual(odd.sectionsUnchanged, [])
        XCTAssertEqual(odd.sectionDigests, [:])
    }

    func testCarryMovesOneSectionAndRefusesANameItDoesNotKnow() throws {
        let held = try decode(Snapshot.self, """
        {"generated_at": 5, "counts": {"working": 2},
         "board": {"available": true, "cards": [
           {"id": "a", "root": "/p", "title": "A"},
           {"id": "b", "root": "/p", "title": "B"},
           {"id": "c", "root": "/p", "title": "C"}]}}
        """)
        XCTAssertEqual(held.board.cards.count, 3)

        var fresh = try decode(Snapshot.self,
                               #"{"generated_at": 9, "agents": {"running": []}}"#)
        XCTAssertTrue(fresh.board.cards.isEmpty)

        XCTAssertTrue(fresh.carry("board", from: held))
        XCTAssertEqual(fresh.board.cards.count, 3)
        XCTAssertTrue(fresh.board.available)
        // Only the named section moves: the counts stay the fresh answer's.
        XCTAssertEqual(fresh.counts.working, 0)
        XCTAssertEqual(fresh.generatedAt, 9)

        // A name the phone does not decode, and a name that is not a section,
        // carry nothing and change nothing.
        XCTAssertFalse(fresh.carry("signals", from: held))
        XCTAssertFalse(fresh.carry("state_digest", from: held))
        XCTAssertFalse(fresh.carry("generated_at", from: held))
        XCTAssertEqual(fresh.generatedAt, 9)
        XCTAssertEqual(fresh.counts.working, 0)

        // Every section the phone knows is one it can carry.
        for section in Snapshot.Section.allCases {
            var probe = Snapshot()
            XCTAssertTrue(probe.carry(section.rawValue, from: held), section.rawValue)
        }
    }

    func testAnOrdinaryAnswerIsNotUnchanged() throws {
        let answer = try decode(StateAnswer.self, #"{"generated_at": 12}"#)
        XCTAssertFalse(answer.unchanged)
        XCTAssertEqual(answer.stateDigest, "")
    }

    // MARK: - the snapshot

    func testAnEmptyObjectDecodesAsASnapshot() throws {
        let snapshot = try decode(Snapshot.self, "{}")
        XCTAssertEqual(snapshot.generatedAt, 0)
        XCTAssertEqual(snapshot.needsYouCount, 0)
        XCTAssertEqual(snapshot.stateDigest, "")
        XCTAssertFalse(snapshot.inbox.available)
        XCTAssertTrue(snapshot.inbox.acks.isEmpty)
        XCTAssertFalse(snapshot.security.available)
        XCTAssertTrue(snapshot.security.alerts.isEmpty)
        XCTAssertTrue(snapshot.decisionItems.isEmpty)
    }

    func testAnAbsentSecuritySectionListsNoAlertAndAPresentOneDoes() throws {
        let absent = try decode(Snapshot.self, #"{"board":{"cards":[]}}"#)
        XCTAssertFalse(absent.security.available)
        XCTAssertTrue(absent.decisionItems.isEmpty)
        XCTAssertFalse(absent.board.accessLogSupported)
        let present = try decode(Snapshot.self, """
        {"security":{"available":true,"alerts":[{"id":"b1","door":"lan",
         "door_word":"Wi-Fi","text":"5 refused","window_seconds":600}]},
         "board":{"access_log_supported":true}}
        """)
        XCTAssertTrue(present.security.available)
        XCTAssertEqual(present.security.alerts.map(\.id), ["b1"])
        XCTAssertEqual(present.security.alerts[0].windowSeconds, 600)
        XCTAssertEqual(present.security.alerts[0].doorText, "Wi-Fi")
        // A burst alert is the Profile screen's access log's, never a
        // Needs you entry (20 Sep 2026).
        XCTAssertTrue(present.decisionItems.isEmpty)
        // An older Mac sends no door word; the token stands in.
        let unworded = try decode(Snapshot.self, """
        {"security":{"available":true,"alerts":[{"id":"b2","door":"lan"}]}}
        """)
        XCTAssertEqual(unworded.security.alerts[0].doorText, "lan")
        XCTAssertEqual(present.needsYouCount, 0)
        XCTAssertTrue(present.board.accessLogSupported)
        // A wrong-typed section falls back rather than failing the frame.
        let wrong = try decode(Snapshot.self, #"{"security": 7}"#)
        XCTAssertFalse(wrong.security.available)
    }

    func testAbsentInboxLeavesNeedsYouCountUnchanged() throws {
        let body = """
        {"agents":{"waiting":[
          {"session_id":"q1","nickname":"Ask","project":"P",
           "questions":[{"text":"ok?"}]}
        ]}}
        """
        let snapshot = try decode(Snapshot.self, body)
        XCTAssertFalse(snapshot.inbox.available)
        XCTAssertTrue(snapshot.inbox.acks.isEmpty)
        XCTAssertEqual(snapshot.needsYouCount, 1)
    }

    func testWrongTypedSectionsFallBackToTheirDefaults() throws {
        // What `c.value` buys over `decodeIfPresent` alone: `decodeIfPresent`
        // throws on a present key of the wrong type, and one ragged section
        // would take the whole frame with it.
        let snapshot = try decode(
            Snapshot.self,
            #"{"counts": "nonsense", "agents": 7, "generated_at": 5}"#)
        XCTAssertEqual(snapshot.generatedAt, 5)
        XCTAssertEqual(snapshot.counts.working, 0)
        XCTAssertTrue(snapshot.agents.running.isEmpty)
    }

    // MARK: - a row

    func testAnAgentWithOnlyASessionIdDecodesWithEveryReachFalse() throws {
        let agent = try decode(Agent.self, #"{"session_id": "s"}"#)
        XCTAssertEqual(agent.sessionId, "s")
        XCTAssertFalse(agent.canClose)
        XCTAssertFalse(agent.canType)
        XCTAssertFalse(agent.canLowPriority)
    }

    func testAnAgentCarryingTheReachFlagsDecodesThemTrue() throws {
        let agent = try decode(
            Agent.self,
            #"{"session_id": "s", "can_close": true, "can_type": true, "can_low_priority": true}"#)
        XCTAssertTrue(agent.canClose)
        XCTAssertTrue(agent.canType)
        XCTAssertTrue(agent.canLowPriority)
    }

    // MARK: - the board

    func testAnEmptyBoardIsUnavailableRatherThanBroken() throws {
        let board = try decode(Board.self, "{}")
        XCTAssertFalse(board.available)
        XCTAssertTrue(board.cards.isEmpty)
    }

    /// An older Mac sends neither acknowledgement marker; both decode
    /// **false**, so Mark checked / Mark reviewed are drawn absent rather
    /// than present and 404ing inside the sealed reply.
    func testAnAbsentAckMarkerDecodesFalseAndAPresentOneTrue() throws {
        let older = try decode(Board.self, "{}")
        XCTAssertFalse(older.manualClearWritable)
        XCTAssertFalse(older.reviewWritable)
        let newer = try decode(
            Board.self, #"{"manual_clear_writable":true,"review_writable":true}"#)
        XCTAssertTrue(newer.manualClearWritable)
        XCTAssertTrue(newer.reviewWritable)
        let half = try decode(Board.self, #"{"review_writable":true}"#)
        XCTAssertFalse(half.manualClearWritable)
        XCTAssertTrue(half.reviewWritable)
    }

    func testACardWithOnlyAnIdTakesEveryDefault() throws {
        let card = try decode(BoardCard.self, #"{"id": "c"}"#)
        XCTAssertEqual(card.id, "c")
        XCTAssertEqual(card.revision, 0)
        XCTAssertEqual(card.priority, "")
        XCTAssertEqual(card.parallelLimit, 0)
        XCTAssertFalse(card.isQueued)
        XCTAssertFalse(card.isInFlight)
        XCTAssertFalse(card.isRefining)
        XCTAssertEqual(card.closedBy, "")
        XCTAssertNil(card.reviewedAt)
        XCTAssertFalse(card.awaitsReview)
    }

    func testClosedByAndReviewedAtDecodeReviewState() throws {
        let missing = try decode(BoardCard.self, #"{"id":"c","closed_by":"cipher"}"#)
        XCTAssertEqual(missing.closedBy, "cipher")
        XCTAssertNil(missing.reviewedAt)
        XCTAssertTrue(missing.awaitsReview)

        let nullReview = try decode(
            BoardCard.self, #"{"id":"c","closed_by":"cipher","reviewed_at":null}"#)
        XCTAssertNil(nullReview.reviewedAt)
        XCTAssertTrue(nullReview.awaitsReview)

        let malformed = try decode(
            BoardCard.self, #"{"id":"c","closed_by":"cipher","reviewed_at":"nope"}"#)
        XCTAssertNil(malformed.reviewedAt)
        XCTAssertTrue(malformed.awaitsReview)

        let zero = try decode(
            BoardCard.self, #"{"id":"c","closed_by":"cipher","reviewed_at":0}"#)
        XCTAssertEqual(zero.reviewedAt, 0)
        XCTAssertFalse(zero.awaitsReview)

        let namedOnly = try decode(
            BoardCard.self, #"{"id":"c","closed_by_name":"Cipher"}"#)
        XCTAssertEqual(namedOnly.closedBy, "")
        XCTAssertFalse(namedOnly.awaitsReview)
    }

    // MARK: - everything else a surface reads

    func testAWorkRecordFromNothingSaysItsFilesAreUnavailable() throws {
        let record = try decode(WorkRecord.self, "{}")
        XCTAssertFalse(record.filesAvailable)
        XCTAssertEqual(record.filesTotal, 0)
    }

    // MARK: - run health

    /// A card carrying `run_health` decodes it; a card without decodes
    /// `nil` (no run, not a zero line); an older Mac's board decodes the
    /// marker false, so the card screen draws the line absent.
    func testRunHealthDecodesPresentAbsentAndTheOlderMarker() throws {
        let with = try decode(BoardCard.self, """
            {"id":"c","run_health":{"class":"large","basis":"project",
             "turns":84,"tokens_k":2100,"ctx_pct":65,"asks":3,"refusals":1,
             "attempts":2,"returns":1,"fix_rounds":2,"attention":false,
             "live":true}}
            """)
        let health = try XCTUnwrap(with.runHealth)
        XCTAssertEqual(health.sizeClass, "large")
        XCTAssertEqual(health.basis, "project")
        XCTAssertEqual(health.turns, 84)
        XCTAssertEqual(health.tokensK, 2100)
        XCTAssertEqual(health.ctxPct, 65)
        XCTAssertEqual(health.asks, 3)
        XCTAssertEqual(health.refusals, 1)
        XCTAssertEqual(health.attempts, 2)
        XCTAssertEqual(health.returns, 1)
        XCTAssertEqual(health.fixRounds, 2)
        XCTAssertFalse(health.attention)
        XCTAssertTrue(health.live)
        XCTAssertEqual(RunHealthLine.text(health),
                       "LARGE · 84t · 2.1M · ctx 65% · asks 3 · refusals 1 · attempt 2 · back 1 · fixes 2")

        let without = try decode(BoardCard.self, #"{"id":"c"}"#)
        XCTAssertNil(without.runHealth)

        let partial = try decode(BoardCard.self, #"{"id":"c","run_health":{"class":""}}"#)
        let unsized = try XCTUnwrap(partial.runHealth)
        XCTAssertNil(unsized.turns)
        XCTAssertNil(unsized.fixRounds)
        XCTAssertEqual(RunHealthLine.text(unsized),
                       "UNSIZED · asks 0 · refusals 0 · attempt 0 · fixes –")

        let older = try decode(Board.self, "{}")
        XCTAssertFalse(older.runHealthSupported)
        let newer = try decode(Board.self, #"{"run_health_supported":true}"#)
        XCTAssertTrue(newer.runHealthSupported)
    }

    func testTheRemainingPublishedModelsAllSurviveAnEmptyObject() throws {
        XCTAssertNoThrow(try decode(UsageBar.self, "{}"))
        XCTAssertNoThrow(try decode(CardFull.self, "{}"))
        XCTAssertNoThrow(try decode(PhoneDevices.self, "{}"))
        XCTAssertNoThrow(try decode(LogPage.self, "{}"))
        XCTAssertNoThrow(try decode(Counts.self, "{}"))
        XCTAssertNoThrow(try decode(Agents.self, "{}"))
        XCTAssertNoThrow(try decode(WorkRecordFile.self, "{}"))
    }

    func testACardFullFromNothingIsUnavailableAndHoldsNoCard() throws {
        let full = try decode(CardFull.self, "{}")
        XCTAssertFalse(full.available)
        XCTAssertNil(full.card)
        XCTAssertFalse(full.plan.available)
    }

    // MARK: - this phone's own lease

    func testAnUnknownDeviceHasNoLease() throws {
        let devices = try decode(
            PhoneDevices.self,
            #"{"devices": [{"id": "abc", "lease_expires_at": 42}]}"#)
        XCTAssertEqual(devices.leaseExpiresAt(deviceId: "abc"), 42)
        XCTAssertEqual(devices.leaseExpiresAt(deviceId: "somebody-else"), 0)
    }
}

extension DecodeToleranceTests {
    func testCollaborationMissingNullRaggedAndUnknownVersion() throws {
        for raw in ["{}", #"{"collaboration":null}"#, #"{"collaboration":42}"#] {
            let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(raw.utf8))
            XCTAssertNil(snapshot.collaboration)
        }
        let future = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"collaboration":{"version":99,"available":true}}
        """.utf8))
        XCTAssertFalse(future.collaboration!.supported)
        let ragged = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"collaboration":{"version":1,"available":true,"partial":false,
          "nodes":[null,42,{"id":"valid","kind":"future"}],"edges":[]}}
        """.utf8))
        XCTAssertEqual(ragged.collaboration?.nodes.map(\.id), ["valid"])
        XCTAssertTrue(ragged.collaboration!.partial)
        XCTAssertFalse(ragged.collaboration!.nodes[0].known)
    }
}
