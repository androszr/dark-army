import AppKit
import Combine
import SwiftUI
import XCTest
@testable import BobPanel

/// Scrolling the board must not redraw the board.
///
/// Reported 26 Sep 2026: scrolling the Backlog stuttered and froze. The rows
/// are lazy, so every scroll step tears down the tiles that left the view,
/// and each tile's assistant switcher reports "let go of the keyboard" from
/// its `onDisappear`. `BoardState.noteSwitcherFocus` wrote that answer —
/// `nil`, already held — into a `@Published` slot, and a `@Published` write
/// publishes whether or not the value moved. Every tile, every row and the
/// whole panel observe `BoardState`, so each tile scrolled out of view
/// re-ran the board, every tile on screen and the panel's inbox arithmetic:
/// measured against the live board (178 cards), 42 republishes and 380 tile
/// redraws in a 6,000pt scroll, against 44 tiles actually brought into view.
@MainActor
final class BoardScrollRedrawTests: XCTestCase {

    // MARK: - The rule

    /// Releasing a slot nobody holds, or a slot another card holds, is not
    /// news: nothing is published. A real claim and a real release still are.
    func testOnlyAChangedSwitcherHolderPublishes() {
        let state = BoardState()
        var publishes = 0
        let sink = state.objectWillChange.sink { publishes += 1 }
        defer { sink.cancel() }

        state.noteSwitcherFocus(card: "a", focused: false)
        XCTAssertEqual(publishes, 0, "a release of an empty slot must not publish")

        state.noteSwitcherFocus(card: "a", focused: true)
        XCTAssertEqual(publishes, 1)
        XCTAssertEqual(state.switcherFocused, "a")

        state.noteSwitcherFocus(card: "a", focused: true)
        XCTAssertEqual(publishes, 1, "a repeated claim by the holder is not news")

        state.noteSwitcherFocus(card: "b", focused: false)
        XCTAssertEqual(publishes, 1, "another tile leaving must not publish")
        XCTAssertEqual(state.switcherFocused, "a")

        state.noteSwitcherFocus(card: "a", focused: false)
        XCTAssertEqual(publishes, 2)
        XCTAssertNil(state.switcherFocused)
    }

    // MARK: - The board, hosted and scrolled

    /// A long Backlog whose every tile carries the live assistant switcher
    /// (tools offered, no session bound), hosted the way the panel hosts it.
    private func hostedBoard() -> (DaemonClient, BoardState, NSWindow, NSHostingView<PanelView>) {
        _ = NSApplication.shared
        let client = DaemonClient()
        var board = Board()
        board.available = true
        board.dispatchEnabled = true
        board.tools = ["claude", "codex"]
        board.counts = ["backlog": 120]
        board.cards = (0..<120).map { i in
            var card = BoardCard()
            card.id = String(format: "%032x", i + 1)
            card.title = "Backlog card \(i)"
            card.summary = String(repeating: "What this card is for, in a sentence or two. ",
                                  count: 3)
            card.column = "backlog"
            card.project = "alpha"
            card.tool = "claude"
            card.position = Double(i)
            return card
        }
        // Through `apply`, the stream's own route, so the board is also the
        // carried section a later slim frame fills from.
        var seed = Snapshot()
        seed.board = board
        client.apply(seed, normalized: "seed", via: "test")
        let state = BoardState()
        state.projectFilter = []
        state.query = ""
        state.rowFlips = []
        let host = NSHostingView(rootView: PanelView(client: client, keys: KeyRouter(),
                                                     focus: FocusRouter(), board: state))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 800),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderFront(nil)
        return (client, state, window, host)
    }

    private func settle(_ host: NSView, _ seconds: Double = 0.3) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        host.layoutSubtreeIfNeeded()
    }

    /// The board's scroll view: the tallest document in the window.
    private func boardScroller(in view: NSView) -> NSScrollView? {
        func all(_ v: NSView) -> [NSScrollView] {
            ((v as? NSScrollView).map { [$0] } ?? []) + v.subviews.flatMap(all)
        }
        return all(view).max {
            ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0)
        }
    }

    /// Scrolling a long row brings tiles in and tears tiles down, and none of
    /// it may publish `BoardState` — the object every tile observes. Fails
    /// without the guard in `noteSwitcherFocus`: each tile that leaves the
    /// view publishes once.
    func testScrollingTheBoardPublishesNothingToTheTiles() throws {
        let (_, state, window, host) = hostedBoard()
        defer { window.close() }
        settle(host, 0.6)
        let scroller = try XCTUnwrap(boardScroller(in: host))
        let clip = scroller.contentView
        let reach = (scroller.documentView?.frame.height ?? 0) - clip.bounds.height
        XCTAssertGreaterThan(reach, 2000, "the board must be long enough to scroll")

        var publishes = 0
        let sink = state.objectWillChange.sink { publishes += 1 }
        defer { sink.cancel() }
        #if DEBUG
        BoardCardView.bodyEvaluations = 0
        #endif
        var y: CGFloat = 0
        while y < min(reach, 4000) {
            y += 40
            clip.scroll(to: NSPoint(x: 0, y: y))
            scroller.reflectScrolledClipView(clip)
            host.layoutSubtreeIfNeeded()
            window.displayIfNeeded()
        }
        // And back up, so the first tiles are torn down and rebuilt too.
        clip.scroll(to: .zero)
        scroller.reflectScrolledClipView(clip)
        settle(host)

        XCTAssertEqual(publishes, 0,
                       "a tile leaving the view must not republish the board's state")
        #if DEBUG
        // Only tiles brought into view drew — each at most once per entry.
        // With the per-disappearance republish every tile on screen redrew
        // on every step, several hundred bodies for this scroll.
        XCTAssertGreaterThan(BoardCardView.bodyEvaluations, 0, "the scroll brought tiles in")
        XCTAssertLessThan(BoardCardView.bodyEvaluations, 120,
                          "tiles redrew beyond the ones the scroll brought into view")
        #endif
    }

    /// An agents-only frame mid-scroll redraws no tile: the board observes
    /// `BoardFeed`, which that frame does not reach, and no tile observes
    /// the client.
    func testAnAgentsOnlyFrameRedrawsNoTile() async throws {
        let (client, _, window, host) = hostedBoard()
        defer { window.close() }
        settle(host, 0.6)
        let scroller = try XCTUnwrap(boardScroller(in: host))
        scroller.contentView.scroll(to: NSPoint(x: 0, y: 1200))
        scroller.reflectScrolledClipView(scroller.contentView)
        settle(host)

        var feedPublishes = 0
        let sink = client.boardFeed.objectWillChange.sink { feedPublishes += 1 }
        defer { sink.cancel() }
        #if DEBUG
        BoardCardView.bodyEvaluations = 0
        #endif
        for tick in 1...3 {
            let frame = """
            {"generated_at": \(tick + 10),
             "agents": {"running": [{"session_id": "s\(tick)", "nickname": "n\(tick)",
                                     "current_tool": "Edit", "idle_seconds": \(tick)}],
                        "waiting": [], "sleeping": [], "finished": [], "abandoned": []}}
            """
            let prepared = await DaemonClient.prepareFrame(frame, lastApplied: "", via: "test")
            let p = try XCTUnwrap(prepared)
            client.apply(p.snapshot, normalized: p.normalized, via: "test")
            settle(host, 0.1)
        }
        XCTAssertEqual(feedPublishes, 0, "an agents-only frame must not reach the board")
        #if DEBUG
        XCTAssertEqual(BoardCardView.bodyEvaluations, 0,
                       "an agents-only frame redrew board tiles")
        #endif
    }
}
