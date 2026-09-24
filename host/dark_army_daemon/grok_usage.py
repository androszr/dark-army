"""Read-only rollup of a Grok session's ``updates.jsonl``.

Grok appends one ACP ``turn_completed`` usage record per user turn. The
record is that turn's ledger — tokens, model calls, cost in integer ticks —
not a running total. A session's bill is the *sum* of those records.
Mid-turn updates also stamp ``params._meta.totalTokens``, which is the
live context fill; a first-turn session has that and no usage record yet.

1 USD = 10¹⁰ ticks. Cost is stamped only when the server reported a complete
figure; a turn that lacks ``costUsdTicks`` (or carries ``cost_is_partial`` /
``usage_is_incomplete``) means the whole session's cost is unknown. We never
sum a subset and present it as the bill.

Read incrementally: the file is already ~1 MB after an hour and its mtime
moves on every turn, so a re-read from byte 0 per snapshot is the cost
``session_stats.StatsCache`` was written to avoid. Nothing here writes to
``~/.grok/``.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# Exact integer ticks. Float dollars cannot be summed without drift.
USD_TICKS = 10_000_000_000


def iso_epoch(value) -> Optional[float]:
    """Epoch seconds from a number or an ISO-8601 string, else None.

    The one ISO→epoch parser for every Grok reader — billing, roster, events
    each grew their own copy of exactly this. Bool is not a timestamp."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None

# grok-4.6's window when signals.json has not told us otherwise.
DEFAULT_CONTEXT_WINDOW = 500_000


@dataclass
class GrokUsage:
    """Rolled-up usage for one session's ``updates.jsonl``."""

    cost_usd: Optional[float] = None       # None when any turn was unpriced
    cost_is_partial: bool = False
    input_tokens: int = 0                  # ACP: full prompt sum, cache included
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    model_calls: int = 0
    api_duration_ms: int = 0
    turns: int = 0                         # usage records, i.e. user turns
    models: list[str] = field(default_factory=list)
    last_input_tokens: int = 0             # last turn's prompt sum (all calls)
    last_model_calls: int = 0
    last_total_tokens: int = 0             # latest _meta.totalTokens (live fill)
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None

    @property
    def last_prompt_tokens(self) -> int:
        """Approximate the last model call's prompt.

        ACP ``inputTokens`` sums every call in the turn. The last call is the
        current window; we do not have a per-call split, so the mean is the
        honest figure. A single-call turn is exact.
        """
        if self.last_input_tokens <= 0:
            return 0
        return self.last_input_tokens // max(self.last_model_calls, 1)


@dataclass
class _UsageAccum:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    model_calls: int = 0
    api_duration_ms: int = 0
    turns: int = 0
    ticks: int = 0
    saw_unpriced: bool = False
    saw_priced: bool = False
    models: list[str] = field(default_factory=list)
    _model_seen: set[str] = field(default_factory=set)
    last_input_tokens: int = 0
    last_model_calls: int = 0
    last_total_tokens: int = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    offset: int = 0
    folded: int = 0                        # usage records consumed this read


