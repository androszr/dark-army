# host/dark_army_menubar/first_run.py
"""Fresh-install defaults: write the marker and enable launch at login, exactly once.

Exactly once is the whole trick. The preferences file is the proof of a prior
install: if it exists, someone has run this app before, and an upgrade must
never quietly switch anything back on. That is also why the enabled values
are *written to disk* rather than flipped in ``preferences.DEFAULTS`` — a
default change would reach back and re-enable things for every existing user
who never touched that row.

The hooks, the statusline collector, the title pen and the VS Code extension
are not handled here: ``main()`` installs those on every launch already.
"""

import logging
from pathlib import Path
from typing import NamedTuple

from dark_army_daemon.paths import PREFS_PATH

from . import launch_report, launchd
from .preferences import save_preferences

logger = logging.getLogger("dark-army.menubar")

# Preference keys whose shipped default is off but which a fresh install should
# start with on. Empty now: nothing here is opted in beyond the marker and
# launchd below.
FIRST_RUN_PREFERENCES = {}

# Written alongside them so the marker survives even if a future version stops
# setting one of the keys above.
FIRST_RUN_KEY = "first_run_completed"


def is_first_run(path: Path = PREFS_PATH) -> bool:
    """True when this machine has no preferences file, i.e. a fresh install."""
    return not Path(path).exists()


class FirstRunReport(NamedTuple):
    """`apply_first_run()`'s answer: whether this was the first run, and what
    happened to the login item in `launch_report`'s vocabulary. The status
    never claims more than `launchd.enable()` proved."""

    applied: bool
    login_status: str
    login_detail: str


def apply_first_run(path: Path = PREFS_PATH) -> FirstRunReport:
    """Opt a fresh install into launch at login, and say what that did.

    The exactly-once guard is unchanged: a preferences file means a prior run
    and nothing is written. On that path the login item is reported as it
    stands — the plist's presence or absence — never touched. A launchd that
    cannot be written must not cost the user a menu bar.
    """
    if not is_first_run(path):
        try:
            present = launchd.is_enabled()
        except Exception:
            return FirstRunReport(False, launch_report.UNKNOWN,
                                  "login item state not readable")
        return FirstRunReport(
            False, launch_report.UNCHANGED,
            "login item already set up" if present else "login item is off")

    logger.info("First run — enabling launch at login by default")
    # Marker first: if a side effect below explodes, the next launch still comes
    # up as a normal one instead of retrying the whole thing forever.
    save_preferences(path=path, updates={**FIRST_RUN_PREFERENCES, FIRST_RUN_KEY: True})

    try:
        if launchd.is_enabled():
            status, detail = launch_report.login_outcome(None, already=True)
        else:
            status, detail = launch_report.login_outcome(launchd.enable())
    except Exception as exc:
        logger.exception("First run: could not enable launch at login")
        status, detail = launch_report.login_outcome(None, error=exc)

    return FirstRunReport(True, status, detail)


def apply_first_run_defaults(path: Path = PREFS_PATH) -> bool:
    """Opt a fresh install into launch at login. Returns True if it did anything.

    The Boolean face of `apply_first_run()`, kept for its existing callers.
    """
    return apply_first_run(path).applied
