import XCTest
@testable import BobPanel

/// The ledger's honesty, with no window and no daemon. A report value goes
/// in and the picture comes back already decided.
final class LedgerFoldTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_758_000_000)

    /// The success criterion: the figure they came for is the token cost of
    /// Claude, Grok and Codex in one total, and a provider-reported dollar
    /// sits beside it without being added.
    func testTheHeadlineIsTheTokenCostOfAllThreeAndTheReportIsNotAdded() {
        let picture = picture(cards: [
            card("c", token: 2, reported: 40),
            card("g", provider: "grok", token: 3, reported: 0.5),
            card("x", provider: "codex", token: 5),
        ])
        XCTAssertEqual(picture.spent, "$10.00")
        XCTAssertEqual(picture.reported, "$40.50")
        XCTAssertNotEqual(picture.spent, "$50.50")
        XCTAssertFalse(picture.spent.contains("~"))
        XCTAssertTrue(picture.aside.contains("$40.50 reported"))
        XCTAssertTrue(picture.aside.contains("not added"))
        XCTAssertFalse(picture.aside.contains("not in the total"))
        XCTAssertFalse(picture.aside.contains("estimated"))
        let today = picture.days.last
        XCTAssertEqual(today?.claudeToken, 2)
        XCTAssertEqual(today?.grokToken, 3)
        XCTAssertEqual(today?.codexToken, 5)
        XCTAssertEqual(today?.figure, "$10.00")
        XCTAssertEqual(today?.reportedMark, "$40.50 reported")
        XCTAssertEqual(today?.magnitude, 10)
    }

    func testAReportedZeroShowsBesideATokenCost() {
        let picture = picture(cards: [card("z", token: 1.25, reported: 0)])
        let today = picture.days.last
        XCTAssertEqual(today?.figure, "$1.25")
        XCTAssertEqual(today?.reportedMark, "$0.00 reported")
        XCTAssertEqual(picture.reported, "$0.00")
        let line = picture.days.flatMap(\.cards).first { $0.id == "z" }
        XCTAssertEqual(line?.figure, "$1.25 · $0.00 reported")
    }

    func testAnAbsentReportIsNotZeroAndAnEmptyDayIsNotZero() {
        let picture = picture(cards: [card("a", token: 1.25)])
        XCTAssertNil(picture.reported)
        XCTAssertFalse(picture.aside.contains("reported"))
        let today = picture.days.last
        XCTAssertNil(today?.reportedMark)
        XCTAssertEqual(today?.figure, "$1.25")
        let empty = picture.days.first
        XCTAssertEqual(empty?.figure, "")
        XCTAssertNotEqual(empty?.figure, "$0.00")
        for column in picture.days {
            XCTAssertFalse(LedgerWeekLayout.figureLine(column)
                .contains { $0.text == "$0.00" }, column.day)
        }
    }

    func testACodexCardWithATokenCostIsPricedAndNotInsideClaude() {
        let codex = card("cx", provider: "codex", token: 4)
        let picture = picture(cards: [codex], claude: false, grok: false)
        XCTAssertEqual(picture.spent, "$4.00")
        XCTAssertFalse(picture.aside.contains("cost unknown"))
        let today = picture.days.last
        XCTAssertEqual(today?.codexToken, 4)
        XCTAssertEqual(today?.claudeToken, 0)
        let line = today?.cards.first { $0.id == "cx" }
        XCTAssertEqual(line?.figure, "$4.00")
        XCTAssertEqual(line?.ink, .codex)
        XCTAssertEqual(LedgerWeekLayout.segments(today ?? LedgerDayColumn(),
                                                 lens: .spent, scale: 4).map(\.ink),
                       [.codex])
        XCTAssertTrue(picture.masthead.contains("Codex"))
    }

    func testAPartlyUnpricedCardKeepsItsTokenCostAndAddsTheDash() {
        var work = card("p", provider: "grok", token: 2)
        work.sessions[0].tokenUnpricedTurns = 3
        let picture = picture(cards: [work])
        XCTAssertEqual(picture.spent, "$2.00")
        XCTAssertTrue(picture.aside.contains("1 session with turns not priced"))
        XCTAssertFalse(picture.aside.contains("cost unknown"))
        let today = picture.days.last
        XCTAssertEqual(today?.unknown, true)
        XCTAssertEqual(LedgerWeekLayout.figureLine(today ?? LedgerDayColumn()).map(\.text),
                       ["$2.00", LedgerFold.dash])
    }

    func testAWhollyUnpricedCardIsCostUnknownAndAPartlyPricedOneIsNot() {
        var partly = card("p", token: 2)
        partly.sessions[0].tokenUnpricedTurns = 1
        let unpriced = card("u")
        let picture = picture(cards: [partly, unpriced])
        XCTAssertTrue(picture.aside.contains("1 cost unknown"))
        XCTAssertTrue(picture.aside.contains("1 session with turns not priced"))
        XCTAssertTrue(LedgerFold.honesty.contains("A dash is not priced"))
        XCTAssertFalse(LedgerFold.honesty.contains("nobody published"))
    }

    func testACodexLabelledSessionWhoseTurnsAreClaudeIsDrawnAsClaude() {
        var work = card("m", provider: "codex", token: 6)
        work.sessions[0].claudeTokenCostUsd = 5
        work.sessions[0].codexTokenCostUsd = 1
        let picture = picture(cards: [work])
        let today = picture.days.last
        XCTAssertEqual(today?.claudeToken, 5)
        XCTAssertEqual(today?.codexToken, 1)
        XCTAssertEqual(picture.spent, "$6.00")
        XCTAssertEqual(today?.cards.first?.ink, .claude)
    }

    func testTheProviderFilterIsPerShareNotPerCard() {
        // A Codex-led card whose turns were Claude's: with Claude off it
        // draws no Claude segment, and its Codex share stays.
        var codexLed = card("cl", provider: "codex", token: 5)
        codexLed.sessions[0].claudeTokenCostUsd = 4
        codexLed.sessions[0].codexTokenCostUsd = 1
        // A Claude-led card with a Codex session: Claude off keeps the Codex
        // share instead of dropping the card whole.
        var claudeLed = card("cc", token: 3)
        claudeLed.sessions[0].claudeTokenCostUsd = 3
        var review = claudeLed.sessions[0]
        review.sessionId = "s-cc-review"
        review.provider = "codex"
        review.phase = "review"
        review.tokenCostUsd = 2
        review.claudeTokenCostUsd = nil
        review.codexTokenCostUsd = 2
        claudeLed.sessions.append(review)
        let off = picture(cards: [codexLed, claudeLed], claude: false, grok: true)
        let today = off.days.last
        XCTAssertEqual(today?.claudeToken, 0)
        XCTAssertEqual(today?.codexToken, 3)
        XCTAssertEqual(off.spent, "$3.00")
        XCTAssertEqual(Set(today?.cards.map(\.id) ?? []), ["cl", "cc"])
        XCTAssertFalse(LedgerWeekLayout.segments(today ?? LedgerDayColumn(),
                                                 lens: .spent, scale: 3)
            .contains { $0.ink == .claude })

        // A card whose only share is Claude's, with Claude off, is filtered
        // out rather than drawn as "cost unknown".
        var claudeOnly = card("co", provider: "codex", token: 4)
        claudeOnly.sessions[0].claudeTokenCostUsd = 4
        let gone = picture(cards: [claudeOnly], claude: false, grok: true)
        XCTAssertTrue(gone.days.allSatisfy { $0.cards.isEmpty })
        XCTAssertNotEqual(gone.spent, "cost unknown")
    }

    func testACodexLabelledClaudeSessionIsOffWithClaudeOffReportAndAll() {
        var work = card("m", provider: "codex", token: 4, reported: 9,
                        tokens: 100, ms: 60_000)
        work.sessions[0].claudeTokenCostUsd = 4
        var codexRun = work.sessions[0]
        codexRun.sessionId = "s-m-codex"
        codexRun.tokenCostUsd = 1
        codexRun.claudeTokenCostUsd = nil
        codexRun.codexTokenCostUsd = 1
        codexRun.reportedCostUsd = nil
        codexRun.outputTokens = 7
        work.sessions.append(codexRun)
        let picture = picture(cards: [work], claude: false, grok: true)
        XCTAssertEqual(picture.spent, "$1.00")
        XCTAssertNil(picture.reported)
        XCTAssertFalse(picture.aside.contains("reported"))
        XCTAssertEqual(picture.tokens, "7")
        XCTAssertNil(picture.days.last?.reportedMark)
    }

    func testAnUnpricedCodexSessionKeepsItsCardWhenThePricedShareIsOff() {
        var work = card("k", token: 3)
        work.sessions[0].claudeTokenCostUsd = 3
        var codexRun = work.sessions[0]
        codexRun.sessionId = "s-k-codex"
        codexRun.provider = "codex"
        codexRun.phase = "review"
        codexRun.tokenCostUsd = nil
        codexRun.claudeTokenCostUsd = nil
        codexRun.tokenUnpricedTurns = 2
        work.sessions.append(codexRun)
        let picture = picture(cards: [work], claude: false, grok: true)
        let line = picture.days.flatMap(\.cards).first { $0.id == "k" }
        XCTAssertNotNil(line)
        XCTAssertEqual(line?.figure, LedgerFold.dash)
        XCTAssertEqual(line?.ink, .unknown)
        XCTAssertEqual(picture.spent, "cost unknown")
        XCTAssertTrue(picture.aside.contains("1 cost unknown"))
        XCTAssertEqual(picture.days.last?.unknown, true)
    }

    func testALeftoverSessionUnpricedOnSeveralDaysCountsOnce() {
        let days = (0..<3).map { offset -> HistoryOtherDay in
            var other = HistoryOtherDay()
            other.day = LedgerFold.localDay(LedgerFold.timestamp(daysAgo: offset, now: now))
            other.claudeTokenCostUsd = 1
            other.claudeTokenUnpricedSessions = 1
            other.claudeTokenUnpricedSessionIds = ["s-long"]
            return other
        }
        var partly = card("p", token: 2)
        partly.sessions[0].tokenUnpricedTurns = 1
        let picture = picture(cards: [partly], other: days)
        XCTAssertTrue(picture.aside.contains("2 sessions with turns not priced"),
                      picture.aside)
        XCTAssertFalse(picture.aside.contains("part not priced"))
    }

    func testTheAsideSaysCodexIsStillBeingReadAndNamesAnUnknownSplit() {
        var work = card("c", token: 1)
        work.sessions[0].cacheSplitUnknownTurns = 2
        var built = report(cards: [work])
        built.effectiveness.codexHistoryPartial = true
        let picture = LedgerFold.picture(
            report: built, projects: projects, range: "7d", lens: .spent,
            claude: true, grok: true, person: "", selectedDay: "", openCard: "",
            now: now)
        XCTAssertTrue(picture.aside.contains("Codex history is still being read"))
        XCTAssertTrue(picture.aside.contains("priced at the 5-minute rate"))
        XCTAssertEqual(picture.spent, "$1.00")
    }

    func testPeopleAndProjectsLeadWithTokenCostAndAppendAReportOnlyWhenOneExists() {
        var ada = card("a", token: 3, reported: 1)
        ada.who = "Ada"
        var bo = card("b", token: 2)
        bo.who = "Bo"
        let picture = picture(cards: [ada, bo])
        let rows = Dictionary(uniqueKeysWithValues: picture.people.map { ($0.who, $0.amount) })
        XCTAssertEqual(rows["Ada"], "$3.00 · $1.00 reported")
        XCTAssertEqual(rows["Bo"], "$2.00")
        XCTAssertEqual(picture.projects.first?.amount, "$5.00 · $1.00 reported")
    }

    func testTheOpenDayAndTheOpenRunSayTokenCostAndReportedApart() {
        let work = card("r", token: 2, reported: 0)
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        let dayKey = LedgerFold.localDay(ts)
        let day = LedgerFold.picture(
            report: report(cards: [work]), projects: projects, range: "7d",
            lens: .spent, claude: true, grok: true, person: "",
            selectedDay: dayKey, openCard: "", now: now)
        guard case .day(_, let spent, let reported, let unknown, _, _) = day.dock else {
            return XCTFail("expected the day")
        }
        XCTAssertEqual(spent, "$2.00")
        XCTAssertEqual(reported, "$0.00 reported")
        XCTAssertFalse(unknown)
        let open = picture(cards: [work], open: "r")
        guard case .run(let run) = open.dock else { return XCTFail("expected the run") }
        XCTAssertEqual(run.spent, "$2.00")
        XCTAssertEqual(run.reported, "$0.00")
        XCTAssertTrue(run.scope.contains("token cost"))
    }

    func testNullCostIsADashNotZero() {
        let picture = picture(cards: [card("u")])
        let line = picture.days.flatMap(\.cards).first { $0.id == "u" }
        XCTAssertEqual(line?.figure, "—")
        XCTAssertNotEqual(line?.figure, "$0.00")
        XCTAssertEqual(picture.spent, "cost unknown")
        XCTAssertNotEqual(picture.spent, "$0.00")
    }

    func testCodexStaysWhenClaudeIsOffAndIsNotZero() {
        var codex = card("cx")
        codex.sessions[0].provider = "codex"
        let picture = picture(cards: [codex], claude: false, grok: true)
        let line = picture.days.flatMap(\.cards).first { $0.id == "cx" }
        XCTAssertNotNil(line)
        XCTAssertEqual(line?.figure, "—")
        XCTAssertNotEqual(line?.figure, "$0.00")
        XCTAssertEqual(picture.spent, "cost unknown")
    }

    func testOtherMovesSpentAndTokensButNotTimeAndLeavesWhenFiltered() {
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        var work = card("c", token: 1, tokens: 10, ms: 60_000, bound: ts)
        work.who = "Ada"
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.claudeTokenCostUsd = 4
        other.claudeOutputTokens = 100
        let open = picture(cards: [work], other: [other])
        XCTAssertEqual(open.spent, "$5.00")
        XCTAssertEqual(open.tokens, "110")
        XCTAssertEqual(open.time, "1m")

        let person = picture(cards: [work], other: [other], person: "Ada")
        XCTAssertEqual(person.spent, "$1.00")
        XCTAssertEqual(person.tokens, "10")
        XCTAssertEqual(person.time, open.time)
        XCTAssertFalse(person.days.contains { $0.otherLine != nil })

        var scoped = report(cards: [work], other: [other])
        scoped.effectiveness.root = "/proj"
        let rooted = LedgerFold.picture(
            report: scoped, projects: projects, range: "7d", lens: .spent,
            claude: true, grok: true, person: "", selectedDay: "", openCard: "",
            now: now)
        XCTAssertEqual(rooted.spent, "$1.00")
        XCTAssertEqual(rooted.tokens, "10")
        XCTAssertEqual(rooted.time, open.time)
        XCTAssertEqual(open.waiting, person.waiting)
        XCTAssertEqual(open.waiting, rooted.waiting)
    }

    func testADayWithMoneyAndNoCardIsNoNamedCard() {
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.claudeTokenCostUsd = 4
        let picture = picture(cards: [], other: [other])
        let day = picture.days.first { $0.day == other.day }
        XCTAssertEqual(day?.noNamedCard, true)
        XCTAssertEqual(day?.emptyNote, "No named card")
        XCTAssertEqual(day?.otherLine, "other $4.00")
    }

    func testNilAcceptedIsNotSubmitted() {
        var work = card("c", token: 1)
        work.accepted = nil
        let picture = picture(cards: [work], open: "c")
        guard case .run(let run) = picture.dock else {
            return XCTFail("expected the run")
        }
        XCTAssertEqual(run.verdict, "not submitted")
    }

    func testUnavailableOutcomesDoNotDrawZeroAccepted() {
        var built = report(cards: [])
        built.effectiveness.root = "/proj"
        built.effectiveness.outcomesAvailable = false
        built.effectiveness.outcomesReason = "No submitted work in this period."
        built.effectiveness.summary.acceptedOutcomes = 0
        let scoped = LedgerFold.picture(
            report: built, projects: projects, range: "7d", lens: .spent,
            claude: true, grok: true, person: "", selectedDay: "", openCard: "",
            now: now)
        XCTAssertFalse(scoped.acceptance.contains("0 accepted"))
        XCTAssertEqual(scoped.acceptance, "No submitted work in this period.")
        let wide = self.picture(cards: [])
        XCTAssertTrue(wide.acceptance.contains("not computed"))
        XCTAssertFalse(wide.acceptance.contains("0 accepted"))
    }

    func testACardBeforeTheFirstColumnStillCounts() {
        let early = card("early", token: 8, daysAgo: 7)
        let picture = picture(cards: [early])
        XCTAssertEqual(picture.days.count, 7)
        XCTAssertEqual(picture.spent, "$8.00")
        let line = picture.days.flatMap(\.cards).first { $0.id == "early" }
        XCTAssertEqual(line?.figure, "$8.00")
        XCTAssertEqual(picture.days.first?.cards.contains { $0.id == "early" }, true)
    }

    func testAGrokOnlyPictureLeavesOutClaudeLeftovers() {
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.claudeTokenCostUsd = 9
        other.claudeReportedCostUsd = 30
        other.grokTokenCostUsd = 2
        other.grokReportedCostUsd = 1
        other.claudeTokenUnpricedSessions = 4
        let picture = picture(cards: [], other: [other], claude: false, grok: true)
        XCTAssertEqual(picture.spent, "$2.00")
        XCTAssertEqual(picture.reported, "$1.00")
        XCTAssertFalse(picture.aside.contains("cost unknown"))
        let today = picture.days.last
        XCTAssertEqual(today?.otherLine, "other $2.00")
        XCTAssertEqual(today?.emptyNote, "No named card")
    }

    func testLeftoverCodexIsAlwaysOnAndOnItsOwnSegment() {
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.codexTokenCostUsd = 3
        other.codexTokenUnpricedSessions = 1
        let picture = picture(cards: [], other: [other], claude: false, grok: true)
        XCTAssertEqual(picture.spent, "$3.00")
        XCTAssertTrue(picture.aside.contains("1 session with turns not priced"))
        let today = picture.days.last
        XCTAssertEqual(today?.codexToken, 3)
        XCTAssertEqual(today?.claudeToken, 0)
        XCTAssertEqual(today?.otherLine, "other $3.00")
    }

    func testAnOtherDayWithNoReportKeysDrawsNoReportedZero() {
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.claudeTokenCostUsd = 5
        let picture = picture(cards: [], other: [other])
        XCTAssertNil(picture.reported)
        XCTAssertNil(picture.days.last?.reportedMark)
    }

    func testSevenDayUsualIsTheMedianOfTokenCostDaysOnTheSpentLensOnly() {
        let cards = (0..<7).map { offset in
            card("d\(offset)", token: Double(7 - offset), daysAgo: offset)
        }
        let spent = picture(cards: cards, lens: .spent)
        XCTAssertEqual(spent.days.count, 7)
        XCTAssertEqual(spent.usual, 4)
        let tokens = picture(cards: cards, lens: .tokens)
        XCTAssertNil(tokens.usual)
        let month = LedgerFold.picture(
            report: report(cards: cards), projects: projects, range: "30d",
            lens: .spent, claude: true, grok: true, person: "",
            selectedDay: "", openCard: "", now: now)
        XCTAssertEqual(month.days.count, 30)
        XCTAssertNil(month.usual)
        XCTAssertEqual(month.dock, .rest(
            heading: "Thirty days", acceptance: month.acceptance,
            honesty: LedgerFold.honesty))
    }

    func testThinColumnsFillTheRowUntilOneWouldBeThinnerThanEightPoints() {
        let filled = LedgerColumnLayout.columnWidth(count: 30, available: 1100)
        XCTAssertEqual(filled, (1100 - 3 * 29) / 30, accuracy: 0.001)
        XCTAssertGreaterThanOrEqual(filled, LedgerColumnLayout.minimum)
        XCTAssertFalse(LedgerColumnLayout.scrolls(count: 30, available: 1100))
        XCTAssertFalse(LedgerColumnLayout.scrolls(count: 90, available: 1100))
        XCTAssertEqual(LedgerColumnLayout.columnWidth(count: 200, available: 1100),
                       LedgerColumnLayout.minimum)
        XCTAssertTrue(LedgerColumnLayout.scrolls(count: 200, available: 1100))
        XCTAssertFalse(LedgerColumnLayout.scrolls(count: 30, available: 0))
    }

    func testThinAxisLabelsTheFifthDayTheLastDayAndTheOpenDay() {
        XCTAssertTrue(LedgerColumnLayout.showsTick(index: 0, count: 30, selected: false))
        XCTAssertFalse(LedgerColumnLayout.showsTick(index: 1, count: 30, selected: false))
        XCTAssertTrue(LedgerColumnLayout.showsTick(index: 1, count: 30, selected: true))
        XCTAssertTrue(LedgerColumnLayout.showsTick(index: 5, count: 30, selected: false))
        XCTAssertTrue(LedgerColumnLayout.showsTick(index: 29, count: 30, selected: false))
    }

    // MARK: - Fixtures

    private var projects: [BoardProject] {
        var project = BoardProject()
        project.name = "dark-army"
        project.root = "/proj"
        return [project]
    }

    private func picture(cards: [HistoryOutcomeCard],
                         other: [HistoryOtherDay] = [],
                         lens: LedgerLens = .spent,
                         claude: Bool = true,
                         grok: Bool = true,
                         person: String = "",
                         open: String = "") -> LedgerPicture {
        LedgerFold.picture(
            report: report(cards: cards, other: other), projects: projects,
            range: "7d", lens: lens, claude: claude, grok: grok, person: person,
            selectedDay: "", openCard: open, now: now)
    }

    private func report(cards: [HistoryOutcomeCard],
                        other: [HistoryOtherDay] = []) -> HistoryReport {
        var report = HistoryReport()
        report.available = true
        report.range = "7d"
        report.otherDays = other
        report.waiting.totalSeconds = 125
        report.effectiveness.available = true
        report.effectiveness.root = ""
        report.effectiveness.outcomesAvailable = false
        report.effectiveness.outcomesReason = "A total across the Mac is not computed."
        report.effectiveness.cards = cards
        return report
    }

    private func card(_ id: String, provider: String = "claude",
                      token: Double? = nil, reported: Double? = nil,
                      measured: Double? = nil, estimated: Double? = nil,
                      tokens: Int? = nil, ms: Int? = nil, bound: Double? = nil,
                      daysAgo: Int = 0) -> HistoryOutcomeCard {
        var card = HistoryOutcomeCard()
        card.cardId = id
        card.title = id
        card.root = "/proj"
        card.who = "Ada"
        card.turns = 2
        var session = HistoryOutcomeSession()
        session.sessionId = "s-\(id)"
        session.provider = provider
        session.phase = "implementation"
        session.boundAt = bound ?? LedgerFold.timestamp(daysAgo: daysAgo, now: now)
        // A session with a token cost has history rows; one without either
        // is a card whose spend history no longer holds.
        session.known = ms != nil || token != nil
        session.durationMs = ms
        session.measuredCostUsd = measured
        session.estimatedCostUsd = estimated
        session.tokenCostUsd = token
        session.reportedCostUsd = reported
        session.outputTokens = tokens
        session.model = "claude-opus"
        card.sessions = [session]
        return card
    }
}
