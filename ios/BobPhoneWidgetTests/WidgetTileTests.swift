import XCTest

/// The home-screen tile and the Lock Screen circle, tested for the first
/// time.
///
/// `host/tests/test_phone_widget.py` asserts that `TickRing.spoken(` is
/// *called*, that `rotationHours = 12`, and that the slogan literals are
/// short. None of that reaches the arithmetic: the 30° comfort step, the
/// `min`/`max` clamps, the three-way centre, the double-modulus that keeps a
/// pre-1970 bucket in range, or the deliberate exclusion of `faces` from
/// `figures`.
///
/// No `import BobPhone` and no `@testable`: the widget's five sources are
/// compiled into this bundle, so its types are internal to this module.
/// The trade-off is stated in the plan — these exercise a second compilation
/// of those sources rather than the shipped appex binary; the appex is still
/// linked in the same run, because the app target depends on it.
///
/// Seam: plain values and `JSONDecoder` over string literals. Nothing here
/// reads or writes the note on disk — that touches a real App Group
/// container; only the `Codable` half is exercised.
///
/// One consequence to keep in mind before adding a case here: `Bundle.main`
/// in the shipped appex *is* the appex, but this bundle is hosted by
/// `BobPhone.app`, so here it is the **app**. A portrait- or brand-loading
/// test added to this target would therefore read the app's resources and
/// pass even if that resource had fallen out of the appex's own copy phase.
/// Keep such a test in `host/tests/test_portrait_tree.py`, where it can see
/// both trees.
@MainActor
final class WidgetTileTests: XCTestCase {

    // MARK: - the ring's step

    func testTheStepIsThirtyDegreesUpToTheComfortFloor() {
        XCTAssertEqual(TickRing.step(live: 0), 30.0, accuracy: 0.0001)
        XCTAssertEqual(TickRing.step(live: 1), 30.0, accuracy: 0.0001)
        XCTAssertEqual(TickRing.step(live: 12), 30.0, accuracy: 0.0001)
    }

    func testAboveTheFloorTheCircleIsDividedByTheRealCount() {
        XCTAssertEqual(TickRing.step(live: 13), 360.0 / 13.0, accuracy: 0.0001)
    }

    // MARK: - the ticks

    func testTicksAscendByTheStepWithTheNeedyOnesHeavyAndFirst() {
        let ticks = TickRing.ticks(needsYou: 2, working: 3)
        XCTAssertEqual(ticks.count, 5)
        let step = TickRing.step(live: 5)
        for (index, tick) in ticks.enumerated() {
            XCTAssertEqual(tick.degrees, Double(index) * step, accuracy: 0.0001)
            XCTAssertEqual(tick.heavy, index < 2, "tick \(index)")
        }
    }

    func testNegativeCountsClampToZeroRatherThanCrashing() {
        // `0..<live` traps on a negative bound; the clamps are what stops a
        // ragged snapshot taking the tile down.
        XCTAssertTrue(TickRing.ticks(needsYou: -3, working: -2).isEmpty)
        XCTAssertEqual(TickRing.ticks(needsYou: -3, working: 2).count, 2)
    }

    // MARK: - thinning and the pen widths

    func testThinningIsWholeUpToTwelveAndLowerAbove() {
        XCTAssertEqual(TickRing.thinning(live: 0), 1.0, accuracy: 0.0001)
        XCTAssertEqual(TickRing.thinning(live: 12), 1.0, accuracy: 0.0001)
        XCTAssertLessThan(TickRing.thinning(live: 24), 1.0)
        XCTAssertGreaterThan(TickRing.thinning(live: 24), 0.0)
    }

    func testThePenWidthsHoldTheirFloorsOnACrowdedRing() {
        XCTAssertEqual(TickRing.heavyWidth(live: 100), 1.6, accuracy: 0.0001)
        XCTAssertEqual(TickRing.hairWidth(live: 100), 0.6, accuracy: 0.0001)
        XCTAssertGreaterThan(TickRing.heavyWidth(live: 1),
                             TickRing.hairWidth(live: 1))
    }

