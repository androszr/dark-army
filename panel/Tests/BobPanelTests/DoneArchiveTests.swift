import XCTest
@testable import BobPanel

/// The panel's half of "finished cards are served on demand": the splice that
/// puts the held archive back into every frame, and the two-token rule that
/// decides when the held copy has gone stale.
final class DoneArchiveTests: XCTestCase {
    private let tokenA = String(repeating: "a", count: 64)
    private let tokenB = String(repeating: "b", count: 64)
    private let tokenC = String(repeating: "c", count: 64)

    private func card(_ id: String, title: String = "") -> BoardCard {
        var card = BoardCard()
        card.id = id
        card.title = title.isEmpty ? id : title
        card.column = "done"
        return card
    }

    // MARK: - splice

    func testSplicePrefersTheSnapshotsCopy() {
        let snapshot = [card("a", title: "fresh")]
        let archive = [card("a", title: "stale"), card("b")]
        let out = DoneArchive.splice(snapshot, archive: archive)
        XCTAssertEqual(out.map(\.id), ["a", "b"])
        XCTAssertEqual(out.first?.title, "fresh")
    }

    func testSpliceAppendsEverythingTheSnapshotLacks() {
        let out = DoneArchive.splice([card("a")],
                                     archive: [card("b"), card("c")])
        XCTAssertEqual(out.map(\.id), ["a", "b", "c"])
    }

    func testSpliceIsIdempotent() {
        let archive = [card("b"), card("c")]
        let once = DoneArchive.splice([card("a")], archive: archive)
        let twice = DoneArchive.splice(once, archive: archive)
        XCTAssertEqual(once.map(\.id), twice.map(\.id))
    }

    func testSpliceOfAnEmptyArchiveChangesNothing() {
        let snapshot = [card("a"), card("b")]
        XCTAssertEqual(DoneArchive.splice(snapshot, archive: []).map(\.id),
                       ["a", "b"])
    }

    // MARK: - decide

    func testDecideKeepsWhenBothTokensMatch() {
        XCTAssertEqual(DoneArchive.decide(clear: tokenA, view: tokenB,
                                          heldClear: tokenA, heldView: tokenB),
                       .keep)
    }

    func testDecideRefreshesWhenOnlyTheViewTokenMoved() {
        XCTAssertEqual(DoneArchive.decide(clear: tokenA, view: tokenC,
                                          heldClear: tokenA, heldView: tokenB),
                       .refresh)
    }

    func testDecideDropsWhenMembershipMoved() {
        XCTAssertEqual(DoneArchive.decide(clear: tokenC, view: tokenB,
                                          heldClear: tokenA, heldView: tokenB),
                       .dropAndRefresh)
    }

    func testDecideFetchesWhenNothingIsHeldYet() {
        XCTAssertEqual(DoneArchive.decide(clear: tokenA, view: tokenB,
                                          heldClear: "", heldView: ""),
                       .refresh)
    }

    /// An older daemon publishes neither token. Dropping a held column because
    /// a field went missing would blank a screen for a daemon that never
    /// withheld anything.
    func testDecideKeepsAgainstADaemonThatPublishesNoTokens() {
        XCTAssertEqual(DoneArchive.decide(clear: "", view: "",
                                          heldClear: tokenA, heldView: tokenB),
                       .keep)
    }

    // MARK: - tolerant decode

    func testBoardWithoutTheViewTokenDecodesEmpty() throws {
        let json = Data(#"{"available": true, "cards": []}"#.utf8)
        let board = try JSONDecoder().decode(Board.self, from: json)
        XCTAssertEqual(board.doneViewToken, "")
        XCTAssertEqual(board.doneClearToken, "")
    }

    func testCardWithoutDonePreviewDecodesFalse() throws {
        let json = Data(#"{"id": "a", "title": "t"}"#.utf8)
        let card = try JSONDecoder().decode(BoardCard.self, from: json)
        XCTAssertFalse(card.donePreview)
    }

    func testCardWithDonePreviewDecodesTrue() throws {
        let json = Data(#"{"id": "a", "done_preview": true}"#.utf8)
        let card = try JSONDecoder().decode(BoardCard.self, from: json)
        XCTAssertTrue(card.donePreview)
    }

    func testReportCarriesBothTokens() throws {
        let json = Data("""
        {"available": true, "cards": [],
         "done_clear_token": "\(tokenA)", "done_view_token": "\(tokenB)"}
        """.utf8)
        let report = try JSONDecoder().decode(BoardReport.self, from: json)
        XCTAssertEqual(report.doneClearToken, tokenA)
        XCTAssertEqual(report.doneViewToken, tokenB)
    }

    // MARK: - holding

    @MainActor
    func testAdoptStampsTheReportsOwnTokens() {
        let archive = DoneArchive()
        var report = BoardReport()
        report.available = true
        report.cards = [card("a")]
        report.doneClearToken = tokenA
        report.doneViewToken = tokenB
        archive.adopt(report)
        XCTAssertEqual(archive.cards.map(\.id), ["a"])
        XCTAssertEqual(archive.state, .ready)
        XCTAssertEqual(DoneArchive.decide(clear: tokenA, view: tokenB,
                                          heldClear: archive.clearStamp,
                                          heldView: archive.viewStamp),
                       .keep)
    }

    /// A failed refresh of a copy that is already good leaves it on screen —
    /// the column must not blank because one fetch missed.
    @MainActor
    func testAFailedRefreshKeepsAGoodCopy() {
        let archive = DoneArchive()
        var report = BoardReport()
        report.available = true
        report.cards = [card("a")]
        archive.adopt(report)
        archive.failed()
        XCTAssertEqual(archive.state, .ready)
        XCTAssertEqual(archive.cards.map(\.id), ["a"])
    }

    @MainActor
    func testAFailedFirstFetchSaysSo() {
        let archive = DoneArchive()
        archive.failed()
        XCTAssertEqual(archive.state, .failed)
    }
}
