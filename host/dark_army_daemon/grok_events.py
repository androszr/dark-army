"""Read-only rollup of a Grok session's ``events.jsonl``.

Grok writes ``tool_started`` / ``tool_completed`` as the file grows — this is
the live per-tool count, unlike ``signals.json`` which only appears at turn
end and only carries a single ``toolCallCount``. Names stay as Grok spelled
them (``write``, ``search_replace``, ``read_file``); the churn rule aliases
those onto the edit set rather than renaming them into Claude's vocabulary.

Same incremental cache as ``grok_usage.UsageCache``. Nothing here writes to
``~/.grok/``.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from typing import Optional

from .grok_usage import DeltaCache, iso_epoch


# Outcomes that mean the tool did what it was asked. Anything else is a
# failure — ``error``, ``cancelled``, an unexpected string, a missing field.
_OK_OUTCOMES = {"success", "completed"}


@dataclass
class GrokEvents:
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_failures: int = 0
    last_ts: Optional[float] = None

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_counts.values())


@dataclass
class _EventAccum:
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_failures: int = 0
    last_ts: Optional[float] = None
    offset: int = 0
    folded: int = 0


def _ts_of(obj: dict) -> Optional[float]:
    return iso_epoch(obj.get("ts") or obj.get("timestamp"))


def _fold_line(line: str, acc: _EventAccum) -> None:
    # Cheap reject: the file is mostly phase_changed.
    if '"tool_started"' not in line and '"tool_completed"' not in line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    kind = obj.get("type")
    if kind not in ("tool_started", "tool_completed"):
        return
    acc.folded += 1
    ts = _ts_of(obj)
    if ts is not None and (acc.last_ts is None or ts > acc.last_ts):
        acc.last_ts = ts
    name = obj.get("tool_name")
    if kind == "tool_started" and isinstance(name, str) and name:
        acc.tool_counts[name] = acc.tool_counts.get(name, 0) + 1
    if kind == "tool_completed":
        outcome = obj.get("outcome")
        if outcome not in _OK_OUTCOMES:
            acc.tool_failures += 1


def _finalize(acc: _EventAccum) -> GrokEvents:
    return GrokEvents(
        tool_counts=dict(acc.tool_counts),
        tool_failures=acc.tool_failures,
        last_ts=acc.last_ts,
    )


class EventsCache(DeltaCache):
    """Memoises ``events.jsonl`` parsing by mtime, reading only the delta."""

    def __init__(self, max_entries: int = 512) -> None:
        super().__init__(_EventAccum, _fold_line, _finalize, max_entries)

    def get(self, path: str) -> GrokEvents:
        return super().get(path)


def parse_events(path: str) -> GrokEvents:
    return EventsCache().get(path)


#: How much of the tail `permission_pending` reads. A Grok turn writes a few
#: hundred bytes per tool call and the permission pair sits at the very end
#: of the file when the hook that asks about it fires, so this is thousands
#: of lines of headroom, not a bound anything reaches.
PERMISSION_TAIL_BYTES = 65536


def _permission_tail(path: str, tail_bytes: int) -> bytes | None:
    """Read a bounded regular-file tail without ever waiting on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            size = os.fstat(fd)
            if not stat.S_ISREG(size.st_mode):
                return None
            os.lseek(fd, max(0, size.st_size - tail_bytes), os.SEEK_SET)
            return os.read(fd, tail_bytes)
        finally:
            os.close(fd)
    except OSError:
        return None


def permission_pending(path: str, tail_bytes: int = PERMISSION_TAIL_BYTES,
                       ) -> Optional[bool]:
    """Whether the newest permission Grok asked for is still unanswered.

    Grok fires its ``permission_prompt`` Notification for **every** tool
    call — the permission phase runs whether or not a dialog is drawn, and
    on an allowed tool it resolves in milliseconds (``wait_ms: 0``). The
    hook cannot tell the two apart, but ``events.jsonl`` can: a real prompt
    is a ``permission_requested`` with no ``permission_resolved`` after it.

    ``True`` when the last request in the tail has no resolution after it,
    ``False`` when it has, ``None`` when the tail carries no request at all
    or the file cannot be read — the caller then keeps the hook's own
    reading rather than inventing one. A tail read, never the whole file:
    this runs on the daemon's loop, once per permission event.
    """
    chunk = _permission_tail(path, tail_bytes)
    if chunk is None:
        return None
    for raw in reversed(chunk.split(b"\n")):
        if b'"permission_requested"' not in raw and \
                b'"permission_resolved"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "permission_resolved":
            return False
        if kind == "permission_requested":
            return True
    return None


def permission_request_detail(path: str) -> dict:
    """Fields on the newest open permission request, or empty on doubt.

    Grok 1.0.41 often records only a tool name. A command is returned only
    when Grok actually writes one; callers must not invent it.
    """
    chunk = _permission_tail(path, PERMISSION_TAIL_BYTES)
    if chunk is None:
        return {}
    for raw in reversed(chunk.split(b"\n")):
        if b'"permission_resolved"' in raw:
            return {}
        if b'"permission_requested"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(obj, dict) or obj.get("type") != "permission_requested":
            return {}
        return {key: obj[key] for key in
                ("tool_name", "command", "description", "tool_input")
                if key in obj}
    return {}
