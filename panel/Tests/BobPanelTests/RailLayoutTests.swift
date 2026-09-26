import XCTest
@testable import BobPanel

/// The workspace's pane and Escape rules, tabled. Every function in
/// `RailLayout` is pure, so the decisions the view draws from are pinned
/// here rather than on a screen.
final class RailLayoutTests: XCTestCase {

    // MARK: leftPane

    func testAgentsTabWithSelectionIsDetail() {
        XCTAssertEqual(RailLayout.leftPane(tab: .agents, selected: "s1"),
                       .detail("s1"))
    }

    func testAgentsTabWithoutSelectionIsBoard() {
        XCTAssertEqual(RailLayout.leftPane(tab: .agents, selected: nil), .board)
    }

    func testInboxTabIsAlwaysBoard() {
        XCTAssertEqual(RailLayout.leftPane(tab: .inbox, selected: "s1"), .board)
        XCTAssertEqual(RailLayout.leftPane(tab: .inbox, selected: nil), .board)
    }

    func testTheCommTabDrawsTheMissionColumnWhateverIsSelected() {
        XCTAssertEqual(RailLayout.leftPane(tab: .comm, selected: nil), .mission)
        XCTAssertEqual(RailLayout.leftPane(tab: .comm, selected: "x"), .mission)
    }

    func testHistoryCoversTheBoardWithOrWithoutASelection() {
        XCTAssertEqual(RailLayout.leftPane(tab: .history, selected: "s1"), .history)
        XCTAssertEqual(RailLayout.leftPane(tab: .history, selected: nil), .history)
    }

    // MARK: escapeRung

    func testEscapeEndsEditingFirst() {
        XCTAssertEqual(RailLayout.escapeRung(editing: true, filterActive: false,
                                             detailOpen: false, drilledIn: false),
                       .endEditing)
        XCTAssertEqual(RailLayout.escapeRung(editing: false, filterActive: true,
                                             detailOpen: false, drilledIn: false),
                       .endEditing)
    }

    func testEscapeClosesDetail() {
        XCTAssertEqual(RailLayout.escapeRung(editing: false, filterActive: false,
                                             detailOpen: true, drilledIn: false),
                       .deselect)
    }

    func testEscapePopsDrill() {
        XCTAssertEqual(RailLayout.escapeRung(editing: false, filterActive: false,
                                             detailOpen: false, drilledIn: true),
                       .back)
    }

    func testEscapeHidesWhenNothingIsOpen() {
        XCTAssertEqual(RailLayout.escapeRung(editing: false, filterActive: false,
                                             detailOpen: false, drilledIn: false),
                       .hide)
    }

    func testEditingBeatsDetail() {
        // A caret in the terminal's input line or the reply box is given up
        // before the detail closes beneath it.
        XCTAssertEqual(RailLayout.escapeRung(editing: true, filterActive: false,
                                             detailOpen: true, drilledIn: true),
                       .endEditing)
    }

    func testDetailBeatsDrill() {
        XCTAssertEqual(RailLayout.escapeRung(editing: false, filterActive: false,
                                             detailOpen: true, drilledIn: true),
                       .deselect)
    }

