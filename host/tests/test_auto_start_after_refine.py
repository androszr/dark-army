"""Start the work by itself once the plan lands — the two clients' half.

Source pins in `test_composer_save_and_refine.py`'s stated style: `ios/` has no
test target, and the panel's request body is built inside the private,
network-bound `DaemonClient.post`, so the shape is pinned here, where every
verification pass runs it.

The one name rule this file also exists to hold: **nothing on the wire, in the
store or on either client may be spelled `auto_start`**. `board_autostart`
already means "the queue drain may start the head of the line" and sits one
line away in the same snapshot; two things named the same is how this becomes
unreadable. The wire name is `start_when_planned`.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PHONE_MODELS = ROOT / "ios" / "BobPhone" / "Models.swift"
PHONE_CARD = ROOT / "ios" / "BobPhone" / "CardDetailView.swift"
PHONE_COMPOSER = ROOT / "ios" / "BobPhone" / "ComposerView.swift"
PANEL_MODELS = ROOT / "panel" / "Sources" / "BobPanel" / "BoardModels.swift"
PANEL_CARD = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardView.swift"
PANEL_SHEET = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"
PANEL_CLIENT = ROOT / "panel" / "Sources" / "BobPanel" / "BoardClient.swift"
PANEL_STATE = ROOT / "panel" / "Sources" / "BobPanel" / "BoardState.swift"

DAEMON = ROOT / "host" / "dark_army_daemon"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path} is missing")
    return path.read_text(encoding="utf-8")


# --- the name ----------------------------------------------------------------


@pytest.mark.parametrize("path", [
    PHONE_MODELS, PHONE_CARD, PHONE_COMPOSER,
    PANEL_MODELS, PANEL_CARD, PANEL_SHEET, PANEL_CLIENT, PANEL_STATE,
])
def test_no_client_spells_it_auto_start(path):
    assert "auto_start" not in _read(path)
    assert "autoStart" not in _read(path)


def test_the_daemon_names_the_column_start_when_planned_everywhere():
    """The private helpers `_schedule_auto_start` / `_auto_start_after_refine`
    are named for the *behaviour* and touch no snapshot key; nothing else in
    the daemon may carry the word."""
    for py in sorted(DAEMON.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            # Prose may name the trap it exists to warn about; code may not.
            if stripped.startswith(("#", "--", "\"\"\"", "*")):
                continue
            if "auto_start" not in line:
                continue
            assert "_schedule_auto_start" in line \
                or "_auto_start_after_refine" in line \
                or "_auto_start_tasks" in line, (py.name, line)


# --- both clients decode the flag and the marker ------------------------------


@pytest.mark.parametrize("path", [PHONE_MODELS, PANEL_MODELS])
def test_both_models_decode_the_flag_off_by_default(path):
    text = _read(path)
    assert 'case startWhenPlanned = "start_when_planned"' in text
    # `'1'` is on; `''` and an **absent** key are both off, and off is the
    # required direction — an older Mac must never make every card look armed.
    assert 'startWhenPlanned = c.value(.startWhenPlanned, "") == "1"' in text


def test_the_phone_alone_decodes_the_marker():
    """The phone can meet an older Mac; the panel ships with its daemon and
    decodes no version markers (20 Sep 2026)."""
    text = _read(PHONE_MODELS)
    assert ('case startWhenPlannedSupported = '
            '"start_when_planned_supported"') in text
    assert ("startWhenPlannedSupported = "
            "c.value(.startWhenPlannedSupported, false)") in text
    assert "startWhenPlannedSupported" not in _read(PANEL_MODELS)


# --- the tick is drawn where a Refine could still happen ----------------------


def test_the_panel_card_face_draws_the_tick_beside_refine():
    text = _read(PANEL_CARD)
    assert "if canRefine {" in text
    assert "startWhenPlannedTick" in text


def test_the_panel_sheet_draws_both_ticks():
    text = _read(PANEL_SHEET)
    # The saved card's immediate toggle.
    assert "if let live, canRefine(live) {" in text
    # The composer's draft tick.
    assert "state.draft.startWhenPlanned.toggle()" in text


def test_the_phone_gates_both_ticks_on_the_marker():
    assert "if canRefine && board.startWhenPlannedSupported {" \
        in _read(PHONE_CARD)
    assert "if board.startWhenPlannedSupported {" in _read(PHONE_COMPOSER)


# --- the writes --------------------------------------------------------------


def test_the_panel_toggle_sends_no_expected_revision():
    """A one-field toggle is not a form save; guarding it would refuse the
    press whenever anything unrelated on the card had moved. `boardUnqueue`'s
    shape."""
    text = _read(PANEL_CLIENT)
    start = text.index("func boardStartWhenPlanned(")
    body = text[start:text.index("\n    }", start)]
    assert '"action": "board_update"' in body
    assert '"start_when_planned": on ? "1" : ""' in body
    assert "expected_revision" not in body


def test_the_phone_toggle_sends_no_expected_revision():
    text = _read(PHONE_CARD)
    start = text.index("private func setStartWhenPlanned()")
    body = text[start:text.index("\n    }", start)]
    assert "PhoneActions.boardUpdate" in body
    assert '"start_when_planned"' in body
    assert "expected_revision" not in body


def test_the_panel_create_carries_the_draft_tick():
    client = _read(PANEL_CLIENT)
    assert "startWhenPlanned: Bool = false" in client
    assert '"start_when_planned": startWhenPlanned ? "1" : ""' in client
    assert "startWhenPlanned: draft.startWhenPlanned)" in _read(PANEL_SHEET)
    assert "var startWhenPlanned = false" in _read(PANEL_STATE)


def test_the_phones_banked_path_carries_nothing_about_starting():
    """`refine`'s stated rule and the same reason: a banked draft carries no
    intent about starting anything."""
    text = _read(PHONE_COMPOSER)
    assert 'fields["start_when_planned"] = "1"' in text
    start = text.index("private func bank() {")
    body = text[start:text.index("\n    }", start)]
    assert "startWhenPlanned" not in body
    assert "start_when_planned" not in body
