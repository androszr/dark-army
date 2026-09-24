# host/tests/test_own_terminal_spawn.py
"""A card with no connected terminal can spawn one inside Dark Army.

START HERE is a per-press `own_terminal` on `board_dispatch`, not a new
verb and not a rename of the machine-wide preference. The button is
absent against an older daemon and while the preference is already on
(Start then already opens Dark Army's own terminal).
"""
from pathlib import Path

from dark_army_daemon.api_server import ApiServer

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_the_flag_is_envelope_not_a_new_action():
    assert "own_terminal" in ApiServer._BOARD_ENVELOPE
    assert "board_spawn_terminal" not in ApiServer.LAN_ACTIONS
    assert "board_spawn_terminal" not in ApiServer.REMOTE_ACTIONS


def test_the_panel_decodes_the_live_flag_and_no_marker():
    """The panel ships with its daemon, so it reads the live preference and
    decodes no version marker (20 Sep 2026); the phone keeps both."""
    src = _read(PANEL / "BoardModels.swift")
    assert 'case ownTerminalEnabled = "own_terminal_enabled"' in src
    assert "ownTerminalEnabled = c.value(.ownTerminalEnabled, false)" in src
    assert "ownTerminalSpawnSupported" not in src


def test_the_phone_decodes_both_flags_false_by_default():
    src = _read(PHONE / "Models.swift")
    assert 'case ownTerminalEnabled = "own_terminal_enabled"' in src
    assert 'case ownTerminalSpawnSupported = "own_terminal_spawn_supported"' in src
    assert "ownTerminalEnabled = c.value(.ownTerminalEnabled, false)" in src
    assert "ownTerminalSpawnSupported = c.value(.ownTerminalSpawnSupported, false)" in src


def test_start_here_is_absent_when_start_already_opens_bob():
    card = _read(PANEL / "BoardCardView.swift")
    assert "canSpawnHere" in card
    # The card reads the board-wide facts as a value (`BoardChrome`), not
    # off the client's whole board — same fact, same rule.
    assert "let chrome: BoardChrome" in card
    assert "canStart && !chrome.ownTerminalEnabled" in card
    assert "startHereButton" in card
    phone = _read(PHONE / "CardDetailView.swift")
    assert "canSpawnHere" in phone
    assert "board.ownTerminalSpawnSupported" in phone
    assert "!board.ownTerminalEnabled" in phone
    assert "START HERE" in phone


def test_the_press_sends_own_terminal_and_not_a_new_verb():
    panel = _read(PANEL / "BoardClient.swift")
    assert 'body["own_terminal"] = "true"' in panel
    phone = _read(PHONE / "CardDetailView.swift")
    assert 'fields["own_terminal"] = "true"' in phone
    assert "PhoneActions.boardDispatch" in phone
    assert "board_spawn_terminal" not in phone


def test_the_plan_gate_replay_keeps_the_here_press():
    drop = _read(PANEL / "BoardDrop.swift")
    assert "var ownTerminal: Bool = false" in drop
    view = _read(PANEL / "BoardView.swift")
    assert "ownTerminal: pending.ownTerminal" in view
    card = _read(PANEL / "BoardCardView.swift")
    assert "ownTerminal: ownTerminal" in card


def test_here_has_its_own_arm_slot():
    state = _read(PANEL / "BoardState.swift")
    assert "armedHere" in state
    assert "func armHere(" in state
    arm = _read(PHONE / "Arm.swift")
    assert "case startHere" in arm
    assert "startHere = id" in arm


def test_the_copy_says_dark_army_not_bob():
    card = _read(PANEL / "BoardCardView.swift")
    assert "Start this assistant on a terminal Dark Army itself opens" in card
    assert "Start on Dark Army's own terminal" in card
    phone = _read(PHONE / "CardDetailView.swift")
    assert "Start on Dark Army's own terminal" in phone
    assert "bob" not in card.split("startHereButton")[1][:800].lower()
    assert "bob" not in phone.split("startHereLabel")[1][:600].lower()