    // MARK: - the centre

    func testAnIdleCentreDrawsADashAndSaysIdle() {
        let centre = TickRing.centre(needsYou: 0, working: 0, stale: false)
        XCTAssertEqual(centre.text, "–")
        XCTAssertEqual(centre.caption, "IDLE")
        XCTAssertFalse(centre.waiting)
    }

    func testSomebodyWaitingOutranksWork() {
        let centre = TickRing.centre(needsYou: 2, working: 3, stale: false)
        XCTAssertEqual(centre.text, "2")
        XCTAssertEqual(centre.caption, "NEEDS")
        XCTAssertTrue(centre.waiting)
    }

    func testWorkAloneCountsTheWorkers() {
        let centre = TickRing.centre(needsYou: 0, working: 3, stale: false)
        XCTAssertEqual(centre.text, "3")
        XCTAssertEqual(centre.caption, "WORK")
        XCTAssertFalse(centre.waiting)
    }

    func testStaleAppendsOneQuestionMarkToTheCaptionAndNothingElse() {
        for (needs, work) in [(0, 0), (2, 3), (0, 3)] {
            let fresh = TickRing.centre(needsYou: needs, working: work,
                                        stale: false)
            let stale = TickRing.centre(needsYou: needs, working: work,
                                        stale: true)
            XCTAssertEqual(stale.caption, fresh.caption + "?")
            XCTAssertEqual(stale.text, fresh.text)
            XCTAssertEqual(stale.waiting, fresh.waiting)
        }
    }

    // MARK: - what VoiceOver reads

    func testTheSpokenSentenceCoversTheThreeStatesAndTheStaleTail() {
        XCTAssertEqual(TickRing.spoken(needsYou: 0, working: 0, stale: false),
                       "no agents running")
        XCTAssertEqual(TickRing.spoken(needsYou: 2, working: 3, stale: false),
                       "2 need you, 3 working")
        XCTAssertEqual(TickRing.spoken(needsYou: 0, working: 3, stale: false),
                       "nobody needs you, 3 working")

        let stale = TickRing.spoken(needsYou: 2, working: 3, stale: true)
        XCTAssertTrue(stale.hasPrefix("2 need you, 3 working"), stale)
        XCTAssertTrue(stale.hasSuffix("a while ago"), stale)
    }

    // MARK: - whose face this hour wears

    func testTheHourIsEpochBucketsAndNotACalendarValue() {
        // No calendar, no time zone, no locale: a local hour repeats once a
        // year at the DST fallback, which is a tile that visibly goes
        // backwards.
        XCTAssertEqual(FaceRotation.hour(Date(timeIntervalSince1970: 7_200)), 2)
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        XCTAssertEqual(FaceRotation.hour(now.addingTimeInterval(3_600))
                       - FaceRotation.hour(now), 1)
    }

    private func faces(_ count: Int) -> [FleetSummary.Face] {
        (0..<count).map { FleetSummary.Face(slug: "slug\($0)", name: "n\($0)") }
    }

    func testAnEmptyRosterDrawsNoFace() {
        XCTAssertNil(FaceRotation.face([], at: Date()))
    }

    func testTheFaceIsTheHourModuloTheRoster() throws {
        let roster = faces(3)
        let date = Date(timeIntervalSince1970: 7_200)   // hour 2
        XCTAssertEqual(FaceRotation.face(roster, at: date)?.slug, "slug2")
        let next = Date(timeIntervalSince1970: 7_200 + 3_600)   // hour 3
        XCTAssertEqual(FaceRotation.face(roster, at: next)?.slug, "slug0")
    }

    func testAPre1970DateStillGetsAFace() {
        // The whole reason for `((h % n) + n) % n`: a negative bucket would
        // index out of range and trap.
        let roster = faces(3)
        let ancient = Date(timeIntervalSince1970: -7_200)
        XCTAssertNotNil(FaceRotation.face(roster, at: ancient))
    }

    // MARK: - the slogans

