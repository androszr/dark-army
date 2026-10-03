"""The phone lists every project's plans and reads one.

`Plans.swift` — the rows, the body, the search and the stale-reply rule —
is phone-only (the Mac lists no plans yet, so there is no byte pin); it is
run here under `swiftc` beside `ScoutReports.swift`, whose generic
`ReportsLoad.apply` it builds on, over the table
`ios/BobPhoneTests/PlanSearchTests.swift` holds. The wiring — the two
sealed reads, the project membership, the literal titles, the Menu's gate,
the card jump — is pinned by source greps. Contracts:
`docs/sealed-reads-contract.md` (*`plans` and `plan` are sealed reads*) and
`docs/phone-contract.md` (*Plans open from the Menu's Plans tile*).
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
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PHONE_FILES = ("Plans.swift", "PlansView.swift", "PlanReaderView.swift")

#: `KeyedDecodingContainer.value` lives in `Models.swift`; the harness
#: carries the same three lines so the rule compiles without the app.
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
    let row = try! JSONDecoder().decode(PlanRow.self, from: rowData)
    print("MATCH \(PlanSearch.matches(query: c.query, row: row))")
}
let full = try! JSONDecoder().decode(PlanRow.self, from: Data("""
{"path": "/r/plans/2026-09-25-x.md", "name": "2026-09-25-x.md",
 "day": "2026-09-25", "project": "dark-army", "status": "draft",
 "area": "pocket", "card_title": "Phone plans", "card_column": "backlog",
 "written_at": 1790000000, "unknown_key": [1, 2]}
