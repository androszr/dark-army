import XCTest
@testable import BobPanel

/// The scout-report list and reader's shared rules (`ScoutReports.swift`,
/// byte-copied to the phone): tolerant decoding, the stale-reply rule, the
/// header's order and the search table.
final class ScoutReportsTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    func testEmptyObjectsDecode() throws {
        let index = try decode(ScoutReportIndex.self, "{}")
        XCTAssertEqual(index, ScoutReportIndex())
        XCTAssertEqual(index.rows, [])
        let row = try decode(ScoutReportRow.self, "{}")
        XCTAssertEqual(row.path, "")
        XCTAssertNil(row.checked)
        let body = try decode(ScoutReportBody.self, "{}")
        XCTAssertFalse(body.available)
        XCTAssertEqual(body.header, [:])
    }

    func testRowDecodesTheWireNames() throws {
        let row = try decode(ScoutReportRow.self, #"""
        {"path":"/p/scout/2026-09-25-x/report.md","written_at":12.5,
         "card_title":"Scout: x","card_id":"c1","card_column":"done",
         "checked":null,"title":"T","verdict":"V","project":"alpha"}
        """#)
        XCTAssertEqual(row.id, "/p/scout/2026-09-25-x/report.md")
        XCTAssertEqual(row.writtenAt, 12.5)
        XCTAssertEqual(row.cardTitle, "Scout: x")
        XCTAssertNil(row.checked)
        XCTAssertEqual(row.displayTitle, "T")
    }

    func testABodyWithFollowUpsAndSourcesDecodes() throws {
        let body = try decode(ScoutReportBody.self, #"""
        {"available":true,"has_header":true,"body":"## Question\n",
         "header":{"verdict":"Build it.","sources":["a.py","b.js"],
                   "follow_ups":[{"title":"One","summary":"first"},
                                 {"title":"Two","summary":""}]}}
        """#)
        XCTAssertTrue(body.available)
        XCTAssertTrue(body.hasHeader)
        XCTAssertEqual(body.header["verdict"], .text("Build it."))
        XCTAssertEqual(body.header["sources"], .items(["a.py", "b.js"]))
        XCTAssertEqual(body.header["follow_ups"], .items(["One — first", "Two"]))
    }

    func testHeaderRowsFollowTheBlocksOrderAndOmitWhatIsMissing() {
        let header: [String: ScoutHeaderValue] = [
            "sources": .items(["a.py", "b.js"]),
            "verdict": .text("V"),
            "card": .text("Scout: x"),
            "follow_ups": .items(["One — first", "Two — second"]),
            "confidence": .text("high"),
        ]
        let rows = ScoutReportHeader.rows(header).map { "\($0.label)=\($0.value)" }
        XCTAssertEqual(rows, [
            "Card=Scout: x", "Verdict=V", "Confidence=high",
            "Follow-up=One — first", "Follow-up=Two — second",
            "Sources=a.py, b.js",
        ])
        XCTAssertTrue(ScoutReportHeader.rows([:]).isEmpty)
    }

    func testReportsLoadDropsAStaleReply() {
        var index = ScoutReportIndex()
        index.available = true
        XCTAssertNil(ReportsLoad.apply(requested: "a", current: "b", fetched: index))
        var body = ScoutReportBody()
        body.available = true
        XCTAssertNil(ReportsLoad.apply(requested: "/x", current: "/y", fetched: body))
    }

    func testAFailedFetchNeverKeepsThePreviousRows() {
        let outcome = ReportsLoad.apply(requested: "a", current: "a",
                                        fetched: ScoutReportIndex?.none)
        XCTAssertEqual(outcome?.value, ScoutReportIndex())
        XCTAssertEqual(outcome?.detail, ScoutReportIndex.fetchFailedLine)
        var closed = ScoutReportIndex()
        closed.rows = [ScoutReportRow()]
        let unavailable = ReportsLoad.apply(requested: "a", current: "a",
                                            fetched: closed)
        XCTAssertEqual(unavailable?.value.rows, [])
        XCTAssertEqual(unavailable?.detail, ScoutReportIndex.unavailableLine)
    }

    func testAnUnavailableBodyCarriesTheDaemonsWords() {
        var body = ScoutReportBody()
        body.reason = "that report is not one Dark Army lists"
        let outcome = ReportsLoad.apply(requested: "/x", current: "/x", fetched: body)
        XCTAssertEqual(outcome?.detail, "that report is not one Dark Army lists")
    }

    func testSearchTable() {
        var row = ScoutReportRow()
        row.title = "Why the relay is busy"
        row.verdict = "The idle poll is most of the bill"
        row.question = "What drives Upstash?"
        row.project = "dark-army"
        row.cardTitle = "Scout: Redis"
        row.recommendation = "build"
        for query in ["relay", "IDLE POLL", "upstash", "dark-army", "redis",
                      "build", "", "   "] {
            XCTAssertTrue(ScoutReportSearch.matches(query: query, row: row), query)
        }
        XCTAssertFalse(ScoutReportSearch.matches(query: "zzqx", row: row))
        row.title = "Café report"
        XCTAssertTrue(ScoutReportSearch.matches(query: "cafe", row: row))
        XCTAssertTrue(ScoutReportSearch.noMatchLine(query: " zzqx ")
            .hasPrefix("“zzqx” is not in a title"))
    }

    func testDateLineJoinsTheDayAndTheClock() {
        let posix = Locale(identifier: "en_US_POSIX")
        let utc = TimeZone(identifier: "UTC")!
        XCTAssertEqual(ScoutReportRow.dateLine(day: "2026-09-25", writtenAt: 0,
                                               locale: posix, timeZone: utc),
                       "2026-09-25")
        let line = ScoutReportRow.dateLine(day: "", writtenAt: 1_790_000_000,
                                           locale: posix, timeZone: utc)
        XCTAssertTrue(line.hasPrefix("2026-09-21 · "), line)
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

    // MARK: the text search merged into the instant filter

    private func row(_ path: String, title: String = "", at writtenAt: Double,
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
        let hit = try decode(ScoutReportRow.self, #"""
        {"path":"/p/r.md","snippet":"the box.js loop","match_line":7,"match":"body"}
        """#)
        XCTAssertEqual(hit.snippet, "the box.js loop")
        XCTAssertEqual(hit.matchLine, 7)
        XCTAssertEqual(hit.match, "body")
        let empty = try decode(ScoutReportRow.self, "{}")
        XCTAssertEqual(empty.snippet, "")
        XCTAssertEqual(empty.matchLine, 0)
        let index = try decode(ScoutReportIndex.self, #"""
        {"query":"box","searched":3,"unsearched":1,"search_truncated":true,
         "hits_truncated":false,"rows":[]}
        """#)
        XCTAssertEqual(index.query, "box")
        XCTAssertEqual(index.searched, 3)
        XCTAssertTrue(index.searchTruncated)
        XCTAssertFalse(index.hitsTruncated)
        XCTAssertTrue(index.bodySearchLine.contains("1 report was not read"))
        XCTAssertEqual(ScoutReportIndex().bodySearchLine, "")
    }

    func testWantsBodySearchFromThreeCharacters() {
        XCTAssertEqual(ScoutReportSearch.minBodyChars, 3)
        XCTAssertFalse(ScoutReportSearch.wantsBodySearch("ab"))
        XCTAssertFalse(ScoutReportSearch.wantsBodySearch("  ab  "))
        XCTAssertTrue(ScoutReportSearch.wantsBodySearch("abc"))
    }

    func testMergeListsABodyOnlyHitOnceWithItsSnippet() {
        let rows = [row("/a", title: "Relay", at: 30), row("/b", title: "Widget", at: 20)]
        let hits = [row("/c", title: "Mailbox", at: 25, snippet: "the relay loop")]
        let merged = ScoutReportSearch.merge(query: "relay", rows: rows, hits: hits)
        XCTAssertEqual(merged.map(\.path), ["/a", "/c"])
        XCTAssertEqual(merged[1].snippet, "the relay loop")
        XCTAssertEqual(merged[0].snippet, "")
    }

    func testMergeKeepsAnIndexRowFoundBothWaysWithTheHitsSnippet() {
        let rows = [row("/a", title: "Relay", at: 30)]
        let hits = [row("/a", title: "Relay", at: 30, snippet: "relay in the body")]
        let merged = ScoutReportSearch.merge(query: "relay", rows: rows, hits: hits)
        XCTAssertEqual(merged.map(\.path), ["/a"])
        XCTAssertEqual(merged[0].snippet, "relay in the body")
        XCTAssertEqual(merged[0].matchLine, 3)
    }

    func testMergeIsNewestFirst() {
        let rows = [row("/new", title: "box", at: 50), row("/old", title: "box", at: 10)]
        let hits = [row("/mid", at: 30, snippet: "box"), row("/tie", at: 50, snippet: "box")]
        let merged = ScoutReportSearch.merge(query: "box", rows: rows, hits: hits)
        XCTAssertEqual(merged.map(\.path), ["/new", "/tie", "/mid", "/old"])
    }

    func testAnEmptyOrShortQueryYieldsTheIndexRowsAlone() {
        let rows = [row("/a", title: "Relay", at: 30), row("/b", title: "Widget", at: 20)]
        let hits = [row("/c", at: 25, snippet: "x")]
        XCTAssertEqual(ScoutReportSearch.merge(query: "", rows: rows, hits: hits), rows)
        XCTAssertEqual(ScoutReportSearch.merge(query: "re", rows: rows, hits: hits)
                           .map(\.path), ["/a"])
    }

    func testHitRowsAreOnlyThoseForTheNeedleTypedNow() {
        var hits = ScoutReportIndex()
        hits.query = "relay"
        hits.rows = [row("/c", at: 1, snippet: "relay")]
        XCTAssertEqual(ScoutReportSearch.hitRows(query: " relay ", hits: hits).count, 1)
        XCTAssertEqual(ScoutReportSearch.hitRows(query: "relays", hits: hits).count, 0)
        XCTAssertEqual(ScoutReportSearch.hitRows(query: "re", hits: hits).count, 0)
    }

    func testNoMatchLineNamesTheTextOnceItWasSearched() {
        XCTAssertTrue(ScoutReportSearch.noMatchLine(query: "zzqx", searchedText: true)
                          .hasSuffix("any report's text."))
        XCTAssertEqual(ScoutReportSearch.noMatchLine(query: "zzqx"),
                       "“zzqx” is not in a title, a verdict, a question or a project.")
        XCTAssertEqual(ScoutReportSearch.bodyPlaceholder,
                       "grep title, verdict, question, project, text…")
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
