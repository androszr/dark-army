"""`preferences.json`: defaults filled in on read, and a write that changes
only the keys it names."""
import json
import pytest
from pathlib import Path
from dark_army_menubar.preferences import (
    load_preferences, save_preferences, DEFAULTS)


@pytest.fixture(name="prefs_file")
def _a_preferences_file_of_its_own(tmp_path):
    return tmp_path.joinpath("preferences.json")


def _on_disk(path):
    """The file as JSON, bypassing the reader and its defaults."""
    return json.loads(path.read_text(encoding="utf-8"))


def _read_back(path):
    return load_preferences(path)


def test_with_no_file_every_default_is_the_answer(prefs_file):
    assert load_preferences(prefs_file) == dict(DEFAULTS)


def test_load_merges_missing_keys_with_defaults(prefs_file):
    """A stored file need not carry every key, and one it does not know about is
    not thrown away — preferences are written by more than one version of the
    app."""
    prefs_file.write_text(json.dumps({"something_older": 7}))
    result = _read_back(prefs_file)
    assert result["something_older"] == 7
    for key, value in DEFAULTS.items():
        assert result[key] == value

def test_save_preserves_existing_keys(prefs_file):
    """Read-modify-write: saving one preference must not drop the others."""
    prefs_file.write_text(json.dumps({"notification_sound": True,
                                      "something_older": 7}))
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    result = _on_disk(prefs_file)
    assert result["notification_sound"] is False
    assert result["something_older"] == 7

def test_save_creates_file_if_missing(prefs_file):
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    result = _on_disk(prefs_file)
    assert result["notification_sound"] is False
    # And reading it back gives the stored value, not the default it overrode.
    assert load_preferences(prefs_file)["notification_sound"] is False


@pytest.mark.parametrize("stored", [0, 1, 7])
def test_a_number_stored_for_a_switch_comes_back_as_that_number(prefs_file, stored):
    """Older builds wrote on/off switches as 0 and 1. The reader hands the
    number back untouched, so callers test truthiness, never `is True`."""
    prefs_file.write_text(json.dumps({"notification_sound": stored}))
    read = load_preferences(prefs_file)["notification_sound"]
    assert type(read) is int and read == stored
    assert bool(read) is bool(stored)


def test_session_timeout_round_trips(tmp_path):
    """It used to live only in the simulator's config store, so with the sim
    disabled there was nothing to read the choice back from and it reverted to
    5 minutes on every launch."""
    path = tmp_path / "preferences.json"
    save_preferences(path, {"session_timeout": 1800})
    assert load_preferences(path)["session_timeout"] == 1800


def test_session_timeout_defaults_to_five_minutes(tmp_path):
    """No stored value must not become 0. It used to be absent from DEFAULTS
    entirely and `app.py` carried the 300 as a `prefs.get` fallback — the
    default of record living in the reader rather than in the defaults. Same
    number, one place, and the guard the old test wanted still holds."""
    loaded = load_preferences(tmp_path / "nope.json")
    assert loaded["session_timeout"] == 300
    assert DEFAULTS["session_timeout"] == 300


def test_dictation_shortcut_is_empty():
    assert DEFAULTS["dictation_shortcut"] == {}
    # Finishing means the tab goes; the report is read in the panel.
    assert DEFAULTS["board_close_terminal"] is True


def test_dictation_shortcut_survives_a_read_modify_write_alongside_an_unknown_key(
        prefs_file):
    """An older build that does not know `dictation_shortcut` must still write
    it back, and a key from a hypothetical newer build must survive too."""
    prefs_file.write_text(json.dumps({
        "dictation_shortcut": {"key_code": 2, "modifiers": 1048576, "label": "D"},
        "from_a_newer_build": True,
    }))
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    stored = json.loads(prefs_file.read_text())
    assert stored["dictation_shortcut"] == {
        "key_code": 2, "modifiers": 1048576, "label": "D"}
    assert stored["from_a_newer_build"] is True
    assert stored["notification_sound"] is False
    loaded = _read_back(prefs_file)
    assert loaded["dictation_shortcut"] == stored["dictation_shortcut"]
    assert loaded["from_a_newer_build"] is True


def test_removed_keys_on_disk_are_kept_and_ignored(prefs_file):
    """A settings file written by today's Dark Army keeps working after the upgrade:
    load merges the stored keys without raising, save of a surviving key
    keeps them all, and none of them is in DEFAULTS."""
    removed = {
        "terminal_title": False,
        "tldr_hint": False,
        "work_report": False,
        "permission_broker": False,
        "panel_docked": True,
        "dock_summon": "click",
        "dock_ribbon_style": "dots",
        "dock_locked": True,
        "board_wrap_up": True,
        "board_queue": True,
        "card_prepare": False,
        "board_filing": False,
        "statusline": False,
        "launch_at_login": False,
    }
    prefs_file.write_text(json.dumps(removed))
    loaded = _read_back(prefs_file)
    for key, value in removed.items():
        assert loaded[key] == value
        assert key not in DEFAULTS
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    stored = json.loads(prefs_file.read_text())
    for key, value in removed.items():
        assert stored[key] == value
    assert stored["notification_sound"] is False


