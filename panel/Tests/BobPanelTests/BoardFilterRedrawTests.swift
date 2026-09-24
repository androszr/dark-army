import AppKit
import SwiftUI
import Vision
import XCTest
@testable import BobPanel

/// The real board, in a real window, hosted the way `PanelView.widePane`
/// hosts it (`.equatable()`): ticking a project must change what the row
/// headings say without a fold press. Reported 23 Sep 2026 — the numbers
/// moved only when a heading was pressed, because a fold is what rebuilt
/// the rows.
final class BoardFilterRedrawTests: XCTestCase {
    private struct Host: View {
        let client: DaemonClient
        @ObservedObject var state: BoardState
        var body: some View {
            BoardView(client: client, state: state).equatable()
        }
    }

    @MainActor
    func testTickingAProjectRedrawsTheHeadings() throws {
        _ = NSApplication.shared
        let client = DaemonClient()
        var board = Board()
        board.available = true
        board.counts = ["prep": 5, "backlog": 1]
        board.cards = (0..<6).map { i in
            var card = BoardCard()
            card.id = String(format: "%032x", i + 1)
            card.title = "Card \(i)"
            card.column = i < 5 ? "prep" : "backlog"
            card.project = i < 4 ? "alpha" : "beta"
            return card
        }
        client.snapshot.board = board
        let state = BoardState()
        state.projectFilter = []
        state.query = ""
        state.rowFlips = ["prep", "backlog", "in_progress"]   // all four folded
        let host = NSHostingView(rootView: PanelView(client: client, keys: KeyRouter(),
                                                     focus: FocusRouter(), board: state))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 800),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        window.orderFront(nil)
        func settle() {
            RunLoop.main.run(until: Date().addingTimeInterval(0.3))
            host.layoutSubtreeIfNeeded()
        }
        func lines() -> [String] {
            let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
            host.cacheDisplay(in: host.bounds, to: rep)
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = false
            // Read at three times the size: the 12pt heading digit is
            // what OCR drops first at 1x.
            let src = rep.cgImage!
            let ctx = CGContext(data: nil, width: src.width * 3, height: src.height * 3,
                                bitsPerComponent: 8, bytesPerRow: 0,
                                space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            ctx.interpolationQuality = .high
            ctx.draw(src, in: CGRect(x: 0, y: 0, width: src.width * 3, height: src.height * 3))
            try? VNImageRequestHandler(cgImage: ctx.makeImage()!).perform([request])
            return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        }
        /// What the window shows, read back off its pixels: the heading
        /// line `[ PREP ] 5` as recognised text.
        func heading(_ title: String) -> String? {
            let lines = lines()
            // OCR splits `[ PREP ] 5` unpredictably and reads 0 as Ø.
            let text = lines.joined(separator: " ").replacingOccurrences(of: "Ø", with: "0")
            let pattern = title.uppercased() + #"[\s\]]*(\d+)"#
            guard let match = text.range(of: pattern, options: .regularExpression)
            else { return nil }
            let digits = text[match].filter(\.isNumber)
            return "\(title), \(digits) cards, folded"
        }
        /// Up to three seconds for the window to say `expected`: a loaded
        /// machine draws late, and a stale heading never gets there at all.
        func heading(_ title: String, becomes expected: String) -> String? {
            var last: String?
            for _ in 0..<10 {
                last = heading(title)
                if last == expected { return last }
                settle()
            }
            return last
        }
        settle()
        XCTAssertTrue(BoardColumn.allCases.allSatisfy { state.rowFolded($0) })
        XCTAssertEqual(heading("Prep", becomes: "Prep, 5 cards, folded"), "Prep, 5 cards, folded")
        state.toggleProjectFilter("beta")
        settle()
        XCTAssertEqual(heading("Prep", becomes: "Prep, 1 cards, folded"), "Prep, 1 cards, folded")
        state.toggleProjectFilter("beta")
        state.toggleProjectFilter("alpha")
        settle()
        XCTAssertEqual(heading("Prep", becomes: "Prep, 4 cards, folded"), "Prep, 4 cards, folded")
        state.clearProjectFilter()
    }

    /// The Done heading's Clear control is row chrome too: arming it must
    /// draw the first confirmation without a fold press.
    @MainActor
    func testArmingClearDoneDrawsTheConfirmation() throws {
        _ = NSApplication.shared
        let client = DaemonClient()
        var board = Board()
        board.available = true
        board.counts = ["done": 2]
        board.doneClearToken = String(repeating: "a", count: 64)
        board.cards = (0..<2).map { i in
            var card = BoardCard()
            card.id = String(format: "%032x", i + 1)
            card.title = "Finished \(i)"
            card.column = "done"
            card.project = "alpha"
            return card
        }
        client.snapshot.board = board
        let state = BoardState()
        state.projectFilter = []
        state.query = ""
        state.rowFlips = ["prep", "backlog", "in_progress", "done"]   // Done open
        let host = NSHostingView(rootView: PanelView(client: client, keys: KeyRouter(),
                                                     focus: FocusRouter(), board: state))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 800),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        window.orderFront(nil)
        func settle() {
            RunLoop.main.run(until: Date().addingTimeInterval(0.3))
            host.layoutSubtreeIfNeeded()
        }
        func screen() -> String {
            let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
            host.cacheDisplay(in: host.bounds, to: rep)
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = false
            try? VNImageRequestHandler(cgImage: rep.cgImage!).perform([request])
            return (request.results ?? [])
                .compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
        }
        func screen(showing words: String) -> String {
            var text = ""
            for _ in 0..<10 {
                text = screen()
                if text.contains(words) { break }
                settle()
            }
            return text
        }
        settle()
        XCTAssertTrue(screen(showing: "Clear all done items").contains("Clear all done items"))
        state.armClearDone(scope: BulkClearDoneScope(board: board))
        settle()
        let armed = screen(showing: "Confirm clear all")
        XCTAssertTrue(armed.contains("Confirm clear all"), armed)
        XCTAssertFalse(armed.contains("Clear all done items"), armed)
    }
}
