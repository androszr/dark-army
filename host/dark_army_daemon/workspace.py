"""What project is this session *in*?

The panel's first grouping level is the project, and until now the answer was
``basename(cwd)`` — computed independently in three places (``protocol.py``,
``agents_poll.py``, ``grok_roster.py``) and wrong in the same way in all three.
A session started in ``dark-army/host`` called itself ``host``, one in the
scratchpad called itself ``scratchpad``, and neither grouped with the sibling
agents working on the very same repo.

The unit a person actually means by "project" is the **VS Code workspace**: the
window they have open. The extension already writes one lock file per window
carrying that window's folders (and, from 0.1.5, its name and workspace file),
so the mapping cwd → project is a lookup, not a guess. No subprocess, no `git`,
no network — this runs inside the agents snapshot every few seconds.

The ladder, in order, and why each rung exists:

0. **An enrolled project folder containing the cwd** (30 Aug 2026). Enrolment
   is the strongest statement available that "this folder is a project" — a
   person pointed at it and let Dark Army watch it, and every watched session is
   inside one by construction — so the project's *own folder name* wins over
   everything below, with no exceptions. That is the point: the tabs used to
   mix a typed display name (`Ledgerly` for `finance-demo`), a subfolder's own
   name (`host`, split off from the repo around it) and a plain basename, and
   one rule with exceptions would have left the mixture in place. A deliberately
   named `.code-workspace` therefore does *not* keep its chosen name. Nested
   enrolled roots resolve to the inner one, mirroring rung 1: enrolling
   `dark-army/host` separately is the ledger saying it is its own project.
0b. **A scratchpad path mangling an enrolled root**, rung 2's trick run against
   the ledger instead of against open windows, so scratch work lands under its
   project's folder name with no window open at all.
1. **A window whose folder contains the cwd.** The primary rule, and the one
   that fixes ``host/`` — a subdirectory of a workspace folder belongs to that
   workspace. Longest folder wins, so nested checkouts resolve to the inner one.
2. **A scratchpad path naming its origin.** Claude Code puts a session's
   scratchpad under ``/tmp/claude-<uid>/<mangled-project-path>/<uuid>/``, which
   is nowhere near the repo but *says* which repo it is for. Matched by
   re-mangling each known folder (``/`` → ``-``) and comparing, never by
   decoding the segment: ``-`` → ``/`` is ambiguous the moment a directory name
   contains a hyphen, and this repo is called ``dark-army``.
3. **Claude Code's own project root**, from the statusline payload
   (``workspace.project_dir`` / ``workspace.git_worktree``). Already collected
   and already shipped in every row's ``metrics`` — nobody read it. This is the
   "git root" rung, without the daemon ever running git.
4. **``basename(cwd)``** — what every row said before this module existed. A
   session outside every workspace, with no statusline, still gets a label.

Rungs 1–4 are untouched and still answer for every session outside every
enrolled project.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from . import enrollment, vscode_reveal
# Re-exported: `daemon.py` and the tests address them as `workspace.pid_alive`
# / `workspace._lock_pid`; they moved next to the lock reader so it can drop
# a dead window's lock before anything talks to it.
from .vscode_reveal import _lock_pid, pid_alive  # noqa: F401

logger = logging.getLogger(__name__)

# The lock files are cheap (a glob and a few small reads), but this is called
# once per row per snapshot. One reading per few seconds is plenty: a window's
# folders change when a window is opened, which is not a thing that happens
# between two ticks and matters.
CACHE_TTL_SECONDS = 5.0

_WORKSPACE_SUFFIX = ".code-workspace"


class Window(NamedTuple):
    """One VS Code window, reduced to the two things naming a project needs.

    `pid` is the extension host that wrote the lock (`0` where the lock did
    not say — an extension older than the field). Naming reads it never; the
    first-run checklist reads it to refuse a lock whose window is gone, since
    a crashed editor leaves its lock behind and a dead lock is no evidence
    that a folder is open.
    """

    name: str
    folders: tuple
    pid: int = 0


_lock = threading.Lock()
_cached: tuple = ((), 0.0)


def _window_name(lock: dict) -> str:
    """What to call the window this lock describes.

    The saved `.code-workspace` file wins over `workspaceName` because the file
    is the identity the user chose and typed; `vscode.workspace.name` is a
    display string that has carried suffixes in the past. `workspaceName` is
    still the only thing that can name an *untitled* workspace, which has no
    file to take a basename from. A lock written by an extension older than
    0.1.5 has neither key and falls through to its single folder — which is
    already the fix for the `host/` case, so an unreloaded window is not stranded.
    """
    fs_path = lock.get("workspaceFsPath")
    if isinstance(fs_path, str) and fs_path.endswith(_WORKSPACE_SUFFIX):
        return os.path.basename(fs_path)[: -len(_WORKSPACE_SUFFIX)]

    name = lock.get("workspaceName")
    if isinstance(name, str) and name.strip():
        return name.strip()

    folders = lock.get("workspaceFolders")
    if isinstance(folders, list) and len(folders) == 1 and isinstance(folders[0], str):
        return os.path.basename(folders[0].rstrip(os.sep))

    # A multi-folder window with no identity cannot be named, and inventing one
    # from its first folder would put two windows under one heading.
    return ""


def _folders(lock: dict) -> tuple:
    raw = lock.get("workspaceFolders")
    if not isinstance(raw, list):
        return ()
    return tuple(
        f.rstrip(os.sep) for f in raw if isinstance(f, str) and f.startswith(os.sep)
    )


def windows(force: bool = False) -> tuple:
    """Every open VS Code window as (name, folders), cached for CACHE_TTL_SECONDS.

    Reuses the extension-lock reader rather than globbing again: a lock the
    daemon would refuse to talk to is a lock we should not be naming windows
    from either.
    """
    global _cached
    # `at == 0.0` is "never read", so a machine with no VS Code open caches its
    # empty answer like any other rather than globbing once per row forever.
    now = time.monotonic()
    with _lock:
        cached, at = _cached
        if not force and at and (now - at) < CACHE_TTL_SECONDS:
            return cached

    found = []
    try:
        for lock_data in vscode_reveal._bob_ext_locks():
            folders = _folders(lock_data)
            name = _window_name(lock_data)
            if name and folders:
                found.append(Window(name, folders, _lock_pid(lock_data)))
    except Exception:
        logger.debug("could not read VS Code windows", exc_info=True)
        found = []

    result = tuple(found)
    with _lock:
        _cached = (result, now)
    return result


def invalidate() -> None:
    """Drop the cached window list. For tests, and for anything that knows a
    window just appeared."""
    global _cached
    with _lock:
        _cached = ((), 0.0)


def _contains(folder: str, cwd: str) -> bool:
    """Is `cwd` inside `folder`? Component-aware — `/a/bo` does not contain
    `/a/bob`, which a bare `startswith` would happily claim."""
    return cwd == folder or cwd.startswith(folder + os.sep)


def _by_containment(cwd: str, open_windows: Iterable) -> str:
    best = ""
    best_len = -1
    for window in open_windows:
        for folder in window.folders:
            if _contains(folder, cwd) and len(folder) > best_len:
                best, best_len = window.name, len(folder)
    return best


def _mangled_segments(cwd: str) -> list:
    """The `-`-prefixed path segments a scratchpad path carries its origin in.

    Shared by both scratchpad rungs — one scan, so the enrolment rung and the
    window rung can never disagree about what counts as a scratchpad segment.
    """
    return [s for s in cwd.split(os.sep) if s.startswith("-")]


def _by_scratchpad(cwd: str, open_windows: Iterable) -> str:
    """A scratchpad path carries its project's full path with `/` replaced by
    `-`. Compare against the mangled form of each known folder rather than
    decoding the segment (see the module docstring)."""
    segments = _mangled_segments(cwd)
    if not segments:
        return ""
    for window in open_windows:
        for folder in window.folders:
            if folder.replace(os.sep, "-") in segments:
                return window.name
    return ""


def _by_enrolled_scratchpad(cwd: str) -> str:
    """`_by_scratchpad`'s trick against the enrolment ledger rather than the
    open windows, answering with the enrolled folder's own basename.

    Deliberately reads `enrollment.enrolled_roots()` and never the ledger's
    admission helper — see `enrollment.enrolled_label`'s docstring for why
    naming must not ride the symbol the test suite patches.
    """
    segments = _mangled_segments(cwd)
    if not segments:
        return ""
    try:
        roots = enrollment.enrolled_roots()
    except Exception:  # a ledger that cannot be read names nothing
        return ""
    best = ""
    for root in roots:
        if root.replace(os.sep, "-") in segments and len(root) > len(best):
            best = root
    return os.path.basename(best.rstrip(os.sep)) if best else ""


def _from_metrics(metrics: Optional[dict]) -> str:
    if not isinstance(metrics, dict):
        return ""
    for key in ("project_dir", "git_worktree"):
        value = metrics.get(key)
        if isinstance(value, str) and value.strip():
            return os.path.basename(value.rstrip(os.sep))
    return ""


def project_label(cwd: str, metrics: Optional[dict] = None, fallback: str = "") -> str:
    """The project this session belongs to. See the module docstring for the ladder.

    `fallback` is whatever the row already carried — used when there is no cwd
    at all (a background agent the reconciler knows only by name), so this can
    never make a row *less* labelled than it was.
    """
    if not isinstance(cwd, str) or not cwd:
        return fallback

    cwd = str(Path(cwd)).rstrip(os.sep) or os.sep
    open_windows = windows()

    return (
        enrollment.enrolled_label(cwd)
        or _by_enrolled_scratchpad(cwd)
        or _by_containment(cwd, open_windows)
        or _by_scratchpad(cwd, open_windows)
        or _from_metrics(metrics)
        or os.path.basename(cwd)
        or fallback
    )
