# host/tests/test_card_action_weight.py
"""One outlined verb per board card, chosen by its column — source pins.

`plans/2026-09-20-one-primary-button-per-card.md`. The rule itself is tabled
in `panel/Tests/BobPanelTests/CardActionWeightTests.swift`; what this file
pins is what those tests cannot see: that the rule is Foundation-only, that
its table answers as the plan states when *run* under `swiftc`, that every
static has a Swift test naming it, and that the panel's card face carries
exactly one place a red outline can be drawn — the `weighted` helper every
one of the four verbs goes through.

The phone half of the plan (its `verbs` block) is not pinned here yet: the
block it was written against has since become `CardSections.nextAction`, and
how the phone should follow is an open question for the person.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
RULE = PANEL / "CardActionWeight.swift"
CARD_VIEW = PANEL / "BoardCardView.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "CardActionWeightTests.swift"

ENUM = "enum CardActionWeight {"
HELPER = "private func weighted<Content: View>("

#: Every static the rule declares.
STATICS = (
    "enum Verb: String { case refine, start, startHere, done }",
    'static func primary(column: String, kind: String = "") -> Verb?',
    'static func isPrimary(_ verb: Verb, column: String, kind: String = "") -> Bool',
)

#: The table, as the plan states it: column → the verb outlined there.
#: A `column:kind` token is a kind-aware probe (`prep:scout`).
TABLE = (
    ("prep", "refine"),
    ("backlog", "start"),
    ("in_progress", "done"),
    ("done", "none"),
    ("", "none"),
    ("bogus", "none"),
    ("prep:scout", "start"),
    ("backlog:scout", "start"),
    ("in_progress:scout", "done"),
    ("done:scout", "none"),
)

COLUMNS = ("prep", "backlog", "in_progress", "done")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    assert start in text, f"missing block: {start!r}"
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                block = text[i : j + 1]
                assert len(block) > len(start), f"empty block: {start!r}"
                return block
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _squashed(text: str) -> str:
    return " ".join(_code(text).split())


# --- the rule is pure and tabled -------------------------------------------


def test_the_rule_is_pure_and_names_its_statics():
    """Foundation only and no `BoardColumn`: the phone compiles the same
    block with no such type in reach."""
    src = _read(RULE)
    block = _code(_block(src, ENUM))
    for needle in STATICS:
        assert needle in block, needle
    assert "import SwiftUI" not in _code(src)
    assert "BoardColumn" not in _code(src)
    assert "ProgressView" not in src


def test_the_table_by_running_the_rule(tmp_path):
    """The golden is produced by *running* the rule, never by reading it:
    `primary` over the four columns and two strangers, and `.startHere`
    never primary anywhere."""
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    block = _block(_read(RULE), ENUM)
    harness = (
        "import Foundation\n" + block + "\n"
        "for c in CommandLine.arguments.dropFirst() {\n"
        "    if c.hasPrefix(\"here:\") {\n"
        "        print(CardActionWeight.isPrimary(.startHere, column: String(c.dropFirst(5))))\n"
        "    } else if c.contains(\":\") {\n"
        "        let parts = c.split(separator: \":\", maxSplits: 1)\n"
        "        print(CardActionWeight.primary(column: String(parts[0]), kind: String(parts[1]))?.rawValue ?? \"none\")\n"
        "    } else {\n"
        "        print(CardActionWeight.primary(column: c)?.rawValue ?? \"none\")\n"
        "    }\n"
        "}\n")
    path = tmp_path / "WeightProbe.swift"
    executable = tmp_path / "WeightProbe"
    path.write_text(harness)
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable), *(c for c, _ in TABLE)],
                         capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split("\n")[:-1] == [v for _, v in TABLE]
    ran = subprocess.run([str(executable), *(f"here:{c}" for c in COLUMNS)],
                         capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split() == ["false"] * len(COLUMNS)


def test_the_swift_tests_table_it():
    tests = _read(SWIFT_TESTS)
    for column in COLUMNS:
        assert f'CardActionWeight.primary(column: "{column}")' in tests, column
    assert ".startHere" in tests
    assert "CardActionWeight.isPrimary(" in tests
    assert tests.count("func test") >= 6
    assert 'kind: "scout"' in tests


# --- the panel face has one outline site ------------------------------------


def test_the_panel_face_has_one_outline_site():
    """Every verb the card offers goes through `weighted`, which holds the
    one `AlarmOutline` on the face; a second site would be a second rule."""
    code = _code(_read(CARD_VIEW))
    assert code.count(".buttonStyle(AlarmOutline") == 1
    helper = _block(code, HELPER)
    assert ".buttonStyle(AlarmOutline" in helper
    assert ".buttonStyle(.plain)" in helper
    assert "CardActionWeight.isPrimary(verb, column: card.column, kind: card.kind)" in helper
    # Each branch carries its own tracking area; the call sites add none.
    assert helper.count(".clickable(enabled)") == 2
    for name, call in (
        ("startButton", "weighted(.start,"),
        ("startHereButton", "weighted(.startHere,"),
        ("refineButton", "weighted(.refine,"),
        ("doneButton", "weighted(.done,"),
    ):
        block = _block(code, f"private var {name}: some View {{")
        assert call in block, name
        assert "AlarmOutline" not in block, name
        assert ".clickable(" not in block, name
    for word in ('small("Back")', 'small("Reopen")', 'small("Details")'):
        assert word in code, word
    delete = _block(code, "private var deleteButton: some View {")
    assert ".buttonStyle(.plain)" in delete
    assert "Color.red" in delete
    # `small` disarms before it acts, so no arm-then-confirm verb is built on it.
    assert 'small("Done")' not in code
    assert 'small("START")' not in code
