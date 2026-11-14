"""Compact cards on the board: default on, storage, the panel action, no daemon verb."""
import inspect
import json
from pathlib import Path

from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import PHONE_PREFERENCES
from dark_army_daemon.paths import PREFS_PATH, ensure_state_dir
from dark_army_menubar.app import BobCompanionApp
from dark_army_menubar.preferences import DEFAULTS, load_preferences

ROOT = Path(__file__).resolve().parents[2]


def _action_app():
    from dark_army_menubar import app as A
    instance = object.__new__(A.BobCompanionApp)
    instance._settings = {}
    instance._daemon = None
    instance._push_panel_context = lambda: None
    return instance


def _dispatch(instance, name, value=None):
    from dark_army_menubar import app as A
    original = A.callAfter
    A.callAfter = lambda fn, *a: fn(*a)
    try:
        instance._on_panel_action(name, value)
    finally:
        A.callAfter = original


class _RecordingPanel:
    available = True
    executable_path = None

    def __init__(self):
        self.contexts = []

    def set_context(self, **fields):
        self.contexts.append(fields)
        return True

    def alive(self):
        return True


def test_default_is_on_and_matches_the_load_literal():
    assert DEFAULTS["board_compact"] is True
    source = inspect.getsource(BobCompanionApp.__init__)
    assert 'prefs.get("board_compact", True)' in source


def test_set_board_compact_is_a_panel_action():
    assert "set_board_compact" in BobCompanionApp.PANEL_ACTIONS


def test_set_board_compact_writes_the_key_and_leaves_the_rest():
    ensure_state_dir()
    PREFS_PATH.write_text(json.dumps({
        "notification_sound": False,
        "board_parallel": 2,
    }), encoding="utf-8")
    instance = _action_app()

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError(f"daemon.{name} should not be called")

    instance._daemon = _Boom()
    _dispatch(instance, "set_board_compact", False)

    assert instance._settings["board_compact"] is False
    stored = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    assert stored["board_compact"] is False
    assert stored["notification_sound"] is False
    assert stored["board_parallel"] == 2


def test_absent_board_compact_loads_on():
    ensure_state_dir()
    PREFS_PATH.write_text(json.dumps({"notification_sound": False}),
                          encoding="utf-8")
    loaded = load_preferences()
    assert loaded["board_compact"] is True
    assert loaded["notification_sound"] is False


def test_the_key_is_not_a_daemon_or_phone_thing():
    assert "set_board_compact" not in ApiServer.LAN_ACTIONS
    assert "set_board_compact" not in ApiServer.REMOTE_ACTIONS
    assert "set_board_compact" not in ApiServer.BOARD_ACTIONS
    assert "board_compact" not in PHONE_PREFERENCES
    daemon_dir = ROOT / "host" / "dark_army_daemon"
    for path in daemon_dir.rglob("*.py"):
        assert "board_compact" not in path.read_text(encoding="utf-8"), path


def test_the_swift_side_decodes_and_offers_the_switch():
    sources = ROOT / "panel" / "Sources" / "BobPanel"
    settings = (sources / "Settings.swift").read_text(encoding="utf-8")
    assert 'raw["board_compact"] as? Bool' in settings
    menu = (sources / "SettingsMenuModel.swift").read_text(encoding="utf-8")
    assert menu.count('"set_board_compact"') == 2
    search = (sources / "SettingsSearch.swift").read_text(encoding="utf-8")
    assert '"toggle:set_board_compact"' in search
    # One choice for the whole board: no per-row switch and no habit key.
    for path in sources.glob("*.swift"):
        assert "board_row_density_flips" not in path.read_text(encoding="utf-8"), path
