import XCTest
@testable import BobPanel

/// The card timeline's shared rule, tabled.
///
/// `plans/2026-09-13-card-timeline.md`. `CardTimeline` turns the daemon's
/// `timeline` report into rows and one open line; both card screens draw
/// those and re-derive nothing. The `elapsed` table here is the same fixture
/// `host/tests/test_card_timeline.py` runs against the Python
/// `elapsed_text`, so the two formatters cannot drift apart.
final class CardTimelineTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    /// The shared fixture table — every row is asserted on the Python side
    /// too, by name.
    func testElapsedMatchesTheSharedTable() {
        let table: [(Double?, String)] = [
            (0, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"),
            (7800, "2h 10m"), (86400, "1d"), (100800, "1d 4h"), (nil, ""),
        ]
        for (seconds, want) in table {
            XCTAssertEqual(CardTimeline.elapsed(seconds), want, "\(String(describing: seconds))")
        }
        XCTAssertEqual(CardTimeline.elapsed(-5), "")
        XCTAssertEqual(CardTimeline.elapsed(.nan), "")
    }

    func testRowsKeepTheDaemonsOrderAndWordTheGaps() {
        let report = CardTimelineReport(generatedAt: 1_000, steps: [
            CardTimelineStep(kind: "created", label: "Written down", at: 100),
            CardTimelineStep(kind: "plan_attached", label: "Plan attached", at: nil,
                             gap: "not observed"),
            CardTimelineStep(kind: "queued", label: "Queued", at: 700,
                             gap: "not observed"),
            CardTimelineStep(kind: "started", label: "Started", at: 710,
                             note: "attempt 2", sincePreviousSeconds: 10),
            CardTimelineStep(kind: "permission_asks", label: "3 permission asks",
                             at: 800, note: "3 asks", durable: false,
                             sincePreviousSeconds: 90),
        ])
        let rows = CardTimeline.rows(report)
        XCTAssertEqual(rows.map(\.label),
                       ["Written down", "Plan attached", "Queued", "Started",
                        "3 permission asks"])
        // The first row has no gap at all.
        XCTAssertNil(rows[0].gap)
        // An unobserved moment reads "not observed" in the time column and
        // in the gap after it; the observed neighbour after it still says
        // its gap was not observed.
        XCTAssertEqual(rows[1].when, CardTimeline.notObserved)
        XCTAssertEqual(rows[1].gap, "not observed")
        XCTAssertNotEqual(rows[2].when, CardTimeline.notObserved)
        XCTAssertEqual(rows[2].gap, "not observed")
        // A measured gap is the elapsed figure.
        XCTAssertEqual(rows[3].gap, "10s")
        XCTAssertEqual(rows[3].note, "attempt 2")
        XCTAssertEqual(rows[4].gap, "1m")
        XCTAssertFalse(rows[4].durable)
        // Words only — no row carries a colour.
        XCTAssertEqual(rows.map(\.id), [0, 1, 2, 3, 4])
    }

    func testOpenLineWithAndWithoutSince() {
        let seen = CardTimelineReport(
            generatedAt: 1_000,
            open: CardTimelineOpen(kind: "review", label: "Waiting for your review",
                                   since: 1_000 - 180_000))
        XCTAssertEqual(CardTimeline.openLine(seen, now: seen.generatedAt),
                       "Waiting for your review · 2d 2h")
        let unseen = CardTimelineReport(
            open: CardTimelineOpen(kind: "planned",
                                   label: "Planned, waiting to be started",
                                   since: nil))
        XCTAssertEqual(CardTimeline.openLine(unseen, now: 5),
                       "Planned, waiting to be started · not observed")
        XCTAssertNil(CardTimeline.openLine(CardTimelineReport(), now: 5))
    }

    /// The daemon legitimately omits `at`, `since_previous_seconds`, `open`
    /// and `note`; an older daemon sends no `steps` at all. None of those
    /// may throw, and an unknown `kind` is kept and drawn by its label.
    func testDecodeTolerance() throws {
        let missingAt = try decode(CardTimelineReport.self, """
        {"available": true, "generated_at": 50,
         "steps": [{"kind": "plan_attached", "label": "Plan attached",
                    "observed": false, "gap": "not observed"}]}
        """)
        XCTAssertTrue(missingAt.available)
        XCTAssertEqual(missingAt.steps.count, 1)
        XCTAssertNil(missingAt.steps[0].at)
        XCTAssertFalse(missingAt.steps[0].observed)
        XCTAssertNil(missingAt.steps[0].sincePreviousSeconds)
        XCTAssertEqual(missingAt.steps[0].note, "")
        XCTAssertNil(missingAt.open)
        XCTAssertEqual(CardTimeline.rows(missingAt)[0].when, CardTimeline.notObserved)

        let bare = try decode(CardTimelineReport.self, "{}")
        XCTAssertFalse(bare.available)
        XCTAssertEqual(bare.steps, [])
        XCTAssertNil(bare.open)
        XCTAssertEqual(CardTimeline.rows(bare), [])

        let unknown = try decode(CardTimelineReport.self, """
        {"available": true, "steps": [{"kind": "teleported", "label": "Teleported",
                                        "at": 12, "observed": true}],
         "open": {"kind": "elsewhere", "label": "Somewhere new", "since": 2}}
        """)
        XCTAssertEqual(CardTimeline.rows(unknown).map(\.label), ["Teleported"])
        XCTAssertEqual(CardTimeline.openLine(unknown, now: 62), "Somewhere new · 1m")

        // And the report rides `BoardReport` as a sibling of the cards,
        // absent from an older daemon.
        let older = try decode(BoardReport.self, #"{"available": true, "cards": []}"#)
        XCTAssertNil(older.timeline)
        let newer = try decode(BoardReport.self, """
        {"available": true, "cards": [], "timeline": {"available": true, "steps": []}}
        """)
        XCTAssertNotNil(newer.timeline)
    }

    func testTheMirroredConstantsAreTheDaemonsWords() {
        XCTAssertEqual(CardTimeline.notObserved, "not observed")
        XCTAssertEqual(CardTimeline.recentOnlyNote,
                       "Permission asks older than a day are not kept.")
    }
}

extension CardTimelineStep: Equatable {
    public static func == (lhs: CardTimelineStep, rhs: CardTimelineStep) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.gap == rhs.gap
    }
}
