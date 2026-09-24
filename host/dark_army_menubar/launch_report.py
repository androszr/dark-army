# host/dark_army_menubar/launch_report.py
"""What *this launch* did to the machine, told truthfully.

A first launch rewrites the hook handler, checks the editor extension and
enables the login item, and until now it said nothing about any of it. The
panel now draws one short line — "This launch: hooks current; editor
extension installed; login item enabled." — and this module is the record it
reads. Three rules keep the line honest:

- **Attempted is not succeeded.** A plist on disk is `failed` when
  `launchctl bootstrap` refused it; an extension the CLI could not be found
  for is `failed`, not "installed"; an installer that raised is `failed`
  or `unknown`, never `changed`.
- **Nothing is re-run to measure it.** Every status here is read off the
  result the existing installer already produced (or a before/after
  comparison around it), so the launch does exactly what it did before.
- **Ephemeral.** This is a dict for the life of one process, pushed down the
  existing panel context. It is not persisted, not a ledger and not an
  installer of its own.

Only the three categories a person asked about are named here (hooks, the
editor extension, the login item). The statusline, the title pen, the
close-out script and the channel are still installed exactly as before and
are listed in the walkthrough document's evidence inventory, not on this
line.
"""
from __future__ import annotations

import threading
from typing import Optional

#: The result vocabulary. The panel maps each to words; anything outside it
#: is treated as `unknown` on the way in so a typo can never draw as success.
PENDING = "pending"
CHANGED = "changed"
UNCHANGED = "unchanged"
SKIPPED = "skipped"
FAILED = "failed"
UNKNOWN = "unknown"
STATUSES = (PENDING, CHANGED, UNCHANGED, SKIPPED, FAILED, UNKNOWN)

#: The three fields, in the order the sentence names them.
HOOKS = "hooks"
EXTENSION = "extension"
LOGIN_ITEM = "login_item"
FIELDS = (HOOKS, EXTENSION, LOGIN_ITEM)

#: The most a detail may carry. Never stderr, never a path with a home
#: directory in it, never a key — one clause a person can read.
MAX_DETAIL_CHARS = 160


def _clean_detail(detail) -> str:
    text = " ".join(str(detail or "").split())
    return text[:MAX_DETAIL_CHARS]


class LaunchReport:
    """One launch's outcomes, thread-safe: the extension result arrives from
    the `vscode-ext-install` worker while the main thread reads the dict for a
    context push. Every field starts `pending`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fields: dict[str, dict] = {
            name: {"status": PENDING, "detail": ""} for name in FIELDS
        }

    def set(self, field: str, status: str, detail: str = "") -> None:
        """Record one outcome. An unknown field is ignored; an unknown status
        is written as `unknown` rather than trusted."""
        if field not in self._fields:
            return
        if status not in STATUSES:
            status = UNKNOWN
        with self._lock:
            self._fields[field] = {"status": status,
                                   "detail": _clean_detail(detail)}

    def status(self, field: str) -> str:
        with self._lock:
            return self._fields.get(field, {}).get("status", UNKNOWN)

    def to_dict(self) -> dict:
        """A copy for the panel context — plain strings only."""
        with self._lock:
            return {name: dict(value) for name, value in self._fields.items()}

    @property
    def settled(self) -> bool:
        """No field is still pending."""
        with self._lock:
            return all(v["status"] != PENDING for v in self._fields.values())


def login_outcome(result, *, already: bool = False,
                  error: Optional[BaseException] = None) -> tuple[str, str]:
    """Map `launchd.enable()`'s answer onto the vocabulary.

    `already` is the caller saying it did not run `enable()` because the
    plist was already there; `error` is an exception it caught. A `None`
    result (an older stub, or a caller that forgot) is `unknown`: it is not
    evidence either way.
    """
    if error is not None:
        return FAILED, "login item could not be set up"
    if already:
        return UNCHANGED, "login item already set up"
    if result is None:
        return UNKNOWN, "login item result not reported"
    written = bool(getattr(result, "written", False))
    bootstrapped = bool(getattr(result, "bootstrapped", False))
    detail = str(getattr(result, "detail", "") or "")
    if written and bootstrapped:
        return CHANGED, detail or "login item enabled"
    if written:
        # A plist on disk after a failed bootstrap is exactly the case this
        # module exists for: it must never read as "enabled successfully".
        return FAILED, detail or "login item written but not registered"
    return FAILED, detail or "login item not written"
