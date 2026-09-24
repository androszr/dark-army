"""Fresh-install defaults: write the marker and enable launch at login, once.

The once is the part worth pinning down — the failure that matters is not a first
run that misses a box, it is an upgrade that re-ticks one the user cleared.
"""
import json

import pytest

from dark_army_menubar import first_run
from dark_army_menubar.preferences import load_preferences


@pytest.fixture
def prefs_file(tmp_path):
    return tmp_path / "preferences.json"


@pytest.fixture
def stubbed(monkeypatch):
    """Record the side effects instead of touching the user's machine."""
    calls = {"launchd": 0}
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: False)
    monkeypatch.setattr(
        first_run.launchd, "enable",
        lambda: calls.__setitem__("launchd", calls["launchd"] + 1),
    )
    return calls


def test_fresh_install_opts_into_everything(prefs_file, stubbed):
    assert first_run.apply_first_run_defaults(prefs_file) is True

    stored = json.loads(prefs_file.read_text())
    assert stored[first_run.FIRST_RUN_KEY] is True
    assert stubbed == {"launchd": 1}

    prefs = load_preferences(prefs_file)
    assert prefs["notification_sound"] is True


def test_existing_install_is_left_alone(prefs_file, stubbed):
    """A preferences file is proof of a prior run: an option turned off stays off."""
    prefs_file.write_text(json.dumps({"notification_sound": False}))

    assert first_run.apply_first_run_defaults(prefs_file) is False
    assert stubbed == {"launchd": 0}


def test_second_launch_does_not_reapply(prefs_file, stubbed):
    first_run.apply_first_run_defaults(prefs_file)
    assert first_run.apply_first_run_defaults(prefs_file) is False
    assert stubbed == {"launchd": 1}


def test_already_installed_pieces_are_not_reinstalled(prefs_file, stubbed, monkeypatch):
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: True)

    assert first_run.apply_first_run_defaults(prefs_file) is True
    assert stubbed == {"launchd": 0}


def test_a_failing_side_effect_does_not_block_the_others(prefs_file, stubbed, monkeypatch):
    def boom():
        raise OSError("no write access")

    monkeypatch.setattr(first_run.launchd, "enable", boom)

    assert first_run.apply_first_run_defaults(prefs_file) is True
    # And the marker is down, so the next launch is an ordinary one.
    assert first_run.is_first_run(prefs_file) is False


def test_first_run_writes_only_the_marker(prefs_file, stubbed):
    first_run.apply_first_run_defaults(prefs_file)
    stored = json.loads(prefs_file.read_text())
    assert stored == {first_run.FIRST_RUN_KEY: True}
