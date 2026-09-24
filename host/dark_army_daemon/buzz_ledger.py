"""The buzz ledger: one line per alert the daemon decided to send.

The phone is the only surface on this Mac that interrupts anybody (the Mac
banners are off), and every wrong "asked you a question" buzz lands in a
pocket. Nothing recorded which buzzes went out or why the policy called one
a question: `_alerts` is a 32-entry tail in memory that no route publishes
and a restart forgets, and the relay's own health track logs failures and
recoveries alone. This is the daemon's private diary of the drain — written
at the moment `_deliver_alerts` hands a batch to the phone leg, so the next
round of "fewer false buzzes" can be measured instead of guessed
(the false-buzz investigation of 20 Sep 2026).

Three properties, each load-bearing:

* **One file, loaded whole and bounded.** `buzz-ledger.jsonl` under the
  state directory (`paths._PRIVATE_FILES`, 0600), at most `MAX_ENTRIES`
  lines and nothing older than `RETENTION_SECONDS` — `event_log.EventLog`'s
  shape: a deque trimmed on every append, the file rewritten atomically only
  when an age prune dropped a line or the on-disk count ran more than
  `PRUNE_SLACK` past the cap.
* **Identifiers, kinds, booleans, counts and clocks only.** A line's keys
  are exactly `LINE_KEYS`; the evidence behind the kind decision is counts
  and booleans off the entry (`evidence()`), never the entry itself; the
  card contributes its `hook` by name and nothing else.
* **Nothing secret and no words.** `append` refuses a line whose keys are
  not exactly `LINE_KEYS` and any `FORBIDDEN_KEYS` member at any depth — the
  access log's fence (`key`, `claim`, `token`, …) widened with every name a
  question, a summary, a banner or a path is carried under. The ledger is
  never served on any door; the fence is there anyway.

What the phone leg did with each alert is `phone.outcome`: the push leg's
own word (`empty`, `push_off`, `remote_off`, `no_connector`, `no_loop`,
`no_devices`, `sent:<n>`, `raised`) or, since schema 2 (22 Sep 2026), one
of the phone leg's refusals — `withheld:finished` (a finished turn
never buzzes the phone), `withheld:unlisted` (since 23 Sep 2026, a word
under the same key: a non-`security` alert whose session is not on the
phone's Needs you list (`live_activity.listed_sessions`: a live row
waiting, prompted or carded, or a prompt with no row), so a push would be
cleared by `clearDeliveredIfQuiet` on the phone's next check-in),
`withheld:mac_active` (the Mac was touched inside
`MAC_PRESENT_SECONDS`), `dropped:card_gone` and `dropped:muted` (a card
buzz held for the grace whose card went, or whose session was muted, before
it ran out). `phone.held_seconds` is how long the leg held the alert before
that verdict, `0.0` for one sent at once (`docs/transport-contract.md`,
*The phone leg is filtered, gated and held*). A schema-1 line on disk has
no `held_seconds` and is kept as data all the same.

The Mac-presence reading (`mac_idle_seconds`) is a **capability probe**: it
tries `Quartz` and answers `None` on any failure. It is not a platform test
and this module holds none.
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

from . import access_log
from .paths import BUZZ_LEDGER_PATH

logger = logging.getLogger(__name__)

#: The shape of a line. Kept as data on load, so a reader can tell lines of
#: one build from another's.
SCHEMA_VERSION = 2
#: Never more lines than this on disk, oldest dropped first.
MAX_ENTRIES = 2000
#: And nothing older than a month is kept or returned.
RETENTION_SECONDS = 30 * 86400
#: How far past the cap the *file* may run before it is rewritten.
PRUNE_SLACK = 100

#: The access log's fence, widened with every key the alert, the entry and
#: the card carry words under. Refused at any depth — with one structural
#: exception: the `evidence` sub-dict names three of these (`questions`,
#: `question`, `reply_options`) as **counts and a bool**, so that dict is
#: fenced by shape instead (`_evidence_shape`: exactly `EVIDENCE_KEYS`, every
#: value a number, a bool, None, or a short token for the four string
#: fields). A word cannot hide in an int; a dict or a list under any of
#: those keys is refused.
FORBIDDEN_KEYS = access_log.FORBIDDEN_KEYS + tuple(
    k for k in ("title", "body", "subtitle", "message", "text", "work",
                "last_summary", "last_text", "last_report", "question",
                "questions", "reply_options", "options", "input_preview",
                "description", "cwd", "transcript_path", "project", "branch")
    if k not in access_log.FORBIDDEN_KEYS)

#: Exactly the keys a line carries — no more, no fewer.
LINE_KEYS = ("v", "id", "ts", "drain", "alert_id", "session_id", "rule",
             "kind", "severity", "created_at", "collapsed", "push_kind",
             "badge", "banner", "evidence", "mac", "phone")
#: What the policy saw when it chose the kind: counts and booleans off the
#: entry the alert was decided against, and the card's hook by name.
EVIDENCE_KEYS = ("card_hook", "questions", "question", "reply_options",
                 "summary_chars", "text_chars", "report", "state", "category",
                 "provider", "idle_seconds", "user_prompts", "cooldown_gap")
#: Whether the Mac looked attended at the drain.
MAC_KEYS = ("frontmost_fresh", "frontmost_hit", "panel_on_screen",
            "idle_seconds")
#: What the phone leg did with it, and how long it held it first.
PHONE_KEYS = ("outcome", "devices", "held_seconds")
#: The evidence fields that are tokens rather than numbers, and how long a
#: token may be — a hook name, a state, a category, a provider.
EVIDENCE_TOKEN_KEYS = ("card_hook", "state", "category", "provider")
MAX_TOKEN_CHARS = 32


# --- the evidence ------------------------------------------------------------

def _count(value) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evidence(entry: Optional[dict], card: Optional[dict], *,
             cooldown_gap: Optional[float],
             category: Optional[str] = None) -> dict:
    """The inputs `alerts._kind` read, reduced to counts and booleans.

    Pure: a dict in, a dict out, no I/O. Called on the snapshot executor
    with the very `entry` and `card` the policy evaluated, so the ledger can
    never describe a different row than the one that was classified. Every
    missing input reads as its empty value; `entry` of `None` (a `security`
    row, which has no session) yields the same keys, all empty. The card
    contributes `hook` **by name** — it is the raw hook message and carries
    the enrolment key — and nothing else. `category` is the bucket the
    entry sat in when the policy read it: the published row has its
    `_category` stripped, so the caller names the bucket it found the entry
    under and the entry's own stamp, where present, wins.
    """
    e = entry if isinstance(entry, dict) else {}
    c = card if isinstance(card, dict) else {}
    stats = e.get("stats") if isinstance(e.get("stats"), dict) else {}
    question = e.get("question")
    return {
        "card_hook": str(c.get("hook") or ""),
        "questions": _count(e.get("questions")),
        "question": bool(isinstance(question, dict) and question),
        "reply_options": _count(e.get("reply_options")),
        "summary_chars": len(str(e.get("last_summary") or "").strip()),
        "text_chars": len(str(e.get("last_text") or "")),
        "report": bool(e.get("last_report")),
        "state": str(e.get("state") or ""),
        "category": str(e.get("_category") or category or ""),
        "provider": str(e.get("provider") or ""),
        "idle_seconds": _number(e.get("idle_seconds")),
        "user_prompts": int(_number(stats.get("user_prompts")) or 0),
        "cooldown_gap": _number(cooldown_gap),
    }


def mac_idle_seconds() -> Optional[float]:
    """Seconds since the last HID event on this Mac, or None.

    A capability probe, not a platform test: `Quartz` is tried and any
    failure — no module, no display, a call that raises — is `None`, which
    every reader treats as "unknown". Runs inside the executor callable that
    writes the line, never on the loop.
    """
    try:
        import Quartz
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGAnyInputEventType))
    except Exception:
        return None


# --- the fence ---------------------------------------------------------------

def _has_forbidden(value, depth: int = 0) -> Optional[str]:
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


def _evidence_shape(value) -> Optional[str]:
    """Why an `evidence` dict is refused, or None. Shape, not names: the
    key set is exactly `EVIDENCE_KEYS`, and every value is a number, a bool,
    None, or — for `EVIDENCE_TOKEN_KEYS` alone — a short string."""
    if not isinstance(value, dict) or set(value) != set(EVIDENCE_KEYS):
        return "evidence keys"
    for k, v in value.items():
        if v is None or isinstance(v, (bool, int, float)):
            continue
        if k in EVIDENCE_TOKEN_KEYS and isinstance(v, str) \
                and len(v) <= MAX_TOKEN_CHARS:
            continue
        return f"evidence.{k}"
    return None


def _plain(value):
    """JSON-safe copy; anything exotic becomes its str."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return str(value)