    func testASloganIsStableWithinAnHourAndMovesAcrossOne() {
        let date = Date(timeIntervalSince1970: 1_700_000_000)
        XCTAssertEqual(WidgetSlogans.line(for: date),
                       WidgetSlogans.line(for: date.addingTimeInterval(60)))
        var seen: Set<String> = []
        for step in 0..<WidgetSlogans.lines.count {
            seen.insert(WidgetSlogans.line(
                for: date.addingTimeInterval(Double(step) * 3_600)))
        }
        XCTAssertEqual(seen.count, WidgetSlogans.lines.count)
    }

    func testANegativeHourStillPicksALine() {
        XCTAssertFalse(
            WidgetSlogans.line(for: Date(timeIntervalSince1970: -7_200)).isEmpty)
    }

    func testEveryShippedSloganFitsTheColumn() {
        for line in WidgetSlogans.lines {
            XCTAssertLessThanOrEqual(line.count, WidgetSlogans.maxLineChars, line)
        }
    }

    // MARK: - the note the app leaves for the tile

    private func summary(_ json: String) throws -> FleetSummary {
        try JSONDecoder().decode(FleetSummary.self, from: Data(json.utf8))
    }

    func testANoteFromAnOlderAppKeepsTheDefaultDimAfter() throws {
        // Six hundred, not "open Dark Army": a summary written before `dimAfter`
        // existed must still draw.
        let note = try summary("{}")
        XCTAssertEqual(note.dimAfter, 600)
        XCTAssertEqual(note.needsYou, 0)
        XCTAssertEqual(note.working, 0)
        XCTAssertEqual(note.todo, 0)
        XCTAssertTrue(note.bars.isEmpty)
        XCTAssertTrue(note.faces.isEmpty)
    }

    func testAStatedDimAfterWins() throws {
        XCTAssertEqual(try summary(#"{"dimAfter": 900}"#).dimAfter, 900)
    }

    func testAnUnknownExtraKeyStillDecodes() throws {
        let note = try summary(#"{"needsYou": 2, "somethingNew": "later"}"#)
        XCTAssertEqual(note.needsYou, 2)
    }

    func testAFaceFromNothingStandsBy() throws {
        let face = try JSONDecoder().decode(FleetSummary.Face.self,
                                            from: Data("{}".utf8))
        XCTAssertEqual(face.rule, "sleep")
        XCTAssertEqual(face.slug, "")
    }

    func testASummaryRoundTripsThroughItsOwnEncoder() throws {
        let note = FleetSummary(
            needsYou: 2, working: 3, todo: 4, generatedAt: 1_700_000_000,
            dimAfter: 900,
            bars: [FleetSummary.Bar(provider: "claude", label: "week",
                                    percent: 41, stale: false)],
            faces: [FleetSummary.Face(slug: "cipher", name: "Cipher",
                                      doing: "working", rule: "work")])
        let data = try JSONEncoder().encode(note)
        XCTAssertEqual(try JSONDecoder().decode(FleetSummary.self, from: data),
                       note)
    }

    // MARK: - what "the numbers changed" means

    func testFiguresExcludesTheRoster() {
        // Deliberate, and must stay so: a roster flaps at turn cadence, and a
        // `faces` term here would spend a reload every poll while the numbers
        // a person acts on never moved.
        let one = FleetSummary(
            needsYou: 1, working: 2, todo: 3,
            faces: [FleetSummary.Face(slug: "cipher")])
        let two = FleetSummary(
            needsYou: 1, working: 2, todo: 3,
            faces: [FleetSummary.Face(slug: "vex")])
        XCTAssertNotEqual(one, two)
        XCTAssertEqual(one.figures.0, two.figures.0)
        XCTAssertEqual(one.figures.1, two.figures.1)
        XCTAssertEqual(one.figures.2, two.figures.2)
        XCTAssertEqual(one.figures.3, two.figures.3)
    }

    func testAChangedCountIsAChangedFigure() {
        let one = FleetSummary(needsYou: 1, working: 2, todo: 3)
        let two = FleetSummary(needsYou: 9, working: 2, todo: 3)
        XCTAssertNotEqual(one.figures.0, two.figures.0)
    }
}
