import XCTest
@testable import BobPanel

/// The budget chip's short names, figure text, and reserved height — the
/// strings that used to wrap, pinned so a silent shortening or a three-line
/// restore is a failing test.
final class UsageChipTests: XCTestCase {

    func testSessionLabelIsFiveHours() {
        XCTAssertEqual(
            UsageChipText.label(kind: "session", shortLabel: "", title: "Current session"),
            "5h")
    }

    func testWeeklyAllLabelIsSevenDays() {
        XCTAssertEqual(
            UsageChipText.label(kind: "weekly_all", shortLabel: "", title: "Current week"),
            "7d")
    }

    func testGrokWeeklyLabelIsSevenDays() {
        XCTAssertEqual(
            UsageChipText.label(kind: "grok_weekly", shortLabel: "", title: "Grok week"),
            "7d")
    }

    func testCodexWeeklyLabelUsesTheShortLabel() {
        XCTAssertEqual(
            UsageChipText.label(kind: "codex_secondary", shortLabel: "7d", title: "Codex, 7d"),
            "7d")
    }

    func testCodexSessionLabelUsesTheShortLabel() {
        XCTAssertEqual(
            UsageChipText.label(kind: "codex_primary", shortLabel: "5h", title: "Codex, 5h"),
            "5h")
    }

    /// The exact string that wrapped: a per-model window named in full, not
    /// shortened to an initial. Distinguishes this chip from the plain weekly.
    func testWeeklyScopedLabelKeepsTheModelName() {
        XCTAssertEqual(
            UsageChipText.label(
                kind: "weekly_scoped",
                shortLabel: "",
                title: "Current week (Fable)"),
            "7d·Fable")
    }

    func testUnknownKindFallsBackToShortLabel() {
        XCTAssertEqual(
            UsageChipText.label(kind: "mystery", shortLabel: "xh", title: "Mystery window"),
            "xh")
    }

    func testUnknownKindFallsBackToTitleWhenShortLabelIsEmpty() {
        XCTAssertEqual(
            UsageChipText.label(kind: "mystery", shortLabel: "", title: "Mystery window"),
            "Mystery window")
    }

    func testModelSuffixWithoutParenthesesIsModel() {
        XCTAssertEqual(UsageChipText.modelSuffix(title: "Current week"), "model")
    }

    func testModelSuffixWithCloseBeforeOpenIsModel() {
        XCTAssertEqual(UsageChipText.modelSuffix(title: ") before ("), "model")
    }

    func testFigureTruncatesRatherThanRounds() {
        XCTAssertEqual(UsageChipText.figure(percent: 42.7, stale: false), "42%")
    }

    func testFigureIsAnEnDashWhenPercentIsNil() {
        XCTAssertEqual(UsageChipText.figure(percent: nil, stale: false), "–")
    }

    func testFigureIsAnEnDashWhenStaleEvenIfPercentIsPresent() {
        XCTAssertEqual(UsageChipText.figure(percent: 91, stale: true), "–")
    }

    /// Whoever restores the three-line chip has to delete an assertion that
    /// says why the fourth line exists.
    func testSettingsHeightIsChromePlusFourLineChip() {
        XCTAssertEqual(
            PanelMetrics.settings,
            PanelMetrics.settingsChrome + PanelMetrics.usageChip)
        XCTAssertEqual(PanelMetrics.settings, 70)
        XCTAssertGreaterThan(PanelMetrics.settings, 52)
    }

    // MARK: - the age note
    //
    // Every failure path in the live scoped fetch lands on a *held* reading, so
    // the chip has to be able to say how old one is. Silence would be the same
    // bug the fetch was written to fix, one layer up.

    private let now: Double = 1_788_264_000

    func testAFreshReadingCarriesNoNote() {
        XCTAssertNil(UsageChipText.ageNote(asOf: now - 60, now: now))
        XCTAssertNil(UsageChipText.ageNote(asOf: now - 1799, now: now))
    }

    func testTheThresholdIsHalfAnHour() {
        XCTAssertEqual(UsageChipText.staleReadSeconds, 1800)
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 1800, now: now),
                       "read 30 min ago")
    }

    func testMinutesPhrasing() {
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 2460, now: now),
                       "read 41 min ago")
    }

    func testHoursPhrasing() {
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 3600, now: now),
                       "read 1 h ago")
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 57_600, now: now),
                       "read 16 h ago")
    }

    func testDaysPhrasing() {
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 259_200, now: now),
                       "read 3 d ago")
    }

    /// An older daemon publishes no `as_of` at all. Inventing an age for it
    /// would be the same untruth pointing the other way.
    func testAnUndatedBarGetsNoNote() {
        XCTAssertNil(UsageChipText.ageNote(asOf: nil, now: now))
    }

    /// A clock that has gone backwards is not a reason to shout.
    func testAReadingFromTheFutureGetsNoNote() {
        XCTAssertNil(UsageChipText.ageNote(asOf: now + 600, now: now))
    }

    // MARK: - the source gate
    //
    // The note claims a refresh did not get through. That is true of the live
    // scoped fetch and of a held cache, and false of the two account-wide bars,
    // which Claude Code reports on every turn: those are old because the Mac
    // was quiet. Drawing it there put a false alarm on both meters after every
    // half-hour lull.

    func testAStatuslineFigureNeverCarriesTheNote() {
        XCTAssertEqual(UsageChipText.selfReportedSource, "statusline")
        XCTAssertNil(UsageChipText.ageNote(asOf: now - 57_600,
                                           source: "statusline", now: now))
    }

    /// The bar the live fetch is actually responsible for.
    func testAnOAuthFigureStillCarriesTheNote() {
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 57_600,
                                             source: "oauth", now: now),
                       "read 16 h ago")
    }

    /// A held cache is the failure path the note was written for.
    func testACachedFigureStillCarriesTheNote() {
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 2460,
                                             source: "cache", now: now),
                       "read 41 min ago")
    }

    /// Codex and Grok bars name no source and have no local fallback, so an
    /// old one really is a refresh that did not get through.
    func testAnUnnamedSourceStillCarriesTheNote() {
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 2460, now: now),
                       "read 41 min ago")
        XCTAssertEqual(UsageChipText.ageNote(asOf: now - 2460, source: "",
                                             now: now),
                       "read 41 min ago")
    }

    /// The gate is on the source, not on the bar being fresh: a statusline
    /// figure from days ago is still silent.
    func testTheStatuslineGateBeatsAnyAge() {
        XCTAssertNil(UsageChipText.ageNote(asOf: now - 604_800,
                                           source: "statusline", now: now))
    }

    /// Tolerant decode, and the default is the honest one: a daemon that names
    /// no source must not be silently treated as self-reporting.
    func testSourceDecodesTolerantlyAndDefaultsToUnnamed() throws {
        let bare = Data(#"{"kind":"session","percent":12}"#.utf8)
        let bar = try JSONDecoder().decode(UsageBar.self, from: bare)
        XCTAssertEqual(bar.source, "")
        let named = Data(#"{"kind":"session","source":"statusline"}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(UsageBar.self, from: named).source,
                       "statusline")
    }
}
