# host/tests/test_phone_card_message.py
"""Pins for the Send-a-message box on both card screens.

`ios/` has no test target by explicit decision and nothing in this repo builds
the phone, so the Swift half here is a Python lint over the source —
`test_phone_card_write_feedback.py`'s pattern.

What is pinned is mostly *wording*. Two different acts share one corner of the
card screen — typing at the assistant already working the card, and starting a
helper to answer a question about it — and the only thing telling them apart is
the heading, the button label and the caption underneath. If a later edit
changes one of the three and not the others the box starts lying, so all three
are named here on both surfaces, together with the two decode gates
(`can_message` on the row, `card_message_writable` on the board) that make the
button *absent* against an older Mac rather than present and refused.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"

PHONE_MODELS = PHONE / "Models.swift"
PHONE_ACTIONS = PHONE / "Actions.swift"
PHONE_CARD = PHONE / "CardDetailView.swift"
PANEL_SHEET = PANEL / "BoardCardSheet.swift"
PANEL_MODELS = PANEL / "Models.swift"
PANEL_CLIENT = PANEL / "BoardClient.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- the phone ---------------------------------------------------------------

def test_the_phone_decodes_both_new_keys_with_false():
    src = _read(PHONE_MODELS)
    assert 'case canMessage = "can_message"' in src
    assert "canMessage = c.value(.canMessage, false)" in src
    assert 'case cardMessageWritable = "card_message_writable"' in src
    assert ("cardMessageWritable = c.value(.cardMessageWritable, false)"
            in src)


def test_the_phone_has_the_panels_own_row_lookup():
    """A fourth open-coded walk over the buckets is how two surfaces start
    disagreeing about which row a card is bound to."""
    assert "func row(session: String) -> (Agent, String)?" in _read(PHONE_MODELS)


def test_the_phone_names_the_verb():
    src = _read(PHONE_ACTIONS)
    assert 'static let boardMessage = "board_message"' in src


def test_the_phone_card_screen_posts_the_verb_through_the_scoped_seam():
    src = _read(PHONE_CARD)
    assert "PhoneActions.boardMessage" in src
    # Named one at a time rather than as an ordered literal: this pin is
    # about `message` sharing the screen's one press slot, and a new verb
    # joining that enum is not this test's business.
    for verb in ("start", "done", "refine", "delete", "move", "tool",
                 "model", "save", "approve", "message"):
        assert re.search(rf"\bcase\b[^\n]*\b{verb}\b", src), verb
    assert "messageOpen" in src
    assert "func sendMessage()" in src


def test_the_phone_press_is_gated_on_both_the_row_flag_and_the_marker():
    """Absent, never inert: the row says whether this Mac *could* type, and
    the board says whether it knows the verb at all."""
    src = _read(PHONE_CARD)
    assert "board.cardMessageWritable" in src
    assert "row.canMessage" in src


def test_the_phone_draws_the_same_three_strings():
    src = _read(PHONE_CARD)
    assert '"SEND A MESSAGE"' in src
    assert "Typed straight onto that terminal's input line" in src


def test_the_phone_does_not_treat_the_card_as_leaving():
    """`low_priority`'s own argument: the card is not going anywhere, so it
    must not join the settling set that hides a row on its way out."""
    src = _read(PHONE_CARD)
    assert "settlingCards" not in src.split("func sendMessage()")[1][:800]


# --- the Mac -----------------------------------------------------------------

def test_the_panel_decodes_the_row_flag_and_no_marker():
    """The panel ships with its daemon, so it decodes no version marker
    (20 Sep 2026); the row's own `can_message` is a live fact and stays."""
    src = _read(PANEL_MODELS)
    assert 'case canMessage = "can_message"' in src
    assert "canMessage = c.value(.canMessage, false)" in src
    assert "cardMessageWritable" not in src


def test_the_panel_client_carries_the_verb():
    src = _read(PANEL_CLIENT)
    assert '"action": "board_message"' in src
    assert "func boardMessage(cardId: String, text: String)" in src


def test_the_panel_sheet_forks_on_one_named_predicate():
    src = _read(PANEL_SHEET)
    assert "private var messagesTerminal: Bool" in src
    assert "row.canMessage" in src
    assert "client.boardMessage(cardId:" in src


@pytest.mark.parametrize("words", [
    "Send a message to the session working this card",
    "SEND A MESSAGE",
    "Typed straight onto that terminal's input line",
])
def test_the_panels_message_box_names_what_it_does(words):
    assert words in _read(PANEL_SHEET)


@pytest.mark.parametrize("words", [
    "Ask about this card",
    "Ask — starts a helper session",
    "Answered by a helper session, not by whoever is ",
])
def test_the_panels_ask_box_still_names_what_it_does(words):
    assert words in _read(PANEL_SHEET)


def test_the_thread_says_which_of_the_two_acts_a_message_was():
    src = _read(PANEL_SHEET)
    assert 'msg.via == "terminal"' in src
    assert "typed into the session's terminal" in src