def test_session_title_stays_in_defaults_so_a_stored_false_wins():
    """The ⋯ row is gone; the key is not. An upgrade must not start sending
    opening-prompt text off the machine behind somebody's back."""
    assert DEFAULTS["session_title"] is True


def test_a_stored_session_title_false_survives_load(prefs_file):
    prefs_file.write_text(json.dumps({"session_title": False}))
    assert load_preferences(prefs_file)["session_title"] is False


def test_board_close_terminal_defaults_on():
    """30 Aug 2026: finishing means the tab goes. Clearing alone left the tab
    open, and the program in it opened a fresh empty session that Dark Army listed as
    a sleeping ghost row."""
    assert DEFAULTS["board_close_terminal"] is True


def test_a_deliberate_close_terminal_off_is_never_overridden(prefs_file):
    """The whole flip discipline, as a test: only the toggle ever writes this
    key, so every stored value is a deliberate press and `load_preferences`
    must hand it back unchanged — no migration, no sentinel."""
    prefs_file.write_text(json.dumps({"board_close_terminal": False}))
    assert load_preferences(prefs_file)["board_close_terminal"] is False


def test_the_per_project_parallel_map_defaults_empty():
    """A project with no entry gets `board_parallel`, the shared default. An
    empty map is the state where every project does — never a missing key."""
    assert DEFAULTS["board_parallel_by_root"] == {}
    assert DEFAULTS["board_parallel"] == 1


def test_the_per_project_parallel_map_round_trips(prefs_file):
    """Read-modify-write, so setting one project's dial disturbs no sibling
    key — including the machine dial the map is an override of."""
    save_preferences(prefs_file,
                     {"board_parallel": 2, "notification_sound": False})
    save_preferences(updates={"board_parallel_by_root": {"/a/proj": 3}},
                     path=prefs_file)

    stored = json.loads(prefs_file.read_text())
    assert stored["board_parallel_by_root"] == {"/a/proj": 3}
    assert stored["board_parallel"] == 2
    assert stored["notification_sound"] is False
    assert load_preferences(prefs_file)["board_parallel_by_root"] == {
        "/a/proj": 3}


def test_a_file_holding_a_json_list_is_treated_as_empty(prefs_file):
    """Valid JSON that is not an object used to raise out of `update` and
    take the whole launch down with it."""
    prefs_file.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert load_preferences(path=prefs_file) == DEFAULTS


def test_save_leaves_no_temp_sibling_behind(prefs_file):
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    assert load_preferences(path=prefs_file)["notification_sound"] is False
    leftovers = [p for p in prefs_file.parent.iterdir()
                 if p.name != prefs_file.name]
    assert leftovers == []


def test_a_failed_save_keeps_the_old_file_and_cleans_up(prefs_file, monkeypatch):
    """Write-then-rename: with the rename taken away, the stored preferences
    survive untouched and no temp file accumulates."""
    import os as _os
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    monkeypatch.setattr(_os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError("gone")))
    with pytest.raises(OSError):
        save_preferences(path=prefs_file, updates={"notification_sound": True})
    assert load_preferences(path=prefs_file)["notification_sound"] is False
    leftovers = [p for p in prefs_file.parent.iterdir()
                 if p.name != prefs_file.name]
    assert leftovers == []


def test_typed_reply_defaults_off():
    """The MVP switch: a default install replies exactly as it did before the
    key existed, and an older build ignores the key in `preferences.json`."""
    assert DEFAULTS["typed_reply"] is False


def test_agent_model_keys_default_to_empty_tables():
    assert DEFAULTS["agent_models"] == {}
    assert DEFAULTS["agent_models_by_root"] == {}


def test_agent_model_tables_survive_a_save_of_an_unrelated_key(prefs_file):
    """Additive and downgrade-safe: the read-modify-write keeps both maps
    when some other preference is written."""
    prefs_file.write_text(json.dumps({
        "agent_models": {"claude": {"planner": "sonnet"}},
        "agent_models_by_root": {"/tmp/p": {"grok": {"main": "grok-4.5"}}},
    }))
    save_preferences(path=prefs_file, updates={"notification_sound": False})
    result = _on_disk(prefs_file)
    assert result["agent_models"] == {"claude": {"planner": "sonnet"}}
    assert result["agent_models_by_root"] == {"/tmp/p": {"grok": {"main": "grok-4.5"}}}
    loaded = _read_back(prefs_file)
    assert loaded["agent_models"] == {"claude": {"planner": "sonnet"}}