class BuzzLedger:
    """The ledger. `open()` before use; every method is thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else BUZZ_LEDGER_PATH
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
        """Load the file — tolerating a half-written tail and unknown keys —
        then prune. A `v` this build does not know is kept as data."""
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
                    entries.append({k: row[k] for k in LINE_KEYS if k in row})
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("buzz ledger unreadable; starting empty", exc_info=True)
        entries.sort(key=lambda e: float(e.get("ts") or 0.0))
        with self._lock:
            self._entries = deque(entries)
            self._lines = lines
            self._closed = False
            if any(self._prune_locked()):
                self._rewrite()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # -- writes --

    def append(self, line: dict) -> Optional[dict]:
        """Record one buzz. Returns the entry, or None on a refusal — a key
        set that is not exactly `LINE_KEYS`, or a forbidden name anywhere."""
        if not isinstance(line, dict) or set(line) != set(LINE_KEYS):
            logger.warning("buzz ledger refused a line with keys %r",
                           sorted(line) if isinstance(line, dict) else type(line))
            return None
        for sub, keys in (("mac", MAC_KEYS), ("phone", PHONE_KEYS)):
            if not isinstance(line.get(sub), dict) \
                    or set(line[sub]) != set(keys):
                logger.warning("buzz ledger refused a line: bad %s", sub)
                return None
        hit = _evidence_shape(line.get("evidence")) \
            or _has_forbidden({k: v for k, v in line.items() if k != "evidence"})
        if hit:
            logger.warning("buzz ledger refused a line carrying %r", hit)
            return None
        entry = {k: _plain(line[k]) for k in LINE_KEYS}
        if not entry.get("id"):
            entry["id"] = secrets.token_hex(6)
        entry["ts"] = float(entry.get("ts") or time.time())
        with self._lock:
            if self._closed:
                return None
            self._entries.append(entry)
            self._write_line(entry)
            aged, _capped = self._prune_locked()
            if aged or self._lines > MAX_ENTRIES + PRUNE_SLACK:
                self._rewrite()
        return dict(entry)

    # -- reads --

    def recent(self, since: Optional[float] = None,
               limit: Optional[int] = None) -> list:
        """Newest first; `ts` strictly greater than `since` when given; at
        most `limit`. Copies. For tests and a `tail` by hand — nothing
        serves this."""
        floor = time.time() - RETENTION_SECONDS
        with self._lock:
            rows = [r for r in self._entries
                    if float(r.get("ts") or 0.0) >= floor]
        rows.reverse()
        if since is not None:
            rows = [r for r in rows if float(r.get("ts") or 0.0) > float(since)]
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        return [json.loads(json.dumps(r)) for r in rows]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- internals, lock held --

    def _prune_locked(self) -> tuple:
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
            logger.warning("buzz ledger append failed", exc_info=True)

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
            logger.warning("buzz ledger rewrite failed", exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
