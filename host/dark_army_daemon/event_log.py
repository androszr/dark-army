"""A bounded diary of what happened on this Mac, for the phone to read later.

`/api/state` describes *now*. A run that began, ended, errored or blocked on a
permission ask while the phone was asleep left nothing anybody could read
afterwards — and most of what happens, happens while nobody is looking. This
is the daemon's own append-only journal of those moments, with the display
fields resolved and the sentence composed **at write time**, so every surface
that reads it draws the Mac's own words rather than composing its own.

Three properties, each load-bearing:

* **One file, loaded whole.** `event-log.jsonl` under the state directory
  (`paths._PRIVATE_FILES`, 0600), at most `MAX_ENTRIES` lines and nothing
  older than `RETENTION_SECONDS`. It is read into a `deque` on `open()` and
  reads are answered from memory — `recent()` costs no I/O, which is the
  property that matters for a read fetched behind a poll. One line is
  appended per event. The deque is trimmed on **every** append, so a read
  never sees more than `MAX_ENTRIES` or anything older than a day; the
  *file* is rewritten atomically (`tmp` + `os.replace`) only when an age
  prune dropped something or the on-disk line count has run `PRUNE_SLACK`
  past the cap — never once per append at the cap, which was a full
  rewrite and fsync per event while `recent()` waited on the lock.
* **The sentence is composed here and nowhere else.** `sentence()` is the one
  place a human line about an event is written; the daemon hands in fields
  and the phone shows `text` verbatim. Never an id: `who` is the nickname,
  then the title, then the provider, then "an agent".
* **Nothing secret can get in.** `append` refuses an unknown kind and any
  `FORBIDDEN_KEYS` member at the top level or anywhere inside `detail` — the
  broker's permission row carries `claim`, the channel's carries `port`, and
  a caller that copied `**row` would have leaked one. Callers copy named
  keys; this is the second fence.

The lock covers both the deque and the file, so an append from the executor
and a read from the loop never interleave half a line.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .paths import EVENT_LOG_PATH

logger = logging.getLogger(__name__)

#: Nothing older than a day is ever returned or kept.
RETENTION_SECONDS = 86400
#: And never more lines than this, oldest dropped first.
MAX_ENTRIES = 500
#: How far past the cap the *file* may run before it is rewritten. The deque
#: is trimmed on every append; the file is compacted only once it has grown
#: this many surplus lines, so a diary sitting at the cap costs one appended
#: line per event rather than a rewrite of every line.
PRUNE_SLACK = 50

#: Every kind of moment the diary records. `append` refuses anything else.
KINDS = (
    "session_start",
    "session_end",
    "session_error",
    "permission_ask",
    "permission_resolved",
    "card_dispatched",
    "card_done",
    "card_manual",
    # A person recording Passed or Failed on a manual check file — the
    # outcome, never the file's path.
    "card_manual_outcome",
    "card_dispatch_failed",
    "card_plan_attached",
    "card_plan_approved",
    "card_work_recorded",
    # A burst of refused knocks at the phone doors (`access_log.py`). The
    # access log holds the whole record; this is the diary's one line so
    # Recently and Catch up mention it.
    "access_burst",
)

#: Every key an entry may carry. The phone's `LogEntry` decodes exactly these
#: and the drift test imports the tuple, so a key added here without a Swift
#: counterpart fails a test rather than silently going undrawn.
PUBLISHED_KEYS = (
    "id", "ts", "kind", "text", "project", "nickname", "title",
    "session_id", "card_id", "detail",
)

#: Keys that must never appear in an entry, at any depth. `claim` is the
#: permission broker's secret, `port` is the channel's internal address, and
#: the other three are the names a secret is most often stored under.
FORBIDDEN_KEYS = ("claim", "port", "token", "key", "secret")

#: A permission ask's description is clipped in the sentence, not refused.
MAX_DESCRIPTION_CHARS = 80


# --- the sentence -------------------------------------------------------------

def _duration(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _who(fields: dict, *, use_title: bool = True) -> str:
    """The subject of a sentence, never an id.

    Session entries fall back to the row's title (the session's own name),
    then the provider. Card entries skip the title — there it is the *card's*
    title, and "the work finished the card the work" is not a sentence — and
    fall back to the tool the card names."""
    nickname = str(fields.get("nickname") or "").strip()
    if nickname:
        return nickname
    if use_title:
        title = str(fields.get("title") or "").strip()
        if title:
            return title
    provider = str(fields.get("provider") or fields.get("tool") or "").strip()
    if provider:
        return provider
    return "an agent"


def _in(project: str) -> str:
    project = str(project or "").strip()
    return f" in {project}" if project else ""


def sentence(kind: str, **fields) -> str:
    """The one human line about an event. Detail keys may ride either at the
    top level or inside ``detail``; the top level wins on a clash."""
    merged = dict(fields.get("detail") or {})
    merged.update({k: v for k, v in fields.items() if k != "detail"})
    project = merged.get("project", "")
    title = str(merged.get("title") or "").strip()

    if kind == "session_start":
        return f"{_who(merged)} started{_in(project)}"
    if kind == "session_end":
        text = f"{_who(merged)} finished{_in(project)}"
        duration = _duration(merged.get("duration_seconds"))
        if duration:
            text += f" after {duration}"
        cost = merged.get("cost")
        if isinstance(cost, (int, float)) and cost > 0:
            text += f", ${cost:.2f}"
        reason = str(merged.get("end_reason") or "").strip()
        if reason and reason != "ended":
            text += f" — {reason}"
        return text
    if kind == "session_error":
        return f"{_who(merged)} hit an API error{_in(project)}"
    if kind == "permission_ask":
        tool = str(merged.get("tool") or "a tool")
        text = f"{_who(merged)} wants to run {tool}"
        description = " ".join(str(merged.get("description") or "").split())
        if description:
            if len(description) > MAX_DESCRIPTION_CHARS:
                description = description[:MAX_DESCRIPTION_CHARS - 1] + "…"
            text += f": {description}"
        return text
    if kind == "permission_resolved":
        tool = str(merged.get("tool") or "a tool")
        outcome = str(merged.get("outcome") or "")
        head = f"{_who(merged)}'s {tool}"
        if outcome in ("allow", "allowed"):
            return f"{head} was allowed"
        if outcome in ("deny", "denied"):
            return f"{head} was denied"
        why = str(merged.get("why") or "").strip()
        return f"{head} lapsed — {why}" if why else f"{head} lapsed"
    if kind == "card_dispatched":
        tool = str(merged.get("tool") or "an assistant")
        return f"{title or 'a card'} started with {tool}{_in(project)}"
    if kind == "card_done":
        closed_by = str(merged.get("closed_by") or "")
        if closed_by == "user":
            return f"{title or 'a card'} was moved to Done by hand"
        if closed_by == "terminal_close":
            # A person pressed Close terminal on the session this card was
            # dispatched from. `closed_by` here is a diary detail key and has
            # never been the store column, which holds a session id.
            return f"{title or 'a card'} was finished when its terminal was closed"
        return f"{_who(merged, use_title=False)} finished the card {title or ''}".rstrip()
    if kind == "card_manual":
        return f"{title or 'a card'} needs a manual check"
    if kind == "card_manual_outcome":
        outcome = str(merged.get("status") or "").strip().lower()
        word = outcome if outcome in ("passed", "failed") else "recorded"
        text = f"{title or 'a card'}: manual check {word}"
        note = " ".join(str(merged.get("note") or "").split())
        if note and note.lower() != "none":
            if len(note) > MAX_DESCRIPTION_CHARS:
                note = note[:MAX_DESCRIPTION_CHARS - 1] + "…"
            text += f" — {note}"
        return text
    if kind == "card_dispatch_failed":
        error = str(merged.get("error") or "").strip()
        head = f"{title or 'a card'} could not start"
        return f"{head} — {error}" if error else head
    if kind == "card_plan_attached":
        return f"{_who(merged, use_title=False)} attached a plan to {title or 'a card'}"
    if kind == "card_plan_approved":
        return f"somebody approved the plan on {title or 'a card'}"
    if kind == "card_work_recorded":
        # A glance at Recently says a record landed, so nobody has to open
        # the card to find out whether one exists. Dark Army's own observation, and
        # the sentence says so — "wrote down what changed", never "changed".
        who = _who(merged, use_title=False)
        files = merged.get("files")
        head = f"Dark Army wrote down what {who} changed{_in(project)}"
        try:
            count = int(files)
        except (TypeError, ValueError):
            count = -1
        if count == 0:
            return f"{head}: nothing"
        if count > 0:
            return f"{head}: {count} file" + ("s" if count != 1 else "")
        return head
    if kind == "access_burst":
        # `door` is the label (`access_log.door_label`, "Wi-Fi") and the
        # sentence supplies the noun; `peer` is an address or a paired
        # device id, never a key or a channel id.
        peer = str(merged.get("peer") or "").strip() or "an unknown source"
        door = str(merged.get("door") or "").strip() or "phone"
        try:
            count = int(merged.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            window = int(merged.get("window_seconds") or 0)
        except (TypeError, ValueError):
            window = 0
        minutes = max(1, (window + 59) // 60)
        plural = "s" if minutes != 1 else ""
        return (f"Dark Army refused {count} connection attempts from {peer} "
                f"at the {door} door in {minutes} minute{plural}")
    return ""


# --- the store -----------------------------------------------------------------

def _has_forbidden(value, depth: int = 0) -> Optional[str]:
    """The first forbidden key found anywhere in ``value``, or None."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k) in FORBIDDEN_KEYS:
                return str(k)
            hit = _has_forbidden(v, depth + 1)
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for v in value:
            hit = _has_forbidden(v, depth + 1)
            if hit:
                return hit
    return None


