"""A few minutes of each session's recent numbers, so a rule can say *at this rate*.

Every metric this app holds is either an instant reading (context 62%, cost
$1.20) or a lifetime average (`output_tokens_per_sec`, `api_duration_ms`). Both
answer "what is this session like"; neither answers "what is happening now",
which is the question behind every interesting alert. `signals.py` says so in
its own header and deliberately implements no trend rule rather than fake one
from an average that stopped moving an hour ago.

This is the missing state, and it is deliberately tiny: a bounded ring of
`(t, ctx_pct, cost_usd, output_tokens)` per session — a few hundred bytes each,
in memory, forgotten with the session. Nothing is persisted; `history.db`
already keeps a minute-resolution sample for the record, and this is for the
next fifteen minutes rather than for last Tuesday.

**Slopes are refused more often than they are given.** A rate computed from two
readings a second apart, or from a series that spans ten seconds, is noise with
a decimal point — and the whole purpose here is a number a person will act on.
So `trend()` returns `None` for every field it cannot stand behind, and callers
render nothing rather than a confident wrong answer. That is the same rule
`_num` follows in `signals.py`: absent is never zero.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

# One sample per session per this many seconds. The statusline ticks on every
# assistant message — several times a minute on a busy agent — and a series
# sampled that finely says nothing a coarser one does not.
MIN_INTERVAL = 10.0

# Ring depth. With MIN_INTERVAL that is ~15 minutes, which is the horizon the
# runway rule reasons over; older readings describe a phase of the session that
# has already ended.
CAPACITY = 90

# A slope needs both enough points and enough time behind it. Two readings
# thirty seconds apart across a single tool call is not a trend.
MIN_SAMPLES = 3
MIN_SPAN_SECONDS = 60.0

# Ignore a series whose own end is stale: a session that stopped reporting five
# minutes ago has no *current* rate, and extrapolating its last one is how a
# quiet session gets accused of being about to blow its context.
MAX_AGE_SECONDS = 180.0


class SampleRing:
    """Recent readings per session, and the rates you can derive from them."""

    def __init__(self, capacity: int = CAPACITY, min_interval: float = MIN_INTERVAL):
        self.capacity = capacity
        self.min_interval = min_interval
        self._rows: dict[str, deque] = {}

    # ── writing ──────────────────────────────────────────────────────────────

    def add(self, session_id: str, now: float, metrics: dict) -> bool:
        """Record one reading. False when it was thrown away as too soon.

        Takes the whole metrics dict rather than three arguments so the caller
        is a single line at the one place statusline payloads land, and so a
        payload missing a field simply carries None through — a session whose
        cost is unreported must not enter the series as $0.
        """
        if not session_id or not isinstance(metrics, dict):
            return False
        row = self._rows.get(session_id)
        if row is None:
            row = self._rows[session_id] = deque(maxlen=self.capacity)
        elif row and now - row[-1][0] < self.min_interval:
            return False
        row.append((
            now,
            _num(metrics.get("ctx_used_pct")),
            _num(metrics.get("cost_usd")),
            # `ctx_output_tokens`, which is what the statusline actually calls
            # it — `output_tokens` lives on `stats`, is a lifetime total from the
            # transcript, and would have sampled None forever.
            _num(metrics.get("ctx_output_tokens")),
        ))
        return True

    def forget(self, session_id: str) -> None:
        self._rows.pop(session_id, None)

    def keep_only(self, session_ids) -> None:
        """Drop every session not in `session_ids`.

        Called from the same sweep that forgets a session elsewhere: this is the
        only structure in the daemon keyed by session id that nothing else
        prunes, and a ring per session that ended is a slow leak rather than a
        visible bug.
        """
        alive = set(session_ids)
        for sid in [s for s in self._rows if s not in alive]:
            del self._rows[sid]

    def __len__(self) -> int:
        return len(self._rows)

    # ── reading ──────────────────────────────────────────────────────────────

    def trend(self, session_id: str, now: float) -> dict:
        """Rates for one session, in the units a sentence would use.

        Every value is `None` unless the series can support it. `samples` and
        `span_seconds` are always reported, because "we have no answer yet" and
        "we have an answer of zero" have to be tellable apart by whoever renders
        this.
        """
        row = self._rows.get(session_id)
        if not row:
            return {"samples": 0, "span_seconds": 0.0}
        rows = list(row)
        span = rows[-1][0] - rows[0][0]
        out = {"samples": len(rows), "span_seconds": round(span, 1)}
        if len(rows) < MIN_SAMPLES or span < MIN_SPAN_SECONDS:
            return out
        if now - rows[-1][0] > MAX_AGE_SECONDS:
            # The series has stopped. Its last slope described a session that is
            # no longer doing whatever produced it.
            return out

        ctx_per_sec = _slope([(t, v) for t, v, _c, _o in rows])
        cost_per_sec = _slope([(t, v) for t, _x, v, _o in rows])
        out_per_sec = _slope([(t, v) for t, _x, _c, v in rows])

        if ctx_per_sec is not None:
            out["ctx_pct_per_min"] = round(ctx_per_sec * 60.0, 3)
        if cost_per_sec is not None:
            out["cost_usd_per_hour"] = round(cost_per_sec * 3600.0, 4)
        if out_per_sec is not None:
            out["output_tokens_per_min"] = round(out_per_sec * 60.0, 1)

        latest_ctx = _latest([(t, v) for t, v, _c, _o in rows])
        if ctx_per_sec is not None and ctx_per_sec > 0 and latest_ctx is not None:
            remaining = max(0.0, 100.0 - latest_ctx)
            out["ctx_runway_seconds"] = round(remaining / ctx_per_sec, 1)
        return out


def _num(value) -> Optional[float]:
    """Same rule as `signals._num`: a missing reading is None, never zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _latest(pairs) -> Optional[float]:
    for _t, value in reversed(pairs):
        if value is not None:
            return value
    return None


def _slope(pairs) -> Optional[float]:
    """Units per second, by least squares. None when there is nothing to fit.

    Least squares rather than (last − first) / Δt: the context percentage drops
    by a third the moment a session compacts, and one endpoint landing either
    side of that would otherwise report a rate that never happened. A fit over
    the whole ring is dragged by a compaction rather than defined by it — and if
    the drop dominates, the slope comes out negative, which reads as "no runway
    to report" instead of an invented one.
    """
    points = [(t, v) for t, v in pairs if v is not None]
    if len(points) < MIN_SAMPLES:
        return None
    t0 = points[0][0]
    n = float(len(points))
    xs = [t - t0 for t, _v in points]
    ys = [v for _t, v in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0:
        return None
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return num / denom


__all__ = ["SampleRing", "MIN_INTERVAL", "CAPACITY", "MIN_SAMPLES",
           "MIN_SPAN_SECONDS", "MAX_AGE_SECONDS"]
