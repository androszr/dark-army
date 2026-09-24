"""Pure lifecycle timing arithmetic. No sqlite, no processes, no network.

Card-time, not labour. Confirmed spans are the only elapsed source; unknown
starts, sleep, clock jumps and downtime stay explicit. Rework overlaps the
other three categories and is never added to them.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .board_outcomes import MAX_GAP, sample_delta

CATEGORIES = ("queue", "execution", "review", "rework")
REVIEW_CAUSES = ("review", "manual_check")
BUCKETS = ((0.0, 60.0), (60.0, 300.0), (300.0, 1800.0), (1800.0, 7200.0), (7200.0, None))
QUANTILE_ALGORITHM = "nearest_rank"
UNITS = "seconds"
RETENTION_DAYS = 366
MAX_PERIOD_DAYS = 366
DEFAULT_PERIOD_SECONDS = 30 * 86400
OVERLAP_NOTE = "Rework overlaps the other categories; do not add these rows."
SCHEMA = 1

#: Close dispositions that never enter the completed sample `n`.
CANCELLED = frozenset({"cancelled", "removed", "censored"})
COMPLETED = frozenset({"completed"})
OPEN = frozenset({"open", ""})

GAP_REASONS = (
    "process_boundary",
    "missing_evidence",
    "clock_gap",
    "negative_delta",
    "nonfinite_clock",
    "utc_disagreement",
    "observer_failure",
    "state_changed",
    "retention",
    "impl_unknown",
)


def nearest_rank(values, p):
    """Nearest-rank quantile, index ``max(0, ceil(p*n)-1)``.

    ``None`` when there is no completed sample — never a measured zero.
    """
    if not values:
        return None
    n = len(values)
    idx = max(0, math.ceil(float(p) * n) - 1)
    return sorted(values)[min(idx, n - 1)]


def histogram(values):
    """Fixed-bucket counts. Sums to ``n`` (the length of ``values``)."""
    counts = [0] * len(BUCKETS)
    for value in values:
        for i, (lo, hi) in enumerate(BUCKETS):
            if value >= lo and (hi is None or value < hi):
                counts[i] += 1
                break
    return counts


def period_bounds(start, end, *, now=None, as_of=None):
    """Validate ``[from,to)`` and freeze ``as_of = min(to, now)``.

    Raises ``ValueError`` with the API's own words.
    """
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValueError("report needs a finite period of at most 366 days")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        raise ValueError("report needs a finite period of at most 366 days")
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("report needs a finite period of at most 366 days")
    if end - start > MAX_PERIOD_DAYS * 86400:
        raise ValueError("report needs a finite period of at most 366 days")
    snapshot = now if now is not None else end
    if as_of is None:
        as_of = min(end, snapshot)
    else:
        if isinstance(as_of, bool) or not isinstance(as_of, (int, float)) or not math.isfinite(as_of):
            raise ValueError("as_of must be a finite UTC timestamp")
        as_of = min(end, as_of)
    return float(start), float(end), float(as_of)


def clip_span(utc_start, utc_end, start, as_of):
    """Clip a confirmed ``[utc_start,utc_end)`` onto ``[start,as_of)``.

    Returns seconds, or 0 when the span does not overlap the window.
    """
    if utc_start is None or utc_end is None:
        return 0.0
    lo = max(float(utc_start), float(start))
    hi = min(float(utc_end), float(as_of))
    return max(0.0, hi - lo)


def replay_close_time(boundaries, episode_id, as_of):
    """When the episode was closed as of ``as_of``, or None if open then."""
    ended = None
    for row in boundaries:
        if int(row.get("episode_id") or 0) != int(episode_id):
            continue
        ts = row.get("ts")
        if ts is None or ts > as_of:
            continue
        kind = str(row.get("kind") or "")
        disp = str(row.get("disposition") or "")
        if disp in COMPLETED or kind.endswith("_completed") or kind in ("completed", "closed"):
            ended = ts
        elif kind.endswith("_reopened") or kind == "reopened":
            ended = None
        elif disp in CANCELLED or kind.endswith("_cancelled") or kind.endswith("_removed"):
            ended = ts
    return ended


def replay_disposition(boundaries, episode_id, as_of, episode=None):
    """Episode disposition at ``as_of``, ignoring later reopen/cancel/complete.

    ``boundaries`` are already ordered by ``(ts, id)``. An episode with no
    start before the cutoff is absent (``None``). Stored ``ended_at`` /
    ``disposition`` are a last resort when a close boundary was pruned.
    """
    started = False
    state = "open"
    for row in boundaries:
        if int(row.get("episode_id") or 0) != int(episode_id):
            continue
        ts = row.get("ts")
        if ts is None or ts > as_of:
            continue
        kind = str(row.get("kind") or "")
        disp = str(row.get("disposition") or "")
        if kind.endswith("_started") or kind == "started":
            started = True
            state = "open"
        elif disp in COMPLETED or kind.endswith("_completed") or kind in ("completed", "closed"):
            state = "completed"
        elif disp in CANCELLED or kind.endswith("_cancelled") or kind.endswith("_removed") \
                or kind in CANCELLED:
            state = disp or kind.rsplit("_", 1)[-1]
        elif kind.endswith("_reopened") or kind == "reopened":
            state = "open"
    if not started:
        if episode and episode.get("started_at") is not None \
                and episode["started_at"] <= as_of:
            started = True
            ended_at = episode.get("ended_at")
            stored = str(episode.get("disposition") or "")
            if ended_at is not None and ended_at <= as_of and stored in COMPLETED | CANCELLED:
                state = stored if stored in CANCELLED else "completed"
            else:
                state = "open"
        else:
            return None
    elif state == "open" and episode:
        ended_at = episode.get("ended_at")
        stored = str(episode.get("disposition") or "")
        if ended_at is not None and ended_at <= as_of and stored in COMPLETED | CANCELLED:
            state = stored if stored in CANCELLED else "completed"
    return state


def coverage_at_cutoff(episode, spans, as_of, *, category="", ended=None):
    """Complete at ``as_of`` from spans known then — never the live coverage flag.

    Queue direct-start (ended == started, or provenance ``direct``) may have
    no spans. Every other category needs a confirmed span that started before
    the cutoff. A gap at or before ``as_of`` makes the sample partial.
    Callers must pass **unfiltered** spans, gap rows included.
    """
    started = episode.get("started_at")
    if started is None or started > as_of:
        return False
    if episode.get("left_censored"):
        return False
    eid = int(episode["id"])
    card_id = episode.get("card_id")
    clip_end = min(float(ended), float(as_of)) if ended is not None else float(as_of)
    gaps = False
    confirmed = False
    for span in spans:
        utc_start = span.get("utc_start")
        if utc_start is None or float(utc_start) >= as_of:
            continue
        span_eid = span.get("episode_id")
        eid_match = span_eid is not None and int(span_eid) == eid
        card_gap = (
            span_eid in (None, "")
            and str(span.get("coverage") or "") == "gap"
            and span.get("card_id") == card_id
            and float(started) <= float(utc_start) < clip_end
        )
        if not eid_match and not card_gap:
            continue
        if str(span.get("coverage") or "") == "gap":
            gaps = True
            continue
        confirmed = True
    if gaps:
        return False
    if category == "queue" and (
            (ended is not None and ended == started)
            or str(episode.get("provenance") or "") == "direct"):
        return True
    return confirmed


def empty_summary(category):
    return {
        "category": category,
        "observed_seconds": 0.0,
        "n": 0,
        "N": 0,
        "cards": 0,
        "p50": None,
        "p90": None,
        "min": None,
        "max": None,
        "buckets": [0] * len(BUCKETS),
        "open": 0,
        "cancelled": 0,
        "partial": 0,
        "unknown": 0,
        "left_censored": 0,
        "coverage": None,
        "carry_in": {"open": 0, "completed": 0, "cancelled": 0, "partial": 0,
                     "observed_seconds": 0.0},
        "quantile_algorithm": QUANTILE_ALGORITHM,
        "units": UNITS,
        "sample_count": 0,
    }


def _union_seconds(spans, start, as_of):
    """Elapsed union of clipped spans. Overlap counts once."""
    intervals = []
    for span in spans:
        lo = max(float(span["utc_start"]), float(start))
        hi = min(float(span["utc_end"]), float(as_of))
        if hi > lo:
            intervals.append((lo, hi))
    intervals.sort()
    total = 0.0
    cur_lo = cur_hi = None
    for lo, hi in intervals:
        if cur_lo is None:
            cur_lo, cur_hi = lo, hi
            continue
        if lo <= cur_hi:
            cur_hi = max(cur_hi, hi)
        else:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
    if cur_lo is not None:
        total += cur_hi - cur_lo
    return total


def category_summary(category, episodes, spans, boundaries, start, as_of):
    """One of the four rows: observed seconds, distribution, coverage."""
    out = empty_summary(category)
    kinds = {category}
    if category == "review":
        kinds = {"review", "manual_check"}
    matching = [e for e in episodes if str(e.get("kind") or "") in kinds]
    # Distribution uses submitted-result episodes for review; manual_check
    # is a named cause, not a second row in `n`.
    dist_kinds = {"review"} if category == "review" else kinds
    dist_eps = [e for e in matching if str(e.get("kind") or "") in dist_kinds]

    cat_spans = [s for s in spans if str(s.get("category") or "") in kinds
                 and str(s.get("coverage") or "") != "gap"]
    if category == "review":
        out["observed_seconds"] = _union_seconds(cat_spans, start, as_of)
        by_cause = {}
        for cause in REVIEW_CAUSES:
            by_cause[cause] = _union_seconds(
                [s for s in cat_spans if str(s.get("cause") or s.get("category")) == cause],
                start, as_of)
        out["cause_seconds"] = by_cause
    else:
        out["observed_seconds"] = sum(
            clip_span(s.get("utc_start"), s.get("utc_end"), start, as_of)
            for s in cat_spans)

    durations = []
    cards = set()
    carry = out["carry_in"]
    for episode in dist_eps:
        started = episode.get("started_at")
        disp = replay_disposition(boundaries, episode["id"], as_of, episode)
        ended = replay_close_time(boundaries, episode["id"], as_of)
        if ended is None and episode.get("ended_at") is not None \
                and episode["ended_at"] <= as_of \
                and str(episode.get("disposition") or "") in COMPLETED | CANCELLED:
            ended = episode["ended_at"]
        if disp is None and started is not None and started <= as_of:
            disp = str(episode.get("disposition") or "open")
            if ended is None or ended > as_of:
                disp = "open"
        if started is None:
            out["left_censored"] += 1
            if disp in COMPLETED:
                out["partial"] += 1
            elif disp in CANCELLED:
                out["cancelled"] += 1
            elif disp in OPEN or disp is None:
                out["open"] += 1
            continue
        if started > as_of:
            continue
        if started < start:
            # Carry-in: never the main distribution.
            if disp in COMPLETED and ended is not None and ended <= as_of:
                carry["completed"] += 1
            elif disp in CANCELLED:
                carry["cancelled"] += 1
            elif disp in OPEN or disp is None:
                carry["open"] += 1
            else:
                carry["partial"] += 1
            eid = episode["id"]
            carry["observed_seconds"] += sum(
                clip_span(s.get("utc_start"), s.get("utc_end"), start, as_of)
                for s in cat_spans if int(s.get("episode_id") or 0) == int(eid)
                and float(s.get("utc_start") or 0) < as_of)
            continue
        # Known start in the period → denominator N.
        out["N"] += 1
        cards.add(episode.get("card_id"))
        closed_by_cutoff = disp in COMPLETED and ended is not None and ended <= as_of
        eligible = coverage_at_cutoff(
            episode, spans, as_of, category=category, ended=ended)
        if closed_by_cutoff and eligible:
            eid = episode["id"]
            clip_end = min(float(ended), float(as_of))
            duration = sum(
                clip_span(s.get("utc_start"), s.get("utc_end"), started, clip_end)
                for s in cat_spans
                if int(s.get("episode_id") or 0) == int(eid)
                and str(s.get("coverage") or "") != "gap"
                and float(s.get("utc_start") or 0) < as_of)
            # Direct-start queue zero has no spans; duration is 0 and eligible.
            if category == "queue" and duration == 0 and ended == started:
                duration = 0.0
            durations.append(duration)
            out["n"] += 1
        elif disp in CANCELLED:
            out["cancelled"] += 1
        elif disp in OPEN or disp is None or not closed_by_cutoff:
            out["open"] += 1
        else:
            out["partial"] += 1
            out["unknown"] += 1

    out["cards"] = len(cards)
    out["sample_count"] = out["n"]
    if out["N"]:
        out["coverage"] = out["n"] / out["N"]
    if durations:
        out["p50"] = nearest_rank(durations, 0.5)
        out["p90"] = nearest_rank(durations, 0.9)
        out["min"] = min(durations)
        out["max"] = max(durations)
        out["buckets"] = histogram(durations)
    return out


def card_observed(spans, card_id, category, start, as_of):
    kinds = {category}
    if category == "review":
        kinds = {"review", "manual_check"}
        return _union_seconds(
            [s for s in spans if s.get("card_id") == card_id
             and str(s.get("category") or "") in kinds
             and str(s.get("coverage") or "") != "gap"],
            start, as_of)
    return sum(
        clip_span(s.get("utc_start"), s.get("utc_end"), start, as_of)
        for s in spans
        if s.get("card_id") == card_id
        and str(s.get("category") or "") == category
        and str(s.get("coverage") or "") != "gap")


def open_age(episode, as_of):
    started = episode.get("started_at")
    if started is None or str(episode.get("disposition") or "") not in OPEN:
        return None
    return max(0.0, float(as_of) - float(started))


def build_report(*, root="", card_id="", start, end, as_of, now, cards, episodes,
                 spans, boundaries, attempts, generation, tracking_since=None,
                 retention=None, deleted_ids=(), limit=25, offset=0, sort="queue",
                 available=True, reason="", measurements_available=True):
    """The on-the-wire report. Summaries first; then one paged collection."""
    start, end, as_of = period_bounds(start, end, now=now, as_of=as_of)
    sort = sort if sort in CATEGORIES else "queue"
    summaries = {
        category: category_summary(category, episodes, spans, boundaries, start, as_of)
        for category in CATEGORIES
    }
    report = {
        "supported": True,
        "available": bool(available),
        "measurements_available": bool(measurements_available),
        "reason": reason or "",
        "schema": SCHEMA,
        "from": start,
        "to": end,
        "as_of": as_of,
        "generation": generation,
        "root": root,
        "card_id": card_id,
        "tracking_since": tracking_since,
        "retention_days": RETENTION_DAYS,
        "retention": retention or {},
        "quantile_algorithm": QUANTILE_ALGORITHM,
        "units": UNITS,
        "histogram_buckets": [[lo, hi] for lo, hi in BUCKETS],
        "overlap_note": OVERLAP_NOTE,
        "summaries": summaries,
        "sort": sort,
        "offset": offset,
        "limit": limit,
    }
    if not available:
        report["cards"] = []
        report["episodes"] = []
        report["next_offset"] = None
        return report

    if card_id:
        rows = _card_timeline(card_id, episodes, spans, boundaries, attempts,
                              cards, start, as_of, deleted_ids)
        report["episodes"] = rows[offset:offset + limit]
        report["next_offset"] = offset + limit if len(rows) > offset + limit else None
        report["cards"] = []
        meta = cards.get(card_id) or {}
        report["title"] = meta.get("title") or ""
        report["live"] = card_id not in deleted_ids and not meta.get("deleted_at")
        if not report["live"]:
            report["removed_note"] = "No longer on the board"
    else:
        rows = _delayed_cards(cards, spans, episodes, start, as_of, sort, deleted_ids)
        report["cards"] = rows[offset:offset + limit]
        report["next_offset"] = offset + limit if len(rows) > offset + limit else None
        report["episodes"] = []
    return report


def _delayed_cards(cards, spans, episodes, start, as_of, sort, deleted_ids):
    rows = []
    for cid, meta in cards.items():
        observed = {cat: card_observed(spans, cid, cat, start, as_of) for cat in CATEGORIES}
        open_eps = [e for e in episodes
                    if e.get("card_id") == cid
                    and str(e.get("kind") or "") == sort
                    and str(e.get("disposition") or "") in OPEN]
        age = None
        if open_eps:
            ages = [open_age(e, as_of) for e in open_eps]
            age = max((a for a in ages if a is not None), default=None)
        rows.append({
            "card_id": cid,
            "title": meta.get("title") or "",
            "root": meta.get("root") or "",
            "live": cid not in deleted_ids and not meta.get("deleted_at"),
            "observed_seconds": observed,
            "open_age": age,
            "period_observed": observed.get(sort, 0.0),
        })
    rows.sort(key=lambda r: (-float(r["period_observed"] or 0.0), r["card_id"]))
    return rows


def _card_timeline(card_id, episodes, spans, boundaries, attempts, cards,
                   start, as_of, deleted_ids):
    rows = []
    for episode in episodes:
        if episode.get("card_id") != card_id:
            continue
        disp = replay_disposition(boundaries, episode["id"], as_of) or episode.get("disposition")
        eid = episode["id"]
        observed = sum(
            clip_span(s.get("utc_start"), s.get("utc_end"), start, as_of)
            for s in spans if int(s.get("episode_id") or 0) == int(eid)
            and str(s.get("coverage") or "") != "gap")
        wall = None
        if episode.get("started_at") is not None:
            end_ts = episode.get("ended_at") if disp not in OPEN else as_of
            if end_ts is not None:
                wall = max(0.0, float(end_ts) - float(episode["started_at"]))
        gap_count = sum(1 for s in spans
                        if int(s.get("episode_id") or 0) == int(eid)
                        and str(s.get("coverage") or "") == "gap")
        attempt = None
        if episode.get("attempt_id"):
            attempt = next((a for a in attempts if a["id"] == episode["attempt_id"]), None)
        rows.append({
            "episode_id": eid,
            "kind": episode.get("kind") or "",
            "cause": episode.get("cause") or episode.get("kind") or "",
            "disposition": disp,
            "started_at": episode.get("started_at"),
            "ended_at": episode.get("ended_at") if disp not in OPEN else None,
            "observed_seconds": observed,
            "elapsed_seconds": wall,
            "coverage": episode.get("coverage") or "",
            "gap_count": gap_count,
            "gap_reasons": episode.get("gap_reasons") or "",
            "provenance": episode.get("provenance") or "",
            "attempt_id": episode.get("attempt_id"),
            "provider": (attempt or {}).get("provider") or "",
            "session_id": (attempt or {}).get("session_id") or "",
            "overlay": str(episode.get("kind") or "") == "rework",
        })
    rows.sort(key=lambda r: (r["started_at"] is None, r["started_at"] or 0, r["episode_id"]))
    return rows


def utc_day(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).date().isoformat()


# Re-export for observers that already import sample_delta from outcomes.
__all__ = [
    "BUCKETS", "CATEGORIES", "MAX_GAP", "OVERLAP_NOTE", "QUANTILE_ALGORITHM",
    "RETENTION_DAYS", "SCHEMA", "UNITS", "build_report", "card_observed",
    "category_summary", "clip_span", "histogram", "nearest_rank",
    "period_bounds", "replay_disposition", "sample_delta", "utc_day",
]
