import AppKit
import SwiftUI
import XCTest
@testable import BobPanel

/// Exercise the real SwiftUI/AppKit layout: testing the fold set alone cannot
/// detect a lazy stack keeping the heights of tiles that have disappeared.
final class BoardCollapseLayoutTests: XCTestCase {
    @MainActor
    func testFoldingScrolledBoardKeepsContentInViewport() throws {
        _ = NSApplication.shared
        let client = DaemonClient()
        client.snapshot.board.available = true
        client.snapshot.board.counts = ["backlog": 30, "in_progress": 6]
        client.snapshot.board.cards = (0..<36).map { i in
            var card = BoardCard()
            card.id = String(format: "%032x", i + 1)
            card.title = "Layout card \(i)"
            card.summary = "A card that must remain reachable after a stage folds."
            card.column = i < 30 ? "backlog" : "in_progress"
            return card
        }
        let state = BoardState()
        state.rowFlips = []
        let host = NSHostingView(rootView: BoardView(client: client, state: state))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 650),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        window.orderFront(nil)
        func settle() {
            RunLoop.main.run(until: Date().addingTimeInterval(0.25))
            host.layoutSubtreeIfNeeded()
        }
        func scrollView(in view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView { return scroll }
            return view.subviews.lazy.compactMap { scrollView(in: $0) }.first
        }
        settle()
        if let capturePath = ProcessInfo.processInfo.environment["SIGNAL_BOARD_CAPTURE"] {
            let bitmap = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: bitmap)
            let png = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
            try png.write(to: URL(fileURLWithPath: capturePath))
        }
        let scroll = try XCTUnwrap(scrollView(in: host))
        let document = try XCTUnwrap(scroll.documentView)
        XCTAssertGreaterThan(document.frame.height, scroll.contentView.bounds.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 900))
        scroll.reflectScrolledClipView(scroll.contentView)
        settle()
        state.rowFlips = ["prep", "backlog", "done"]
        settle()
        XCTAssertGreaterThan(document.frame.height, 100)
        XCTAssertLessThanOrEqual(document.frame.height, scroll.contentView.bounds.height + 1)
        XCTAssertLessThan(scroll.contentView.bounds.minY, document.frame.maxY)
        state.rowFlips = ["prep", "backlog", "in_progress"]
        settle()
        XCTAssertGreaterThan(document.frame.height, 100)
        XCTAssertLessThanOrEqual(document.frame.height, scroll.contentView.bounds.height + 1,
                                "Four folded headings must fit, without stale offscreen tile space")
        XCTAssertLessThan(scroll.contentView.bounds.minY, document.frame.maxY)
        XCTAssertEqual(scroll.contentView.bounds.minY, 0, accuracy: 1)
        // Search temporarily opens folded stages, then restores the folds.
        // This must invalidate layout too, without changing the saved flips.
        let flips = state.rowFlips
        state.query = "Layout card"
        settle()
        XCTAssertGreaterThan(document.frame.height, scroll.contentView.bounds.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 900))
        scroll.reflectScrolledClipView(scroll.contentView)
        settle()
        state.query = ""
        settle()
        XCTAssertEqual(state.rowFlips, flips)
        XCTAssertLessThanOrEqual(document.frame.height, scroll.contentView.bounds.height + 1)
        XCTAssertEqual(scroll.contentView.bounds.minY, 0, accuracy: 1)
        state.rowFlips = []
        settle()
        XCTAssertGreaterThan(document.frame.height, scroll.contentView.bounds.height)
    }
}
