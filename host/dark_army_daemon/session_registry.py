"""Claude Code's own live-session registry, read from disk.

Claude Code writes one file per running session to ``~/.claude/sessions/<pid>.json``
and rewrites it as the session's status changes:

    {"pid": 41588, "sessionId": "78be5105-…", "kind": "interactive",
     "cwd": "/…/dark-army", "status": "busy", "version": "2.1.232",
     "name": "dark-army-6c", "nameSource": "derived",
     "messagingSocketPath": "/tmp/cc-socks/41588.sock"}

Three things Dark Army wants are in there and nowhere else:

* **``nameSource``.** ``derived`` until the user runs ``/rename``, then
  ``explicit``. That makes ``/rename`` a naming override with no UI to build —
  and it is the only way to tell a name the user *chose* from the default slug,
  which ``_session_name`` already goes out of its way to reject.
* **``messagingSocketPath``.** Whether the session can be reached by another
  agent's ``SendMessage`` at all. Dark Army cannot send one (that socket is not a
  public API, and the own-child rule it relies on is documented as unreliable on
  macOS) — but "this one is unreachable" is worth saying, because otherwise it is
  discovered by trying.
* **``status``**, for interactive sessions, without forking anything.

That last point matters beyond convenience: ``claude agents --json`` forks a Bun
runtime and was the most expensive thing this app did. This is a directory of
small JSON files. It does **not** replace the reconciler — the CLI is still the
only source for background agents and for sessions on other machines — so it is
an additional cheap signal, not a substitute.

Read-only, always. Nothing here writes to another program's state directory.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dark-army")

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# A guard, not a limit anyone should reach: one file per *live* session, and the
# directory is cleaned as sessions exit. If it ever holds thousands, something is
# wrong upstream and the snapshot path must not pay for it.
MAX_FILES = 256


@dataclass(frozen=True)
class RegistryEntry:
    """One live session as Claude Code describes it."""

    session_id: str = ""
    pid: Optional[int] = None
    name: str = ""
    name_source: str = ""          # "derived" | "explicit"
    status: str = ""               # e.g. "busy", "idle"
    kind: str = ""                 # "interactive" | …
    cwd: str = ""
    socket_path: str = ""
    version: str = ""

    @property
    def named_by_user(self) -> bool:
        """True only when the user actually chose this name. A derived name is
        the project slug plus two hex characters, which says nothing a row does
        not already say in its project column."""
        return self.name_source == "explicit" and bool(self.name)

    @property
    def addressable(self) -> bool:
        """Whether another agent's SendMessage could reach this session."""
        return bool(self.socket_path)


def _entry_from(raw: dict) -> Optional[RegistryEntry]:
    sid = str(raw.get("sessionId") or "")
    if not sid:
        return None                     # without an id it cannot be matched up
    pid = raw.get("pid")
    return RegistryEntry(
        session_id=sid,
        pid=int(pid) if isinstance(pid, int) else None,
        name=str(raw.get("name") or ""),
        name_source=str(raw.get("nameSource") or ""),
        status=str(raw.get("status") or ""),
        kind=str(raw.get("kind") or ""),
        cwd=str(raw.get("cwd") or ""),
        socket_path=str(raw.get("messagingSocketPath") or ""),
        version=str(raw.get("version") or ""),
    )


def read_registry(sessions_dir: Path = SESSIONS_DIR) -> dict[str, RegistryEntry]:
    """Every live session Claude Code has registered, keyed by session id.

    Never raises: this is a best-effort read of another program's files, on the
    snapshot path. A missing directory (Claude Code too old, or never run) is the
    normal empty case, not an error worth logging every few seconds.
    """
    out: dict[str, RegistryEntry] = {}
    try:
        paths = sorted(sessions_dir.glob("*.json"))[:MAX_FILES]
    except OSError:
        return out
    for path in paths:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            # A file caught mid-rewrite parses as garbage. It will be there again
            # in three seconds; there is nothing useful to say about it.
            continue
        if not isinstance(raw, dict):
            continue
        entry = _entry_from(raw)
        if entry is not None:
            out[entry.session_id] = entry
    return out


__all__ = ["RegistryEntry", "read_registry", "SESSIONS_DIR", "MAX_FILES"]
