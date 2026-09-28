"""The History week's money, one fold on the Mac and the phone.

`LedgerWeek` is one Foundation-only enum, byte-pinned Mac/phone from the
`enum LedgerWeek {` marker down (`test_comm_rules.py`'s idiom), and its
arithmetic is run under `swiftc` rather than read (`test_cast.py`'s idiom).
The Mac's `LedgerFold` calls it — `panel/Tests/BobPanelTests/LedgerWeekTests.swift`
proves the Mac's seven-day spent columns equal `LedgerWeek.picture` — so what
is asserted here about the phone's week is what the Mac's History draws.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PANEL_FILE = REPO / "panel" / "Sources" / "BobPanel" / "LedgerWeek.swift"
PHONE_FILE = REPO / "ios" / "BobPhone" / "LedgerWeek.swift"
PROJECT = REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
MARKER = "enum LedgerWeek {"


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


# --- the byte-pinned pair ------------------------------------------------------


def test_the_panel_and_phone_share_one_fold():
    panel, phone = _shared(PANEL_FILE), _shared(PHONE_FILE)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 3000, f"parsed too little from the {name} copy"
        for needle in ("static func picture(cards:", "static func fold(works:",
                       "static func work(card:", "static func column(day:",
                       "static func segments(", "static func usd(",
                       "static let notPriced = \"not priced\""):
            assert needle in region, (name, needle)
    assert panel == phone, (
        "ios/BobPhone/LedgerWeek.swift has drifted from the panel's copy — "
        "mirror the edit onto the other side; never re-baseline one side.")


def test_the_fold_is_foundation_only():
    for path in (PANEL_FILE, PHONE_FILE):
        text = path.read_text()
        assert "import Foundation" in text
        assert "SwiftUI" not in text, path
        region = _shared(path)
        # Self-contained: the phone has none of the Mac's formatters or
        # report types, so the shared copy may name none of them.
        for foreign in ("Format.", "HistoryFormat", "HistoryReport", "Theme."):
            assert foreign not in region, (path.name, foreign)


def test_the_mac_calls_the_shared_fold():
    fold = (REPO / "panel" / "Sources" / "BobPanel" / "LedgerFold.swift").read_text()
    assert "LedgerWeek.fold(" in fold
    assert "LedgerWeek.work(" in fold
    layout = (REPO / "panel" / "Sources" / "BobPanel" / "LedgerLayout.swift").read_text()
    assert "LedgerWeek.segments(" in layout


def test_the_phone_project_compiles_the_four_files():
    project = PROJECT.read_text()
    for name in ("LedgerWeek.swift", "HistoryWeek.swift", "HistoryWeekView.swift",
                 "HistoryWeekTests.swift"):
        assert f"{name} in Sources" in project, name


# --- the fold, run --------------------------------------------------------------


HARNESS = r'''
var utc = Calendar(identifier: .gregorian)
utc.timeZone = TimeZone(identifier: "UTC")!
utc.locale = Locale(identifier: "en_US_POSIX")
let now = Date(timeIntervalSince1970: 1_758_000_000)
func at(_ daysAgo: Int) -> Double {
    now.timeIntervalSince1970 - Double(daysAgo) * 86_400
}
func card(_ id: String, _ provider: String = "claude", token: Double? = nil,
          reported: Double? = nil, daysAgo: Int = 0, known: Bool? = nil,
          unpricedTurns: Int? = nil) -> LedgerWeek.Card {
    LedgerWeek.Card(id: id, sessions: [LedgerWeek.Session(
        sessionId: "s-\(id)", provider: provider, phase: "implementation",
        boundAt: at(daysAgo), known: known ?? (token != nil),
        tokenCostUsd: token, tokenUnpricedTurns: unpricedTurns,
        reportedCostUsd: reported)])
}
func columns(_ p: LedgerWeek.Picture) -> [[String: Any]] {
    p.days.map { c in
        var out: [String: Any] = ["day": c.day, "figure": c.figure,
                                  "unknown": c.unknown, "tokenCost": c.tokenCost]
        if let mark = c.reportedMark { out["reportedMark"] = mark }
        return out
    }
}
func picture(_ p: LedgerWeek.Picture) -> [String: Any] {
    var out: [String: Any] = ["total": p.total, "unpriced": p.unpriced,
                              "columns": columns(p), "span": p.span]
    if let reported = p.reported { out["reported"] = reported }
    if let usual = p.usual { out["usual"] = usual }
    return out
}
var out: [String: Any] = [:]
out["three"] = picture(LedgerWeek.picture(cards: [
    card("c", token: 4.0, reported: 6.0, daysAgo: 0),
    card("g", "grok", token: 2.0, reported: 1.5, daysAgo: 1),
    card("x", "codex", token: 1.0, daysAgo: 2),
], others: [], now: now, calendar: utc))
out["unpriced_day"] = picture(LedgerWeek.picture(cards: [
    card("p", token: 2.0, daysAgo: 0),
    card("u", daysAgo: 3, known: false),
], others: [], now: now, calendar: utc))
out["nothing_priced"] = picture(LedgerWeek.picture(cards: [
    card("u", daysAgo: 3, known: false),
], others: [], now: now, calendar: utc))
out["empty"] = picture(LedgerWeek.picture(cards: [], others: [], now: now, calendar: utc))
out["reported_zero"] = picture(LedgerWeek.picture(cards: [
    card("z", token: 1.25, reported: 0, daysAgo: 0),
    card("a", token: 2.0, daysAgo: 1),
], others: [], now: now, calendar: utc))
let keys = LedgerWeek.dayKeys(now: now, calendar: utc)
out["keys"] = keys
out["today"] = LedgerWeek.dayKey(now, calendar: utc)
out["early"] = picture(LedgerWeek.picture(cards: [
    card("old", token: 3.0, daysAgo: 10),
], others: [
    LedgerWeek.OtherDay(day: "2025-09-01", claude: 0.5, claudeReported: 0.25),
    LedgerWeek.OtherDay(day: keys[0], codex: 0.25),
], now: now, calendar: utc))
out["late"] = picture(LedgerWeek.picture(cards: [], others: [
    LedgerWeek.OtherDay(day: "2025-09-17", claude: 0.5, claudeReported: 0.25),
    LedgerWeek.OtherDay(day: keys[6], codex: 0.25),
    LedgerWeek.OtherDay(day: keys[3], grok: 1.0),
], now: now, calendar: utc))
out["segments"] = LedgerWeek.segments(claude: 1, grok: 2, codex: 3).map(\.provider)
out["segments_zero"] = LedgerWeek.segments(claude: 1, grok: 0, codex: 2).map(\.provider)
out["median"] = LedgerWeek.picture(cards: (0..<7).map {
    card("m\($0)", token: Double([5, 1, 9, 3, 7, 2, 8][$0]), daysAgo: $0)
}, others: [], now: now, calendar: utc).usual ?? -1
out["usd"] = [0, 0.004, 0.05, 1, 9.99, 10, 123.456, 4819.2].map { LedgerWeek.usd($0) }
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("ledger-week")
    source = tmp / "main.swift"
    source.write_text(PANEL_FILE.read_text() + "\n" + HARNESS)
    executable = tmp / "LedgerWeekProbe"
    built = subprocess.run([swiftc, str(source), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True,
                         timeout=30)
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


def test_a_week_of_three_providers_totals_the_token_cost_and_keeps_the_report_aside(week):
    """The success criterion's first half: the seven-day total is the token
    cost of Claude, Grok and Codex together, and the reported dollar sits
    beside it without being added."""
    three = week["three"]
    assert three["total"] == "$7.00"
    assert three["reported"] == "$7.50 reported"
    assert three["total"] != "$14.50"
    figures = [c["figure"] for c in three["columns"]]
    assert figures[-3:] == ["$1.00", "$2.00", "$4.00"]
    for column in three["columns"]:
        assert "7.50" not in column["figure"]
        assert "reported" not in column["figure"]
    marks = [c.get("reportedMark") for c in three["columns"]]
    assert marks[-3:] == [None, "$1.50 reported", "$6.00 reported"]


def test_a_day_with_no_price_reads_not_priced_never_zero(week):
    """The success criterion's second half: a day nothing could be priced on
    is the dash, a week with no price anywhere is "not priced", and neither
    is ever $0.00."""
    day = week["unpriced_day"]["columns"][3]
    assert day["figure"] == "—"
    assert day["unknown"] is True
    assert week["unpriced_day"]["total"] == "$2.00"
    assert week["unpriced_day"]["unpriced"] == 1
    for name in ("nothing_priced", "empty"):
        assert week[name]["total"] == "not priced", name
        assert "$0.00" not in json.dumps(week[name]), name
    # A day nothing happened on draws nothing at all, not $0.00.
    assert week["empty"]["columns"][0]["figure"] == ""


def test_a_reported_zero_is_present_and_an_absent_report_is_not(week):
    columns = week["reported_zero"]["columns"]
    assert columns[-1]["reportedMark"] == "$0.00 reported"
    assert columns[-1]["figure"] == "$1.25"
    assert "reportedMark" not in columns[-2]
    assert columns[-2]["figure"] == "$2.00"


def test_work_before_the_first_column_lands_on_it(week):
    early = week["early"]
    first = early["columns"][0]
    # The card ten days back, the leftover row a fortnight back and the
    # first day's own leftover row all land on the first column.
    assert first["tokenCost"] == pytest.approx(3.75)
    assert first["figure"] == "$3.75"
    assert first["reportedMark"] == "$0.25 reported"
    assert early["total"] == "$3.75"


def test_a_leftover_day_after_the_last_column_lands_on_it(week):
    """The Mac dates a leftover row by its own clock; a phone in a time zone
    behind it sees the Mac's today as tomorrow. It joins the last column
    rather than falling out of the week."""
    late = week["late"]
    last = late["columns"][-1]
    assert last["tokenCost"] == pytest.approx(0.75)
    assert last["reportedMark"] == "$0.25 reported"
    assert late["columns"][3]["tokenCost"] == pytest.approx(1.0)
    assert late["total"] == "$1.75"


def test_segments_are_claude_grok_codex_and_omit_zeros(week):
    assert week["segments"] == ["claude", "grok", "codex"]
    assert week["segments_zero"] == ["claude", "codex"]


def test_the_week_is_seven_local_days_ending_today(week):
    keys = week["keys"]
    assert len(keys) == 7
    assert keys == sorted(keys)
    assert len(set(keys)) == 7
    assert keys[-1] == week["today"] == "2025-09-16"
    assert week["three"]["span"] == "Wed 10 Sep – Tue 16 Sep"


def test_usual_is_the_median_of_the_seven_days(week):
    assert week["median"] == 5


def test_the_shared_dollar_formats_like_the_mac(week):
    assert week["usd"] == ["$0.00", "<$0.01", "$0.05", "$1.00", "$9.99",
                           "$10.00", "$123.46", "$4819.20"]
