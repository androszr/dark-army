import XCTest
@testable import BobPhone

/// The Scouting screen's grep rule and the header's order — the same
/// `ScoutReports.swift` the Mac runs, byte-pinned by
/// `test_scout_reports_surface.py`.
final class ScoutReportSearchTests: XCTestCase {
    private func row() -> ScoutReportRow {
        var row = ScoutReportRow()
        row.title = "Why the relay is busy"
        row.verdict = "The idle poll is most of the bill"
        row.question = "What drives Upstash?"
        row.project = "dark-army"
        row.cardTitle = "Scout: Redis"
        row.recommendation = "build"
        return row
    }

    func testEachSearchedFieldMatches() {
        for query in ["relay", "idle poll", "upstash", "dark-army", "redis", "build"] {
            XCTAssertTrue(ScoutReportSearch.matches(query: query, row: row()), query)
        }
        XCTAssertFalse(ScoutReportSearch.matches(query: "zzqx", row: row()))
    }

    func testMatchingIgnoresCaseAndDiacritics() {
        var r = row()
        r.title = "Café report"
        XCTAssertTrue(ScoutReportSearch.matches(query: "CAFE", row: r))
    }

    func testAnEmptyOrBlankQueryMatchesEverythingAndIsNotASearch() {
        XCTAssertTrue(ScoutReportSearch.matches(query: "", row: row()))
        XCTAssertTrue(ScoutReportSearch.matches(query: "   ", row: row()))
        XCTAssertFalse(ScoutReportSearch.isSearching("  "))
        XCTAssertTrue(ScoutReportSearch.isSearching("relay"))
    }

    func testHeaderRowsFollowTheBlocksOrder() {
        let header: [String: ScoutHeaderValue] = [
            "sources": .items(["a.py", "b.js"]),
            "verdict": .text("V"),
            "card": .text("Scout: x"),
            "follow_ups": .items(["One — first", "Two — second"]),
        ]
        let lines = ScoutReportHeader.rows(header).map { "\($0.label)=\($0.value)" }
        XCTAssertEqual(lines, ["Card=Scout: x", "Verdict=V",
                               "Follow-up=One — first", "Follow-up=Two — second",
                               "Sources=a.py, b.js"])
    }

    /// The card face's verdict line (`ScoutVerdictLine`) over the six rows
    /// `test_scout_reports_surface.py` runs under `swiftc`.
    func testVerdictLineTable() {
        let rows: [(String, String, String?)] = [
            ("X waits.", "build", "X waits. · build"),
            ("X waits.", "do-not-build", "X waits. · do not build"),
            ("X waits.", "needs-decision", "X waits. · needs a decision"),
            ("X waits.", "more-scouting", "X waits. · more scouting"),
            ("X waits.", "", "X waits."),
            ("", "build", nil),
        ]
        for (verdict, recommendation, expected) in rows {
            XCTAssertEqual(ScoutVerdictLine.text(verdict: verdict,
                                                 recommendation: recommendation),
                           expected, "\(verdict) / \(recommendation)")
        }
        XCTAssertEqual(ScoutVerdictLine.spoken(verdict: "X waits.", recommendation: "build"),
                       "verdict: X waits.. recommendation: build")
        XCTAssertEqual(ScoutVerdictLine.spoken(verdict: "X waits.", recommendation: ""),
                       "verdict: X waits.")
        XCTAssertEqual(ScoutVerdictLine.spoken(verdict: "", recommendation: "build"), "")
    }

    // The text search merged into the instant filter — the panel's
    // `ScoutReportsTests` cases, mirrored.

    private func hitRow(_ path: String, title: String = "", at writtenAt: Double,
                        snippet: String = "") -> ScoutReportRow {
        var row = ScoutReportRow()
        row.path = path
        row.title = title
        row.writtenAt = writtenAt
        row.snippet = snippet
        if !snippet.isEmpty {
            row.matchLine = 3
            row.match = "body"
        }
        return row
    }

