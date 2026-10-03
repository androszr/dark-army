"""How close Claude's limits ran, one fold on the Mac and the phone.

`LimitPressure` is one Foundation-only enum, byte-pinned Mac/phone from the
`enum LimitPressure {` marker down (`test_ledger_week.py`'s idiom), and its
arithmetic is run under `swiftc` rather than read. The Mac's `HistoryLimits`
and the phone's `HistoryWeekLimits` both call it, so what is asserted here
about the phone's chart is what the Mac's History draws.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PANEL_FILE = REPO / "panel" / "Sources" / "BobPanel" / "LimitPressure.swift"
PHONE_FILE = REPO / "ios" / "BobPhone" / "LimitPressure.swift"
PROJECT = REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
MARKER = "enum LimitPressure {"


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


# --- the byte-pinned pair ------------------------------------------------------


def test_the_panel_and_phone_share_one_fold():
    panel, phone = _shared(PANEL_FILE), _shared(PHONE_FILE)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 2000, f"parsed too little from the {name} copy"
        for needle in ("static func picture(points:", "static func columnCount(",
                       "static func height(", "static func percent("):
            assert needle in region, (name, needle)
    assert panel == phone, (
        "ios/BobPhone/LimitPressure.swift has drifted from the panel's copy — "
        "mirror the edit onto the other side; never re-baseline one side.")


def test_the_fold_is_foundation_only():
    for path in (PANEL_FILE, PHONE_FILE):
        text = path.read_text()
        assert "import Foundation" in text
        assert "SwiftUI" not in text, path
        region = _shared(path)
        for foreign in ("Format.", "HistoryFormat", "HistoryReport", "Theme."):
            assert foreign not in region, (path.name, foreign)


def test_both_clients_call_the_shared_fold():
    mac = (REPO / "panel" / "Sources" / "BobPanel" / "History.swift").read_text()
    phone = (REPO / "ios" / "BobPhone" / "HistoryWeek.swift").read_text()
    assert "LimitPressure.picture(" in mac
    assert "LimitPressure.picture(" in phone


def test_the_phone_project_compiles_the_fold():
    project = PROJECT.read_text()
    assert project.count("LimitPressure.swift in Sources") == 2


def test_the_mac_drawing_carries_no_amber_or_alarm():
    view = (REPO / "panel" / "Sources" / "BobPanel" / "LimitPressureView.swift").read_text()
    for banned in ("Theme.amber", "Theme.alarm", "Theme.attention", "Theme.danger",
                   "GeometryReader"):
        assert banned not in view, banned


# --- the fold, run --------------------------------------------------------------


HARNESS = r'''
typealias P = LimitPressure
func col(_ c: P.Column) -> [String: Any] {
    var out: [String: Any] = ["reset": c.reset]
    if let v = c.fiveHour { out["fiveHour"] = v }
    if let v = c.sevenDay { out["sevenDay"] = v }
    return out
}
func pic(_ p: P.Picture?) -> Any {
    guard let p = p else { return NSNull() }
    var out: [String: Any] = ["count": p.columns.count, "headline": p.headline,
                              "spoken": p.spoken, "columns": p.columns.map(col)]
    if let v = p.fiveHourPeak { out["fiveHourPeak"] = v }
    if let v = p.sevenDayPeak { out["sevenDayPeak"] = v }
    return out
}
let day = 86_400.0
var out: [String: Any] = [:]
// A week: 84 columns of 7200 s from t = 0.
let week = P.picture(
    points: [P.Point(ts: 100, fiveHour: 10, sevenDay: 5),
             P.Point(ts: 200, fiveHour: 80, sevenDay: 6),
             P.Point(ts: 300, fiveHour: 20, sevenDay: nil),
             P.Point(ts: 7_200 * 3 + 5, fiveHour: nil, sevenDay: 9),
             P.Point(ts: -5, fiveHour: 99, sevenDay: 99),
             P.Point(ts: 7 * day + 1, fiveHour: 98, sevenDay: 98)],
    resets: [7_200 * 10 + 1, 7 * day + 50, -3], from: 0, to: 7 * day)
out["week"] = pic(week)
out["seven_only"] = pic(P.picture(
    points: [P.Point(ts: 10, fiveHour: nil, sevenDay: 41.4)],
    resets: [], from: 0, to: day))
out["no_readings"] = pic(P.picture(
    points: [P.Point(ts: 10, fiveHour: nil, sevenDay: nil)],
    resets: [5], from: 0, to: day))
out["empty"] = pic(P.picture(points: [], resets: [], from: 0, to: day))
out["nil_from"] = pic(P.picture(
    points: [P.Point(ts: 10, fiveHour: 1, sevenDay: nil)],
    resets: [], from: nil, to: day))
out["nil_to"] = pic(P.picture(
    points: [P.Point(ts: 10, fiveHour: 1, sevenDay: nil)],
    resets: [], from: 0, to: nil))
out["backwards"] = pic(P.picture(
    points: [P.Point(ts: 10, fiveHour: 1, sevenDay: nil)],
    resets: [], from: day, to: 0))
out["last_slice_has_to"] = pic(P.picture(
    points: [P.Point(ts: day, fiveHour: 33, sevenDay: nil)],
    resets: [], from: 0, to: day))
out["month"] = pic(P.picture(
    points: [P.Point(ts: 100, fiveHour: 10, sevenDay: nil)],
    resets: [150, 40 * 3600], from: 0, to: 30 * day))
out["height"] = [P.height(150) ?? -1, P.height(50) ?? -1, P.height(-4) ?? -1,
                 P.height(nil) == nil ? 1 : 0]
out["counts"] = [P.columnCount(from: 0, to: day), P.columnCount(from: 0, to: 7 * day),
                 P.columnCount(from: 0, to: 30 * day),
                 P.columnCount(from: 0, to: 90 * day)]
out["percent"] = [P.percent(nil), P.percent(97.4), P.percent(0)]
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def pressure(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("limit-pressure")
    source = tmp / "main.swift"
    source.write_text(PANEL_FILE.read_text() + "\n" + HARNESS)
    executable = tmp / "LimitPressureProbe"
    built = subprocess.run([swiftc, str(source), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True,
                         timeout=30)
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


def test_a_week_is_84_columns_with_peaks_a_reset_and_a_seven_day_level(pressure):
    week = pressure["week"]
    assert week["count"] == 84
    first = week["columns"][0]
    # The max of 10 / 80 / 20, not the mean.
    assert first["fiveHour"] == 80
    assert first["sevenDay"] == 6
    assert week["fiveHourPeak"] == 80
    assert week["sevenDayPeak"] == 9
    assert week["headline"] == "5h peak 80% · 7d peak 9%"
    assert "five-hour limit peaked at 80 percent" in week["spoken"]
    assert "seven-day at 9 percent" in week["spoken"]


def test_a_slice_with_no_reading_is_absent_never_zero(pressure):
    columns = pressure["week"]["columns"]
    empty = columns[5]
    assert "fiveHour" not in empty and "sevenDay" not in empty
    # A seven-day-only slice leaves its five-hour half absent.
    assert columns[3] == {"reset": False, "sevenDay": 9}
    for column in columns:
        assert column.get("fiveHour") != 0


def test_a_reset_marks_exactly_its_column(pressure):
    marked = [i for i, c in enumerate(pressure["week"]["columns"]) if c["reset"]]
    # 7200 * 10 + 1 falls in column 10; the instants outside the window are ignored.
    assert marked == [10]


def test_points_outside_the_window_are_ignored(pressure):
    week = pressure["week"]
    assert week["fiveHourPeak"] == 80          # 99 before `from`, 98 after `to`


def test_the_last_slice_includes_the_end(pressure):
    assert pressure["last_slice_has_to"]["columns"][-1]["fiveHour"] == 33


def test_only_seven_day_readings_leave_no_five_hour_half(pressure):
    seven = pressure["seven_only"]
    assert "fiveHourPeak" not in seven
    assert seven["sevenDayPeak"] == 41.4
    assert seven["headline"] == "7d peak 41%"
    assert "5h" not in seven["headline"]


def test_nothing_measured_or_no_window_draws_no_chart(pressure):
    for name in ("no_readings", "empty", "nil_from", "nil_to", "backwards"):
        assert pressure[name] is None, name


def test_a_month_draws_no_reset_marks_but_a_week_still_does(pressure):
    assert not any(c["reset"] for c in pressure["month"]["columns"])
    assert any(c["reset"] for c in pressure["week"]["columns"])


def test_the_spoken_sentence_has_a_verb_for_a_seven_day_only_week(pressure):
    assert pressure["seven_only"]["spoken"] == (
        "Claude's seven-day limit peaked at 41 percent")


def test_the_scale_is_absolute_and_the_counts_are_fixed(pressure):
    assert pressure["height"] == [1, 0.5, 0, 1]
    assert pressure["counts"] == [12, 84, 84, 84]
    assert pressure["percent"] == ["—", "97%", "0%"]
