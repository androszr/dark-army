"""The Mac and the phone use the same words for the same action.

`plans/2026-09-25-usability-accessibility-pass.md`, Phase 4. `Verbs.swift` is
one word list — Stop, Delete, Hide, Dismiss, Close terminal, Low priority:
the plain label, the armed "really?" label and the spoken form — copied
byte-for-byte to both apps from `enum Verbs {` down, the way `CommRules` and
the other shared rules are. The table is *run* under `swiftc` for its labels,
and the four view files that draw these verbs read it and carry none of the
retired words. The `.help(` tooltips stay literal (`test_panel_tooltips.py`).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"
PANEL_VERBS = PANEL / "Verbs.swift"
PHONE_VERBS = PHONE / "Verbs.swift"
PROJECT = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
MARKER = "enum Verbs {"

#: The twelve labels, as the plan states them: each verb's plain label and
#: its armed one. Hide and Dismiss fire on the first press, so they arm to
#: their own word.
LABELS = (
    ("stop", "Stop", "Really stop?"),
    ("delete", "Delete", "Really delete?"),
    ("hide", "Hide", "Hide"),
    ("dismiss", "Dismiss", "Dismiss"),
    ("closeTerminal", "Close terminal", "Really close the terminal?"),
    ("lowPriority", "Low priority", "Really switch to low priority?"),
)

#: The view files that draw these verbs, and must read the table.
READERS = (
    PANEL / "RowBars.swift",
    PANEL / "ProcessTable.swift",
    PHONE / "AgentDetailView.swift",
    PHONE / "NeedsYouView.swift",
)

RETIRED = ('"Retire"', '"Acknowledge & close terminal"', '"Hide until this thread changes"')


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _shared(path: Path) -> str:
    lines = _read(path).splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


def test_the_two_copies_are_one_list():
    assert _shared(PANEL_VERBS) == _shared(PHONE_VERBS)


def test_a_doctored_copy_would_not_compare_equal():
    doctored = _shared(PHONE_VERBS).replace('"Really stop?"', '"Stop now?"', 1)
    assert doctored != _shared(PANEL_VERBS)


def test_the_list_is_foundation_only_and_built_into_the_phone():
    for path in (PANEL_VERBS, PHONE_VERBS):
        code = _code(_read(path))
        assert "import Foundation" in code
        assert "import SwiftUI" not in code and "import AppKit" not in code
        assert "import UIKit" not in code
    project = _read(PROJECT)
    assert project.count("/* Verbs.swift in Sources */") == 2
    assert "path = Verbs.swift;" in project


def test_the_table_prints_the_twelve_labels(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    harness = (
        "import Foundation\n" + _shared(PANEL_VERBS) + "\n"
        "for verb in Verbs.Verb.allCases {\n"
        "    print(\"\\(verb.rawValue)|\\(verb.label)|\\(verb.armedLabel)|\\(verb.arms)|"
        "\\(verb.spoken(armed: false))|\\(verb.spoken(armed: true))|\\(verb.noUndo ?? \"-\")\")\n"
        "}\n")
    path = tmp_path / "VerbsProbe.swift"
    executable = tmp_path / "VerbsProbe"
    path.write_text(harness)
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    rows = [line.split("|") for line in ran.stdout.splitlines()]
    assert [(r[0], r[1], r[2]) for r in rows] == list(LABELS)
    by_name = {r[0]: r for r in rows}
    # Only the armed verbs arm, and each speaks its confirmation.
    for name, _, _ in LABELS:
        arms = by_name[name][3] == "true"
        assert arms == (name not in ("hide", "dismiss")), name
        assert (by_name[name][4] != by_name[name][5]) == arms, name
    assert by_name["closeTerminal"][6] == "Closes the tab and ends this agent. There is no undo."
    assert all(by_name[n][6] == "-" for n in ("stop", "delete", "hide", "dismiss", "lowPriority"))


@pytest.mark.parametrize("path", READERS, ids=lambda p: p.name)
def test_the_views_read_the_list_and_carry_no_retired_word(path):
    text = _read(path)
    assert "Verbs." in _code(text), path.name
    for word in RETIRED:
        assert word not in text, (path.name, word)


def test_each_surface_draws_the_shared_words():
    bars = _code(_read(PANEL / "RowBars.swift"))
    for needle in ("static let button = Verbs.closeTerminal.label",
                   "static let armedButton = Verbs.closeTerminal.armedLabel",
                   "static let button = Verbs.lowPriority.label",
                   "static let armedButton = Verbs.lowPriority.armedLabel",
                   "Verbs.stop.armedLabel : Verbs.stop.label",
                   "Verbs.delete.armedLabel : Verbs.delete.label"):
        assert needle in bars, needle
    detail = _code(_read(PHONE / "AgentDetailView.swift"))
    for needle in ("Verbs.stop.armedLabel : Verbs.stop.label",
                   "Verbs.delete.armedLabel : Verbs.delete.label",
                   "Verbs.lowPriority.armedLabel", "Verbs.closeTerminal.armedLabel",
                   "Verbs.hide.label"):
        assert needle in detail, needle
    needs = _code(_read(PHONE / "NeedsYouView.swift"))
    verbs = needs.split("enum InboxAgentVerb", 1)[1]
    for needle in ("case .close: return Verbs.closeTerminal.label",
                   "case .hide: return Verbs.hide.label",
                   "case .lowPriority: return Verbs.lowPriority.label",
                   "case .stop: return Verbs.stop.label"):
        assert needle in verbs, needle
    assert "Verbs.closeTerminal.noUndo" in needs
    triage = _code(_read(PANEL / "TriageKeys.swift"))
    for verb in ("dismiss", "stop", "delete", "closeTerminal"):
        assert f"Verbs.{verb}.label.lowercased()" in triage, verb


def test_the_away_line_says_it_in_plain_words():
    composer = _read(PHONE / "ComposerView.swift")
    assert "through the relay" not in composer
    assert 'Text("Writing from away — this can take up to two minutes.")' in composer
