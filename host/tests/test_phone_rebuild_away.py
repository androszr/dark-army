"""Rebuild & restart from away: the phone's one rule, run under `swiftc`.

`RebuildRules.swift` is compiled against a harness `RebuildSection` (the seven
fields the rules read) and a table of cases printed as JSON, the
`test_phone_batch_start.py` seam; the XCTest twin is
`ios/BobPhoneTests/RebuildRulesTests.swift`. The wiring (every surface reads
`RebuildRules.offered`, the sends stay `post`, the marker decodes tolerantly,
the panel decodes none) is pinned by source greps. Contract:
`docs/phone-contract.md`, *Rebuild & restart opens from the Menu's Rebuild
tile*; `docs/transport-contract.md`, *`rebuild_app` is chosen on both tuples*.
"""

from __future__ import annotations

import itertools
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULES = PHONE / "RebuildRules.swift"

HARNESS = r'''
import Foundation

struct RebuildSection {
    var available = false
    var label = ""
    var rebuilding = false
    var restarting = false
    var lastOutcome = ""
    var lastFinishedAt: Double? = nil
    var lastError = ""
}

func esc(_ s: String) -> String {
    let data = try! JSONSerialization.data(withJSONObject: [s])
    let text = String(data: data, encoding: .utf8)!
    return String(text.dropFirst().dropLast())
}

let utc = TimeZone(identifier: "UTC")!
var out: [String] = []
func put(_ key: String, _ value: String) { out.append("\"\(key)\": \(value)") }

for available in [false, true] {
    for away in [false, true] {
        for allowed in [false, true] {
            let s = RebuildSection(available: available)
            put("offered-\(available)-\(away)-\(allowed)",
                "\(RebuildRules.offered(section: s, away: away, awayAllowed: allowed))")
        }
    }
}
let idle = RebuildSection(available: true, label: "Rebuild & Reload")
let busy = RebuildSection(available: true, rebuilding: true)
put("press-away-allowed-idle",
    "\(RebuildRules.canPress(section: idle, away: true, awayAllowed: true))")
put("press-away-allowed-busy",
    "\(RebuildRules.canPress(section: busy, away: true, awayAllowed: true))")
put("press-away-not-allowed",
    "\(RebuildRules.canPress(section: idle, away: true, awayAllowed: false))")
put("press-away-default",
    "\(RebuildRules.canPress(section: idle, away: true))")
put("line-away-not-allowed", esc(RebuildRules.line(
    section: idle, connected: true, away: true, awayAllowed: false, timeZone: utc)))
put("line-away-allowed-idle", esc(RebuildRules.line(
    section: idle, connected: true, away: true, awayAllowed: true, timeZone: utc)))
put("line-away-allowed-down", esc(RebuildRules.line(
    section: busy, connected: false, away: true, awayAllowed: true,
    pressedAt: 100, timeZone: utc)))
let failed = RebuildSection(available: true, lastOutcome: "failed",
                            lastFinishedAt: 200, lastError: "boom")
put("line-away-allowed-failed", esc(RebuildRules.line(
    section: failed, connected: true, away: true, awayAllowed: true,
    pressedAt: 100, timeZone: utc)))
put("away-line", esc(RebuildRules.awayLine))
put("waiting-line", esc(RebuildRules.waitingLine))
put("armed-warning", esc(RebuildRules.awayArmedWarning))
print("{" + out.joined(separator: ", ") + "}")
'''


@pytest.fixture(scope="module")
def table(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-rebuild-away")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "rebuild-away"
    built = subprocess.run(
        [swiftc, str(RULES), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def test_offered_is_available_and_home_or_allowed(table):
    for available, away, allowed in itertools.product([False, True], repeat=3):
        key = f"offered-{str(available).lower()}-{str(away).lower()}-{str(allowed).lower()}"
        assert table[key] is (available and (not away or allowed)), key


def test_the_press_rules_away(table):
    assert table["press-away-allowed-idle"] is True
    assert table["press-away-allowed-busy"] is False
    assert table["press-away-not-allowed"] is False
    assert table["press-away-default"] is False


def test_the_lines_away(table):
    assert table["line-away-not-allowed"] == table["away-line"]
    assert "home Wi-Fi only" in table["away-line"]
    assert table["line-away-allowed-idle"] == "Rebuild & Reload"
    assert table["line-away-allowed-down"] == table["waiting-line"]
    assert table["line-away-allowed-failed"] == "Rebuild failed: boom"


def test_the_away_warning_says_the_link_drops(table):
    assert "You are away" in table["armed-warning"]
    assert "drops off the link" in table["armed-warning"]


def test_every_surface_reads_the_one_rule():
    for name in ("MenuView.swift", "RebuildView.swift", "AgentDetailView.swift"):
        assert "RebuildRules.offered(" in _read(PHONE / name), name
    for name in ("RebuildView.swift", "AgentDetailView.swift"):
        text = _read(PHONE / name)
        assert "awayAllowed: awayAllowed" in text, name
        assert "RebuildRules.awayArmedWarning" in text, name
        assert text.count("client.post(action: PhoneActions.rebuildApp)") == 1, name
        assert "enqueue(action: PhoneActions.rebuildApp" not in text, name
    assert "rebuildAwaySupported" in _read(PHONE / "MenuView.swift")


def test_the_marker_decodes_tolerantly_and_the_panel_knows_nothing():
    models = _read(PHONE / "Models.swift")
    assert 'case rebuildAwaySupported = "rebuild_away_supported"' in models
    assert "rebuildAwaySupported = c.value(.rebuildAwaySupported, false)" in models
    for path in (ROOT / "panel" / "Sources").rglob("*.swift"):
        text = path.read_text()
        assert "rebuildAwaySupported" not in text, path
        assert "rebuild_away_supported" not in text, path


def test_the_docs_name_the_marker():
    for name in ("transport-contract.md", "phone-contract.md"):
        assert "rebuild_away_supported" in _read(ROOT / "docs" / name), name
    assert "is chosen at home only" not in _read(ROOT / "docs" / "transport-contract.md")
