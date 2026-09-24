# host/tests/test_phone_widget_reload_budget.py
"""The home-screen tile stays inside WidgetKit's daily reload budget.

Found 21 Sep 2026: the tile sat on yesterday's numbers under an age line
reading "1 day" while the app checked in all day. WidgetKit honours 40–70
reloads a day and *defers* the rest until the window rolls; reloads asked
for in the foreground are exempt, and the ones that count — every
`BGAppRefreshTask` run (a fresh client, its throttle empty, so it reloaded
every run) and the unconditional flush at every `.background` — spent the
budget by the afternoon on a heavy day. Now `WidgetReloadBudget` keeps a
persisted ledger for the counted reloads (three per two hours, 36 a day),
a counted reload is asked for only when it would change the tile, the
last reload of any kind is recorded in `UserDefaults` so a fresh process
knows what the tile holds, and the tile's dim clock counts from its
note's own `generatedAt` rather than from the reload. The rule is
Foundation-only and runs here under `swiftc`; the wiring is pinned by
source greps. Contract: `docs/phone-contract.md`, *The home-screen tile
keeps moving with the app closed*.
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
BUDGET = PHONE / "WidgetReloadBudget.swift"
CLIENT = PHONE / "Client.swift"
APP = PHONE / "BobPhoneApp.swift"
WIDGET = ROOT / "ios" / "BobPhoneWidget" / "BobPhoneWidget.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

HARNESS = r'''
import Foundation

struct Case: Decodable {
    var stamps_ago: [Double]
    var changed: Bool
    var note_age: Double?
    var dim_after: Double
}

let data = FileHandle.standardInput.readDataToEndOfFile()
let cases = try! JSONDecoder().decode([Case].self, from: data)
let now = Date(timeIntervalSince1970: 1_700_000_000)
print("LIMITS \(WidgetReloadBudget.perWindow) \(Int(WidgetReloadBudget.window))")
for c in cases {
    let stamps = c.stamps_ago.map { now.timeIntervalSince1970 - $0 }
    let allows = WidgetReloadBudget.allows(stamps: stamps, now: now)
    let spent = WidgetReloadBudget.spend(stamps: stamps, now: now)
    let worth = WidgetReloadBudget.worthIt(changed: c.changed, noteAge: c.note_age,
                                           dimAfter: c.dim_after)
    print("ALLOWS \(allows) SPENT \(spent.count) WORTH \(worth)")
}
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


@pytest.fixture(scope="module")
def budget_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-widget-budget")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-widget-budget"
    built = subprocess.run(
        [swiftc, str(BUDGET), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, cases: list[dict]) -> tuple[tuple[int, int], list[tuple[bool, int, bool]]]:
    proc = subprocess.run([str(binary)], input=json.dumps(cases),
                          capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    lines = proc.stdout.splitlines()
    per, window = lines[0].split()[1:]
    rows = []
    for line in lines[1:]:
        parts = line.split()
        rows.append((parts[1] == "true", int(parts[3]), parts[5] == "true"))
    return (int(per), int(window)), rows


def _case(stamps_ago=(), changed=False, note_age=0.0, dim_after=1200.0):
    return {"stamps_ago": list(stamps_ago), "changed": changed,
            "note_age": note_age, "dim_after": dim_after}


def test_the_ledger_holds_the_day_under_forty(budget_bin):
    """Three counted reloads per two hours is 36 a day — under the 40
    WidgetKit promises a frequently viewed tile at the least."""
    (per, window), _ = _run(budget_bin, [])
    assert per * (86400 // window) < 40
    assert per == 3 and window == 7200


def test_a_spent_window_refuses_and_an_old_stamp_falls_out(budget_bin):
    _, rows = _run(budget_bin, [
        _case(),                                    # empty ledger
        _case(stamps_ago=[10, 20]),                 # two spent, room for one
        _case(stamps_ago=[10, 20, 30]),             # three spent: no
        _case(stamps_ago=[10, 20, 7300]),           # the oldest left the window
        _case(stamps_ago=[-30, 10, 20]),            # a stamp from the future still counts
    ])
    assert [r[0] for r in rows] == [True, True, False, True, False]
    # `spend` prunes to the window before adding now.
    assert [r[1] for r in rows] == [1, 3, 4, 3, 4]


def test_a_counted_reload_is_only_worth_a_changed_or_ageing_tile(budget_bin):
    _, rows = _run(budget_bin, [
        _case(changed=False, note_age=30),          # same numbers, fresh note: no
        _case(changed=True, note_age=30),           # numbers moved: yes
        _case(changed=False, note_age=1200),        # the tile is about to dim: yes
        _case(changed=False, note_age=1199),        # a second short of it: no
        {"stamps_ago": [], "changed": False, "note_age": None,
         "dim_after": 1200},                        # never reloaded: yes
    ])
    assert [r[2] for r in rows] == [False, True, True, False, True]


def test_the_client_counts_only_the_reloads_nobody_watches():
    """The foreground's reloads are exempt from the budget and unchanged;
    a background run's and a departure's go through the ledger and the
    worth-it rule, and every reload of any kind records what the tile now
    holds so a fresh process reads it back instead of reloading blind."""
    client = _read(CLIENT)
    publish = client.split("private func publishWidgetSummary()")[1].split(
        "\n    private func loadReloadSlots()")[0]
    assert "let counted = backgroundRun || departedAt != nil" in publish
    assert "WidgetReloadBudget.worthIt(" in publish
    assert "guard spendCountedReload() else { return }" in publish
    assert "recordReload(of: summary)" in publish
    # The foreground throttle stands as it was.
    assert "changed ? Self.reloadMinSeconds" in publish
    assert ": Self.reloadFreshnessSeconds" in publish
    flush = client.split("func flushWidgetReload()")[1].split("\n    }")[0]
    assert "WidgetReloadBudget.worthIt(" in flush
    assert "guard spendCountedReload() else { return }" in flush
    assert 'reloadTimelines(ofKind: "BobFleetTile")' in flush
    slots = client.split("private func loadReloadSlots()")[1].split(
        "\n    private func recordReload")[0]
    assert "WidgetReloadBudget.lastReloadAtKey" in slots
    assert "WidgetReloadBudget.lastReloadNoteKey" in slots
    record = client.split("private func recordReload(of summary: FleetSummary)")[1].split(
        "\n    }")[0]
    assert "WidgetReloadBudget.lastReloadAtKey" in record
    assert "WidgetReloadBudget.lastReloadNoteKey" in record
    spend = client.split("private func spendCountedReload() -> Bool")[1].split(
        "\n    }")[0]
    assert "WidgetReloadBudget.allows(stamps: stamps, now: now)" in spend
    assert "WidgetReloadBudget.spend(stamps: stamps, now: now)" in spend
    # Exactly the two reload sites the widget test counts.
    assert client.count('reloadTimelines(ofKind: "BobFleetTile")') == 2


def test_the_tile_dims_from_its_note_not_from_the_reload():
    """With the departure flush no longer unconditional, the dim entry's
    clock has to come from the note itself: `generatedAt + dimAfter`,
    never before the next second, and a note already past its horizon is
    dim from the first entry."""
    widget = _read(WIDGET)
    timeline = widget.split("func getTimeline(")[1].split("completion(Timeline(")[0]
    assert "Date(timeIntervalSince1970: $0.generatedAt)" in timeline
    assert "wrote.addingTimeInterval(dimAfter)" in timeline
    assert "max(now.addingTimeInterval(1)," in timeline
    assert "dimmed: now >= wrote.addingTimeInterval(dimAfter)" in timeline
    assert "policy: .never" in timeline + widget.split(timeline)[1][:200]


def test_the_rule_file_is_foundation_only_and_in_the_app_target():
    text = _read(BUDGET)
    imports = re.findall(r"^import (\w+)", text, re.M)
    assert imports == ["Foundation"]
    pbx = _read(PBXPROJ)
    assert "WidgetReloadBudget.swift in Sources" in pbx
    assert "path = BobPhone/WidgetReloadBudget.swift" in pbx
    app = _read(APP)
    background = app.split("case .background:")[1].split("@unknown default:")[0]
    assert "client.flushWidgetReload()" in background


# --- the phone can say what the tile is fed -----------------------------------

PROFILE = PHONE / "ProfileView.swift"
SUMMARY = ROOT / "ios" / "Shared" / "FleetSummary.swift"


def test_the_summary_write_reports_its_outcome_and_the_client_keeps_it():
    """A tile sat on a day-old note for a day while the app checked in all
    day, and nothing on the phone could say whether the note was being
    written at all. `store()` now answers in words (empty is success), the
    client keeps the last answer, and the three check-in paths call it the
    same way as before."""
    summary = SUMMARY.read_text()
    assert "@discardableResult" in summary
    assert "func store() -> String" in summary
    for words in ("no shared container", "encode failed", "write failed"):
        assert words in summary
    client = CLIENT.read_text()
    assert "@Published private(set) var widgetWriteReport" in client
    assert "let writeError = summary.store()" in client
    assert client.count("summary.store()") == 1


def test_the_profile_screen_draws_the_widget_diagnostics():
    profile = PROFILE.read_text()
    assert "enum WidgetDiagnostics" in profile
    assert "WidgetDiagnostics.rows(client: client)" in profile
    for label in ("note on disk", "last write", "last reload asked",
                  "counted reloads (2h)"):
        assert f'"{label}"' in profile
    # Reads the note and the ledger only: no network, no Keychain.
    diag = profile.split("enum WidgetDiagnostics")[1]
    assert "URLSession" not in diag and "Keychain" not in diag
    assert "FleetSummary.load()" in diag
    assert "WidgetReloadBudget.lastReloadAtKey" in diag
    assert "WidgetReloadBudget.recent(stamps, now: now)" in diag
