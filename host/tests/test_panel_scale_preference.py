"""The panel's size dial: default, storage, the panel action, no daemon verb."""
import inspect
import json
from pathlib import Path

from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import PHONE_PREFERENCES
from dark_army_daemon.paths import PREFS_PATH, ensure_state_dir
from dark_army_menubar.app import BobCompanionApp
from dark_army_menubar.preferences import DEFAULTS, load_preferences


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


def test_default_is_one_hundred_and_matches_the_load_literal():
    assert DEFAULTS["panel_scale"] == 100
    source = inspect.getsource(BobCompanionApp.__init__)
    assert 'prefs.get("panel_scale", 100)' in source


def test_set_panel_scale_is_a_panel_action():
    assert "set_panel_scale" in BobCompanionApp.PANEL_ACTIONS


def test_set_panel_scale_writes_the_key_and_leaves_the_rest():
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
    _dispatch(instance, "set_panel_scale", 150)

    assert instance._settings["panel_scale"] == 150
    stored = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    assert stored["panel_scale"] == 150
    assert stored["notification_sound"] is False
    assert stored["board_parallel"] == 2


def test_absent_panel_scale_loads_as_one_hundred():
    ensure_state_dir()
    PREFS_PATH.write_text(json.dumps({"notification_sound": False}),
                          encoding="utf-8")
    loaded = load_preferences()
    assert loaded["panel_scale"] == 100
    assert loaded["notification_sound"] is False


def test_pushed_context_carries_panel_scale():
    from dark_army_menubar.app import BobCompanionApp as App
    app = object.__new__(App)
    app._panel = _RecordingPanel()
    app._settings = {"panel_scale": 150}
    app._vscode_installing = False
    app._rebuilding = False
    app._repo_root = None
    app._grok_limits = {}
    app._panel_probes = {
        "build_info": None,
        "accessibility_trusted": False,
        "macwhisper_installed": False,
    }
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    assert settings["panel_scale"] == 150


def test_set_panel_scale_is_absent_from_every_daemon_action_table():
    assert "set_panel_scale" not in ApiServer.LAN_ACTIONS
    assert "set_panel_scale" not in ApiServer.REMOTE_ACTIONS
    assert "set_panel_scale" not in ApiServer.BOARD_ACTIONS
    assert "panel_scale" not in PHONE_PREFERENCES
    assert "set_panel_scale" not in PHONE_PREFERENCES
    daemon_dir = Path(__file__).resolve().parents[1] / "dark_army_daemon"
    for path in daemon_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "panel_scale" not in text, path
        assert "set_panel_scale" not in text, path


def test_the_settings_window_offers_the_dial():
    """The **Panel size** row is back (25 Sep 2026,
    `plans/2026-09-25-usability-accessibility-pass.md`): the four steps were
    reachable only by hand-editing `panel_scale`. One pick per
    `PanelScale.steps` rung, ticked through `resolved`, sent as the same
    panel action this file pins off every daemon table."""
    source = (Path(__file__).resolve().parents[2] / "panel" / "Sources"
              / "BobPanel" / "SettingsMenuModel.swift").read_text(encoding="utf-8")
    assert source.count("Panel size") == 1
    row = source[source.index('"Panel size"'):]
    row = row[:row.index("}))") + 3]
    assert "PanelScale.steps.map" in row
    assert 'action: "set_panel_scale"' in row
    assert "PanelScale.resolved(s.panelScale) == step.percent" in row
