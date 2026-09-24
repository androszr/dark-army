"""Windowed local token shares, independent of Codex's live-session roster.

Only metadata, model names and usage counters survive parsing. No database,
credentials, prompts, tools or prices. The shared reader is executor-only;
its lock serializes incremental scans across the three usage read doors.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import os
from pathlib import Path
from stat import S_ISDIR
import threading
import time

from .paths import _home


def _stamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _tokens(value):
    if not isinstance(value, dict):
        return None
    # Cached input and reasoning output are subsets, never additional tokens.
    fields = ("input_tokens", "output_tokens")
    for key in (*fields, "cached_input_tokens", "reasoning_output_tokens", "total_tokens"):
        if key not in value and key not in fields:
            continue
        if type(value.get(key)) is not int or not 0 <= value[key] <= 2**63 - 1:
            return None
    if value.get("cached_input_tokens", 0) > value["input_tokens"]:
        return None
    if value.get("reasoning_output_tokens", 0) > value["output_tokens"]:
        return None
    return tuple(value[key] for key in fields)


def window_bounds(key, bars, now):
    """Use only a matching, current Codex window; otherwise say rolling."""
    seconds, label = {"session": (18000, "5h"), "week": (604800, "7d"),
                      "day": (86400, "24h")}[key]
    if key != "day":
        for bar in bars or []:
            if not isinstance(bar, dict) or bar.get("provider") != "codex":
                continue
            reset = _stamp(bar.get("resets_at"))
            minutes = bar.get("window_minutes")
            duration = bar.get("window_seconds")
            exact_duration = (type(minutes) in (int, float) and minutes == seconds / 60
                              or type(duration) in (int, float) and duration == seconds)
            # Current roster bars have only a rounded label. That alone cannot
            # prove the underlying duration; rolling is the honest fallback.
            if (exact_duration and not bar.get("stale")
                    and reset is not None and now < reset <= now + seconds):
                return reset - seconds, now, f"current {label} window"
    return now - seconds, now, f"rolling {label}"


@dataclass
class _Journal:
    identity: tuple
    offset: int = 0
    size: int = -1
    mtime: int = 0
    fragment: bytes = b""
    skipping: bool = False
    thread: str = ""
    created: float | None = None
    model: str = "unknown model"
    cumulative: tuple | None = None
    epoch: int = 0
    records: deque = field(default_factory=deque)
    notes: set = field(default_factory=set)
    read_failed: bool = False


class CodexSpenders:
    def __init__(self, root=None, *, clock=time.time, byte_budget=16 * 1024 * 1024,
                 file_budget=512 * 1024, line_limit=1024 * 1024,
                 discovery_budget=4096, max_files=4096, max_records=100000):
        self.root = Path(root) if root is not None else _home() / ".codex" / "sessions"
        self.clock = clock
        self.byte_budget = max(1, byte_budget)
        self.file_budget = max(1, file_budget)
        self.line_limit = max(1, line_limit)
        self.discovery_budget = max(1, discovery_budget)
        self.max_files = max(1, max_files)
        self.max_records = max(1, max_records)
        self._lock = threading.Lock()
        self._files = {}
        self._queue = deque()
        self._discovery = None
        self._discovery_complete = False
        self._discovery_limited = False
        self._discovery_error = False
        self._walk_failed = False
        self.bytes_read = 0

    def _paths(self):
        # A resumable bounded walk, newest date directories first. Yield every
        # entry so a directory full of unrelated files still spends the budget.
        for directory, dirs, files in os.walk(self.root, onerror=self._discovery_failed):
            dirs.sort(reverse=True)
            for name in sorted(files, reverse=True):
                yield Path(directory) / name if name.endswith(".jsonl") else None
            for _ in dirs:
                yield None

    def _discovery_failed(self, _error):
        # Retain no paths or exception text in the public coverage note.
        self._walk_failed = True
        self._discovery_error = True

    def _discover(self):
        if self._discovery is None:
            self._walk_failed = False
            self._discovery = self._paths()
            # Once a walk has completed, a bounded refresh does not make the
            # already discovered corpus incomplete again. Newly found files
            # announce their unread bytes independently.
        fresh = []
        for _ in range(self.discovery_budget):
            try:
                path = next(self._discovery)
            except StopIteration:
                self._discovery = None
                self._discovery_complete = True
                # A partial refresh cannot prove that a formerly unreadable
                # subtree recovered. Only a whole healthy walk clears it.
                self._discovery_error = self._walk_failed
                break
            if path is None or path in self._files:
                continue
            if len(self._files) >= self.max_files:
                self._discovery_limited = True
                continue
            try:
                stat = path.stat()
            except OSError as error:
                self._discovery_failed(error)
                continue
            self._files[path] = _Journal((stat.st_dev, stat.st_ino))
            fresh.append((stat.st_mtime_ns, path))
        self._queue.extend(path for _, path in sorted(fresh, reverse=True))

    def _line(self, state, line):
        # Most journal bytes are messages/tool output. Avoid decoding those.
        if not any(tag in line for tag in (b'"session_meta"', b'"turn_context"',
                                            b'"token_count"')):
            return
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError):
            state.notes.add("Some journal records could not be read.")
            state.cumulative = None
            return
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            return
        payload = item["payload"]
        kind = item.get("type")
        if kind == "session_meta":
            if not state.thread:
                thread = payload.get("id") or payload.get("session_id")
                state.thread = thread if isinstance(thread, str) else ""
                state.created = _stamp(payload.get("timestamp"))
            return
        if kind == "turn_context":
            model = payload.get("model")
            state.model = model[:200] if isinstance(model, str) and model else "unknown model"
            return
        if kind != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return  # rate-limit-only notification, no usage claim
        cumulative = _tokens(info.get("total_token_usage"))
        last = _tokens(info.get("last_token_usage"))
        previous = state.cumulative
        state.cumulative = cumulative
        if cumulative is None:
            state.notes.add("Some usage counters could not be verified.")
            return
        if cumulative == previous:
            return  # the same reading can be repeated with a new timestamp
        if previous is not None and any(a < b for a, b in zip(cumulative, previous)):
            state.epoch += 1
            state.notes.add("Counter resets exclude an uncertain reading.")
            return
        timestamp = _stamp(item.get("timestamp"))
        if not state.thread or state.created is None or timestamp is None:
            state.notes.add("Some usage has no verifiable thread or timestamp.")
            return
        if timestamp < state.created:
            return  # copied parent/fork history belongs to its original thread
        if last is None:
            if previous is None:
                state.notes.add("An initial lifetime reading has no turn breakdown.")
                return
            last = tuple(a - b for a, b in zip(cumulative, previous))
        if any(a > b for a, b in zip(last, cumulative)):
            state.notes.add("Some usage counters could not be verified.")
            return
        if not sum(last):
            return
        if state.model == "unknown model":
            state.notes.add("Some recorded tokens have no model name.")
        # Cumulative identity, not wall time, deduplicates files of one thread.
        identity = (state.thread, state.epoch, cumulative)
        state.records.append((timestamp, identity, state.model, *last))

    def _read(self, path, state, budget):
        try:
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if (state.identity != identity or stat.st_size < state.size
                        or stat.st_size < state.offset
                        or (stat.st_size == state.size and state.mtime
                            and stat.st_mtime_ns != state.mtime)):
                    state = self._files[path] = _Journal(identity)
                state.size, state.mtime = stat.st_size, stat.st_mtime_ns
                state.read_failed = False
                if state.offset >= stat.st_size:
                    return 0
                stream.seek(state.offset)
                chunk = stream.read(min(budget, self.file_budget))
                state.offset += len(chunk)
        except OSError:
            state.read_failed = True
            return 0
        pieces = (state.fragment + chunk).split(b"\n")
        state.fragment = pieces.pop()
        for line in pieces:
            if state.skipping:
                state.skipping = False
            elif len(line) > self.line_limit:
                state.notes.add("Oversized journal records were skipped.")
                state.cumulative = None
            else:
                self._line(state, line)
        if len(state.fragment) > self.line_limit:
            state.fragment = b""
            state.skipping = True
            state.cumulative = None
            state.notes.add("Oversized journal records were skipped.")
        return len(chunk)

    def snapshot(self, key="session", bars=None):
        with self._lock:
            self.bytes_read = 0
            now = self.clock()
            start, end, label = window_bounds(key, bars, now)
            out = {"provider": "codex", "available": False, "reason": "",
                   "measurement": "local_token_share", "window_start": start,
                   "window_end": end, "window_label": label, "partial": False,
                   "models": []}
            try:
                root_is_directory = S_ISDIR(self.root.stat().st_mode)
            except (FileNotFoundError, NotADirectoryError):
                root_is_directory = False
            except OSError as error:
                self._discovery_failed(error)
                self._discovery = None
                out.update(partial=True, reason="Local Codex history could not be read.")
                return out
            if not root_is_directory:
                out["reason"] = "No local Codex history on this Mac."
                return out
            self._discover()
            budget = self.byte_budget
            # Round-robin until the byte budget is spent or a whole pass is
            # unchanged. A lone large file gets the remaining budget too.
            idle = 0
            while budget > 0 and self._queue and idle < len(self._queue):
                path = self._queue.popleft()
                try:
                    exists = path.exists()
                except OSError as error:
                    self._discovery_failed(error)
                    self._queue.append(path)
                    idle += 1
                    continue
                if not exists:
                    self._files.pop(path, None)
                    continue
                self._queue.append(path)
                read = self._read(path, self._files[path], budget)
                budget -= read
                idle = 0 if read else idle + 1
            self.bytes_read = self.byte_budget - budget
            notes = set()
            records = []
            for state in self._files.values():
                # Only seven days are requested. Prune as the clock advances.
                state.records = deque(r for r in state.records if r[0] >= now - 604800)
                notes.update(state.notes)
                if state.read_failed:
                    notes.add("Some local journals are unavailable.")
                if state.offset < state.size or state.fragment or state.skipping:
                    notes.add("Local history is still being read.")
                records.extend((r, state) for r in state.records)
            if len(records) > self.max_records:
                records.sort(key=lambda pair: pair[0][0], reverse=True)
                keep = {id(record) for record, _ in records[:self.max_records]}
                for state in self._files.values():
                    state.records = deque(r for r in state.records if id(r) in keep)
                    state.notes.add("Older records exceed the local history limit.")
                records = records[:self.max_records]
                notes.add("Older records exceed the local history limit.")
            if not self._discovery_complete or any(s.size < 0
                                                   for s in self._files.values()):
                notes.add("Local history is still being read.")
            if self._discovery_limited:
                notes.add("Some journals exceed the local history limit.")
            if self._discovery_error:
                notes.add("Some local Codex history could not be read.")
            seen, models = set(), {}
            for (ts, identity, model, inp, output), _ in records:
                if identity in seen or not start <= ts <= end:
                    continue
                seen.add(identity)
                row = models.setdefault(model, {"model": model, "input_tokens": 0,
                                                "output_tokens": 0, "tokens": 0})
                row["input_tokens"] += inp
                row["output_tokens"] += output
                row["tokens"] += inp + output
            total = sum(row["tokens"] for row in models.values())
            for row in models.values():
                row["pct"] = round(100 * row["tokens"] / total, 1) if total else None
            out.update(available=bool(self._files), partial=bool(notes),
                       reason=" ".join(sorted(notes)),
                       models=sorted(models.values(), key=lambda r: (-r["tokens"], r["model"])))
            if not self._files and not notes:
                out["reason"] = "No local Codex history on this Mac."
            elif not models and not notes:
                out["reason"] = "No locally recorded tokens in this period."
            return out


reader = CodexSpenders()
