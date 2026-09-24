# host/dark_army_menubar/self_restart.py
"""Whether Dark Army may restart itself, and whether it still owes a notice.

``~/.dark-army/restart-watch.json``, 0600, shaped on ``pack_ledger.py``:
unknown keys survive a write, absent keys take a default on read, and nothing
here raises. The file exists because the two questions outlive the process
asking them — the copy that decides to go down is not the copy that has to say
so afterwards, and the hourly allowance has to be spent across restarts or it
is not an allowance at all.

Pure by design: it imports nothing from the user interface and posts nothing.
``app.py`` decides *when* to ask; this module answers, and the answers are
testable with neither a menu bar nor a banner on screen.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Iterable, List

from dark_army_daemon import paths

logger = logging.getLogger("dark-army.menubar")

SCHEMA = 1
#: Self-restarts allowed inside ``WINDOW_SECONDS``. Past this it stays down
#: with the offline mark and the three-row fallback menu, which is a state
#: somebody can act on; a process that respawns for ever is not.
MAX_AUTO_RESTARTS = 3
WINDOW_SECONDS = 3600.0
#: Stamps further ahead than this are dropped rather than counted. The clock is
#: wall-clock because it has to survive the process (``time.monotonic()`` means
#: nothing across two of them), so a forward jump could otherwise park three
#: stamps in the future and wedge the cap shut permanently.
FUTURE_SLACK_SECONDS = 60.0

#: The one marker value ``notice`` ever holds today.
NOTICE_RESTARTED = "restarted"

RESTARTED_IDENT = "bob.self-restart"
RESTARTED_TITLE = "Dark Army restarted itself"
RESTARTED_BODY = ("The background service stopped, so Dark Army started a "
                  "fresh copy. Your agents are being heard again.")

GAVE_UP_IDENT = "bob.self-restart-gave-up"
GAVE_UP_TITLE = "Dark Army has stopped restarting itself"
# Right-click, named rather than implied: `_StatusClickHandler` sends a left
# click to the panel and only a right or ctrl click to `_popup_menu`, which is
# the one route to these three rows. In the give-up state the panel is usually
# still available, so "click the icon" would open a working panel and the
# promised list would never appear.
GAVE_UP_BODY = ("It restarted itself three times in the last hour and kept "
                "stopping. Right-click the menu bar icon to open the log, "
                "restart or quit.")


def _empty() -> dict:
    return {"schema": SCHEMA, "auto_restarts": [], "notice": ""}


def load(path=None) -> dict:
    """The on-disk note, unknown keys intact. Missing or unreadable → empty."""
    target = paths.RESTART_WATCH_PATH if path is None else path
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    stamps = data.get("auto_restarts")
    if not isinstance(stamps, list):
        data["auto_restarts"] = []
    else:
        data["auto_restarts"] = [t for t in (_number(t) for t in stamps)
                                 if t is not None]
    if not isinstance(data.get("notice"), str):
        data["notice"] = ""
    data.setdefault("schema", SCHEMA)
    return data


def save(state: dict, path=None) -> None:
    target = paths.RESTART_WATCH_PATH if path is None else path
    paths.ensure_state_dir()
    paths.atomic_write_json(target, state, mode=0o600)


def _number(value):
    """``value`` as a finite float, or None if it is not a usable stamp.

    Per element, and coercion inside the guard rather than after it. An
    `isinstance` test is not enough on either end: Python's ints are unbounded,
    so a JSON integer of a few hundred digits passes it and then raises
    `OverflowError` on `float()`; and `json.loads` accepts `Infinity` and `NaN`,
    which are floats that no comparison here can order. Both are junk from a
    file anybody could corrupt, and this module's whole promise is that reading
    it raises nothing — `_health_check` calls it from a bare timer callback on
    the main thread with no `try` above it, so one exception here is the
    recovery never running.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def prune(stamps: Iterable, now: float) -> List[float]:
    """The stamps that still count at ``now``, oldest first. Pure."""
    kept = []
    for t in stamps or ():
        value = _number(t)
        if value is None:
            continue
        if now - WINDOW_SECONDS <= value <= now + FUTURE_SLACK_SECONDS:
            kept.append(value)
    return sorted(kept)


def may_auto_restart(stamps: Iterable, now: float) -> bool:
    """Whether the hourly allowance still has room. Pure."""
    return len(prune(stamps, now)) < MAX_AUTO_RESTARTS


def record_auto_restart(now: float, path=None) -> None:
    """Stamp this self-restart and leave the fresh copy a notice to say.

    Raises whatever ``save`` raises. That is the point: this count is the only
    bound on the flapping, so a caller that swallowed the failure would relaunch
    for ever on a state directory it cannot write. ``_health_check`` calls this
    *before* the teardown and treats a failure as a spent allowance.
    """
    state = load(path)
    state["auto_restarts"] = prune(state.get("auto_restarts"), now) + [float(now)]
    state["notice"] = NOTICE_RESTARTED
    save(state, path)


def take_pending_notice(path=None) -> str:
    """The notice this launch owes the user, cleared as it is read.

    Read-once by construction, so an ordinary launch says nothing and a notice
    can never be said twice — a clear that fails returns ``""`` rather than a
    notice that is still on disk. An empty ledger writes no file at all.
    """
    state = load(path)
    notice = str(state.get("notice") or "")
    if not notice:
        return ""
    state["notice"] = ""
    try:
        save(state, path)
    except OSError:
        # Say nothing rather than say it for ever: a notice returned after a
        # failed clear is still on disk, so "Dark Army restarted itself" would
        # reappear on every launch from here on.
        logger.warning("Could not clear the restart notice; not saying it",
                       exc_info=True)
        return ""
    return notice