    func testAHitRowDecodesItsSnippetAndAnEmptyOneStillDecodes() throws {
        let hit = try JSONDecoder().decode(ScoutReportRow.self, from: Data(#"""
        {"path":"/p/r.md","snippet":"the box.js loop","match_line":7,"match":"body"}
        """#.utf8))
        XCTAssertEqual(hit.snippet, "the box.js loop")
        XCTAssertEqual(hit.matchLine, 7)
        XCTAssertEqual(hit.match, "body")
        let empty = try JSONDecoder().decode(ScoutReportRow.self, from: Data("{}".utf8))
        XCTAssertEqual(empty.snippet, "")
        XCTAssertEqual(empty.matchLine, 0)
    }

    func testWantsBodySearchFromThreeCharacters() {
        XCTAssertFalse(ScoutReportSearch.wantsBodySearch("ab"))
        XCTAssertFalse(ScoutReportSearch.wantsBodySearch("  ab  "))
        XCTAssertTrue(ScoutReportSearch.wantsBodySearch("abc"))
    }

    func testMergeListsABodyOnlyHitOnceWithItsSnippet() {
        let rows = [hitRow("/a", title: "Relay", at: 30), hitRow("/b", title: "Widget", at: 20)]
        let hits = [hitRow("/c", title: "Mailbox", at: 25, snippet: "the relay loop")]
        let merged = ScoutReportSearch.merge(query: "relay", rows: rows, hits: hits)
        XCTAssertEqual(merged.map(\.path), ["/a", "/c"])
        XCTAssertEqual(merged[1].snippet, "the relay loop")
    }

    func testMergeKeepsAnIndexRowFoundBothWaysWithTheHitsSnippet() {
        let rows = [hitRow("/a", title: "Relay", at: 30)]
        let hits = [hitRow("/a", title: "Relay", at: 30, snippet: "relay in the body")]
        let merged = ScoutReportSearch.merge(query: "relay", rows: rows, hits: hits)
        XCTAssertEqual(merged.map(\.path), ["/a"])
        XCTAssertEqual(merged[0].snippet, "relay in the body")
    }

    func testMergeIsNewestFirst() {
        let rows = [hitRow("/new", title: "box", at: 50), hitRow("/old", title: "box", at: 10)]
        let hits = [hitRow("/mid", at: 30, snippet: "box"), hitRow("/tie", at: 50, snippet: "box")]
        let merged = ScoutReportSearch.merge(query: "box", rows: rows, hits: hits)
        XCTAssertEqual(merged.map(\.path), ["/new", "/tie", "/mid", "/old"])
    }

    func testAnEmptyQueryYieldsTheIndexRowsAlone() {
        let rows = [hitRow("/a", title: "Relay", at: 30), hitRow("/b", title: "Widget", at: 20)]
        let hits = [hitRow("/c", at: 25, snippet: "x")]
        XCTAssertEqual(ScoutReportSearch.merge(query: "", rows: rows, hits: hits), rows)
    }

    func testNoMatchLineNamesTheTextOnceItWasSearched() {
        XCTAssertTrue(ScoutReportSearch.noMatchLine(query: "zzqx", searchedText: true)
                          .hasSuffix("any report's text."))
    }

    func testTheTextSearchSendsACollapsedNeedleCappedAt200() {
        XCTAssertEqual(ScoutReportSearch.bodyNeedle("  a  \t trace \n"), "a trace")
        XCTAssertEqual(ScoutReportSearch.maxBodyChars, 200)
        XCTAssertEqual(ScoutReportSearch.bodyNeedle(String(repeating: "x", count: 250))
                           .unicodeScalars.count, 200)
        var hits = ScoutReportIndex()
        hits.query = "a trace"
        hits.rows = [ScoutReportRow()]
        XCTAssertEqual(ScoutReportSearch.hitRows(query: "a   trace", hits: hits).count, 1)
    }

    func testAFailedTextSearchSaysSoInItsOwnWords() {
        let failed = ReportsLoad.applyHits(requested: "box", current: "box", fetched: nil)
        XCTAssertEqual(failed?.value, ScoutReportIndex())
        XCTAssertEqual(failed?.detail, "Dark Army could not search the reports' text.")
        XCTAssertNotEqual(failed?.detail, ScoutReportIndex.fetchFailedLine)
        XCTAssertNil(ReportsLoad.applyHits(requested: "box", current: "boxes", fetched: nil))
        var answered = ScoutReportIndex()
        answered.available = true
        answered.query = "box"
        XCTAssertEqual(ReportsLoad.applyHits(requested: "box", current: "box",
                                             fetched: answered)?.detail, "")
    }
}
