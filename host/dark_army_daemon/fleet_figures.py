"""The live fleet's cost and tokens — one composition, published once.

Pure: stdlib, `run_health` and `run_figures` only. The phone sums nothing.
Cost is the measured USD on the live rows, to the cent, or `None` when no
live row has measured one — drawn "cost unknown", never a made-up `0.0`
(`run_figures.COST_UNKNOWN`). Tokens are `run_health.tokens_of` (fresh
input plus output, never cache reads) quantised by `quantise_tokens_k`.

The rows are the idle KPI's own recipe: the live buckets, plus a
`finished` row that is still `alive` (a session quiet past the grace, which
`counts.idle` still counts). One row per `session_id`, the first source
winning.
"""

from __future__ import annotations

import threading
from collections import deque

from . import run_figures, run_health

#: The buckets a live row is drawn from, in source order. `finished` is
#: consulted separately, and only where `alive is True`.
LIVE_ROW_BUCKETS = ("running", "waiting", "sleeping")

#: Dark Army's words for a cost nobody measured. Re-exported so a fleet line and
#: a card line cannot drift.
COST_UNKNOWN = run_figures.COST_UNKNOWN


def _measured_cost(row: dict):
    """A row's `metrics.cost_usd` when it is a real number, else None.

    A bool is an int in Python and is not a measurement; a string is not
    one either. Non-finite values are not a measurement.
    """
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return None
    value = metrics.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def live_rows(snapshot: dict) -> list[dict]:
    """The rows the fleet figures describe, one per session id.

    Live buckets first, then `finished` rows with `alive is True` (the
    idle count's own rows — a quiet session lives there, not in
    `sleeping`). A non-dict is skipped. The same `session_id` later is
    not a second row.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    seen: set[str] = set()
    out: list[dict] = []

    def take(row) -> None:
        if not isinstance(row, dict):
            return
        sid = str(row.get("session_id") or "")
        # A row with no id cannot be the same session as another; it still
        # counts, under a key nothing else uses.
        key = sid or f"\0{id(row)}"
        if key in seen:
            return
        seen.add(key)
        out.append(row)

    for bucket in LIVE_ROW_BUCKETS:
        rows = snapshot.get(bucket) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            take(row)
    finished = snapshot.get("finished") or []
    if isinstance(finished, list):
        for row in finished:
            if isinstance(row, dict) and row.get("alive") is True:
                take(row)
    return out


#: The burn rate's window: spend over the last hour.
BURN_WINDOW_SECONDS = 3600.0
#: How long the meter must have watched before it says a rate. A rate over
#: a minute's watching is one turn multiplied by sixty — noise, not a pace.
BURN_MIN_SPAN_SECONDS = 600.0


class BurnMeter:
    """The fleet's spend per hour, from what the live rows *grew by*.

    Not total ÷ age: a session idle for three days would read cheap. Each
    observation compares every live row's measured cost and token reading
    with the one last seen for that `session_id`; only growth counts. A
    session seen for the first time is a baseline, never a jump (a
    restart must not book a whole session's lifetime into the
    first minute), and a reading that fell (a reset) rebaselines. Rates are
    the growth inside the last `BURN_WINDOW_SECONDS`, scaled to an hour
    while the meter has watched less than that, and `None` until it has
    watched `BURN_MIN_SPAN_SECONDS`. Observing the same snapshot twice adds
    nothing, so every composer may observe. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, tuple] = {}   # sid -> (cost, tokens, seen_at)
        self._growth: deque = deque()       # (at, cost_delta, tokens_delta)
        self._started: float | None = None
        self._cost_seen = False
        self._tokens_seen = False

    def observe(self, snapshot: dict, now: float) -> None:
        rows = live_rows(snapshot)
        with self._lock:
            if self._started is None:
                self._started = now
            for row in rows:
                sid = str(row.get("session_id") or "")
                if not sid:
                    continue
                cost = _measured_cost(row)
                tokens = run_health.tokens_of(row.get("stats"))
                self._cost_seen = self._cost_seen or cost is not None
                self._tokens_seen = self._tokens_seen or tokens is not None
                prev = self._last.get(sid)
                d_cost = d_tokens = 0
                if prev is not None:
                    p_cost, p_tokens, _ = prev
                    if cost is not None and p_cost is not None and cost > p_cost:
                        d_cost = cost - p_cost
                    if tokens is not None and p_tokens is not None and tokens > p_tokens:
                        d_tokens = tokens - p_tokens
                    # An absent reading keeps the last one it had.
                    cost = p_cost if cost is None else cost
                    tokens = p_tokens if tokens is None else tokens
                if d_cost or d_tokens:
                    self._growth.append((now, d_cost, d_tokens))
                self._last[sid] = (cost, tokens, now)
            horizon = now - BURN_WINDOW_SECONDS
            while self._growth and self._growth[0][0] < horizon:
                self._growth.popleft()
            # A session unseen for a whole window is forgotten; if it comes
            # back it is a baseline again.
            for sid in [k for k, v in self._last.items() if v[2] < horizon]:
                del self._last[sid]

    def rates(self, now: float) -> dict:
        """`{"cost_usd_hour", "tokens_k_hour"}`, each `None` until said.

        Cost per hour to the dime, tokens per hour on `quantise_tokens_k`.
        A quiet hour is a real `0`, not `None`.
        """
        with self._lock:
            started = self._started
            if started is None or now - started < BURN_MIN_SPAN_SECONDS:
                return {"cost_usd_hour": None, "tokens_k_hour": None}
            span = min(BURN_WINDOW_SECONDS, now - started)
            horizon = now - BURN_WINDOW_SECONDS
            cost = sum(c for at, c, _ in self._growth if at >= horizon)
            tokens = sum(t for at, _, t in self._growth if at >= horizon)
            scale = 3600.0 / span
            return {
                "cost_usd_hour": (round(cost * scale, 1)
                                  if self._cost_seen else None),
                "tokens_k_hour": (run_health.quantise_tokens_k(int(tokens * scale))
                                  if self._tokens_seen else None),
            }


def compose(snapshot: dict, meter: BurnMeter | None = None,
            now: float | None = None) -> dict:
    """`{"cost_usd", "cost_measured", "cost_rows", "tokens_k"}` for the fleet.

    `cost_usd` is the sum of measured costs, to the cent, or `None` when
    `cost_measured` is 0. `tokens_k` is the quantised sum of token readings,
    or `None` when no live row has one. `cost_rows` is every live row,
    measured or not. With a `meter` (and `now`), the snapshot is observed
    and `cost_usd_hour` / `tokens_k_hour` join the dict — `BurnMeter.rates`.
    """
    extra: dict = {}
    if meter is not None and now is not None:
        meter.observe(snapshot, now)
        extra = meter.rates(now)
    rows = live_rows(snapshot)
    measured = [cost for row in rows if (cost := _measured_cost(row)) is not None]
    token_sum = 0
    any_tokens = False
    for row in rows:
        reading = run_health.tokens_of(row.get("stats"))
        if reading is None:
            continue
        any_tokens = True
        token_sum += reading
    return {
        "cost_usd": round(sum(measured), 2) if measured else None,
        "cost_measured": len(measured),
        "cost_rows": len(rows),
        "tokens_k": run_health.quantise_tokens_k(token_sum) if any_tokens else None,
        **extra,
    }


__all__ = ["LIVE_ROW_BUCKETS", "COST_UNKNOWN", "BURN_WINDOW_SECONDS",
           "BURN_MIN_SPAN_SECONDS", "BurnMeter", "live_rows", "compose"]