    func testEscapeOnHistoryClimbsRunThenDayThenLeaves() {
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: false, drilledIn: false,
            historyRun: true, historyDay: true, historyOpen: true), .closeHistoryRun)
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: false, drilledIn: false,
            historyRun: false, historyDay: true, historyOpen: true), .closeHistoryDay)
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: true, drilledIn: false,
            historyOpen: true), .leaveHistory)
        XCTAssertEqual(RailLayout.escapeRung(
            editing: true, filterActive: false, detailOpen: false, drilledIn: false,
            historyRun: true, historyDay: true, historyOpen: true), .endEditing)
    }

    // MARK: the Reports tab

    func testReportsCoversTheBoardWithOrWithoutASelection() {
        XCTAssertEqual(RailLayout.leftPane(tab: .reports, selected: "s1"), .reports)
        XCTAssertEqual(RailLayout.leftPane(tab: .reports, selected: nil), .reports)
    }

    func testEscapeOnReportsClosesTheReportThenLeaves() {
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: false, drilledIn: false,
            reportOpen: true, reportsOpen: true), .closeReport)
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: false, drilledIn: false,
            reportsOpen: true), .leaveReports)
        // A caret in the search line is given up before anything closes.
        XCTAssertEqual(RailLayout.escapeRung(
            editing: true, filterActive: false, detailOpen: false, drilledIn: false,
            reportOpen: true, reportsOpen: true), .endEditing)
    }

    func testAHistoryFlagAloneLeavesHistoryNotReports() {
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: false, drilledIn: false,
            historyOpen: true, reportOpen: false, reportsOpen: false), .leaveHistory)
        XCTAssertEqual(RailLayout.escapeRung(
            editing: false, filterActive: false, detailOpen: false, drilledIn: false,
            historyOpen: false, reportsOpen: false), .hide)
    }

    // MARK: selectionAfterTap

    func testTapOnSelectedRowUnselects() {
        XCTAssertNil(RailLayout.selectionAfterTap(selected: "s1", tapped: "s1"))
    }

    func testTapOnAnotherRowSelectsIt() {
        XCTAssertEqual(RailLayout.selectionAfterTap(selected: "s1", tapped: "s2"), "s2")
    }

    func testTapFromNothingSelects() {
        XCTAssertEqual(RailLayout.selectionAfterTap(selected: nil, tapped: "s2"), "s2")
    }

    // MARK: reaimAfterListChange

    func testPrunedSelectionReaims() {
        XCTAssertTrue(RailLayout.reaimAfterListChange(selected: "s1", listed: ["s2"]))
    }

    func testEmptySelectionDoesNotReaim() {
        XCTAssertFalse(RailLayout.reaimAfterListChange(selected: nil, listed: ["s2"]))
        XCTAssertFalse(RailLayout.reaimAfterListChange(selected: nil, listed: []))
    }

    func testListedSelectionStays() {
        XCTAssertFalse(RailLayout.reaimAfterListChange(selected: "s1", listed: ["s1", "s2"]))
    }

    // MARK: aimAfterSnapshot

    func testPendingOpenAimsOnceNothingIsSelected() {
        // The open aimed against a frozen snapshot and found nobody; the
        // first live one gets one more go.
        XCTAssertTrue(RailLayout.aimAfterSnapshot(pending: true, selected: nil,
                                                  revealedForCard: false))
    }

    func testNoPendingOpenNeverAims() {
        // An ordinary snapshot on an open panel must not reopen a closed
        // detail.
        XCTAssertFalse(RailLayout.aimAfterSnapshot(pending: false, selected: nil,
                                                   revealedForCard: false))
    }

    func testPendingOpenLeavesASelectionAlone() {
        // The stale aim, a banner's focus request or a click already chose.
        XCTAssertFalse(RailLayout.aimAfterSnapshot(pending: true, selected: "s1",
                                                   revealedForCard: false))
    }

    func testPendingOpenNeverCoversARevealedCard() {
        // The reverse jump and the `card ⌗` chip asked for the board.
        XCTAssertFalse(RailLayout.aimAfterSnapshot(pending: true, selected: nil,
                                                   revealedForCard: true))
        XCTAssertFalse(RailLayout.aimAfterSnapshot(pending: true, selected: "s1",
                                                   revealedForCard: true))
    }

    // MARK: splitHeights

    func testSplitAtDefaultFraction() {
        let h = RailLayout.splitHeights(total: 1000, fraction: 0.6, minEach: 120)
        XCTAssertEqual(h.top, 400)
        XCTAssertEqual(h.bottom, 600)
    }

    func testSplitBelowTwiceMinimumIsEven() {
        let h = RailLayout.splitHeights(total: 200, fraction: 0.8, minEach: 120)
        XCTAssertEqual(h.top, 100)
        XCTAssertEqual(h.bottom, 100)
    }

    func testSplitHoldsBothMinimums() {
        let low = RailLayout.splitHeights(total: 300, fraction: 0.1, minEach: 120)
        XCTAssertEqual(low.bottom, 120)
        let high = RailLayout.splitHeights(total: 300, fraction: 0.95, minEach: 120)
        XCTAssertEqual(high.top, 120)
    }

    func testSplitSumsToTotal() {
        for total: CGFloat in [0, 50, 239, 240, 241, 777, 1234] {
            for fraction: CGFloat in [0, 0.2, 0.6, 0.8, 1] {
                let h = RailLayout.splitHeights(total: total, fraction: fraction, minEach: 120)
                XCTAssertEqual(h.top + h.bottom, total, accuracy: 0.0001,
                               "total \(total) fraction \(fraction)")
                XCTAssertGreaterThanOrEqual(h.top, 0)
                XCTAssertGreaterThanOrEqual(h.bottom, 0)
            }
        }
    }

    // MARK: draggedFraction

    func testDragDownShrinksTerminal() {
        XCTAssertEqual(RailLayout.draggedFraction(start: 0.6, delta: 100, total: 1000),
                       0.5, accuracy: 0.0001)
    }

    func testDragUpGrowsTerminal() {
        XCTAssertEqual(RailLayout.draggedFraction(start: 0.6, delta: -100, total: 1000),
                       0.7, accuracy: 0.0001)
    }

    func testDragClampsBothEnds() {
        XCTAssertEqual(RailLayout.draggedFraction(start: 0.6, delta: 5000, total: 1000), 0.2)
        XCTAssertEqual(RailLayout.draggedFraction(start: 0.6, delta: -5000, total: 1000), 0.8)
    }

    func testDragIgnoresZeroTotal() {
        XCTAssertEqual(RailLayout.draggedFraction(start: 0.6, delta: 100, total: 0), 0.6)
        XCTAssertEqual(RailLayout.draggedFraction(start: 0.6, delta: 100, total: -5), 0.6)
    }

    // MARK: steppedFraction

    func testStepUpGrowsTerminalByOneStep() {
        XCTAssertEqual(RailLayout.steppedFraction(start: 0.6, up: true),
                       0.7, accuracy: 0.0001)
    }

    func testStepDownShrinksTerminalByOneStep() {
        XCTAssertEqual(RailLayout.steppedFraction(start: 0.6, up: false),
                       0.5, accuracy: 0.0001)
    }

    func testStepClampsBothEnds() {
        XCTAssertEqual(RailLayout.steppedFraction(start: 0.8, up: true), 0.8)
        XCTAssertEqual(RailLayout.steppedFraction(start: 0.75, up: true), 0.8)
        XCTAssertEqual(RailLayout.steppedFraction(start: 0.2, up: false), 0.2)
        XCTAssertEqual(RailLayout.steppedFraction(start: 0.25, up: false), 0.2)
    }
}
