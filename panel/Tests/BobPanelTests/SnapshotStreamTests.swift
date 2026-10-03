import XCTest
@testable import BobPanel

/// The stream's two off-main helpers: the clock-strip the dedupe compares
/// with, and the slim-frame section carry (`Snapshot.carried`). Absent must
/// never decode as empty, and present-but-undecodable must never be mistaken
/// for a slim omission — it is rejected, whole frame and all.
final class SnapshotStreamTests: XCTestCase {

    // MARK: - strippingGeneratedAt

    func testStrippingGeneratedAtTable() {
        let cases: [(input: String, expected: String, note: String)] = [
            // No key at all: identity, via the early return.
            (#"{"counts": {}}"#, #"{"counts": {}}"#, "no key"),
            // Top-level pair with a trailing comma: pair and comma go.
            (#"{"generated_at": 123.45, "counts": {}}"#,
             #"{ "counts": {}}"#, "leading pair"),
            // Pair at the end, no trailing comma: the comma before it stays,
            // exactly as the regex it replaced would have left it.
            (#"{"a":1,"generated_at": 9}"#, #"{"a":1,}"#, "trailing pair"),
            // Both clocks — the top level's and the board section's.
            (#"{"generated_at":1,"board":{"generated_at":2.5,"cards":[]}}"#,
             #"{"board":{"cards":[]}}"#, "two clocks"),
            // The key with a non-number value is kept, as the regex kept it.
            (#"{"generated_at": "abc"}"#, #"{"generated_at": "abc"}"#,
             "non-number value"),
            // The key's text as a string *value* is not a pair and is kept.
            (#"{"note": "generated_at"}"#, #"{"note": "generated_at"}"#,
             "key text as value"),
        ]
        for c in cases {
            XCTAssertEqual(DaemonClient.strippingGeneratedAt(c.input),
                           c.expected, c.note)
        }
    }

    func testStrippingIsStableSoDuplicatesCompareEqual() {
        // Two frames identical but for their clocks must normalize equal —
        // that equality is the whole dedupe.
        let a = #"{"generated_at": 100.5, "counts": {"working": 2}}"#
        let b = #"{"generated_at": 999.9, "counts": {"working": 2}}"#
        XCTAssertEqual(DaemonClient.strippingGeneratedAt(a),
                       DaemonClient.strippingGeneratedAt(b))
    }

    // MARK: - the carried sections

    /// One `Snapshot.Section` per key `_OMITTABLE_SECTIONS` names, and a
    /// non-empty JSON value for each that must survive the round trip.
    private static let sectionSamples: [(Snapshot.Section, String, String)] = [
        (.counts, "counts", #"{"working": 3}"#),
        (.notifications, "notifications",
         #"[{"session_id":"s1","question":"?"}]"#),
        (.agents, "agents", #"{"running":[{"session_id":"s1"}]}"#),
        (.signals, "signals", #"[{"text":"budget"}]"#),
        (.collaboration, "collaboration", #"{"version":1,"available":true,"nodes":[],"edges":[]}"#),
        (.permissions, "permissions",
         #"[{"request_id":"r1","session_id":"s1"}]"#),
        (.board, "board", #"{"available": true, "cards": []}"#),
        (.enrollment, "enrollment", #"{"available": true, "enrolled": []}"#),
        (.devices, "devices", #"{"available": true, "devices": []}"#),
        (.inbox, "inbox", #"{"available": true, "acks": []}"#),
        (.security, "security", #"{"available": true, "alerts": []}"#),
        (.mission, "mission", #"{"available": true, "alive": false}"#),
        (.review, "review", #"{"available": true, "runs": []}"#),
    ]

    func testEverySectionIsCoveredByTheCarryRule() {
        // The set the decoder marks and the set the client copies are the
        // same thirteen — the daemon's `_OMITTABLE_SECTIONS` minus `mesh` and `power`,
        // which the panel does not decode.
        XCTAssertEqual(Snapshot.Section.allCases.count, 13)
        XCTAssertEqual(Set(Self.sectionSamples.map(\.0)),
                       Set(Snapshot.Section.allCases))
    }

    func testAPresentSectionDecodesAndIsNotMarkedCarried() throws {
        for (section, key, value) in Self.sectionSamples {
            let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
            {"generated_at": 1, "\(key)": \(value)}
            """.utf8))
            XCTAssertFalse(snap.carried.contains(section),
                           "\(key) was present and must not be carried")
            // The other twelve were absent, so they are.
            XCTAssertEqual(snap.carried.count, 12, key)
        }
    }

    func testAnAbsentSectionIsMarkedCarriedAndNeverDecodesAsEmpty() throws {
        // The idle slim frame: the clock and the two scalars, nothing else.
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 5, "reconciler_available": true,
         "reconciler_error": ""}
        """.utf8))
        XCTAssertEqual(snap.carried, Set(Snapshot.Section.allCases))
        XCTAssertEqual(snap.generatedAt, 5)
        XCTAssertEqual(snap.reconcilerAvailable, true)
        // Nothing downstream ever sees these defaults — `apply` fills every
        // one of them forward — but they must not read as *news*.
        XCTAssertFalse(snap.board.available)
        XCTAssertFalse(snap.enrollment.available)
    }

    func testAPresentButUndecodableSectionIsRejectedWholeNotCarried() {
        // A present section that cannot decode is a schema disagreement
        // between builds, not a slim omission: the decode throws, which
        // `parseFrame` turns into a REJECTED line and a discarded frame —
        // nothing from it is applied and nothing is carried out of it.
        for (_, key, _) in Self.sectionSamples {
            let text = """
            {"generated_at": 7, "\(key)": 5}
            """
            XCTAssertThrowsError(
                try JSONDecoder().decode(Snapshot.self, from: Data(text.utf8)),
                "\(key): a present-but-undecodable section must reject")
        }
    }

    func testAnEmptiedSectionIsAppliedNotCarried() throws {
        // Emptiness is a real state and only absence is the marker.
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "board": {"available": false, "cards": []},
         "agents": {"running": []}, "notifications": []}
        """.utf8))
        XCTAssertFalse(snap.carried.contains(.board))
        XCTAssertFalse(snap.carried.contains(.agents))
        XCTAssertFalse(snap.carried.contains(.notifications))
    }

    // MARK: - the fill-forward, as `DaemonClient.apply` runs it

    /// `apply`'s loop, in the two lines that matter: an absent section is
    /// filled from the store, a present one becomes the store and stamps.
    private func fill(_ frame: Snapshot, store: inout Snapshot,
                      stamps: inout [Snapshot.Section: Double]) -> Snapshot {
        var snap = frame
        for section in Snapshot.Section.allCases {
            if snap.carried.contains(section) {
                DaemonClient.copy(section, from: store, into: &snap)
            } else {
                DaemonClient.copy(section, from: snap, into: &store)
                stamps[section] = snap.generatedAt
            }
        }
        snap.agentsStamp = stamps[.agents] ?? snap.generatedAt
        snap.collaborationStamp = stamps[.collaboration] ?? snap.generatedAt
        return snap
    }

    func testEverySectionIsFilledForwardWhenTheFrameOmitsIt() throws {
        var store = Snapshot()
        var stamps: [Snapshot.Section: Double] = [:]
        let full = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 100, "counts": {"working": 3},
         "notifications": [{"session_id":"s1","question":"?"}],
         "agents": {"running":[{"session_id":"s1","idle_seconds":60}]},
         "signals": [{"text":"budget"}],
         "permissions": [{"request_id":"r1","session_id":"s1"}],
         "board": {"available": true, "cards": []},
         "enrollment": {"available": true, "enrolled": []},
         "devices": {"available": true, "devices": []}}
        """.utf8))
        _ = fill(full, store: &store, stamps: &stamps)

        // A frame that says nothing at all still applies as a complete
        // picture — this is what a quiet second now looks like on the wire.
        let idle = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 220}
        """.utf8))
        let applied = fill(idle, store: &store, stamps: &stamps)
        XCTAssertEqual(applied.counts.working, 3)
        XCTAssertEqual(applied.notifications.count, 1)
        XCTAssertEqual(applied.agents.running.count, 1)
        XCTAssertEqual(applied.signals.count, 1)
        XCTAssertEqual(applied.permissions.count, 1)
        XCTAssertTrue(applied.board.available)
        XCTAssertTrue(applied.enrollment.available)
        XCTAssertTrue(applied.devices.available)
        // The frame's own clock is the frame's; the fleet's stamp is the
        // frame that last carried a fleet.
        XCTAssertEqual(applied.generatedAt, 220)
        XCTAssertEqual(applied.agentsStamp, 100)
    }

    func testAFrameThatCarriesTheFleetStampsItWithItsOwnClock() throws {
        var store = Snapshot()
        var stamps: [Snapshot.Section: Double] = [:]
        let first = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 100, "agents": {"running":[{"session_id":"s1"}]}}
        """.utf8))
        XCTAssertEqual(fill(first, store: &store, stamps: &stamps).agentsStamp,
                       100)
        let second = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 300, "agents": {"running":[{"session_id":"s2"}]}}
        """.utf8))
        let applied = fill(second, store: &store, stamps: &stamps)
        XCTAssertEqual(applied.agentsStamp, 300)
        XCTAssertEqual(applied.agents.running.first?.sessionId, "s2")
    }

    func testAFullFrameClientAndAnOlderDaemonStampTheFleetAtTheClock() throws {
        // No slim opt-in and no omission: every frame carries every section,
        // so the stamp is the clock and `AgentFacts.aged` adds nothing.
        var store = Snapshot()
        var stamps: [Snapshot.Section: Double] = [:]
        let frame = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 42, "counts": {}, "notifications": [],
         "agents": {"running":[]}, "signals": [], "mesh": [], "collaboration": {"version":1,"available":false},
         "permissions": [], "board": {}, "enrollment": {}, "devices": {}, "inbox": {},
         "security": {}, "mission": {}, "review": {}}
        """.utf8))
        let applied = fill(frame, store: &store, stamps: &stamps)
        XCTAssertTrue(applied.carried.isEmpty)
        XCTAssertEqual(applied.agentsStamp, applied.generatedAt)
        XCTAssertEqual(AgentFacts.aged(60, asOf: applied.agentsStamp,
                                       now: applied.generatedAt), 60)
    }

    // MARK: - AgentFacts.aged

    func testAgedCountsUpFromTheStampTheFleetCameWith() {
        // Sixty seconds idle, composed at 1000, read at 1180: three minutes.
        XCTAssertEqual(AgentFacts.aged(60, asOf: 1_000, now: 1_180), 240)
        // Read at the moment it was composed: unchanged.
        XCTAssertEqual(AgentFacts.aged(60, asOf: 1_000, now: 1_000), 60)
        // No stamp yet — a default snapshot, or an older daemon before the
        // first frame — leaves the figure exactly as it arrived.
        XCTAssertEqual(AgentFacts.aged(60, asOf: 0, now: 9_999), 60)
        // A clock that has gone backwards adds nothing rather than
        // subtracting: the figure never goes down.
        XCTAssertEqual(AgentFacts.aged(60, asOf: 1_000, now: 900), 60)
    }

    func testTheWaitingTileCountsUpWhileTheFleetIsOmitted() throws {
        let agent = try JSONDecoder().decode(Agent.self, from: Data(
            #"{"session_id":"s1","idle_seconds":60}"#.utf8))
        let frozen = AgentFacts.tiles(agent: agent, category: .waiting,
                                      live: true, asOf: 1_000, now: 1_000)
        XCTAssertEqual(frozen.first { $0.id == "waiting" }?.value, "1m")
        let later = AgentFacts.tiles(agent: agent, category: .waiting,
                                     live: true, asOf: 1_000, now: 1_180)
        XCTAssertEqual(later.first { $0.id == "waiting" }?.value, "4m")
    }
}

extension SnapshotStreamTests {
    func testCollaborationCarryStampEmptyClearAndMalformedPresent() throws {
        var store = Snapshot()
        var stamps: [Snapshot.Section: Double] = [:]
        let full = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":100,"collaboration":{"version":1,"available":true,"partial":false,
          "nodes":[{"id":"a","kind":"session"}],"edges":[]}}
        """.utf8))
        _ = fill(full, store: &store, stamps: &stamps)
        let idle = try JSONDecoder().decode(Snapshot.self, from: Data(#"{"generated_at":200}"#.utf8))
        let carried = fill(idle, store: &store, stamps: &stamps)
        XCTAssertEqual(carried.collaboration?.nodes.count, 1)
        XCTAssertEqual(carried.collaborationStamp, 100)
        let empty = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":300,"collaboration":{"version":1,"available":true,"partial":false,"nodes":[],"edges":[]}}
        """.utf8))
        let cleared = fill(empty, store: &store, stamps: &stamps)
        XCTAssertEqual(cleared.collaboration?.nodes.count, 0)
        XCTAssertEqual(cleared.collaborationStamp, 300)
        for malformed in ["null", "42", "[]", #""wrong""#] {
            XCTAssertThrowsError(try JSONDecoder().decode(Snapshot.self, from: Data("{\"collaboration\":\(malformed)}".utf8)))
        }
        XCTAssertNil(try JSONDecoder().decode(Snapshot.self, from: Data("{}".utf8)).collaboration)
    }

    // MARK: - the enrolment checklist facts

    /// The additive `checklist` block inside `enrollment`: decoded when
    /// present, carried with the section when a slim frame omits it,
    /// replaced when a later frame changes an observation, and — on a
    /// reconnect's full frame — simply re-stated. A present-but-malformed
    /// enrolment section still rejects the frame, so a schema disagreement
    /// never erases progress already on screen.
    func testChecklistFactsRideTheEnrolmentSectionAndCarryForward() throws {
        var store = Snapshot()
        var stamps: [Snapshot.Section: Double] = [:]
        let full = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 100, "enrollment": {"available": true,
          "enrolled": [{"root": "/p/a", "label": "a"}], "pending": [], "other": 0,
          "checklist": {"available": true,
            "roots": [{"root": "/p/a", "editor_observed": true, "session_observed": false}]}}}
        """.utf8))
        let applied = fill(full, store: &store, stamps: &stamps)
        XCTAssertEqual(applied.enrollment.checklist?.available, true)
        XCTAssertEqual(applied.enrollment.checklist?.facts(for: "/p/a")?.editorObserved, true)
        XCTAssertEqual(applied.enrollment.checklist?.facts(for: "/p/a")?.sessionObserved, false)

        // Slim frame: the whole section is absent and fills forward, facts included.
        let slim = try JSONDecoder().decode(Snapshot.self, from: Data(#"{"generated_at": 110}"#.utf8))
        let carried = fill(slim, store: &store, stamps: &stamps)
        XCTAssertTrue(slim.carried.contains(.enrollment))
        XCTAssertEqual(carried.enrollment.checklist?.facts(for: "/p/a")?.editorObserved, true)

        // A changed observation replaces the held one.
        let changed = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 120, "enrollment": {"available": true,
          "enrolled": [{"root": "/p/a", "label": "a"}],
          "checklist": {"available": true,
            "roots": [{"root": "/p/a", "editor_observed": true, "session_observed": true}]}}}
        """.utf8))
        let advanced = fill(changed, store: &store, stamps: &stamps)
        XCTAssertEqual(advanced.enrollment.checklist?.facts(for: "/p/a")?.sessionObserved, true)

        // A reconnect's attach frame is always full; it re-states rather than carries.
        let reconnect = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 130, "counts": {}, "agents": {}, "enrollment": {"available": true,
          "enrolled": [], "checklist": {"available": true, "roots": []}}}
        """.utf8))
        let fresh = fill(reconnect, store: &store, stamps: &stamps)
        XCTAssertFalse(reconnect.carried.contains(.enrollment))
        XCTAssertEqual(fresh.enrollment.checklist?.roots.count, 0)
    }

    func testAnEnrolmentSectionWithoutAChecklistIsUnsupportedNotEmpty() throws {
        let older = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "enrollment": {"available": true, "enrolled": []}}
        """.utf8))
        XCTAssertNil(older.enrollment.checklist)
        XCTAssertEqual(FirstRunChecklist.decide(enrollment: older.enrollment, completed: false),
                       .unsupported)
    }

    func testARaggedChecklistBlockDecodesToDefaultsAndAMalformedSectionRejects() throws {
        // Ragged rows inside the block are tolerated: no key means false.
        let ragged = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "enrollment": {"available": true, "enrolled": [],
          "checklist": {"roots": [{"root": "/p/a"}, {"editor_observed": true}]}}}
        """.utf8))
        XCTAssertEqual(ragged.enrollment.checklist?.available, false)
        XCTAssertEqual(ragged.enrollment.checklist?.roots.count, 2)
        XCTAssertEqual(ragged.enrollment.checklist?.facts(for: "/p/a")?.editorObserved, false)
        // A wrong-typed block is no block, not a rejected frame.
        let wrongBlock = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "enrollment": {"available": true, "enrolled": [], "checklist": 5}}
        """.utf8))
        XCTAssertNil(wrongBlock.enrollment.checklist)
        // The section itself being wrong-typed still rejects the whole frame.
        XCTAssertThrowsError(try JSONDecoder().decode(
            Snapshot.self, from: Data(#"{"generated_at": 1, "enrollment": 5}"#.utf8)))
    }

    // MARK: - prepareFrame, the one strip → dedupe → decode

    /// The refresh path awaits this from the main actor; the whole-fleet
    /// parse must still land on the cooperative pool (SE-0338). If the
    /// package ever adopts `NonisolatedNonsendingByDefault`, this is the
    /// assertion that notices the decode moved back onto the main thread.
    @MainActor
    func testPrepareFrameRunsOffTheMainActor() async throws {
        XCTAssertTrue(Thread.isMainThread)
        let text = #"{"generated_at": 3, "board": {"available": true, "cards": []}}"#
        let prepared = await DaemonClient.prepareFrame(text, lastApplied: "", via: "test")
        let frame = try XCTUnwrap(prepared)
        XCTAssertFalse(frame.decodedOnMain)
        XCTAssertEqual(frame.snapshot.generatedAt, 3)
        XCTAssertTrue(frame.snapshot.board.available)
        XCTAssertFalse(frame.snapshot.carried.contains(.board))
        XCTAssertEqual(frame.normalized, DaemonClient.strippingGeneratedAt(text))
    }

    /// Same payload but for its clock: skipped before any decode.
    func testPrepareFrameSkipsADuplicateBeforeDecoding() async {
        let first = #"{"generated_at": 1, "counts": {"working": 2}}"#
        let second = #"{"generated_at": 9, "counts": {"working": 2}}"#
        let applied = DaemonClient.strippingGeneratedAt(first)
        let skipped = await DaemonClient.prepareFrame(second, lastApplied: applied, via: "test")
        XCTAssertNil(skipped)
        // And news against the same baseline is not skipped.
        let news = #"{"generated_at": 9, "counts": {"working": 3}}"#
        let kept = await DaemonClient.prepareFrame(news, lastApplied: applied, via: "test")
        XCTAssertNotNil(kept)
    }

    /// A present-but-undecodable section rejects the whole frame here too.
    func testPrepareFrameRejectsAnUndecodablePayload() async {
        let broken = await DaemonClient.prepareFrame(
            #"{"generated_at": 1, "board": 5}"#, lastApplied: "", via: "test")
        XCTAssertNil(broken)
        let notJSON = await DaemonClient.prepareFrame("not json", lastApplied: "", via: "test")
        XCTAssertNil(notJSON)
    }

    /// A `/api/state` read whose off-actor decode was overtaken by a stream
    /// frame is dropped: the stream's frame is newer.
    func testARefreshOvertakenDuringItsDecodeIsDropped() {
        XCTAssertFalse(DaemonClient.overtaken(before: "a", now: "a"))
        XCTAssertTrue(DaemonClient.overtaken(before: "a", now: "b"))
        XCTAssertTrue(DaemonClient.overtaken(before: "", now: "b"),
                      "the first frame landing during the first read")
    }
}