def _int(value, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _looks_like_usage(obj: dict) -> bool:
    return any(
        key in obj
        for key in (
            "inputTokens", "outputTokens", "costUsdTicks", "totalTokens",
            "input_tokens", "output_tokens", "total_cost_usd_ticks",
        )
    )


def find_usage(obj) -> Optional[tuple[dict, dict]]:
    """Return ``(usage, parent)`` for the first usage-shaped object, or None.

    The usage object sits under ``params.update`` today and under ``_meta`` in
    an ACP ``end_turn`` message. Walk by key rather than a fixed path so a
    shape change degrades to "no cost" instead of an exception.
    """
    if isinstance(obj, dict):
        usage = obj.get("usage")
        if isinstance(usage, dict) and _looks_like_usage(usage):
            return usage, obj
        for value in obj.values():
            found = find_usage(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_usage(value)
            if found is not None:
                return found
    return None


def _flagged_incomplete(usage: dict, parent: dict) -> bool:
    for src in (usage, parent):
        for key in ("cost_is_partial", "usage_is_incomplete",
                    "costIsPartial", "usageIsIncomplete"):
            if src.get(key) is True:
                return True
    return False


def _ticks_of(usage: dict, parent: dict) -> Optional[int]:
    """Integer ticks, or None when the turn is unpriced.

    Absence means unknown, never free. Flags win over a number: Grok omits
    the floats when they are set, but if a future shape leaves both we still
    refuse to treat the figure as complete.
    """
    if _flagged_incomplete(usage, parent):
        return None
    for key in ("costUsdTicks", "total_cost_usd_ticks"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _ts_of(obj: dict) -> Optional[float]:
    value = obj.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _fold_usage(acc: _UsageAccum, usage: dict, parent: dict, ts: Optional[float]) -> None:
    acc.turns += 1
    acc.folded += 1
    acc.input_tokens += _int(usage.get("inputTokens") or usage.get("input_tokens"))
    acc.output_tokens += _int(usage.get("outputTokens") or usage.get("output_tokens"))
    acc.cache_read_tokens += _int(
        usage.get("cachedReadTokens") or usage.get("cache_read_input_tokens")
    )
    acc.cache_creation_tokens += _int(
        usage.get("cacheCreationTokens") or usage.get("cache_creation_input_tokens")
    )
    acc.reasoning_tokens += _int(
        usage.get("reasoningTokens") or usage.get("reasoning_tokens")
    )
    acc.model_calls += _int(usage.get("modelCalls") or usage.get("numTurns"))
    acc.api_duration_ms += _int(
        usage.get("apiDurationMs") or usage.get("api_duration_ms")
    )
    acc.last_input_tokens = _int(
        usage.get("inputTokens") or usage.get("input_tokens")
    )
    acc.last_model_calls = _int(usage.get("modelCalls") or usage.get("numTurns"))

    ticks = _ticks_of(usage, parent)
    if ticks is None:
        acc.saw_unpriced = True
    else:
        acc.saw_priced = True
        acc.ticks += ticks

    models = usage.get("modelUsage")
    if isinstance(models, dict):
        for name in models:
            if isinstance(name, str) and name and name not in acc._model_seen:
                acc._model_seen.add(name)
                acc.models.append(name)

    if ts is not None:
        if acc.first_ts is None or ts < acc.first_ts:
            acc.first_ts = ts
        if acc.last_ts is None or ts > acc.last_ts:
            acc.last_ts = ts


def _finalize(acc: _UsageAccum) -> GrokUsage:
    # Any unpriced turn poisons the total — a partial sum wearing a dollar
    # sign is the failure mode Grok's own docs call out.
    if acc.saw_unpriced or not acc.saw_priced:
        cost: Optional[float] = None
    else:
        cost = acc.ticks / USD_TICKS
    return GrokUsage(
        cost_usd=cost,
        cost_is_partial=acc.saw_unpriced,
        input_tokens=acc.input_tokens,
        output_tokens=acc.output_tokens,
        cache_read_tokens=acc.cache_read_tokens,
        cache_creation_tokens=acc.cache_creation_tokens,
        reasoning_tokens=acc.reasoning_tokens,
        model_calls=acc.model_calls,
        api_duration_ms=acc.api_duration_ms,
        turns=acc.turns,
        models=list(acc.models),
        last_input_tokens=acc.last_input_tokens,
        last_model_calls=acc.last_model_calls,
        last_total_tokens=acc.last_total_tokens,
        first_ts=acc.first_ts,
        last_ts=acc.last_ts,
    )


def tail_is_complete(tail: bytes) -> bool:
    """Whether trailing bytes with no newline are nonetheless a whole record.

    Same contract as ``session_stats._tail_is_complete``: a live file is being
    appended to while we read, and a half-written last line must not be
    consumed. A tail that already parses as JSON is a finished record still
    waiting for its newline.
    """
    tail = tail.strip()
    if not tail:
        return False
    try:
        json.loads(tail)
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def read_jsonl_delta(
    path: Path,
    offset: int,
    fold: Callable[[str], None],
    hold_partial: bool = True,
) -> int:
    """Fold complete lines appended since ``offset``. Returns the new offset."""
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return offset
    if hold_partial:
        nl = chunk.rfind(b"\n")
        tail = chunk[nl + 1:]
        if tail and not tail_is_complete(tail):
            if nl < 0:
                return offset
            chunk = chunk[: nl + 1]
    offset += len(chunk)
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        fold(line)
    return offset


def _session_id_of(obj: dict) -> str:
    params = obj.get("params")
    if isinstance(params, dict):
        for key in ("sessionId", "session_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _model_of(usage: dict) -> str:
    """First modelUsage key, with Grok's `-build` suffix stripped.

    Live rows already stamp `grok-4.6` from summary.json; keeping the suffix
    here would make one session look like two models in the history table.
    """
    models = usage.get("modelUsage")
    if isinstance(models, dict):
        for name in models:
            if isinstance(name, str) and name:
                return name[:-6] if name.endswith("-build") else name
    return ""


def parse_turn_record(obj: dict) -> Optional[dict]:
    """One usage-bearing update as a history turn, or None.

    ``cost_usd`` is None when the turn was unpriced — the caller must not
    treat that as zero. ``message_id`` is stable across re-reads of the same
    record (session + timestamp), which is what makes INSERT OR IGNORE safe.
    """
    if not isinstance(obj, dict):
        return None
    found = find_usage(obj)
    if found is None:
        return None
    usage, parent = found
    ts = _ts_of(obj)
    if ts is None:
        return None
    ticks = _ticks_of(usage, parent)
    session_id = _session_id_of(obj)
    return {
        "session_id": session_id,
        "ts": ts,
        "message_id": f"grok:{session_id}:{int(ts)}" if session_id else "",
        "model": _model_of(usage),
        "input_tokens": _int(usage.get("inputTokens") or usage.get("input_tokens")),
        "output_tokens": _int(usage.get("outputTokens") or usage.get("output_tokens")),
        "cache_read": _int(
            usage.get("cachedReadTokens") or usage.get("cache_read_input_tokens")
        ),
        "cache_creation": _int(
            usage.get("cacheCreationTokens") or usage.get("cache_creation_input_tokens")
        ),
        "duration_ms": _int(
            usage.get("apiDurationMs") or usage.get("api_duration_ms")
        ) or None,
        "cost_usd": (ticks / USD_TICKS) if ticks is not None else None,
        "provider": "grok",
    }


def _meta_total_tokens(obj: dict) -> int:
    """Live context fill from ``params._meta.totalTokens``.

    Mid-turn updates carry this on every thought / tool chunk. It is Grok's
    own window reading — not ``usage.totalTokens``, which is the sum of
    every model call in a completed turn and routinely exceeds the window.
    """
    containers: list[dict] = [obj]
    params = obj.get("params")
    if isinstance(params, dict):
        containers.append(params)
        update = params.get("update")
        if isinstance(update, dict):
            containers.append(update)
    for container in containers:
        meta = container.get("_meta")
        if not isinstance(meta, dict):
            continue
        value = meta.get("totalTokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


def _fold_line(line: str, acc: _UsageAccum) -> None:
    # Cheap reject before json.loads: most lines are tool_call payloads.
    # ``totalTokens`` is the live context fill on _meta; ``usage`` is the
    # turn-end ledger. Either one is worth a parse.
    if '"usage"' not in line and '"totalTokens"' not in line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    tokens = _meta_total_tokens(obj)
    if tokens > 0:
        acc.last_total_tokens = tokens
    found = find_usage(obj)
    if found is None:
        return
    usage, parent = found
    _fold_usage(acc, usage, parent, _ts_of(obj))


class DeltaCache:
    """Memoises a JSONL file's rollup by mtime, reading only the appended delta.

    The one incremental reader behind ``UsageCache`` / ``EventsCache`` /
    ``ChatCache``, which used to be three line-for-line copies of this loop.
    Parametrised on the accumulator: ``accum_factory`` mints one (it must carry
    ``offset`` and ``folded`` ints), ``fold(line, acc)`` consumes one complete
    line, ``finalize(acc)`` turns the accumulator into the public result.

    Bounded LRU: the daemon sees every session that scrolls past and an
    unbounded dict here would grow one entry per session forever. A file that
    shrank below the recorded offset is re-read whole — the accumulated state
    is about a file that no longer exists under that name.

    The hit key is ``(mtime, size)``, not mtime alone. Grok keeps
    ``chat_history.jsonl`` (and the sibling ledgers) open and appends
    without always bumping mtime; a live ``ask_user_question`` then sat
    on disk while every snapshot reused the previous rollup, so Dark
    Army never drew the interview. Size growth with a frozen mtime is
    still a delta.
    """

    def __init__(self, accum_factory: Callable[[], Any],
                 fold: Callable[[str, Any], None],
                 finalize: Callable[[Any], Any],
                 max_entries: int = 512) -> None:
        self._accum_factory = accum_factory
        self._fold = fold
        self._finalize = finalize
        self._cache: "OrderedDict[str, tuple[float, int, Any]]" = OrderedDict()
        self._max_entries = max_entries
        self.last_folded: int = 0          # records consumed by the last get()

    def get(self, path: str):
        self.last_folded = 0
        if not path:
            return self._finalize(self._accum_factory())
        try:
            st = os.stat(path)
        except OSError:
            return self._finalize(self._accum_factory())

        acc = None
        hit = self._cache.get(path)
        if hit is not None:
            cached_mtime, cached_size, cached_acc = hit
            if cached_mtime == st.st_mtime and cached_size == st.st_size:
                self._cache.move_to_end(path)
                return self._finalize(cached_acc)
            acc = (self._accum_factory() if st.st_size < cached_acc.offset
                   else cached_acc)
        if acc is None:
            acc = self._accum_factory()

        acc.folded = 0
        try:
            acc.offset = read_jsonl_delta(Path(path), acc.offset,
                                          lambda line: self._fold(line, acc))
        except OSError:
            self.last_folded = acc.folded
            return self._finalize(acc)

        self.last_folded = acc.folded
        self._cache[path] = (st.st_mtime, st.st_size, acc)
        self._cache.move_to_end(path)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return self._finalize(acc)


class UsageCache(DeltaCache):
    """Memoises ``updates.jsonl`` parsing by mtime, reading only the delta."""

    def __init__(self, max_entries: int = 512) -> None:
        super().__init__(_UsageAccum, _fold_line, _finalize, max_entries)

    def get(self, path: str) -> GrokUsage:
        return super().get(path)


def parse_updates(path: str) -> GrokUsage:
    """One-shot read. Tests and anything that does not poll."""
    return UsageCache().get(path)
