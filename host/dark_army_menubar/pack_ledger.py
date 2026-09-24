# host/dark_army_menubar/pack_ledger.py
"""Which projects the user pressed Install agent pack for.

``~/.dark-army/agent-pack.json``, 0600, read-modify-write under a module
lock. Forward-compatible: unknown keys survive a write, absent keys take a
default on read. Stop syncing removes the entry and writes nothing into the
project.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from dark_army_daemon import enrollment, paths

logger = logging.getLogger("dark-army.menubar")

VERSION = 1
MAX_PACK_PROJECTS = 32

_LOCK = threading.Lock()

_DEFAULTS = {
    "root": "",
    "profile": "",
    "prefix": "",
    "project": "",
    "app": "",
    "gitnexus_repo": "",
    "settings_allow_owned": [],
    # The canonical ignore lines already offered to this project's
    # .gitignore (pack_gitignore.canonical). A row an older build wrote has
    # none and reads as never offered.
    "gitignore_offered": [],
    "installed_at": 0.0,
    "last_sync_at": 0.0,
    "last_result": "",
}


class PackLedgerError(Exception):
    """The record could not be updated. The message is safe to show."""


def _normalise(root: str) -> str:
    text = str(root or "").strip()
    if not text:
        return ""
    try:
        return enrollment.normalise(text)
    except (OSError, ValueError):
        return text.rstrip("/")


def _empty() -> dict:
    return {"version": VERSION, "projects": []}


def load() -> dict:
    """The on-disk record, unknown keys intact. Missing or unreadable → empty."""
    path = paths.AGENT_PACK_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    if not isinstance(data.get("projects"), list):
        data["projects"] = []
    data.setdefault("version", VERSION)
    return data


def _write(data: dict) -> None:
    paths.ensure_state_dir()
    paths.atomic_write_json(paths.AGENT_PACK_PATH, data, mode=0o600)


def _find(projects: list, root: str) -> Optional[dict]:
    for entry in projects:
        if isinstance(entry, dict) and str(entry.get("root") or "") == root:
            return entry
    return None


def entry(root: str) -> Optional[dict]:
    """The stored row for ``root``, or None."""
    want = _normalise(root)
    if not want:
        return None
    with _LOCK:
        found = _find(load().get("projects") or [], want)
    return dict(found) if found else None


def published() -> list[dict]:
    """The subset the panel draws: root, profile, last_sync_at, last_result."""
    with _LOCK:
        rows = list(load().get("projects") or [])
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        root = str(raw.get("root") or "")
        if not root:
            continue
        out.append({
            "root": root,
            "profile": str(raw.get("profile") or ""),
            "last_sync_at": float(raw.get("last_sync_at") or 0.0),
            "last_result": str(raw.get("last_result") or ""),
        })
    return out


def _apply(row: dict, *, profile=None, prefix=None, project=None,
           app=None, gitnexus_repo=None, settings_allow_owned=None,
           gitignore_offered=None, last_result=None, last_sync_at=None, installed_at=None,
           touch: bool = True) -> None:
    if profile is not None:
        row["profile"] = str(profile)
    if prefix is not None:
        row["prefix"] = str(prefix)
    if project is not None:
        row["project"] = str(project)
    if app is not None:
        row["app"] = str(app)
    if gitnexus_repo is not None:
        row["gitnexus_repo"] = str(gitnexus_repo)
    if settings_allow_owned is not None:
        row["settings_allow_owned"] = [str(item) for item in settings_allow_owned]
    if gitignore_offered is not None:
        row["gitignore_offered"] = [str(item) for item in gitignore_offered]
    if last_result is not None:
        row["last_result"] = str(last_result)
    if installed_at is not None:
        row["installed_at"] = float(installed_at)
    if last_sync_at is not None:
        row["last_sync_at"] = float(last_sync_at)
    elif touch:
        row["last_sync_at"] = time.time()


def _refuse_self(want: str) -> None:
    from dark_army_menubar.pack_install import _is_bobs_own
    if _is_bobs_own(want):
        raise PackLedgerError(
            "Dark Army does not install the pack into its own project"
        )


def reserve(
    root: str,
    *,
    profile: Optional[str] = None,
    prefix: Optional[str] = None,
    project: Optional[str] = None,
    app: Optional[str] = None,
    gitnexus_repo: Optional[str] = None,
) -> dict:
    """Insert a stub if this root is new, or return the existing row.

    Capacity and the self-refusal are checked here, before any project
    write. Does not stamp ``last_result`` or ``last_sync_at``.
    """
    want = _normalise(root)
    if not want:
        raise PackLedgerError("no project folder")
    _refuse_self(want)
    now = time.time()
    with _LOCK:
        data = load()
        projects = list(data.get("projects") or [])
        row = _find(projects, want)
        if row is None:
            if len(projects) >= MAX_PACK_PROJECTS:
                raise PackLedgerError(
                    f"already syncing {MAX_PACK_PROJECTS} projects — "
                    "stop one before adding another"
                )
            row = dict(_DEFAULTS)
            row["root"] = want
            row["installed_at"] = now
            projects.append(row)
        _apply(row, profile=profile, prefix=prefix, project=project,
               app=app, gitnexus_repo=gitnexus_repo, touch=False)
        data["projects"] = projects
        data["version"] = data.get("version") or VERSION
        _write(data)
        return dict(row)


def update(
    root: str,
    *,
    profile: Optional[str] = None,
    prefix: Optional[str] = None,
    project: Optional[str] = None,
    app: Optional[str] = None,
    gitnexus_repo: Optional[str] = None,
    settings_allow_owned: Optional[list] = None,
    gitignore_offered: Optional[list] = None,
    last_result: Optional[str] = None,
    last_sync_at: Optional[float] = None,
    installed_at: Optional[float] = None,
) -> Optional[dict]:
    """Update an existing row. None if Stop syncing already dropped it.

    Never inserts — a launch-time resync must not undo ``forget``.
    """
    want = _normalise(root)
    if not want:
        return None
    with _LOCK:
        data = load()
        projects = list(data.get("projects") or [])
        row = _find(projects, want)
        if row is None:
            return None
        _apply(row, profile=profile, prefix=prefix, project=project, app=app,
               gitnexus_repo=gitnexus_repo,
               settings_allow_owned=settings_allow_owned,
               gitignore_offered=gitignore_offered,
               last_result=last_result, last_sync_at=last_sync_at,
               installed_at=installed_at, touch=True)
        data["projects"] = projects
        _write(data)
        return dict(row)


def remember(
    root: str,
    *,
    profile: Optional[str] = None,
    prefix: Optional[str] = None,
    project: Optional[str] = None,
    app: Optional[str] = None,
    gitnexus_repo: Optional[str] = None,
    settings_allow_owned: Optional[list] = None,
    gitignore_offered: Optional[list] = None,
    last_result: Optional[str] = None,
    last_sync_at: Optional[float] = None,
    installed_at: Optional[float] = None,
) -> dict:
    """Insert or update one project's row. Refuses Dark Army's own checkout."""
    reserved = reserve(
        root, profile=profile, prefix=prefix, project=project,
        app=app, gitnexus_repo=gitnexus_repo)
    updated = update(
        reserved["root"], profile=profile, prefix=prefix, project=project,
        app=app, gitnexus_repo=gitnexus_repo,
        settings_allow_owned=settings_allow_owned,
        gitignore_offered=gitignore_offered, last_result=last_result,
        last_sync_at=last_sync_at, installed_at=installed_at)
    if updated is None:
        raise PackLedgerError("that project is no longer syncing")
    return updated


def forget(root: str) -> bool:
    """Drop the row. Writes nothing into the project. True if it was there."""
    want = _normalise(root)
    if not want:
        return False
    with _LOCK:
        data = load()
        projects = list(data.get("projects") or [])
        kept = [
            row for row in projects
            if not (
                isinstance(row, dict) and str(row.get("root") or "") == want
            )
        ]
        if len(kept) == len(projects):
            return False
        data["projects"] = kept
        _write(data)
        return True
