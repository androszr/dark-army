# host/tests/test_widget_circular.py
"""The Lock Screen circular fleet tile, pinned from the Mac's side.

`TickRing.swift` is Foundation-only so this file can compile and *run* it
under `xcrun swiftc`. The drawing (`CircularFleetView.swift`) is grepped
for the contract the arithmetic cannot see: no colour, no self-redaction,
no `+n` summary. `swiftc -parse` over the widget folder is the syntax
gate that folder has never had.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"
TICK = WIDGET_DIR / "TickRing.swift"
CIRCULAR = WIDGET_DIR / "CircularFleetView.swift"

DRIVER = r"""
import Foundation

func put(_ key: String, _ value: String) {
    print("\(key)=\(value)")
}

put("step0", String(TickRing.step(live: 0)))
put("step1", String(TickRing.step(live: 1)))
put("step12", String(TickRing.step(live: 12)))
put("step13", String(TickRing.step(live: 13)))
put("step20", String(TickRing.step(live: 20)))
put("step40", String(TickRing.step(live: 40)))

let t = TickRing.ticks(needsYou: 3, working: 5)
put("ticksCount", String(t.count))
put("ticksHeavy", t.map { $0.heavy ? "H" : "h" }.joined())
put("ticksFirst", String(t[0].degrees))

put("ticks200", String(TickRing.ticks(needsYou: 200, working: 0).count))

var widthsOk = true
var widthFail = -1
for live in 0...200 {
    if !(TickRing.heavyWidth(live: live) > TickRing.hairWidth(live: live)) {
        widthsOk = false
        widthFail = live
        break
    }
}
put("widthsOk", String(widthsOk))
put("widthFail", String(widthFail))
put("heavy200", String(TickRing.heavyWidth(live: 200)))
put("hair200", String(TickRing.hairWidth(live: 200)))

let idle = TickRing.centre(needsYou: 0, working: 0, stale: false)
put("idle", "\(idle.text)|\(idle.caption)|\(idle.waiting)")
let work = TickRing.centre(needsYou: 0, working: 5, stale: false)
put("work", "\(work.text)|\(work.caption)|\(work.waiting)")
let needs = TickRing.centre(needsYou: 3, working: 5, stale: false)
put("needs", "\(needs.text)|\(needs.caption)|\(needs.waiting)")
let staleNeeds = TickRing.centre(needsYou: 3, working: 5, stale: true)
put("staleCaption", staleNeeds.caption)
put("staleText", staleNeeds.text)
put("staleWaiting", String(staleNeeds.waiting))

put("spokenIdle", TickRing.spoken(needsYou: 0, working: 0, stale: false))
put("spokenNeeds", TickRing.spoken(needsYou: 3, working: 5, stale: false))
put("spokenWork", TickRing.spoken(needsYou: 0, working: 5, stale: false))
put("spokenStale", TickRing.spoken(needsYou: 3, working: 5, stale: true))
put("spokenIdleStale", TickRing.spoken(needsYou: 0, working: 0, stale: true))
"""


def _skip_without_swiftc() -> None:
    if shutil.which("xcrun") is None:
        pytest.skip("no Xcode toolchain on this machine")
    try:
        probe = subprocess.run(["xcrun", "--find", "swiftc"],
                               capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no Xcode toolchain on this machine")
    if probe.returncode != 0:
        pytest.skip("no Xcode toolchain on this machine")


def _kv(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def test_the_ring_arithmetic_runs(tmp_path: Path):
    """Compile TickRing.swift against a generated main.swift and assert
    the printed answers. Edge cases that make the state *wrong* rather
    than merely absent: live == 0 must not print a 0 in the centre;
    needsYou == 0, working > 0 must print the working count under WORK;
    a stale reading must change the caption and nothing else; heavyWidth
    must exceed hairWidth at 200 where both floors bind; ticks(200, 0)
    must be 200 long."""
    _skip_without_swiftc()
    assert TICK.is_file(), "TickRing.swift missing"
    main = tmp_path / "main.swift"
    main.write_text(DRIVER)
    probe = tmp_path / "probe"
    try:
        compiled = subprocess.run(
            ["xcrun", "swiftc", "-o", str(probe), str(TICK), str(main)],
            capture_output=True, timeout=120)
    except subprocess.SubprocessError:
        pytest.skip("the toolchain would not run")
    assert compiled.returncode == 0, compiled.stderr.decode(
        "utf-8", "replace")
    try:
        ran = subprocess.run(
            [str(probe)], capture_output=True, timeout=30, text=True)
    except subprocess.SubprocessError:
        pytest.skip("the probe would not run")
    assert ran.returncode == 0, ran.stderr
    kv = _kv(ran.stdout)

    for key in ("step0", "step1", "step12"):
        assert abs(float(kv[key]) - 30.0) < 1e-9, key
    assert abs(float(kv["step13"]) - 360.0 / 13) < 1e-9
    assert abs(float(kv["step20"]) - 360.0 / 20) < 1e-9
    assert abs(float(kv["step40"]) - 360.0 / 40) < 1e-9

    assert kv["ticksCount"] == "8"
    assert kv["ticksHeavy"] == "HHHhhhhh"
    assert abs(float(kv["ticksFirst"]) - 0.0) < 1e-9
    assert kv["ticks200"] == "200"

    assert kv["widthsOk"] == "true"
    assert abs(float(kv["heavy200"]) - 1.6) < 1e-9
    assert abs(float(kv["hair200"]) - 0.6) < 1e-9
    assert float(kv["heavy200"]) > float(kv["hair200"])

    assert kv["idle"] == "–|IDLE|false"
    assert kv["work"] == "5|WORK|false"
    assert kv["needs"] == "3|NEEDS|true"
    assert kv["staleCaption"] == "NEEDS?"
    assert kv["staleText"] == "3"
    assert kv["staleWaiting"] == "true"

    assert kv["spokenIdle"] == "no agents running"
    assert kv["spokenNeeds"] == "3 need you, 5 working"
    assert kv["spokenWork"] == "nobody needs you, 5 working"
    assert kv["spokenStale"] == (
        "3 need you, 5 working, last heard from the Mac a while ago")
    assert kv["spokenIdleStale"] == (
        "no agents running, last heard from the Mac a while ago")


def test_the_circular_view_relies_on_no_colour():
    text = CIRCULAR.read_text()
    assert "WidgetTheme.alarm" not in text
    assert "WidgetTheme.phosphor" not in text
    assert "WidgetTheme.amber" not in text


def test_the_circular_tile_never_redacts_itself():
    """`.privacySensitive()` on the home-screen tile hides the counts when
    that tile is shown on the Lock Screen. Applying it to *this* view would
    redact the tile on the Lock Screen — the only place it is ever drawn —
    leaving a permanently blanked circle. The two counts are therefore
    readable on a locked phone: two small integers, no project, session or
    card name beside them."""
    assert "privacySensitive" not in CIRCULAR.read_text()


def test_the_ring_is_never_summarised():
    for path in (CIRCULAR, TICK):
        text = path.read_text()
        assert "+\\(" not in text, path.name
        assert "prefix(" not in text, path.name


def test_tick_ring_is_pure():
    """Foundation only — the reason `test_the_ring_arithmetic_runs` can
    compile this file on the Mac host."""
    text = TICK.read_text()
    assert "import Foundation" in text
    assert "import SwiftUI" not in text
    assert "import UIKit" not in text
    assert "import WidgetKit" not in text