def _plain(value):
    """JSON-safe copy of a detail value; anything exotic becomes its str."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return str(value)


class EventLog:
    """The diary. `open()` before use; every method is thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else EVENT_LOG_PATH
        self._lock = threading.Lock()
        self._entries: deque = deque()
        #: Lines on disk, which runs ahead of `len(self._entries)` between
        #: rewrites; the rewrite rule reads it, nothing else does.
        self._lines = 0
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    # -- lifecycle --

    def open(self) -> None:
        """Load the file — tolerating a truncated last line and unknown keys —
        then prune. A file that will not parse at all is started afresh."""
        entries: list = []
        lines = 0
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue        # a half-written tail, or noise
                    if not isinstance(row, dict) or "id" not in row:
                        continue
                    entries.append({k: row[k] for k in PUBLISHED_KEYS if k in row})
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("event log unreadable; starting empty", exc_info=True)
        entries.sort(key=lambda e: float(e.get("ts") or 0.0))
        with self._lock:
            self._entries = deque(entries)
            self._lines = lines
            self._closed = False
            # Anything the load dropped — too old, over the cap — is rewritten
            # out at once, so the file on disk is the diary from the start.
            if any(self._prune_locked()):
                self._rewrite()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # -- writes --

    def append(self, kind: str, **fields) -> Optional[dict]:
        """Record one moment. Returns the entry, or None on a refusal."""
        if kind not in KINDS:
            logger.warning("event log refused unknown kind %r", kind)
            return None
        hit = _has_forbidden(fields)
        if hit:
            logger.warning("event log refused a %s entry carrying %r", kind, hit)
            return None
        detail = fields.get("detail")
        detail = _plain(detail) if isinstance(detail, dict) else {}
        entry = {
            "id": secrets.token_hex(6),
            "ts": float(fields.get("ts") or time.time()),
            "kind": kind,
            "text": sentence(kind, **fields),
            "project": str(fields.get("project") or ""),
            "nickname": str(fields.get("nickname") or ""),
            "title": str(fields.get("title") or ""),
            "session_id": str(fields.get("session_id") or ""),
            "card_id": str(fields.get("card_id") or ""),
            "detail": detail,
        }
        with self._lock:
            if self._closed:
                return None
            self._entries.append(entry)
            self._write_line(entry)
            aged, _capped = self._prune_locked()
            if aged or self._lines >= MAX_ENTRIES + PRUNE_SLACK:
                self._rewrite()
        return dict(entry)

    def prune(self) -> bool:
        """Drop what is too old or too many. True when something went; the
        file is rewritten whenever it did."""
        with self._lock:
            dropped = any(self._prune_locked())
            if dropped:
                self._rewrite()
            return dropped

    # -- reads --

    def recent(self, since: Optional[float] = None,
               limit: Optional[int] = None) -> list:
        """Newest first; ``ts`` strictly greater than ``since`` when given;
        at most ``limit``. Copies, so a caller may not reach the deque.

        The retention floor is applied here too, read-only: the age prune
        runs only on a write, so after a quiet day the deque still holds
        yesterday, and "nothing older than a day is ever returned" has to
        hold on a read as well."""
        floor = time.time() - RETENTION_SECONDS
        with self._lock:
            rows = [r for r in self._entries
                    if float(r.get("ts") or 0.0) >= floor]
        rows.reverse()
        if since is not None:
            rows = [r for r in rows if float(r.get("ts") or 0.0) > float(since)]
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        return [dict(r, detail=dict(r.get("detail") or {})) for r in rows]

    def last_ts(self, session_id: str, kind: str) -> Optional[float]:
        """The `ts` of the newest entry of ``kind`` about ``session_id``, or
        None. One walk under the lock; the daemon's enrich loop asks it
        whether a row's own `started_at` predates the finish already on
        record, so a stale-evicted session returning under the same id does
        not read as a second start."""
        if not session_id:
            return None
        with self._lock:
            for entry in reversed(self._entries):
                if entry.get("kind") == kind and entry.get("session_id") == session_id:
                    try:
                        return float(entry.get("ts") or 0.0)
                    except (TypeError, ValueError):
                        return None
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- internals, lock held --

    def _prune_locked(self) -> tuple:
        """Trim the deque; never touches the file. Returns ``(aged, capped)``.
        `append` rewrites on ``aged`` alone — a line older than a day is one
        `open()` would read back, so it must leave the file at once — and
        lets the cap's surplus run to `PRUNE_SLACK`; `open()` and `prune()`
        rewrite on either."""
        floor = time.time() - RETENTION_SECONDS
        aged = False
        while self._entries and float(self._entries[0].get("ts") or 0.0) < floor:
            self._entries.popleft()
            aged = True
        capped = False
        while len(self._entries) > MAX_ENTRIES:
            self._entries.popleft()
            capped = True
        return aged, capped

    def _write_line(self, entry: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
            self._lines += 1
        except OSError:
            logger.warning("event log append failed", exc_info=True)

    def _rewrite(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in self._entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
            self._lines = len(self._entries)
        except OSError:
            logger.warning("event log rewrite failed", exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
