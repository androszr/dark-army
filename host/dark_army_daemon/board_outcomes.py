"""Pure, conservative outcome measurements. No pricing or transcript reads.

Waits are card time, not labour. Only adjacent positive observations establish
elapsed time; the union is counted once even when several causes overlap.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

OBJECTIVE_LIMITS = {"beneficiary": 200, "intended_benefit": 1000,
                    "success_criterion": 1000, "outcome_check_on": 10}
COVERAGES = ("complete_since_tracking", "partial", "unknown")
CAUSES = ("question", "permission", "manual_check", "review")
MAX_GAP = 30.0
# Dark Army's own words about money, composed once and drawn verbatim by both
# clients — `_queue_reason` / `queuedLine(autostart:)`'s discipline. The
# observed figure is every reading Dark Army watched, complete or not, so the note
# is not optional decoration: it is what stops the number reading as a bill.
OBSERVED_COST_NOTE = ("Observed on this Mac; it may undercount helper, subagent "
                      "and child sessions.")


def objective_refusal(fields: dict) -> str:
    for key, cap in OBJECTIVE_LIMITS.items():
        if key not in fields:
            continue
        value = fields[key]
        if not isinstance(value, str) or len(value) > cap:
            return f"{key} must be text of at most {cap} characters"
        if key == "outcome_check_on" and value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                return "later check must be a date in YYYY-MM-DD form"
    return ""


def sample_delta(previous, monotonic: float, utc: float, causes: set):
    """(seconds, confirmed causes, gap). The later sample confirms an interval.

    A cleared cause confirms the last interval too; a missing sample is passed
    as None and confirms nothing. UTC jumps/sleep disagreeing with monotonic
    time are gaps, never elapsed human waits.
    """
    if previous is None:
        return 0.0, set(), False
    pm, pu, pc = previous
    delta = monotonic - pm
    wall = utc - pu
    if delta < 0 or delta > MAX_GAP or wall < 0 or abs(wall - delta) > 2:
        return 0.0, set(), True
    if causes is None:
        return 0.0, set(), True
    return (delta if pc else 0.0), set(pc), False


def day_parts(start: float, seconds: float):
    """Split a confirmed interval into UTC days without changing its total."""
    end = start + seconds
    while start < end:
        boundary = (math.floor(start / 86400) + 1) * 86400
        stop = min(boundary, end)
        yield datetime.fromtimestamp(start, timezone.utc).date().isoformat(), stop - start
        start = stop


def cost_reading(amount, currency, source, *, complete=False):
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        return None
    if not isinstance(source, str) or not source.strip():
        return None
    return {"amount": float(amount), "currency": currency.upper(),
            "source": source[:100], "coverage": "complete" if complete else "partial"}


def lifecycle_cost(runs: list, partial: bool = False) -> dict:
    amounts = {}
    complete = bool(runs) and not partial
    currencies = set()
    for run in runs:
        value = run.get("amount")
        currency = run.get("currency")
        if value is None or not currency:
            complete = False
            continue
        amounts[currency] = amounts.get(currency, 0.0) + value
        currencies.add(currency)
        complete &= run.get("cost_coverage") == "complete"
    complete &= len(currencies) == 1
    return {"totals": amounts, "coverage": "complete" if complete else
            ("partial" if amounts else "unknown")}


def cost_reason(accepted: int, observed: dict) -> str:
    """Why there is no observed cost per accepted outcome, in plain words.

    Empty while a figure exists — the number says it itself. The two reasons
    are deliberately different sentences: nothing accepted yet is not the same
    fact as accepted work Dark Army never saw a price for.
    """
    if observed:
        return ""
    if not accepted:
        return "Cost per accepted outcome: no outcome has been accepted in this period yet."
    return "Cost per accepted outcome: no cost was recorded for the accepted outcomes."


def project_summary(rows: list, submissions: list, reworks: list,
                    start: float, end: float) -> dict:
    accepted = [r for r in rows if r.get("accepted") and
                r.get("first_accepted_at") is not None and
                start <= r["first_accepted_at"] < end]
    submitted = {e["card_id"] for e in submissions if start <= e["ts"] < end}
    reworked = {e["card_id"] for e in reworks if start <= e["ts"] < end
                and e.get("submission_at") is not None
                and start <= e["submission_at"] < end} & submitted
    carry = {e["card_id"] for e in reworks if start <= e["ts"] < end
             and e.get("submission_at") is not None and e["submission_at"] < start}
    cost = {}
    observed = {}
    partial_totals = {}
    coverage = {key: 0 for key in COVERAGES}
    for row in accepted:
        coverage[row.get("wait_coverage", "unknown")] += 1
        measurement = row.get("cost", {})
        for currency, amount in measurement.get("totals", {}).items():
            # Every reading is money Dark Army watched being spent, whatever its
            # coverage. The complete-only figure keeps its own narrower ring.
            seen = observed.setdefault(currency, {"total": 0.0, "covered": 0})
            seen["total"] += amount
            seen["covered"] += 1
            if measurement.get("coverage") == "complete":
                value = cost.setdefault(currency, {"total": 0.0, "covered": 0})
                value["total"] += amount
                value["covered"] += 1
            else:
                partial_totals[currency] = partial_totals.get(currency, 0.0) + amount
    for bucket in (cost, observed):
        for value in bucket.values():
            if value["covered"]:
                value["per_accepted_outcome"] = value["total"] / value["covered"]
            value["outcomes"] = len(accepted)
    # Current spend on work nobody has accepted yet: a separate line, and
    # never a denominator. The predicate is `awaiting_acceptance`'s own.
    awaiting_rows = [r for r in rows if bool(r.get("submission_at"))
                     and not r.get("accepted") and not r.get("rework_open")]
    awaiting_totals = {}
    awaiting_covered = 0
    for row in awaiting_rows:
        totals = row.get("cost", {}).get("totals", {})
        if totals:
            awaiting_covered += 1
        for currency, amount in totals.items():
            awaiting_totals[currency] = awaiting_totals.get(currency, 0.0) + amount
    return {"accepted_outcomes": len(accepted),
            "awaiting_acceptance": len(awaiting_rows),
            "submitted_cards": len(submitted), "reworked_cards": len(reworked),
            "rework_rate": len(reworked) / len(submitted) if submitted else None,
            "carry_in_rework": len(carry),
            "observed_card_hours": sum(r.get("wait_seconds", 0) for r in accepted) / 3600,
            "wait_coverage": coverage, "cost_per_outcome": cost,
            "observed_cost_per_outcome": observed,
            "awaiting_cost_totals": awaiting_totals,
            "awaiting_cost_covered": awaiting_covered,
            "cost_reason": cost_reason(len(accepted), observed),
            "observed_cost_note": OBSERVED_COST_NOTE,
            "partial_cost_totals": partial_totals}
