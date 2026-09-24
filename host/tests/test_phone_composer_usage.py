"""Pins for the phone composer's usage figures under the assistant tiles.

plans/2026-09-20-phone-composer-usage-percent.md: a compact read-only row
under PhoneProviderSwitch, fed by client.usage through ComposerUsageRule.
No new poll. A stale or missing bar is no reading.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
RULES = PHONE / "ComposerUsageRule.swift"
COMPOSER = PHONE / "ComposerView.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _brace_chunk(text: str, start: int) -> str:
    brace = text.find("{", start)
    assert brace >= 0, f"no opening brace after {start}"
    depth = 0
    for i, ch in enumerate(text[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unclosed brace")


def _struct_chunk(text: str, name: str) -> str:
    needle = f"struct {name}"
    start = text.find(needle)
    assert start >= 0, f"no struct {name}"
    return _brace_chunk(text, start)


def test_the_rule_picks_the_named_windows(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    models = _read(MODELS)
    helper = _brace_chunk(models, models.index("extension KeyedDecodingContainer"))
    usage = _struct_chunk(models, "UsageBar")
    rules_src = _read(RULES)
    start = rules_src.find("enum ComposerUsageRule")
    assert start >= 0, "no enum ComposerUsageRule"
    rule = _brace_chunk(rules_src, start)
    program = "import Foundation\n" + helper + "\n" + usage + "\n" + rule + r'''
func decode(_ text: String) throws -> [UsageBar] {
    try JSONDecoder().decode([UsageBar].self, from: Data(text.utf8))
}
let bars = try decode("""
[
  {"kind":"weekly_scoped","provider":"claude","title":"Current week (Fable)","percent":61,"stale":false},
  {"kind":"session","group":"session","provider":"claude","label":"5h","title":"Current session","percent":18,"stale":false},
  {"kind":"weekly_all","group":"weekly","provider":"claude","label":"7d","title":"Current week (all models)","percent":42,"stale":false},
  {"kind":"codex_primary","group":"session","provider":"codex","label":"5h","title":"Codex, 5h","percent":73,"stale":false},
  {"kind":"codex_secondary","group":"weekly","provider":"codex","label":"7d","title":"Codex, 7d","percent":9,"stale":false},
  {"kind":"grok_weekly","group":"weekly","provider":"grok","label":"7d","title":"Grok, this week","percent":55,"stale":true}
]
""")
let rows = ComposerUsageRule.rows(tools: ["claude", "codex", "grok"], bars: bars)
assert(rows.count == 3)
assert(rows[0].provider == "claude")
assert(rows[0].figures.count == 2)
assert(rows[0].figures[0].window == "7d")
assert(rows[0].figures[0].percent == 42.0)
assert(rows[0].figures[0].stale == false)
assert(rows[0].figures[1].window == "5h")
assert(rows[0].figures[1].percent == 18.0)
assert(rows[1].provider == "codex")
assert(rows[1].figures.count == 1)
assert(rows[1].figures[0].window == "7d")
assert(rows[1].figures[0].percent == 9.0)
assert(rows[2].provider == "grok")
assert(rows[2].figures.count == 1)
assert(rows[2].figures[0].window == "7d")
assert(rows[2].figures[0].percent == nil)
assert(rows[2].figures[0].stale == true)

let primaryWeek = try decode("""
[
  {"kind":"codex_primary","group":"weekly","provider":"codex","label":"7d","percent":9,"stale":false},
  {"kind":"codex_secondary","group":"session","provider":"codex","label":"5h","percent":73,"stale":false}
]
""")
let fromPrimary = ComposerUsageRule.rows(tools: ["codex"], bars: primaryWeek)
assert(fromPrimary[0].figures[0].percent == 9.0)
assert(ComposerUsageRule.fiveHour(for: "codex", in: primaryWeek) == nil)

let noGroup = try decode("""
[{"kind":"codex_secondary","provider":"codex","label":"7d","title":"Codex, 7d","percent":11,"stale":false}]
""")
assert(noGroup[0].group == "")
assert(ComposerUsageRule.rows(tools: ["codex"], bars: noGroup)[0].figures[0].percent == 11.0)

let absent = try decode("""
[{"kind":"weekly_all","group":"weekly","provider":"claude","stale":false}]
""")
assert(ComposerUsageRule.rows(tools: ["claude"], bars: absent)[0].figures[0].percent == nil)
assert(ComposerUsageRule.rows(tools: ["claude"], bars: absent)[0].figures[0].stale == false)
'''
    source = tmp_path / "main.swift"
    source.write_text(program)
    built = subprocess.run(
        [swiftc, str(source), "-o", str(tmp_path / "pick")],
        capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stderr
    ran = subprocess.run(
        [str(tmp_path / "pick")], capture_output=True, text=True, timeout=10,
    )
    assert ran.returncode == 0, ran.stderr + ran.stdout


def test_the_rule_is_foundation_only():
    text = _read(RULES)
    imports = re.findall(r"^import (\S+)", text, re.M)
    assert imports == ["Foundation"]
    assert "PhoneClient" not in text
    assert "snapshot" not in text
    assert "SwiftUI" not in text


def test_the_row_sits_under_the_switch_once():
    text = _read(COMPOSER)
    assert text.count("ComposerUsageRow(") == 1
    call = text.index("ComposerUsageRow(")
    switch = text.index("PhoneProviderSwitch(tools: board.tools")
    phase = text.index("if phaseTwoVisible {")
    assert switch < call < phase
    assert "bars: client.usage" in text[call:call + 200]


def test_the_composer_polls_nothing_new():
    text = _read(COMPOSER)
    for needle in ("Timer", "Task.sleep", "pollUsage", "TimelineView"):
        assert needle not in text, needle


def test_the_row_speaks_as_one_element():
    text = _read(COMPOSER)
    start = text.find("struct ComposerUsageLine")
    assert start >= 0, "no struct ComposerUsageLine"
    chunk = _brace_chunk(text, start)
    assert ".accessibilityElement(children: .ignore)" in chunk
    assert ".accessibilityLabel(spoken)" in chunk
    assert "private var spoken: String" in chunk


def test_the_figure_is_the_usage_tabs_own():
    text = _read(COMPOSER)
    assert "UsageChipText.figure(" in text
    assert "Int(percent))%" not in text


def test_the_bar_decodes_group_tolerantly():
    chunk = _struct_chunk(_read(MODELS), "UsageBar")
    assert 'group = c.value(.group, "")' in chunk

