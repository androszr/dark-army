"""The default state directory must not be the live one during a test run.

Every path constant in ``paths`` is derived from ``_home()`` at import time, so
a module that binds ``BOARD_PATH`` or ``PREFS_PATH`` as a default argument —
and a test that constructs it without redirecting anything — still writes into
a temp folder rather than the running fleet's files. The panel carries the same
guard (`PanelStateDirectory`), added after a `swift test` run banked its own
half-typed cards into the user's real Drafts sheet.
"""

import tempfile
from pathlib import Path

from dark_army_daemon import paths


def test_the_state_dir_is_not_the_real_one_under_pytest():
    for real in (Path.home() / ".dark-army", Path.home() / ".bob-companion"):
        assert paths.STATE_DIR != real
    assert str(paths.STATE_DIR).startswith(str(Path(tempfile.gettempdir())))


def test_every_derived_path_follows_the_state_dir():
    homes = [str((Path.home() / name).resolve())
             for name in (".dark-army", ".bob-companion")]
    for value in (paths.SESSIONS_PATH, paths.PREFS_PATH, paths.BOARD_PATH,
                  paths.HISTORY_PATH, paths.DEVICES_PATH, paths.TITLES_PATH,
                  paths.ENROLLMENT_PATH, paths.ATTACHMENTS_DIR,
                  paths.HOOK_PID_CACHE_DIR,
                  paths.USAGE_HOLD_PATH, paths.PID_PATH):
        assert not any(str(value).startswith(h) for h in homes), \
            f"{value} points at live state"
