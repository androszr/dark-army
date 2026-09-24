import XCTest
@testable import BobPanel

/// The ledger's honesty, with no window and no daemon. A report value goes
/// in and the picture comes back already decided.
final class LedgerFoldTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_758_000_000)

    func testMeasuredHeadlineExcludesAnEstimate() {
        let picture = picture(cards: [
            card("m", measured: 2),
            card("e", estimated: 3),
        ])
        XCTAssertEqual(picture.spent, "$2.00")
        XCTAssertEqual(picture.estimated, "~$3.00")
        XCTAssertFalse(picture.spent.contains("~"))
        XCTAssertNotEqual(picture.spent, "$5.00")
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
        var work = card("c", measured: 1, tokens: 10, ms: 60_000, bound: ts)
        work.who = "Ada"
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.claudeMeasuredCostUsd = 4
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
        other.claudeMeasuredCostUsd = 4
        let picture = picture(cards: [], other: [other])
        let day = picture.days.first { $0.day == other.day }
        XCTAssertEqual(day?.noNamedCard, true)
        XCTAssertEqual(day?.emptyNote, "No named card")
        XCTAssertEqual(day?.otherLine, "other $4.00")
    }

    func testNilAcceptedIsNotSubmitted() {
        var work = card("c", measured: 1)
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
        let early = card("early", measured: 8, daysAgo: 7)
        let picture = picture(cards: [early])
        XCTAssertEqual(picture.days.count, 7)
        XCTAssertEqual(picture.spent, "$8.00")
        let line = picture.days.flatMap(\.cards).first { $0.id == "early" }
        XCTAssertEqual(line?.figure, "$8.00")
        XCTAssertEqual(picture.days.first?.cards.contains { $0.id == "early" }, true)
    }

    func testAGrokOnlyPictureLeavesOutClaudeLeftoverEstimates() {
        let ts = LedgerFold.timestamp(daysAgo: 0, now: now)
        var other = HistoryOtherDay()
        other.day = LedgerFold.localDay(ts)
        other.claudeEstimatedCostUsd = 9
        other.grokEstimatedCostUsd = 1
        other.grokMeasuredCostUsd = 2
        other.claudeUnpricedSessions = 4
        let picture = picture(cards: [], other: [other], claude: false, grok: true)
        XCTAssertEqual(picture.spent, "$2.00")
        XCTAssertEqual(picture.estimated, "~$1.00")
        XCTAssertFalse(picture.aside.contains("cost unknown"))
        let today = picture.days.last
        XCTAssertEqual(today?.otherLine, "other $2.00")
        XCTAssertEqual(today?.emptyNote, "No named card")
    }

    func testSevenDayUsualIsTheMedianOfMeasuredDaysOnTheSpentLensOnly() {
        let cards = (0..<7).map { offset in
            card("d\(offset)", measured: Double(7 - offset), daysAgo: offset)
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

    private func card(_ id: String, measured: Double? = nil, estimated: Double? = nil,
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
        session.provider = "claude"
        session.phase = "implementation"
        session.boundAt = bound ?? LedgerFold.timestamp(daysAgo: daysAgo, now: now)
        session.known = ms != nil
        session.durationMs = ms
        session.measuredCostUsd = measured
        session.estimatedCostUsd = estimated
        session.outputTokens = tokens
        session.model = "claude-opus"
        card.sessions = [session]
        return card
    }
}
