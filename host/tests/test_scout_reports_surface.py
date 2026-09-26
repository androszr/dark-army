"""The scout reports have a section on both apps.

`ScoutReports.swift` — the rows, the body, the search, the header's order,
the date line and the stale-reply rule — is one file on the Mac and the
phone, compared byte for byte and run alone under `swiftc` here over the
same table `ios/BobPhoneTests/ScoutReportSearchTests.swift` and
`panel/Tests/BobPanelTests/ScoutReportsTests.swift` hold. The wiring —
the phone's two sealed reads, the project membership, the literal titles,
the Menu's gate, the Mac's tab — is pinned by source greps. Contracts:
`docs/transport-contract.md` (*`scout_reports` and `scout_report` are
sealed reads*) and `docs/phone-contract.md` (*Scout reports open from the
Menu's Scouting tile*).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
SHARED_MAC = PANEL / "ScoutReports.swift"
SHARED_PHONE = PHONE / "ScoutReports.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PHONE_FILES = ("ScoutReports.swift", "ScoutReportsView.swift",
               "ScoutReportReaderView.swift")

#: `KeyedDecodingContainer.value` lives in each app's `Models.swift`; the
#: harness carries the same three lines so the shared file compiles alone.
HARNESS = r'''
import Foundation

extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }
}

struct Case: Decodable {
    var query: String
    var row: [String: String]
}

let data = FileHandle.standardInput.readDataToEndOfFile()
let cases = try! JSONDecoder().decode([Case].self, from: data)
for c in cases {
    let rowData = try! JSONSerialization.data(withJSONObject: c.row)
    let row = try! JSONDecoder().decode(ScoutReportRow.self, from: rowData)
    print("MATCH \(ScoutReportSearch.matches(query: c.query, row: row))")
}
let headerJSON = """
{"available": true, "header": {"sources": ["a.py", "b.js"], "verdict": "V",
 "question": "Q", "card": "Scout: x", "confidence": "high",
 "follow_ups": [{"title": "One", "summary": "first"},
                {"title": "Two", "summary": "second"}]}}
