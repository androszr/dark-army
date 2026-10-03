"""Rebuild & restart: the stamp, the snapshot's defaults and the offer rule.

Three entry points start one rebuild (the Mac window's button, the phone's
Menu tile, an agent's own next-step button) and all of them funnel into the
menu-bar app's single in-flight flag (`BobCompanionApp._on_rebuild`). This
module is the daemon's half of the picture, pure and stdlib-only:

* **The stamp** (`REBUILD_STAMP_PATH`). A successful rebuild restarts the
  process within milliseconds, so the fresh daemon has no memory of it; the
  menu-bar app writes one small JSON file just before it restarts and the
  fresh daemon seeds `rebuild_snapshot()` from it. That is how a phone that
  pressed the button learns the rebuild landed. Forward-compatible: an
  unknown key survives a read, a missing or corrupt file reads `{}`.
* **The offer rule** (`offered`). Whether an agent's row carries the
  one-click button: the agent wrote the `dark-army-next: rebuild` marker with
  its report, it works in Dark Army's own checkout, the Mac can rebuild at all,
  and no successful rebuild has *started* since the marker (a rebuild that
  began before the agent finished may have missed its last edit).
* **The words** the verb answers in.

No path or key ever rides the published facts: `last_error` is a
home-redacted tail of the build's error (`redact_error`), never a path under
the home folder.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from . import paths

REBUILD_UNAVAILABLE_REFUSAL = (
    "This Dark Army is not running from a source checkout, so it has nothing to "
    "rebuild.")
REBUILD_UNREACHABLE_REFUSAL = (
    "Dark Army's menu-bar app is not listening, so it cannot be rebuilt from "
    "here.")
REBUILD_RESTARTING_REFUSAL = (
    "Dark Army is restarting right now; try again once it is back.")
REBUILD_MERGING_REFUSAL = (
    "A card is merging into main right now; rebuild once the merge is done.")
REBUILD_REPLAYED = "that press already ran, so it is not run again"
REBUILD_ALREADY = "already rebuilding"
REBUILD_STARTED = "asked the Mac to rebuild and restart"

#: Longest failure text the published facts carry.
ERROR_LIMIT = 300

def redact_error(text, home=None) -> str:
    """The tail of a build's error with the home folder written as `~`, so
    nothing on a `state` read names a path under it. Redacted before it is
    cut, so a cut never splits a path into an unredacted fragment."""
    value = str(text or "")
    root = str(home if home is not None else os.path.expanduser("~"))
    if root and root != "/":
        value = value.replace(root, "~")
    return value[-ERROR_LIMIT:]


def note_press(token: str) -> None:
    """Remember which press (the phone's one-time command token) started the
    rebuild now under way, in the stamp file the menu-bar app completes on
    success. After the restart the fresh daemon's own receipt ledger is empty,
    so this is what lets it recognise a replay of that same press and refuse
    only it — a different press, from the person or an agent's button, is
    never blocked. Unknown keys survive, so the app's later `write_stamp`
    keeps it."""
    if not token:
        return
    stamp = read_stamp()
    stamp["token"] = str(token)
    path = paths.REBUILD_STAMP_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    paths.atomic_write_json(path, stamp, mode=0o600)


def replayed(token: str) -> bool:
    """Whether `token` is the press the stamp says already started a rebuild."""
    return bool(token) and read_stamp().get("token") == str(token)


def snapshot_defaults() -> dict:
    """The eight keys `rebuild_snapshot()` publishes, every one defaulted."""
    return {
        "available": False,
        "label": "",
        "rebuilding": False,
        "restarting": False,
        "started_at": None,
        "last_outcome": "",
        "last_finished_at": None,
        "last_error": "",
    }


def read_stamp() -> dict:
    """The stamp as written, or `{}` when it is missing, unreadable or not a
    JSON object."""
    try:
        with open(paths.REBUILD_STAMP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_stamp(started_at: float, finished_at: float, ok: bool) -> None:
    """Record a finished rebuild. Atomic: a temp file then `os.replace`, so a
    restart racing the write never leaves half a file. Keeps unknown keys a
    newer build wrote."""
    stamp = read_stamp()
    stamp.update({"started_at": float(started_at),
                  "finished_at": float(finished_at),
                  "ok": bool(ok)})
    path = paths.REBUILD_STAMP_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    paths.atomic_write_json(path, stamp, mode=0o600)


_cache: dict = {"key": None, "stamp": {}}


def cached_stamp() -> dict:
    """`read_stamp()` memoised on the file's `(mtime_ns, size)`, for the
    snapshot executor that asks once per agent per pass."""
    try:
        st = os.stat(paths.REBUILD_STAMP_PATH)
        key: Optional[tuple] = (str(paths.REBUILD_STAMP_PATH),
                                st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key != _cache["key"] or key is None:
        _cache["stamp"] = read_stamp() if key is not None else {}
        _cache["key"] = key
    return _cache["stamp"]


def seed_facts(stamp: dict) -> dict:
    """The published facts a fresh daemon starts with: the defaults, plus what
    the last successful rebuild's stamp says about it."""
    facts = snapshot_defaults()
    if stamp.get("ok"):
        facts["last_outcome"] = "ok"
        try:
            facts["last_finished_at"] = float(stamp.get("finished_at"))
        except (TypeError, ValueError):
            pass
    return facts


def offered(marker_at, stamp: dict, own_root: str, row_root: str,
            facts: dict) -> bool:
    """Whether an agent's row carries the Rebuild button.

    `marker_at` is the transcript clock of the report's marker (0.0 for none);
    `row_root` is the enrolled root the agent works in, or "" for an agent
    in a card folder (the daemon's pass blanks it: a card folder is never
    enrolled itself, so `root_enrolled` would name the main checkout). The
    equality with `own_root` is strict."""
    try:
        marker = float(marker_at or 0.0)
    except (TypeError, ValueError):
        return False
    if marker <= 0:
        return False
    if not (facts or {}).get("available"):
        return False
    if not own_root or row_root != own_root:
        return False
    if (stamp or {}).get("ok"):
        try:
            if float(stamp.get("started_at") or 0) >= marker:
                return False
        except (TypeError, ValueError):
            pass
    return True
