"""Pins over the phone's pipeline screen and the routes behind it.

`ios/` has no test target, so the phone half is a lint over Swift source —
`test_phone_writes.py`'s pattern. The Python half asserts the two tuples, the
`_lan_run` parse and the daemon's hand-over seam, so nothing here relies on
the Swift being compiled.
"""

import asyncio
import json
from pathlib import Path

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon.api_server import ApiServer

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"

VERBS = ("board_unqueue", "board_queue_move",
         "set_board_autostart", "set_board_parallel_root")

AUTOSTART_LABEL = "Start queued cards automatically"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- The two tuples, and the field rings that did not widen ---------------


def test_the_four_verbs_are_on_both_phone_tuples_exactly_once():
    for name in VERBS:
        assert ApiServer.LAN_ACTIONS.count(name) == 1, name
        assert ApiServer.REMOTE_ACTIONS.count(name) == 1, name
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_the_two_queue_verbs_route_through_the_board_branch():
    assert {"board_unqueue", "board_queue_move"} <= ApiServer._LAN_BOARD


def test_the_queue_fields_stay_outside_the_writable_ring():
    """Clear-never-set. A surface may empty a slot and may never claim one."""
    for field in ("queue_state", "queued_at", "queue_rank", "manual_steps"):
        assert field not in ApiServer._BOARD_FIELDS


def test_the_desk_only_dials_are_on_neither_phone_tuple():
    for name in ("set_board_parallel", "set_board_dispatch"):
        assert name not in ApiServer.LAN_ACTIONS
        assert name not in ApiServer.REMOTE_ACTIONS


# --- `_lan_run`'s parse ---------------------------------------------------


class _StubDaemon:
    def __init__(self, answer=(True, "asked the Mac to change that")):
        self.answer = answer
        self.asked = []
        self.unqueued = []
        self.moved = []

    def request_preference(self, key, value):
        self.asked.append((key, value))
        return self.answer

    async def unqueue_card(self, card_id):
        self.unqueued.append(card_id)
        return True, "taken out of the queue"

    async def move_queued_card(self, card_id, before_id):
        self.moved.append((card_id, before_id))
        return True, "moved"


def _server(daemon):
    srv = object.__new__(ApiServer)
    srv._daemon = daemon
    return srv


def _run(srv, action, payload):
    return asyncio.run(srv._lan_run(action, payload))


def test_board_unqueue_reaches_the_daemon():
    daemon = _StubDaemon()
    status, _, body = _run(_server(daemon), "board_unqueue", {"card_id": "c1"})
    assert status == 200
    assert daemon.unqueued == ["c1"]
    assert json.loads(body)["ok"] is True


def test_board_queue_move_carries_the_before_id():
    daemon = _StubDaemon()
    status, _, _ = _run(_server(daemon), "board_queue_move",
                        {"card_id": "c1", "before_id": "c2"})
    assert status == 200
    assert daemon.moved == [("c1", "c2")]


def test_an_absent_before_id_appends_rather_than_refusing():
    daemon = _StubDaemon()
    status, _, _ = _run(_server(daemon), "board_queue_move", {"card_id": "c1"})
    assert status == 200
    assert daemon.moved == [("c1", "")]


@pytest.mark.parametrize("payload", [{}, {"enabled": ""}, {"enabled": "yes"},
                                     {"enabled": "1"}])
def test_set_board_autostart_400s_on_anything_but_on_or_off(payload):
    daemon = _StubDaemon()
    status, _, _ = _run(_server(daemon), "set_board_autostart", payload)
    assert status == 400
    assert daemon.asked == []


@pytest.mark.parametrize("wire,want", [("on", True), ("off", False)])
def test_set_board_autostart_hands_a_bool_over(wire, want):
    daemon = _StubDaemon()
    status, _, body = _run(_server(daemon), "set_board_autostart",
                           {"enabled": wire})
    assert status == 200
    assert daemon.asked == [("board_autostart", want)]
    assert json.loads(body)["detail"]


@pytest.mark.parametrize("payload", [
    {"limit": "1"},                       # no root
    {"root": " ", "limit": "1"},          # blank root
    {"root": "/x"},                       # no limit
    {"root": "/x", "limit": "5"},         # out of range
    {"root": "/x", "limit": "-1"},
    {"root": "/x", "limit": "two"},
])
def test_set_board_parallel_root_400s_on_a_malformed_payload(payload):
    daemon = _StubDaemon()
    status, _, _ = _run(_server(daemon), "set_board_parallel_root", payload)
    assert status == 400
    assert daemon.asked == []


@pytest.mark.parametrize("wire", ["0", "1", "2", "3", "4"])
def test_set_board_parallel_root_hands_a_root_and_an_int_over(wire):
    daemon = _StubDaemon()
    status, _, _ = _run(_server(daemon), "set_board_parallel_root",
                        {"root": "/Users/x/proj", "limit": wire})
    assert status == 200
    assert daemon.asked == [
        ("board_parallel_root", {"root": "/Users/x/proj", "limit": int(wire)})]