"""
let body = try! JSONDecoder().decode(ScoutReportBody.self,
                                     from: Data(headerJSON.utf8))
for line in ScoutReportHeader.rows(body.header) {
    print("HEADER \(line.label)=\(line.value)")
}
let posix = Locale(identifier: "en_US_POSIX")
let utc = TimeZone(identifier: "UTC")!
print("DATE_DAY \(ScoutReportRow.dateLine(day: "2026-09-25", writtenAt: 0, locale: posix, timeZone: utc))")
print("DATE_BOTH \(ScoutReportRow.dateLine(day: "2026-09-25", writtenAt: 1790000000, locale: posix, timeZone: utc))")
print("DATE_TIME \(ScoutReportRow.dateLine(day: "", writtenAt: 1790000000, locale: posix, timeZone: utc))")
print("DATE_NONE [\(ScoutReportRow.dateLine(day: "", writtenAt: 0))]")
print("PLACEHOLDER \(ScoutReportSearch.placeholder)")
print("NOMATCH \(ScoutReportSearch.noMatchLine(query: "  zzqx "))")
let stale = ReportsLoad.apply(requested: "a", current: "b", fetched: ScoutReportIndex())
print("STALE \(stale == nil)")
let failed = ReportsLoad.apply(requested: "a", current: "a", fetched: ScoutReportIndex?.none)
print("FAILED \(failed?.value.rows.count ?? -1) \(failed?.detail ?? "")")
for (v, r) in [("X waits.", "build"), ("X waits.", "do-not-build"),
               ("X waits.", "needs-decision"), ("X waits.", "more-scouting"),
               ("X waits.", ""), ("", "build")] {
    print("VERDICT \(ScoutVerdictLine.text(verdict: v, recommendation: r) ?? "-")")
    print("VERDICT_SPOKEN [\(ScoutVerdictLine.spoken(verdict: v, recommendation: r))]")
}
print("BODYPLACEHOLDER \(ScoutReportSearch.bodyPlaceholder)")
for q in ["ab", " ab ", "abc"] {
    print("WANTS \(q.count) \(ScoutReportSearch.wantsBodySearch(q))")
}
func mk(_ path: String, _ title: String, _ at: Double, _ snippet: String = "") -> ScoutReportRow {
    var r = ScoutReportRow()
    r.path = path
    r.title = title
    r.writtenAt = at
    r.snippet = snippet
    return r
}
let indexRows = [mk("/a", "Relay", 30), mk("/b", "Widget", 20), mk("/c", "relay notes", 10)]
let hitRows = [mk("/d", "Mailbox", 25, "relay d"), mk("/c", "relay notes", 10, "relay c")]
let merged = ScoutReportSearch.merge(query: "relay", rows: indexRows, hits: hitRows)
print("MERGE \(merged.map { $0.path }.joined(separator: " "))")
print("MERGE_SNIPPET \(merged.map { $0.path + "=" + $0.snippet }.joined(separator: " | "))")
print("MERGE_EMPTY \(ScoutReportSearch.merge(query: "", rows: indexRows, hits: hitRows).map { $0.path }.joined(separator: " "))")
print("MERGE_SHORT \(ScoutReportSearch.merge(query: "re", rows: indexRows, hits: hitRows).map { $0.path }.joined(separator: " "))")
print("NOMATCH_TEXT \(ScoutReportSearch.noMatchLine(query: "  zzqx ", searchedText: true))")
print("BODYNEEDLE [\(ScoutReportSearch.bodyNeedle("  a  \t trace \n"))]")
print("BODYNEEDLE_CAP \(ScoutReportSearch.bodyNeedle(String(repeating: "x", count: 250)).unicodeScalars.count)")
let hitsFailed = ReportsLoad.applyHits(requested: "box", current: "box", fetched: nil)
print("HITS_FAILED \(hitsFailed?.value.rows.count ?? -1) \(hitsFailed?.detail ?? "")")
print("HITS_STALE \(ReportsLoad.applyHits(requested: "box", current: "boxes", fetched: nil) == nil)")
'''

ROW = {"title": "Why the relay is busy",
       "verdict": "The idle poll is most of the bill",
       "question": "What drives Upstash?",
       "project": "dark-army",
       "card_title": "Scout: Redis",
       "recommendation": "do-not-build"}


def _read(path: pathlib.Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _function(text: str, name: str) -> str:
    """The body of `func <name>(` up to the next member."""
    start = text.index(f"func {name}(")
    rest = text[start:]
    end = rest.find("\n    func ", 1)
    end2 = rest.find("\n    ///", 1)
    stops = [e for e in (end, end2) if e > 0]
    return rest[:min(stops)] if stops else rest


@pytest.fixture(scope="module")
def shared_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("scout-reports")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "scout-reports"
    built = subprocess.run(
        [swiftc, str(SHARED_PHONE), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, cases: list) -> list:
    proc = subprocess.run([str(binary)], input=json.dumps(cases),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _lines(out: list, prefix: str) -> list:
    return [line[len(prefix) + 1:] for line in out if line.startswith(prefix + " ")]


def test_the_shared_rule_is_byte_equal_on_both_apps():
    assert SHARED_MAC.read_bytes() == SHARED_PHONE.read_bytes()
    text = _read(SHARED_PHONE)
    assert not re.search(r"^import (AppKit|UIKit|SwiftUI)", text, re.M)
    assert ".help(" not in text


def test_search_matches_each_field_and_nothing_else(shared_bin):
    cases = [{"query": q, "row": ROW} for q in (
        "relay", "idle poll", "UPSTASH", "dark-army", "redis", "not-build",
        "", "   ", "zzqx")]
    out = _run(shared_bin, cases)
    assert _lines(out, "MATCH") == ["true"] * 8 + ["false"]


def test_search_ignores_diacritics(shared_bin):
    cafe = dict(ROW, title="Café relay")
    out = _run(shared_bin, [{"query": "cafe", "row": cafe}])
    assert _lines(out, "MATCH") == ["true"]


def test_header_rows_follow_the_answer_blocks_order(shared_bin):
    out = _run(shared_bin, [])
    assert _lines(out, "HEADER") == [
        "Card=Scout: x", "Question=Q", "Verdict=V", "Confidence=high",
        "Follow-up=One — first", "Follow-up=Two — second",
        "Sources=a.py, b.js"]


def test_the_date_line_is_the_day_and_the_clock(shared_bin):
    out = _run(shared_bin, [])
    assert _lines(out, "DATE_DAY") == ["2026-09-25"]
    both = _lines(out, "DATE_BOTH")[0]
    assert re.fullmatch(r"2026-09-25 · \d{1,2}:\d{2}\s?PM", both), both
    assert _lines(out, "DATE_TIME")[0].startswith("2026-09-21 · ")
    assert _lines(out, "DATE_NONE") == ["[]"]


def test_the_words_and_the_stale_rule(shared_bin):
    out = _run(shared_bin, [])
    assert _lines(out, "PLACEHOLDER") == ["grep title, verdict, question, project…"]
    assert _lines(out, "BODYPLACEHOLDER") == [
        "grep title, verdict, question, project, text…"]
    assert _lines(out, "NOMATCH")[0].startswith("“zzqx” is not in a title")
    assert _lines(out, "STALE") == ["true"]
    assert _lines(out, "FAILED") == [
        "0 Dark Army could not read the scout reports."]


def test_the_verdict_line_reads_the_two_stored_values(shared_bin):
    """`ScoutVerdictLine` — the card face's verdict line on the Mac tile,
    the card window and the phone card screen — over the six rows
    `ScoutReportsTests.swift` and `ScoutReportSearchTests.swift` hold."""
    out = _run(shared_bin, [])
    assert _lines(out, "VERDICT") == [
        "X waits. · build", "X waits. · do not build",
        "X waits. · needs a decision", "X waits. · more scouting",
        "X waits.", "-"]
    assert _lines(out, "VERDICT_SPOKEN") == [
        "[verdict: X waits.. recommendation: build]",
        "[verdict: X waits.. recommendation: do not build]",
        "[verdict: X waits.. recommendation: needs a decision]",
        "[verdict: X waits.. recommendation: more scouting]",
        "[verdict: X waits.]",
        "[]"]


def test_the_phone_reads_both_kinds_on_the_sealed_doors():
    client = _read(PHONE / "Client.swift")
    assert client.count('kind: "scout_reports"') == 2
    assert client.count('kind: "scout_report"') == 2
    for name in ("scoutReportsIndex", "scoutReportBody"):
        body = _function(client, name)
        assert "knowsItIsAway" in body and "home.request" in body, name
        assert "!backgroundRun" in body, name
        assert "record.token == self.record?.token" in body, name
        assert '"query"' not in body, f"{name} puts a query string on the relay"
        assert "addingPercentEncoding" not in body, name
    assert '["path": path]' in _function(client, "scoutReportBody")


def test_neither_read_rides_the_poll_or_the_background_refresh():
    """The only callers are the two screens: nothing on the poll, the
    background refresh or the widget asks for a report."""
    callers = {}
    for path in sorted(PHONE.glob("*.swift")):
        text = path.read_text()
        for name in ("scoutReportsIndex(", "scoutReportBody("):
            count = text.count("." + name)
            if count:
                callers[(path.name, name)] = count
    assert callers == {
        # The load and the text search.
        ("ScoutReportsView.swift", "scoutReportsIndex("): 2,
        ("ScoutReportReaderView.swift", "scoutReportBody("): 1,
    }, callers
    widget = ROOT / "ios" / "BobPhoneWidget"
    for path in widget.glob("*.swift"):
        assert "scoutReport" not in path.read_text(), path.name


def test_every_new_phone_file_is_in_the_project():
    project = _read(PBXPROJ)
    for name in PHONE_FILES:
        assert project.count(name) >= 4, name
    assert project.count("ScoutReportSearchTests.swift") >= 4


def test_titles_are_literals_and_prose_is_never_clipped():
    assert '.navigationTitle("scout reports")' in _read(PHONE / "ScoutReportsView.swift")
    assert '.navigationTitle("report")' in _read(PHONE / "ScoutReportReaderView.swift")
    for name in PHONE_FILES:
        assert ".lineLimit(" not in _read(PHONE / name), name


def test_the_list_rows_speak_as_one_element_from_what_they_draw():
    text = _read(PHONE / "ScoutReportsView.swift")
    row = text.split("struct ScoutReportListRow", 1)[1]
    assert ".accessibilityElement(children:" in row
    assert ".accessibilityLabel(spoken)" in row
    spoken = row.split("private var spoken: String", 1)[1]
    for forbidden in ("snapshot", "filter(", ".count"):
        assert forbidden not in spoken, forbidden
    assert ".accessibilityHidden(true)" in row, "the › glyph is decorative"


def test_the_body_is_fetched_on_open_and_drawn_under_its_header():
    reader = _read(PHONE / "ScoutReportReaderView.swift")
    assert "client.scoutReportBody(path:" in reader
    assert "ScoutReportHeader.rows(" in reader
    assert "MarkdownText(source: report.body, base: 12, mono: true)" in reader
    assert reader.index("ScoutReportHeader.rows(") < reader.index("MarkdownText(")
    assert ".textSelection(.enabled)" in reader
    lst = _read(PHONE / "ScoutReportsView.swift")
    assert "ReportsLoad.apply(" in lst and 'let requested = "index"' in lst
    assert "scoutReportBody" not in lst, "the list never fetches a body"


def test_the_menu_gates_scouting_on_the_macs_marker():
    menu = _read(PHONE / "MenuView.swift")
    assert "scoutReportsSupported" in menu
    screen = menu.rsplit("case .scouting:", 1)[1]
    assert "if client.snapshot.board.scoutReportsSupported" in screen
    assert "ScoutReportsView(client: client)" in screen
    assert "MenuNotYet(" in screen, "an older Mac keeps the page that says so"
    models = _read(PHONE / "Models.swift")
    assert 'case scoutReportsSupported = "scout_reports_supported"' in models
    assert "scoutReportsSupported = c.value(.scoutReportsSupported, false)" in models


def test_the_mac_has_a_reports_tab_and_pane():
    panel = _read(PANEL / "PanelView.swift")
    assert 'case reports = "Reports"' in panel
    assert "ScoutReportsPane(" in panel and "ScoutReportsRail(" in panel
    assert "tab == .history || tab == .comm || tab == .reports" in panel
    layout = _read(PANEL / "RailLayout.swift")
    assert "case reports" in layout
    assert "case closeReport" in layout and "case leaveReports" in layout
    client = _read(PANEL / "BoardClient.swift")
    assert '"/api/scout-reports' in client and '"/api/scout-report?path=' in client
    pane = _read(PANEL / "ScoutReportsPane.swift")
    for name in ("struct ReportListView", "struct ReportReaderView"):
        chunk = pane.split(name, 1)[1].split("\nstruct ", 1)[0]
        assert "ScoutReport" not in chunk, f"{name} names a scout type"


# ---- the text search over the bodies ----

def test_the_text_search_threshold_and_merge_run_alone(shared_bin):
    out = _run(shared_bin, [])
    assert _lines(out, "WANTS") == ["2 false", "4 false", "3 true"]
    # An index row matched locally, a body-only hit, a row found both ways:
    # newest first, each path once, the both-ways row keeping its snippet.
    assert _lines(out, "MERGE") == ["/a /d /c"]
    assert _lines(out, "MERGE_SNIPPET") == ["/a= | /d=relay d | /c=relay c"]
    assert _lines(out, "MERGE_EMPTY") == ["/a /b /c"]
    assert _lines(out, "MERGE_SHORT") == ["/a /c"]
    assert _lines(out, "BODYNEEDLE") == ["[a trace]"]
    assert _lines(out, "BODYNEEDLE_CAP") == ["200"]
    # A failed text search has its own words, never the list's.
    assert _lines(out, "HITS_FAILED") == [
        "0 Dark Army could not search the reports' text."]
    assert _lines(out, "HITS_STALE") == ["true"]
    assert _lines(out, "NOMATCH_TEXT") == [
        "“zzqx” is not in a title, a verdict, a question, a project or any "
        "report's text."]


def test_the_threshold_is_one_number_on_the_daemon_and_both_apps():
    shared = _read(SHARED_PHONE)
    assert shared.count("static let minBodyChars = 3") == 1
    index = _read(ROOT / "host" / "dark_army_daemon" / "scout_index.py")
    assert re.search(r"^MIN_QUERY_CHARS = 3$", index, re.M)
    assert shared.count("static let maxBodyChars = 200") == 1
    assert re.search(r"^MAX_QUERY_CHARS = 200$", index, re.M)
    assert "static func merge(" in shared


def test_the_phone_asks_for_the_text_search_in_the_sealed_body():
    client = _read(PHONE / "Client.swift")
    body = _function(client, "scoutReportsIndex")
    assert 'body["q"] = query' in body
    assert '"query"' not in body and "addingPercentEncoding" not in body
    assert client.count('kind: "scout_reports"') == 2


def test_both_apps_debounce_and_merge_and_the_phone_gates_on_the_marker():
    phone = _read(PHONE / "ScoutReportsView.swift")
    assert "scoutReportsBodySearchSupported" in phone
    assert "300_000_000" in phone
    assert "ScoutReportSearch.merge(" in phone
    assert ".task(id: bodyKey)" in phone
    assert 'words += ". found: " + row.snippet' in phone
    pane = _read(PANEL / "ScoutReportsPane.swift")
    assert "300_000_000" in pane
    assert "ScoutReportSearch.merge(" in pane
    # A reload (Refresh, a newly attached report) re-asks a held search.
    assert '.task(id: bodySearchKey + "\\u{1}" + loadKey)' in pane
    assert "ReportsLoad.applyHits(" in pane and "ReportsLoad.applyHits(" in phone
    rail = _read(PANEL / "ScoutReportsRail.swift")
    assert "hits: hits" in rail, "the rail counts the merged list"
    assert "ScoutReportSearch.bodyPlaceholder" in rail
    models = _read(PHONE / "Models.swift")
    assert ("scoutReportsBodySearchSupported = "
            "c.value(.scoutReportsBodySearchSupported, false)") in models
    assert ('case scoutReportsBodySearchSupported = '
            '"scout_reports_body_search_supported"') in models
    client = _read(PANEL / "BoardClient.swift")
    assert '("q", query)' in client
