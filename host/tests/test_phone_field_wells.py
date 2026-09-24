"""Every place the phone asks you to type wears the same well.

Three surfaces take typed text — the composer, the reply box under a waiting
agent, and pairing — and they had three different looks: two bare `TextField`s
with no chrome at all, and one private hairline modifier in `Pairing.swift`.
`FieldWell` in `ios/BobPhone/Theme.swift` is the one shared well; these pins are
what stop the three drifting apart again.

A **Python** test under `host/tests/`, in `test_phone_theme_drift.py`'s house
style, and for its reasons: `cd host && .venv/bin/pytest` is the suite that runs
on this machine and in every verification pass, `ios/` has no test target, and
the TestFlight workflow runs none. A missing file is a failure, never a skip.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
PHONE_THEME = PHONE / "Theme.swift"
PANEL_THEME = ROOT / "panel" / "Sources" / "BobPanel" / "Theme.swift"
COMPOSER = PHONE / "ComposerView.swift"
ANSWER_BOX = PHONE / "AnswerBox.swift"
DETAIL = PHONE / "AgentDetailView.swift"
PAIRING = PHONE / "Pairing.swift"

# The drift test's own regex, deliberately duplicated rather than imported: it
# is the thing being guarded from the other side, and a rename there must not
# silently change what is asserted here.
_COLOUR = re.compile(
    r"static let (\w+) = Color\("
    r"red: (\d+) / 255, green: (\d+) / 255, blue: (\d+) / 255\)"
    r"(?:\.opacity\(([\d.]+)\))?"
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _swift_sources() -> list:
    files = sorted(PHONE.rglob("*.swift"))
    assert len(files) >= 10, f"parsed too few Swift sources under {PHONE}"
    return files


@pytest.mark.parametrize("path", [PHONE_THEME, COMPOSER, ANSWER_BOX, PAIRING])
def test_the_files_exist(path):
    assert path.is_file(), f"missing file: {path}"


def test_there_is_exactly_one_well_and_it_lives_in_the_theme():
    """Copy-paste drift is the failure mode this exists for.

    A second `FieldWell` somewhere else compiles fine and looks identical on
    the day it is written; it is a month later that the two stop matching.
    """
    hits = {p: _read(p).count("struct FieldWell: ViewModifier")
            for p in _swift_sources()}
    defining = {p: n for p, n in hits.items() if n}
    assert defining == {PHONE_THEME: 1}, (
        f"expected one FieldWell definition, in {PHONE_THEME.name}; found "
        f"{ {p.name: n for p, n in defining.items()} }")


def test_the_well_is_built_from_existing_tokens():
    text = _read(PHONE_THEME)
    body = text.split("struct FieldWell: ViewModifier", 1)[1]
    assert len(body) >= 400, "parsed too little of the FieldWell body"
    assert "focused ? Theme.phosphor : Theme.hair" in body, (
        "the focused edge is what makes the active field visible")
    assert ".background(Theme.well)" in body, "the well's fill is Theme.well"
    assert ".contentShape(Rectangle())" in body, (
        "without a content shape the padding ring is not tappable")
    assert "func fieldWell(focused:" in text, "no View.fieldWell convenience"


def test_the_well_added_no_colour_of_its_own():
    """The drift test's blind spot, closed from the other side.

    `test_phone_theme_drift.py` compares only the tokens in its `TOKENS` set,
    so a bespoke `static let wellEdge = Color(red: …)` on the phone would pass
    there and fork the palette. The rule is no new colour literal at all.
    """
    phone = {name for name, *_ in _COLOUR.findall(_read(PHONE_THEME))}
    panel = {name for name, *_ in _COLOUR.findall(_read(PANEL_THEME))}
    assert len(panel) >= 12, f"parsed too little from {PANEL_THEME}"
    assert phone == panel, (
        f"the phone's palette is no longer the panel's: only-phone "
        f"{sorted(phone - panel)}, only-panel {sorted(panel - phone)}")


@pytest.mark.parametrize(
    "path,minimum",
    [(COMPOSER, 1), (ANSWER_BOX, 1), (PAIRING, 3)])
def test_every_typing_surface_uses_the_shared_well(path, minimum):
    """The composer routes all three of its inputs through one `field(…)`
    helper, so its own count is the helper's single call; pairing draws its
    three fields separately and must show three. The agent detail draws no
    well of its own any more: its terminal's send box became SwiftTerm's
    own keyboard (`TerminalPane.swift`), and its reply box is `AnswerBox`'s."""
    text = _read(path)
    assert text.count(".fieldWell(") >= minimum, (
        f"{path.name} uses the shared well fewer than {minimum} times")


def test_the_private_pairing_style_is_gone():
    """`TerminalField` was the third style. Leaving it behind — even unused —
    is an invitation to reach for it again."""
    for path in _swift_sources():
        assert "TerminalField" not in _read(path), (
            f"TerminalField survives in {path.name}")


@pytest.mark.parametrize("path", [COMPOSER, ANSWER_BOX])
def test_the_long_fields_open_with_room(path):
    text = _read(path)
    assert text.count("lineLimit(5...)") == 1, (
        f"{path.name} must reserve five lines exactly once")


def test_the_composer_marks_only_its_long_fields_multiline():
    text = _read(COMPOSER)
    assert text.count("multiline: true") == 5, (
        "the idea, summary, notes, intended benefit and success criterion "
        "are the tall fields; title and who-benefits are not, and "
        "specialists are faces")
    assert "prompt: Text(hint)" in text, "no placeholder hint on the fields"


def test_the_reply_keeps_its_microphone_at_the_top_and_commits_with_send():
    text = _read(ANSWER_BOX)
    free = text.split("private var freeText", 1)[1]
    assert len(free) >= 400, "parsed too little of freeText"
    assert "HStack(alignment: .top" in free, (
        "a centred mic drifts down the screen as the well grows")
    assert "MicButton(id: micField" in free, "the reply lost its microphone"
    # The commit button says SEND — except while its own press is in play,
    # when it wears the queue's mark (`mark`: QUEUED / SENDING… / SENT;
    # test_phone_action_feedback.py pins that half). What this pin refuses
    # is a SAVE/PAIR-style relabel.
    assert text.count('Button(sending("send") ? mark : "SEND")') == 1, (
        "the reply's commit action is written like SAVE and PAIR")
    assert '"Send"' not in text, "the old mixed-case label survives"
