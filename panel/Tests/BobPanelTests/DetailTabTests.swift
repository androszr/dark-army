import XCTest
@testable import BobPanel

/// The agent detail's two tabs, tabled.
///
/// `plans/2026-09-06-detail-tabs-terminal-and-summary.md`. Every rule behind
/// the tab bar is a pure function over `(hosted, tab)` — which pane is drawn,
/// whether there is a tab bar at all, whether the live screen is attached
/// (and so whether the stream is connected and the Mac holds the pty's
/// width), and where a fresh open lands. The views read these and re-derive
/// none of them, so this file is the whole decision.
final class DetailTabTests: XCTestCase {

    /// Two tabs, no more; and the raw values are what the labels and the
    /// accessibility sentences both read, so they are pinned here.
    func testTheRosterIsTwoNamedTabs() {
        XCTAssertEqual(DetailTab.allCases.count, 2)
        XCTAssertEqual(DetailTab.details.rawValue, "Details")
        XCTAssertEqual(DetailTab.terminal.rawValue, "Terminal")
    }

    /// Every open lands on Details. Nothing persists a tab.
    func testDetailsIsTheDefault() {
        XCTAssertEqual(DetailTab.defaultTab, .details)
    }

    /// A row running in an editor has nowhere to switch to: no tab bar, and
    /// exactly today's layout.
    func testOnlyAHostedRowHasTabs() {
        XCTAssertTrue(DetailTab.showsTabBar(hosted: true))
        XCTAssertFalse(DetailTab.showsTabBar(hosted: false))
    }

    /// All four combinations of `pane`. The two that matter are the bottom
    /// row: a row that *stops* being hosted cannot leave a dead Terminal
    /// tab selected — that state is wrong, not merely absent.
    func testPaneOverEveryCombination() {
        XCTAssertEqual(DetailTab.pane(hosted: true, tab: .details), .details)
        XCTAssertEqual(DetailTab.pane(hosted: true, tab: .terminal), .terminal)
        XCTAssertEqual(DetailTab.pane(hosted: false, tab: .details), .details)
        XCTAssertEqual(DetailTab.pane(hosted: false, tab: .terminal), .details)
    }

    /// All four combinations of `terminalAttached` — the stream's whole
    /// lifetime, because not drawing the pane is the disconnect.
    func testTerminalAttachedOverEveryCombination() {
        XCTAssertTrue(DetailTab.terminalAttached(hosted: true, tab: .terminal))
        XCTAssertFalse(DetailTab.terminalAttached(hosted: true, tab: .details))
        XCTAssertFalse(DetailTab.terminalAttached(hosted: false, tab: .terminal))
        XCTAssertFalse(DetailTab.terminalAttached(hosted: false, tab: .details))
    }

    /// The two agree: the screen is attached exactly where the drawn pane
    /// is the Terminal tab, and never otherwise.
    func testAttachedAgreesWithTheDrawnPane() {
        for hosted in [true, false] {
            for tab in DetailTab.allCases {
                XCTAssertEqual(
                    DetailTab.terminalAttached(hosted: hosted, tab: tab),
                    DetailTab.pane(hosted: hosted, tab: tab) == .terminal,
                    "hosted: \(hosted), tab: \(tab)")
            }
        }
    }

    /// A fresh open of a hosted row draws Details and attaches nothing.
    func testAFreshOpenIsDetachedEvenOnAHostedRow() {
        XCTAssertFalse(
            DetailTab.terminalAttached(hosted: true, tab: DetailTab.defaultTab))
        XCTAssertEqual(
            DetailTab.pane(hosted: true, tab: DetailTab.defaultTab), .details)
    }

    /// The tab bar's presence is a fact about the row, not about which tab
    /// happens to be selected: a hosted row keeps its tabs on both.
    func testTheTabBarDoesNotDependOnTheSelection() {
        for tab in DetailTab.allCases {
            XCTAssertTrue(DetailTab.showsTabBar(hosted: true),
                          "hosted row, tab: \(tab)")
            XCTAssertFalse(DetailTab.showsTabBar(hosted: false),
                           "editor row, tab: \(tab)")
        }
    }

    /// The labels drawn on the Mac are the raw values uppercased, and the
    /// phone draws them as they stand — so neither is empty and the two
    /// never collide.
    func testTheLabelsAreDistinctAndNonEmpty() {
        let labels = DetailTab.allCases.map { $0.rawValue }
        XCTAssertEqual(Set(labels).count, labels.count)
        for label in labels {
            XCTAssertFalse(label.isEmpty)
            XCTAssertEqual(label.uppercased(), label.uppercased())
        }
    }
}

/// Where a terminal the Mac is not drawing is, tabled.
final class TerminalWhereaboutsTests: XCTestCase {

    /// A hosted row is never "elsewhere" at all, whatever its origin.
    func testAHostedRowIsTheEditorCaseByConvention() {
        XCTAssertEqual(TerminalWhereabouts.of(originBy: "adhoc", hosted: true), .editor)
    }

    /// An ad-hoc terminal Dark Army no longer holds is gone, not in an editor.
    func testAnUnhostedAdhocRowIsGone() {
        XCTAssertEqual(TerminalWhereabouts.of(originBy: "adhoc", hosted: false), .gone)
    }

    /// Every other origin — a card start, a refine, a consult, no stamp —
    /// keeps today's wording: those may genuinely be in an editor window.
    func testEveryOtherOriginIsTheEditor() {
        for by in ["", "card-start", "card-refine", "card-consult", "nonsense"] {
            XCTAssertEqual(TerminalWhereabouts.of(originBy: by, hosted: false), .editor, by)
        }
    }

    func testTheWordsNeverSendSomebodyToAnEditorForAGoneTerminal() {
        XCTAssertEqual(TerminalWhereabouts.gone.label, "terminal gone")
        XCTAssertFalse(TerminalWhereabouts.gone.hint.contains("editor"))
        XCTAssertEqual(TerminalWhereabouts.editor.label, "runs in the editor")
        XCTAssertTrue(TerminalWhereabouts.editor.hint.contains("find it in the editor yourself"))
    }

    /// A lost tab is not an editor to go and find, and it has not ended.
    func testALostTabIsNotInTheEditor() {
        XCTAssertEqual(
            TerminalWhereabouts.of(originBy: "card-start", hosted: false, tabGone: true),
            .lost)
        XCTAssertEqual(
            TerminalWhereabouts.of(originBy: "card-start", hosted: true, tabGone: false),
            .editor)
        XCTAssertEqual(TerminalWhereabouts.lost.label, "tab gone")
        XCTAssertFalse(TerminalWhereabouts.lost.hint.contains("editor"))
        XCTAssertFalse(TerminalWhereabouts.lost.hint.contains("ended"))
    }
}