def test_a_refused_preference_is_409_in_the_daemons_own_words():
    daemon = _StubDaemon(answer=(False, daemon_mod.PREFERENCE_UNREACHABLE_REFUSAL))
    status, _, body = _run(_server(daemon), "set_board_autostart",
                           {"enabled": "on"})
    assert status == 409
    assert json.loads(body)["detail"] == daemon_mod.PREFERENCE_UNREACHABLE_REFUSAL


# --- The Swift half -------------------------------------------------------


def test_actions_swift_names_all_four_verbs():
    actions = _read(PHONE / "Actions.swift")
    for name in VERBS:
        assert f'"{name}"' in actions, f"{name} missing from Actions.swift"


def test_the_pipeline_screen_exists():
    assert (PHONE / "PipelineView.swift").is_file()


def test_the_board_decodes_each_new_key_with_the_stated_default():
    models = _read(PHONE / "Models.swift")
    for line in (
        'case autostartEnabled = "autostart_enabled"',
        'case parallelLimit = "parallel_limit"',
        'case parallelOverrides = "parallel_overrides"',
        'case queueWritable = "queue_writable"',
        'case preferencesWritable = "preferences_writable"',
        # Absent must read as "the control is not there", never as one that
        # is drawn and silently does nothing.
        "queueWritable = c.value(.queueWritable, false)",
        "preferencesWritable = c.value(.preferencesWritable, false)",
        # An older Mac's queue did drain by itself; the caption must not
        # promise less than the Mac will do.
        "autostartEnabled = c.value(.autostartEnabled, true)",
    ):
        assert line in models, line


def test_the_card_decodes_the_new_fields_and_uses_maybe_for_the_two_clocks():
    models = _read(PHONE / "Models.swift")
    for line in (
        'case queuedAt = "queued_at"',
        'case queueRank = "queue_rank"',
        # The panel's degrade-safe direction: a dispatched card against a
        # Mac that sends neither flag reads as running, not as finished.
        "runActive = c.value(.runActive, true)",
        "workActive = c.value(.workActive, true)",
        # `null` and absent are both nil, so the coalesced sort key falls
        # through to 0 rather than to a made-up number.
        "queuedAt = c.maybe(.queuedAt)",
        "queueRank = c.maybe(.queueRank)",
    ):
        assert line in models, line


def _code(text: str) -> str:
    """The file with its comment lines dropped — a rule about what the code
    does must not be defeated by prose that explains it."""
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


def test_the_pipeline_screen_derives_nothing_about_what_is_running():
    """It states the daemon's numbers. A second opinion drawn beside the
    first is the two-surfaces-disagree failure the fleet's single-source
    rule exists to prevent."""
    view = _read(PHONE / "PipelineView.swift")
    code = _code(view)
    for banned in ("linkState ==", "runActive", "workActive",
                   "run_active", "work_active"):
        assert banned not in code, banned
    # The three rules are the model's copies of the panel's, called rather
    # than re-implemented here.
    for call in ("board.running(in: project)", "board.queued(in: project)",
                 "Board.resolvedLimit(", "Board.showsRunHeading("):
        assert call in view, call


def test_the_autostart_label_is_the_panels_string_byte_for_byte():
    view = _read(PHONE / "PipelineView.swift")
    panel = _read(PANEL / "SettingsMenuModel.swift")
    assert view.count(AUTOSTART_LABEL) == 1
    # The panel's own row still says it, untouched. (Its file also names the
    # string once in a doc comment, which is why this counts the row rather
    # than the file.)
    assert _code(panel).count(f'"{AUTOSTART_LABEL}"') == 1


def test_the_card_screen_no_longer_waits_on_cards():
    """The waiting-on feature is retired (2026-09-20): no phone surface
    reads, draws or writes `blocked_by`, on the same day the Mac lost the
    section and the daemon closed the field at the door."""
    for name in ("CardDetailView.swift", "Models.swift", "BoardView.swift",
                 "ComposerView.swift"):
        text = _read(PHONE / name)
        assert "blockedBy" not in text and "blocked_by" not in text, name
        assert "Blocker" not in text, name


def test_the_client_declares_the_hold_and_clears_it_from_a_snapshot():
    client = _read(PHONE / "Client.swift")
    assert client.count("var settlingPreferences") == 1
    assert client.count("static let preferenceUnsettledNotice") == 1
    # Cleared by a decoded board, never by the status code: the Mac stores
    # and republishes on its own thread well after the 200.
    assert "private func prunePreferenceSettling()" in client
    assert client.count("prunePreferenceSettling()") == 3


def test_the_pipeline_link_is_gated_on_what_the_mac_published():
    board = _read(PHONE / "BoardView.swift")
    assert "board.queueWritable || board.preferencesWritable" in board
    assert "PipelineView(client: client)" in board


def test_no_fifth_tab_was_added():
    app = _read(PHONE / "BobPhoneApp.swift")
    assert "PipelineView" not in app
    router = _read(PHONE / "Router.swift")
    assert "PipelineView" not in router
