"""Per-card cost and working time — the composition and the drift key.

Pure: no SQLite, no daemon import, and no reading of the per-session trend
ring, the transcript roll-up or the price table — `test_run_figures.py` pins
that none of those modules is so much as named here. The two ledgers in
`board.db` are the only sources — cost from `outcome_runs` through
`board_outcomes.lifecycle_cost`, working time from `lifecycle_spans`
`category='execution'` — and this module only turns their raw parts into
the one object that rides the snapshot as `run_figures`. A second count of
either would be wrong by construction
(`plans/2026-09-13-card-cost-and-time.md`, *Risks*).

Granularity is the whole point of the constants: the daemon buys a board
frame when the figures move (`_run_figures_drifted`), so a figure that moved
on every observation pass would buy one every four seconds per live card.
Working time is published to the **minute**, cost to the **cent**, context to
the **whole percent** — the cadence a person reads them at.
"""
from __future__ import annotations

from typing import Optional

# Working time is published floored to this many seconds. One frame per
# live card per minute is the bound this sets on `_run_figures_drifted`.
ACTIVE_QUANTUM_SECONDS = 60

# Dark Army's own words for a cost it could not measure — drawn verbatim by both
# clients, never a made-up `$0`. Codex publishes no measured cost, so a
# Codex card is the standing case.
COST_UNKNOWN = "cost unknown"

COVERAGES = ("complete", "partial", "unknown")


def compose(*, cost: dict, seconds: float, ticking: bool,
            ctx: Optional[float], attempts: int) -> dict:
    """The `run_figures` object for one card, from the raw parts.

    `cost` is `board_outcomes.lifecycle_cost`'s result — `{"totals":
    {currency: amount}, "coverage": ...}`. `cost_usd` is the USD total
    rounded to cents, or `None` where the coverage is `unknown` or there is
    no USD total: a card whose cost Dark Army could not read says so in words,
    never `0.0`. `active_seconds` is floored to the minute, or `None` where
    `seconds` is `None` — no execution span at all, which is not the same
    fact as under a minute of one (a refined-but-never-started card has a
    run row and no working time, and its line carries no time part);
    `ctx_percent` is a whole number or `None`.
    """
    coverage = str((cost or {}).get("coverage") or "unknown")
    if coverage not in COVERAGES:
        coverage = "unknown"
    totals = (cost or {}).get("totals") or {}
    usd = totals.get("USD") if isinstance(totals, dict) else None
    cost_usd: Optional[float]
    if coverage == "unknown" or usd is None or isinstance(usd, bool):
        cost_usd = None
    else:
        try:
            cost_usd = round(float(usd), 2)
        except (TypeError, ValueError):
            cost_usd = None
    if cost_usd is None and coverage != "unknown":
        # A coverage without a USD figure behind it is not one a reader can
        # act on; say "unknown" rather than draw a note beside nothing.
        coverage = "unknown"
    active: Optional[int]
    if seconds is None or isinstance(seconds, bool):
        active = None
    else:
        try:
            whole = max(0, int(float(seconds)))
        except (TypeError, ValueError):
            whole = 0
        active = (whole // ACTIVE_QUANTUM_SECONDS) * ACTIVE_QUANTUM_SECONDS
    ctx_percent: Optional[int]
    if ctx is None or isinstance(ctx, bool):
        ctx_percent = None
    else:
        try:
            ctx_percent = max(0, min(100, int(float(ctx))))
        except (TypeError, ValueError):
            ctx_percent = None
    try:
        count = max(0, int(attempts))
    except (TypeError, ValueError):
        count = 0
    return {
        "cost_usd": cost_usd,
        "cost_coverage": coverage,
        "active_seconds": active,
        "active_ticking": bool(ticking),
        "ctx_percent": ctx_percent,
        "attempts": count,
    }


# The drift term reads cost at the dime and context at this many points,
# coarser than the cent and the percent the line publishes: a working
# Claude session moves both on nearly every ~4 s observation pass, and a
# frame per cent would be a frame per pass. The minute is the bound the
# line is read at; these keep the other two terms under it.
DRIFT_COST_QUANTUM_USD = 0.10
DRIFT_CTX_QUANTUM_PERCENT = 5


def drift_key(figures: dict) -> tuple:
    """What `_run_figures_drifted` compares — coarser than what is drawn.

    Minute, dime, five points of context, coverage and attempts: two
    composes thirty seconds apart with a three-cent, one-percent move
    produce the same key, and the same key buys no frame. `active_ticking`
    is not a term — nothing draws it — and the cent and the percent still
    ride the frame the minute buys.
    """
    f = figures or {}
    cost = f.get("cost_usd")
    dime = None if cost is None else int(round(float(cost) / DRIFT_COST_QUANTUM_USD))
    ctx = f.get("ctx_percent")
    step = None if ctx is None else int(ctx) // DRIFT_CTX_QUANTUM_PERCENT
    return (dime, f.get("cost_coverage"), f.get("active_seconds"),
            step, f.get("attempts"))