""".utf8))
print("CARD \(full.cardLine)")
print("DATE \(full.dateLine)")
print("HEADER \(full.headerLine)")
print("META \(full.metaLine)")
print("TITLE \(full.displayTitle)")
let bare = try! JSONDecoder().decode(PlanRow.self, from: Data("{}".utf8))
print("BARE_CARD [\(bare.cardLine)]")
print("BARE_HEADER [\(bare.headerLine)]")
let body = try! JSONDecoder().decode(PlanBody.self, from: Data("""
{"available": false, "reason": "that plan is not one Dark Army lists"}
""".utf8))
let refused = PlansLoad.apply(requested: "p", current: "p", fetched: body)
print("REFUSED \(refused?.detail ?? "-")")
print("PLACEHOLDER \(PlanSearch.placeholder)")
print("NOMATCH \(PlanSearch.noMatchLine(query: "  zzqx "))")
let stale = PlansLoad.apply(requested: "a", current: "b", fetched: PlanIndex())
print("STALE \(stale == nil)")
let failed = PlansLoad.apply(requested: "a", current: "a", fetched: PlanIndex?.none)
print("FAILED \(failed?.value.rows.count ?? -1) \(failed?.detail ?? "")")
var cut = PlanIndex()
cut.available = true
cut.truncated = true
cut.omitted = 3
print("TRUNCATED \(PlansLoad.apply(requested: "i", current: "i", fetched: cut)?.detail ?? "-")")
'''

ROW = {"title": "The phone lists every project's plans",
       "project": "dark-army",
       "status": "draft",
       "area": "pocket",
       "slug": "phone-plans-library",
       "card_title": "Phone plans library",
       "path": "/Users/x/zebra-root/plans/2026-09-25-phone-plans-library.md",
       "root": "/Users/x/zebra-root",
       "day": "2026-09-25"}


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
def plans_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-plans")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-plans"
    built = subprocess.run(
        [swiftc, str(PHONE / "Plans.swift"), str(PHONE / "ScoutReports.swift"),
         str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, cases: list) -> list:
    proc = subprocess.run([str(binary)], input=json.dumps(cases),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _lines(out: list, prefix: str) -> list:
    return [line[len(prefix) + 1:] for line in out
            if line.startswith(prefix + " ")]


def test_the_rule_file_is_foundation_only():
    text = _read(PHONE / "Plans.swift")
    assert not re.search(r"^import (AppKit|UIKit|SwiftUI)", text, re.M)
    assert "ReportsLoad.apply(" in text, "built on the scout pair's generic"


def test_search_matches_each_field_and_nothing_else(plans_bin):
    cases = [{"query": q, "row": ROW} for q in (
        "every project", "DARK-ARMY", "draft", "pocket", "plans-library",
        "library", "", "   ", "zebra", "2026-09-25", "zzqx")]
    out = _run(plans_bin, cases)
    assert _lines(out, "MATCH") == ["true"] * 8 + ["false"] * 3


def test_search_ignores_diacritics(plans_bin):
    cafe = dict(ROW, title="Café plan")
    out = _run(plans_bin, [{"query": "cafe", "row": cafe}])
    assert _lines(out, "MATCH") == ["true"]


def test_the_row_lines(plans_bin):
    out = _run(plans_bin, [])
    assert _lines(out, "CARD") == ["card: Phone plans · backlog"]
    assert _lines(out, "DATE") == ["2026-09-25"]
    assert _lines(out, "HEADER") == ["draft · pocket"]
    assert _lines(out, "META") == ["dark-army · 2026-09-25"]
    assert _lines(out, "TITLE") == ["2026-09-25-x.md"]
    assert _lines(out, "BARE_CARD") == ["[]"]
    assert _lines(out, "BARE_HEADER") == ["[]"]


def test_the_words_and_the_stale_rule(plans_bin):
    out = _run(plans_bin, [])
    assert _lines(out, "PLACEHOLDER") == ["grep title, project, status, area…"]
    assert _lines(out, "NOMATCH")[0].startswith("“zzqx” is not in a title")
    assert _lines(out, "STALE") == ["true"]
    assert _lines(out, "FAILED") == ["0 Dark Army could not read the plans."]
    assert _lines(out, "REFUSED") == ["that plan is not one Dark Army lists"]
    assert _lines(out, "TRUNCATED") == [
        "3 older plans were left out of this list."]


def test_the_phone_reads_both_kinds_on_the_sealed_doors():
    client = _read(PHONE / "Client.swift")
    assert client.count('kind: "plans"') == 2
    assert client.count('kind: "plan"') == 2
    for name in ("plansIndex", "planBody"):
        body = _function(client, name)
        assert "knowsItIsAway" in body and "home.request" in body, name
        assert "!backgroundRun" in body, name
        assert "record.token == self.record?.token" in body, name
        assert '"query"' not in body, f"{name} puts a query string on the relay"
        assert "addingPercentEncoding" not in body, name
    assert '["path": path]' in _function(client, "planBody")


def test_neither_read_rides_the_poll_or_the_background_refresh():
    """The only callers are the two screens: nothing on the poll, the
    background refresh or the widget asks for a plan."""
    callers = {}
    for path in sorted(PHONE.glob("*.swift")):
        text = path.read_text()
        for name in ("plansIndex(", "planBody("):
            count = text.count("." + name)
            if count:
                callers[(path.name, name)] = count
    assert callers == {
        ("PlansView.swift", "plansIndex("): 1,
        ("PlanReaderView.swift", "planBody("): 1,
    }, callers
    widget = ROOT / "ios" / "BobPhoneWidget"
    for path in widget.glob("*.swift"):
        text = path.read_text()
        assert "plansIndex" not in text and "planBody" not in text, path.name


def test_every_new_phone_file_is_in_the_project():
    project = _read(PBXPROJ)
    lines = project.splitlines()
    for name in PHONE_FILES + ("PlanSearchTests.swift",):
        # Build file, file reference, group child, sources phase: four
        # lines (the test target's group and phase are one line each).
        pattern = re.compile(r"[ /]" + re.escape(name))
        assert sum(1 for line in lines if pattern.search(line)) == 4, name
    assert "path = BobPhoneTests/PlanSearchTests.swift; sourceTree = SOURCE_ROOT;" \
        in project


def test_titles_are_literals_and_prose_is_never_clipped():
    assert '.navigationTitle("plans")' in _read(PHONE / "PlansView.swift")
    assert '.navigationTitle("plan")' in _read(PHONE / "PlanReaderView.swift")
    for name in PHONE_FILES:
        assert ".lineLimit(" not in _read(PHONE / name), name


def test_the_list_rows_speak_as_one_element_from_what_they_draw():
    text = _read(PHONE / "PlansView.swift")
    row = text.split("struct PlanListRow", 1)[1]
    assert ".accessibilityElement(children: .combine)" in row
    assert ".accessibilityLabel(spoken)" in row
    assert '.accessibilityHint("Opens the plan")' in row
    spoken = row.split("private var spoken: String", 1)[1]
    for forbidden in ("snapshot", "filter(", ".count"):
        assert forbidden not in spoken, forbidden
    assert ".accessibilityHidden(true)" in row, "the › glyph is decorative"
    assert 'accessibilityLabel("Search the plans")' in text


def test_the_list_fetches_once_and_never_a_body():
    lst = _read(PHONE / "PlansView.swift")
    assert "PlansLoad.apply(" in lst and 'let requested = "index"' in lst
    assert "planBody" not in lst, "the list never fetches a body"
    assert ".task { await load() }" in lst
    assert ".navigationDestination(item: $openedPath)" in lst
    assert ".scoutReportsIndex(" not in lst and ".scoutReportBody(" not in lst


def test_the_reader_fetches_on_open_and_jumps_to_a_card_on_the_board():
    reader = _read(PHONE / "PlanReaderView.swift")
    assert "client.planBody(path:" in reader
    assert "MarkdownText(source: plan.body, base: 12, mono: true)" in reader
    assert ".textSelection(.enabled)" in reader
    assert "@EnvironmentObject private var sheets: PhoneSheetRouter" in reader
    # The card resolves against the board the phone holds, at the draw.
    assert "client.snapshot.board.cards.first(where: { $0.id == row.cardId })" \
        in reader
    assert "if let card {" in reader
    assert "sheets.show(.card(card))" in reader
    assert 'accessibilityLabel("Open the card \\(card.title)")' in reader
    assert reader.index("sheets.show(") < reader.index("MarkdownText(")
    assert ".scoutReportsIndex(" not in reader
    assert ".scoutReportBody(" not in reader


def test_the_menu_gates_plans_on_the_macs_marker():
    menu = _read(PHONE / "MenuView.swift")
    assert "plans: client.snapshot.board.plansSupported" in menu
    screen = menu.rsplit("case .plans:", 1)[1]
    assert "if client.snapshot.board.plansSupported" in screen
    assert "PlansView(client: client)" in screen
    assert "MenuNotYet(" in screen, "an older Mac keeps the page that says so"
    models = _read(PHONE / "Models.swift")
    assert 'case plansSupported = "plans_supported"' in models
    assert "plansSupported = c.value(.plansSupported, false)" in models
    assert "var plansSupported = false" in models
