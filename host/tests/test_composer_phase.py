# host/tests/test_composer_phase.py
"""The new-card form as two phases — source pins over the Swift.

`plans/2026-09-19-progressive-card-composer.md`. The phase rule itself is
tabled in `panel/Tests/BobPanelTests/ComposerPhaseTests.swift`; what this
file pins is what the Swift tests cannot see: that the phone's copy of
`ComposerPhase` is written exactly as the panel's, that both composers
draw only the gather half before `phaseTwoVisible`, that Prepare opens
the form on both the success and the refusal branch, that drafts carry
`expanded`, and that the daemon never hears of the switch.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
SHEET = PANEL / "BoardCardSheet.swift"
STATE = PANEL / "BoardState.swift"
DRAFTS = PANEL / "Drafts.swift"
PHASE = PANEL / "ComposerPhase.swift"
PHONE_COMPOSER = ROOT / "ios" / "BobPhone" / "ComposerView.swift"
PHONE_OUTBOX = ROOT / "ios" / "BobPhone" / "Outbox.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "ComposerPhaseTests.swift"
DAEMON = ROOT / "host" / "dark_army_daemon"

ENUM = "enum ComposerPhase {"

#: Every static the rule declares. A new one lands here *and* in the Swift
#: tests, or the test below names the one that is missing.
STATICS = (
    "static func expanded(flag: Bool, fields: [String]) -> Bool",
    "static func caption(for reveal: Reveal) -> String?",
    "static let fillMyselfLabel",
    "static let holdReason",
)


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


# --- the rule lives in one place, spelled the same on both sides -------------


def test_the_rule_is_pure_and_names_every_static():
    """Foundation only: the phone compiles the same block with no SwiftUI
    types in reach."""
    src = _read(PHASE)
    block = _code(_block(src, ENUM))
    for needle in STATICS:
        assert needle in block, needle
    assert "import SwiftUI" not in _code(src)
    assert "BoardDraft" not in _code(src)
    assert "BoardColumn" not in _code(src)


def test_the_phone_copy_is_written_exactly_as_the_panels():
    panel = _squashed(_block(_read(PHASE), ENUM))
    phone = _squashed(_block(_read(PHONE_COMPOSER), ENUM))
    assert panel == phone


def test_every_static_has_a_swift_test_naming_it():
    tests = _read(SWIFT_TESTS)
    for needle in STATICS:
        name = needle.split("static ")[1].split(" ")[1].split("(")[0]
        assert f"ComposerPhase.{name}" in tests, name
    assert tests.count("func test") >= 12


# --- the Mac form -------------------------------------------------------------


def test_the_mac_form_draws_only_phase_one_before_the_gate():
    fields = _code(_block(_read(SHEET), "private var fields: some View"))
    assert fields.count("if phaseTwoVisible {") == 1
    gate = fields.index("if phaseTwoVisible {")
    before = fields[:gate]
    after = fields[gate:]
    for needle in (
        'field("Your idea"',
        "prepareRow",
        "ComposerPhase.fillMyselfLabel",
        "composerAttachments",
        'field("Assistant"',
    ):
        assert needle in before, needle
        assert before.index(needle) < gate
    # Under the gate the body forks once: the composer's second phase and
    # the saved card's grouped editor. The composer's order is pinned on
    # its own var, since the field views are shared between the two.
    assert "composerPhaseTwo" in after and "savedCardEditor" in after
    phase_two = _code(_block(_read(SHEET), "private var composerPhaseTwo: some View"))
    last = -1
    for needle in (
        "projectField",
        "titleField",
        "summaryField",
        "modelField",
        "priorityField",
        "instructionsField",
        'field("Who benefits"',
        "composerSpecialists",
    ):
        assert needle in phase_two, needle
        assert phase_two.index(needle) > last, needle
        last = phase_two.index(needle)


def test_prepare_opens_the_form_on_both_branches():
    mac = _code(_block(_read(SHEET), "private func prepare()"))
    assert mac.index("state.revealPhaseTwo()") < mac.index("if result.ok")
    phone = _code(_block(_read(PHONE_COMPOSER), "private func prepare() async"))
    assert phone.index("expanded = true") < phone.index("if result.ok")
    assert _read(SHEET).count("revealPhaseTwo()") == 2
    assert _read(PHONE_COMPOSER).count("expanded = true") == 2


def test_the_phone_form_draws_only_phase_one_before_the_gate():
    body = _code(_block(_read(PHONE_COMPOSER), "var body: some View"))
    assert body.count("if phaseTwoVisible {") == 1
    gate = body.index("if phaseTwoVisible {")
    before = body[:gate]
    after = body[gate:]
    for needle in (
        'field("your idea"',
        "prepareBlock",
        "photosRow",
        "PhoneProviderSwitch(",
    ):
        assert needle in before, needle
    for needle in (
        "projectPicker",
        'field("title"',
        'field("summary"',
        'field("notes"',
        '"expected specialists"',
        '"SAVE"',
    ):
        assert needle in after, needle
    notes = after.index('field("notes"')
    specialists = after.index('"expected specialists"')
    save = after.index('"SAVE"')
    assert notes < specialists < save


# --- drafts carry the flag; the daemon does not -------------------------------


def test_the_draft_rows_carry_expanded():
    drafts = _read(DRAFTS)
    assert '"expanded": expanded' in drafts
    assert 'dict["expanded"] as? Bool ?? false' in drafts
    outbox = _read(PHONE_OUTBOX)
    assert "case expanded" in outbox
    assert "c.value(.expanded, false)" in outbox


def test_the_daemon_never_mentions_the_switch():
    for name in ("board.py", "api_server.py"):
        text = _read(DAEMON / name)
        assert "expanded" not in text, name
        assert "ComposerPhase" not in text, name
