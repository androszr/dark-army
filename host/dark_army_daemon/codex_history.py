"""Backfill: Codex session journals → the history database.

History's ledger prices counted tokens for Claude, Grok and Codex alike, and
Codex's tokens live only in its own journals
(``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``). This scanner writes one
``turns`` row per ``event_msg`` / ``token_count`` record that carries a turn's
usage, ``provider="codex"`` and ``cost_usd`` NULL: the journals state no
dollar, and the price is read at query time (``HistoryStore._token_parts``),
never written back: no session row, no session cost, no estimate stored.

It is **not** ``codex_spenders.CodexSpenders.snapshot``. That reader is a
budget-capped seven-day share for the Usage view and returns percentages, not
turns; it keeps its own shape and its own read. The two agree on what a
reading is — cached input and reasoning output are *subsets* of input and
output, never extra tokens — by sharing ``codex_spenders._tokens``.

Same incremental contract as ``grok_scan``: an unchanged file costs one stat
against ``scan_state`` (its byte offset stored negated, the shared column's
convention), a grown one is read from where the last pass stopped, and a
turn's ``message_id`` — the thread, the cumulative reading that identifies it
(``CodexSpenders``' own identity) and its timestamp — makes a re-read insert
nothing. A journal's thread, model and last cumulative reading are needed to
read its tail, so they are held in memory; a daemon that restarts re-reads a
*changed* journal from byte 0 once, which the ``message_id`` makes free of
duplicates.

A pass is bounded by ``BYTE_BUDGET``. A pass that stops early says so
(``partial``) and prices nothing it did not read: the unread tail is absent,
never $0.

Blocking file and sqlite I/O: call ``scan`` from an executor.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .codex_spenders import _stamp, _tokens
from .history import HistoryStore
from .paths import _home

logger = logging.getLogger("dark-army.codex-history")

SESSIONS_DIR = _home() / ".codex" / "sessions"

#: Bytes one pass may read across every journal. Only three record types are
#: parsed and the rest is skipped on a byte test, so a first pass over ~800 MB
#: took about three seconds on the machine this was written on. A Mac with
#: more than this finishes over several passes and says partial until it
#: does; after that a pass reads only what was appended.
BYTE_BUDGET = 1024 * 1024 * 1024

#: A line longer than this is a tool output or a pasted file, never a usage
#: record, and is skipped whole rather than held in memory.
LINE_LIMIT = 1024 * 1024

_CHUNK = 8 * 1024 * 1024
_TAGS = (b'"session_meta"', b'"turn_context"', b'"token_count"')


@dataclass
class _Journal:
    identity: tuple
    offset: int = 0
    fragment: bytes = b""
    skipping: bool = False
    thread: str = ""
    # The session the turns are filed under: the thread itself, or — for a
    # subagent's journal — the root thread that spawned it.
    session: str = ""
    created: Optional[float] = None
    model: str = ""
    cumulative: Optional[tuple] = None
    total: Optional[dict] = None
    epoch: int = 0


_journals: dict[str, _Journal] = {}
_lock = threading.Lock()


def _count(value) -> Optional[int]:
    """A token count, or None when the field is not a plain non-negative int."""
    if type(value) is not int or value < 0:
        return None
    return value


def _is_thread_id(value) -> bool:
    return (isinstance(value, str) and bool(value) and value.strip() == value
            and len(value) <= 200 and not any(c.isspace() for c in value))


def _root_thread(payload: dict, thread: str) -> str:
    """The thread a journal's turns belong to on a card.

    A subagent (`source.subagent`, or a `parent_thread_id`) writes its own
    journal under its own thread id, but its spend is the spawning
    session's, as a Claude sidechain's is its parent's: Dark Army binds the
    root thread to the card. Codex stamps the root as `session_id` beside
    the child's own `id`; the parent id is the fallback for a journal that
    lacks it. A thread that is not a subagent — a fork included — is its
    own session. `message_id` keeps the child's own thread id, so the turn
    stays one row however often it is read."""
    source = payload.get("source")
    subagent = isinstance(source, dict) and (
        "subagent" in source or "subAgent" in source)
    parent = payload.get("parent_thread_id")
    if not subagent and not _is_thread_id(parent):
        return thread
    root = payload.get("session_id")
    if _is_thread_id(root) and root != thread:
        return root
    if not _is_thread_id(parent) and isinstance(source, dict):
        spawn = (source.get("subagent") or source.get("subAgent") or {})
        spawn = spawn.get("thread_spawn") if isinstance(spawn, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent if _is_thread_id(parent) and parent != thread else thread


def _usage(last: dict) -> Optional[dict]:
    """The four counts one reading is priced from, or None if unreadable.

    ``_tokens`` is ``CodexSpenders``' own check: every count an int, cached
    input no larger than input, reasoning no larger than output. A reading
    that fails it is dropped, not repaired. The cache write is read beside it
    and is left for the pricer to judge: cached plus written larger than the
    input they are both inside is an unpriced turn, not a double count."""
    if _tokens(last) is None:
        return None
    write = _count(last.get("cache_write_input_tokens", 0))
    if write is None:
        return None
    return {
        "input_tokens": last["input_tokens"],
        "output_tokens": last["output_tokens"],
        "cache_read": last.get("cached_input_tokens", 0),
        "cache_creation": write,
    }


def _difference(total: dict, previous: dict) -> Optional[dict]:
    """A turn's usage from two cumulative readings, when the record carried
    no ``last_token_usage`` of its own."""
    fields = ("input_tokens", "output_tokens", "cached_input_tokens",
              "cache_write_input_tokens", "reasoning_output_tokens")
    out = {}
    for key in fields:
        now, before = total.get(key, 0), previous.get(key, 0)
        if _count(now) is None or _count(before) is None or now < before:
            return None
        out[key] = now - before
    return out


def _line(state: _Journal, line: bytes, summary: dict) -> Optional[dict]:
    """One journal line as a turn to store, or None."""
    if not any(tag in line for tag in _TAGS):
        return None
    try:
        item = json.loads(line)
    except (ValueError, UnicodeError):
        state.cumulative = None
        state.total = None
        return None
    if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
        return None
    payload = item["payload"]
    kind = item.get("type")
    if kind == "session_meta":
        if not state.thread:
            thread = payload.get("id") or payload.get("session_id")
            state.thread = thread if isinstance(thread, str) else ""
            state.session = _root_thread(payload, state.thread)
            state.created = _stamp(payload.get("timestamp"))
        return None
    if kind == "turn_context":
        model = payload.get("model")
        state.model = model[:200] if isinstance(model, str) else ""
        return None
    if kind != "event_msg" or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None                   # a rate-limit-only notice, no usage
    total = info.get("total_token_usage")
    cumulative = _tokens(total)
    previous, previous_total = state.cumulative, state.total
    state.cumulative = cumulative
    state.total = total if cumulative is not None else None
    if cumulative is None:
        summary["unreadable"] += 1
        return None
    if cumulative == previous:
        return None                   # the same reading, repeated with a new time
    if previous is not None and any(a < b for a, b in zip(cumulative, previous)):
        state.epoch += 1              # a counter reset: this reading is uncertain
        return None
    timestamp = _stamp(item.get("timestamp"))
    if not state.thread or state.created is None or timestamp is None:
        summary["unreadable"] += 1
        return None
    if timestamp < state.created:
        return None                   # a fork's copied history is its parent's
    last = info.get("last_token_usage")
    if isinstance(last, dict):
        usage = _usage(last)
    elif previous_total is not None:
        usage = _usage(_difference(total, previous_total) or {})
    else:
        usage = None
    if usage is None:
        summary["unreadable"] += 1
        return None
    if usage["input_tokens"] > cumulative[0] or usage["output_tokens"] > cumulative[1]:
        summary["unreadable"] += 1
        return None
    if not (usage["input_tokens"] or usage["output_tokens"]):
        return None
    identity = f"{state.epoch}:{cumulative[0]}:{cumulative[1]}"
    return {
        "session_id": f"codex:{state.session or state.thread}",
        "ts": timestamp,
        "message_id": f"codex:{state.thread}:{identity}:{timestamp:.3f}",
        "model": state.model or None,
        **usage,
        "cost_usd": None,
        "provider": "codex",
    }


def _read(store: HistoryStore, path: Path, state: _Journal, size: int,
          budget: int, summary: dict) -> int:
    """Read up to ``budget`` bytes of one journal from ``state.offset``."""
    read = 0
    with path.open("rb") as stream:
        stream.seek(state.offset)
        while read < budget and state.offset < size:
            chunk = stream.read(min(_CHUNK, budget - read, size - state.offset))
            if not chunk:
                break
            read += len(chunk)
            state.offset += len(chunk)
            pieces = (state.fragment + chunk).split(b"\n")
            state.fragment = pieces.pop()
            for line in pieces:
                if state.skipping:
                    state.skipping = False
                    continue
                if len(line) > LINE_LIMIT:
                    continue
                turn = _line(state, line, summary)
                if turn is None:
                    continue
                session_id, ts = turn.pop("session_id"), turn.pop("ts")
                if store.add_turn(session_id, ts, **turn):
                    summary["turns_added"] += 1
            if len(state.fragment) > LINE_LIMIT:
                state.fragment = b""
                state.skipping = True
    return read


def _journal_paths(sessions_dir: Path) -> list[Path]:
    """Every journal under the sessions folder, newest first — the recent
    week is what History opens on, so a bounded pass reads it first."""
    found = []
    for directory, _dirs, files in os.walk(sessions_dir):
        for name in files:
            if name.endswith(".jsonl"):
                path = Path(directory) / name
                try:
                    found.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    return [path for _mtime, path in sorted(found, reverse=True)]


def scan(store: HistoryStore, sessions_dir: Optional[Path] = None, *,
         byte_budget: int = BYTE_BUDGET,
         journals: Optional[dict] = None) -> dict:
    """Scan every Codex journal once, within ``byte_budget``. Blocking.

    Returns a summary whose ``partial`` is True when a journal still has
    unread bytes (the budget ran out, or a file could not be read): the
    report says so in words rather than publishing a short read as the whole
    period. ``journals`` is the in-memory reading state; tests pass their own."""
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    if journals is None:
        journals = _journals
    summary = {"files_seen": 0, "files_read": 0, "turns_added": 0,
               "unreadable": 0, "bytes_read": 0, "partial": False}
    if not sessions_dir.is_dir():
        return summary
    with _lock:
        try:
            paths = _journal_paths(sessions_dir)
        except OSError:
            logger.warning("Could not list %s", sessions_dir, exc_info=True)
            summary["partial"] = True
            return summary
        try:
            positions = store.scan_positions()
        except Exception:
            logger.debug("Could not bulk-read scan positions", exc_info=True)
            positions = {}
        budget = max(1, int(byte_budget))
        live = set()
        for path in paths:
            summary["files_seen"] += 1
            key = str(path)
            live.add(key)
            try:
                stat = path.stat()
            except OSError:
                summary["partial"] = True
                continue
            seen_mtime, seen = positions.get(key, (0.0, 0))
            done = -int(seen) if seen < 0 else 0
            identity = (stat.st_dev, stat.st_ino)
            state = journals.get(key)
            if state is not None and (state.identity != identity
                                      or stat.st_size < state.offset):
                state = None
            if state is None:
                if stat.st_mtime == seen_mtime and done == stat.st_size:
                    continue          # read whole last time, and unchanged since
                state = journals[key] = _Journal(identity)
            if state.offset >= stat.st_size:
                continue
            if budget <= 0:
                summary["partial"] = True
                continue
            try:
                read = _read(store, path, state, stat.st_size, budget, summary)
            except OSError:
                logger.debug("could not read %s", path, exc_info=True)
                summary["partial"] = True
                continue
            budget -= read
            summary["bytes_read"] += read
            summary["files_read"] += 1
            if state.offset < stat.st_size:
                summary["partial"] = True
            store.set_scan_position(key, stat.st_mtime, -state.offset)
        for key in [k for k in journals if k not in live]:
            journals.pop(key, None)
    if summary["turns_added"] or summary["partial"]:
        logger.info("Codex history scan: %s", summary)
    return summary
