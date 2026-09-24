import XCTest
@testable import BobPanel

final class KnowledgeTests: XCTestCase {
    func testMissingKeysDecode() throws {
        let report = try JSONDecoder().decode(
            KnowledgeReport.self, from: Data("{}".utf8))
        XCTAssertEqual(report.entries, [])
        XCTAssertFalse(report.supported)
        XCTAssertFalse(report.available)
        XCTAssertEqual(report.root, "")
        let entry = try JSONDecoder().decode(
            KnowledgeEntry.self, from: Data("{}".utf8))
        XCTAssertEqual(entry.key, "")
        XCTAssertTrue(entry.isUnconfirmed)
        XCTAssertFalse(entry.isStale)
        XCTAssertEqual(entry.sourceLabel, "agent")
    }

    func testStaleCopyIsArchivedNotAnError() throws {
        let json = Data(#"{"key":"purpose","stale":"1","answer":"old"}"#.utf8)
        let entry = try JSONDecoder().decode(KnowledgeEntry.self, from: json)
        XCTAssertEqual(entry.staleBadge, "archived")
        XCTAssertTrue(entry.isStale)
        XCTAssertFalse(entry.staleBadge.localizedCaseInsensitiveContains("error"))
        XCTAssertFalse(entry.staleBadge.localizedCaseInsensitiveContains("fail"))
    }

    func testUnconfirmedCopy() throws {
        let json = Data(#"{"key":"purpose","answer":"new"}"#.utf8)
        let entry = try JSONDecoder().decode(KnowledgeEntry.self, from: json)
        XCTAssertEqual(entry.unconfirmedLabel, "not yet confirmed")
        XCTAssertTrue(entry.isUnconfirmed)
    }

    @MainActor
    func testRetargetClearsEntriesBeforeTheNewFetch() {
        let state = KnowledgeWindowState()
        var held = KnowledgeReport()
        var entry = KnowledgeEntry()
        entry.key = "purpose"
        entry.answer = "answer for A"
        held.root = "/a"
        held.available = true
        held.entries = [entry]
        state.root = "/a"
        state.report = held
        state.confirmArmed = "purpose"
        state.retarget(root: "/b", label: "b")
        XCTAssertEqual(state.root, "/b")
        XCTAssertTrue(state.report.entries.isEmpty)
        XCTAssertEqual(state.report.root, "")
        XCTAssertNil(state.confirmArmed)
        XCTAssertNil(state.editingKey)
        XCTAssertFalse(KnowledgeLoad.mayWrite(
            report: state.report, root: state.root, key: "purpose"))
    }

    func testStaleReplyIsIgnored() {
        var previous = KnowledgeReport()
        previous.root = "/a"
        var entry = KnowledgeEntry()
        entry.key = "purpose"
        previous.entries = [entry]
        let outcome = KnowledgeLoad.apply(
            requested: "/a", current: "/b", fetched: previous)
        XCTAssertNil(outcome)
    }

    func testFailedFetchClearsEntries() {
        let outcome = KnowledgeLoad.apply(
            requested: "/b", current: "/b", fetched: nil)
        XCTAssertEqual(outcome?.report.entries, [])
        XCTAssertEqual(outcome?.detail, KnowledgeReport.fetchFailedLine)
        XCTAssertTrue(outcome!.detail.contains("Dark Army"))
    }

    func testUnavailableIsARefusalNotAnEmptySuccess() {
        var fetched = KnowledgeReport()
        fetched.root = "/a"
        fetched.available = false
        var entry = KnowledgeEntry()
        entry.key = "purpose"
        fetched.entries = [entry]
        let outcome = KnowledgeLoad.apply(
            requested: "/a", current: "/a", fetched: fetched)
        XCTAssertEqual(outcome?.report.entries, [])
        XCTAssertEqual(outcome?.detail, KnowledgeReport.unavailableLine)
        XCTAssertTrue(outcome!.detail.contains("Dark Army"))
        XCTAssertFalse(outcome!.detail.localizedCaseInsensitiveContains("error"))
    }

    func testTruncatedCopyNamesOmittedKeysAndIsNotAnError() {
        var fetched = KnowledgeReport()
        fetched.root = "/a"
        fetched.available = true
        fetched.truncated = true
        fetched.omittedKeys = ["zshort"]
        var entry = KnowledgeEntry()
        entry.key = "purpose"
        fetched.entries = [entry]
        let outcome = KnowledgeLoad.apply(
            requested: "/a", current: "/a", fetched: fetched)
        XCTAssertEqual(outcome?.report.entries.count, 1)
        XCTAssertEqual(outcome?.detail, fetched.truncatedLine)
        XCTAssertTrue(outcome!.detail.contains("zshort"))
        XCTAssertTrue(outcome!.detail.contains("left out"))
        XCTAssertFalse(outcome!.detail.localizedCaseInsensitiveContains("error"))
        XCTAssertFalse(outcome!.detail.localizedCaseInsensitiveContains("fail"))
    }

    func testMayWriteRequiresMatchingRootAndKey() {
        var report = KnowledgeReport()
        report.root = "/a"
        var entry = KnowledgeEntry()
        entry.key = "purpose"
        report.entries = [entry]
        XCTAssertTrue(KnowledgeLoad.mayWrite(report: report, root: "/a", key: "purpose"))
        XCTAssertFalse(KnowledgeLoad.mayWrite(report: report, root: "/b", key: "purpose"))
        XCTAssertFalse(KnowledgeLoad.mayWrite(report: report, root: "/a", key: "audience"))
        XCTAssertFalse(KnowledgeLoad.mayWrite(report: report, root: "/a", key: ""))
    }

    @MainActor
    func testKnowledgeRootEncodingIsASCIIUnreserved() {
        let cafe = "café/proj".addingPercentEncoding(
            withAllowedCharacters: DaemonClient.knowledgeRootUnreserved)
        XCTAssertNotNil(cafe)
        XCTAssertTrue(cafe!.unicodeScalars.allSatisfy(\.isASCII))
        XCTAssertFalse(cafe!.contains("é"))
        XCTAssertTrue(cafe!.contains("%"))
        let empty = "".addingPercentEncoding(
            withAllowedCharacters: DaemonClient.knowledgeRootUnreserved)
        XCTAssertEqual(empty, "")
    }
}
