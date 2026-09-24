"""A card's timeline: the moments in its life, in order, with the time
between neighbours — composed from stamps the daemon already keeps for
good and never from a guess.

`plans/2026-09-13-card-timeline.md`. Pure composition: no SQLite, no
daemon import. `compose(card, facts, log_rows, now)` takes the card dict
the store returns, the facts dict `LifecycleStoreMixin.card_timeline_facts`
reads under one lock, and the bounded event log's recent rows, and returns
the `timeline` dict both card screens draw verbatim. Every step is one of
`KINDS`, with `at` **`None` and `observed` False** where the daemon never
witnessed the moment — the client draws "not observed" and no number, and
nothing here interpolates (`docs/lifecycle-timing.md`'s rule).

Two sources, kept apart by `durable`: card stamps, the lifecycle ledger and
the outcome ledger are the record; the event log (500 lines, a day) is a
best-effort extra for permission asks and start refusals, folded per
attempt and said to be short-lived by `recent_only_note`.
"""
from __future__ import annotations

from typing import Optional

from .event_log import RETENTION_SECONDS

#: The words a client draws for a moment the daemon did not witness.
NOT_OBSERVED = "not observed"
#: The words on a gap whose left neighbour predates the card's ledger row.
BEFORE_TRACKING = "before tracking began"
#: Said once on a card older than the diary's retention, so a missing ask
#: is read as forgotten rather than as never asked.
RECENT_ONLY_NOTE = "Permission asks older than a day are not kept."
#: The list is bounded; over it the oldest durable steps go first.
MAX_TIMELINE_STEPS = 200

#: `(kind, rank, label)` in canonical order. The rank breaks a tie between
#: two steps at the same second and places an unobserved step among its
#: neighbours; the label is drawn verbatim on both clients.
KINDS: tuple = (
    ("created", 0, "Written down"),
    ("refine_started", 1, "Planning started"),
    ("refine_ended", 2, "Planning ended without a plan"),
    ("plan_attached", 3, "Plan attached"),
    ("plan_approved", 4, "Plan approved"),
    ("queued", 5, "Queued"),
    ("unqueued", 6, "Taken out of the queue"),
    ("started", 7, "Started"),
    ("picked_up", 8, "Assistant picked it up"),
    ("permission_asks", 9, "{n} permission ask(s)"),
    ("start_failed", 10, "Start refused"),
    ("start_abandoned", 11, "Start abandoned"),
    ("ended", 12, "Assistant finished"),
    ("restarted", 13, "Assistant came back"),
    ("manual_flagged", 14, "Flagged for your check"),
    ("manual_cleared", 15, "Check marked done"),
    ("submitted", 16, "Submitted as done"),
    ("moved_done", 17, "Moved to Done"),
    ("reopened", 18, "Reopened"),
    ("revision_requested", 19, "Revision requested"),
    ("reviewed", 20, "Marked reviewed"),
    ("accepted", 21, "Accepted"),
)

_RANK = {kind: rank for kind, rank, _label in KINDS}
_LABEL = {kind: label for kind, _rank, label in KINDS}

#: Ledger boundary kinds that are person-visible moments, and the step each
#: becomes. `launch_*`, `execution_*`, `review_*`, `rework_*`,
#: `queue_completed` and `attempt_removed` are episode bookkeeping the
#: moments here already cover, and are not steps.
_BOUNDARY_STEPS = {
    "refine_started": "refine_started",
    "refine_ended": "refine_ended",
    "plan_attached": "plan_attached",
    "queue_cancelled": "unqueued",
    "attempt_started": "started",
    "session_bound": "picked_up",
    "attempt_completed": "ended",
    "attempt_cancelled": "start_abandoned",
    "attempt_reopened": "restarted",
    "manual_check_started": "manual_flagged",
    "manual_check_completed": "manual_cleared",
}

#: Outcome-ledger event kinds and the step each becomes. A revision request
#: writes both `revision_requested` and `decision_revision` at one instant,
#: so the two fold into one step (`_dedupe`). `revision_note` is not a step.
_OUTCOME_STEPS = {
    "submitted": "submitted",
    "reopened": "reopened",
    "revision_requested": "revision_requested",
    "decision_revision": "revision_requested",
    "accepted": "accepted",
}


def elapsed_text(seconds) -> str:
    """`"12s"`, `"5m"`, `"2h 10m"`, `"3d 4h"`; `""` for `None` or a negative.

    Days above 24 h — the one place that departs from `event_log._duration`,
    which stops at hours. Pinned equal to the Swift `elapsed` by the fixture
    table in `test_card_timeline.py`.
    """
    if seconds is None:
        return ""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    minutes -= hours * 60
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days = hours // 24
    hours -= days * 24
    return f"{days}d {hours}h" if hours else f"{days}d"


def _stamp(value) -> Optional[float]:
    """A card stamp as a float, or `None` for absent, zero or unreadable —
    there is no zero time that means anything."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _step(kind: str, at, *, note: str = "", durable: bool = True,
          label: str = "") -> dict:
    at = _stamp(at)
    return {
        "kind": kind,
        "label": label or _LABEL.get(kind, kind),
        "at": at,
        "observed": at is not None,
        "note": str(note or ""),
        "durable": bool(durable),
    }


def _asks_label(n: int) -> str:
    return f"{n} permission ask" if n == 1 else f"{n} permission asks"


def _durable_steps(card: dict, facts: dict) -> list:
    steps = []
    created = _stamp(card.get("created_at"))
    if created is not None:
        steps.append(_step("created", created))
    boundaries = list(facts.get("boundaries") or [])
    seen_kinds = set()
    attempts = 0
    for row in boundaries:
        kind = str(row.get("kind") or "")
        seen_kinds.add(kind)
        if kind == "queue_started":
            if str(row.get("provenance") or "") != "direct":
                steps.append(_step("queued", row.get("ts")))
            continue
        step_kind = _BOUNDARY_STEPS.get(kind)
        if step_kind is None:
            continue
        note = ""
        if step_kind == "started":
            # Numbered by order here: `lifecycle_attempts.sequence` is the
            # boundary counter at the attempt's open, not an ordinal.
            attempts += 1
            if attempts >= 2:
                note = f"attempt {attempts}"
        steps.append(_step(step_kind, row.get("ts"), note=note))
    # A stamp is a fact, not a guess — but only where no boundary of that
    # kind exists at all (a pre-schema-21 card). `dispatched_at` and
    # `session_ended_at` are the *latest* attempt's, so mixing them with
    # boundaries would draw a restarted card's first start at its last.
    if "plan_attached" not in seen_kinds and str(card.get("plan_path") or ""):
        steps.append(_step("plan_attached", None))
    if "attempt_started" not in seen_kinds:
        dispatched = _stamp(card.get("dispatched_at"))
        if dispatched is not None:
            steps.append(_step("started", dispatched))
    if "attempt_completed" not in seen_kinds:
        ended = _stamp(card.get("session_ended_at"))
        if ended is not None:
            steps.append(_step("ended", ended))
    approved = _stamp(card.get("plan_approved_at"))
    if approved is not None:
        steps.append(_step("plan_approved", approved))
    submitted = False
    for row in facts.get("outcome_events") or []:
        step_kind = _OUTCOME_STEPS.get(str(row.get("kind") or ""))
        if step_kind is None:
            continue
        if step_kind == "submitted":
            submitted = True
        steps.append(_step(step_kind, row.get("ts")))
    if not submitted:
        done = _stamp(card.get("done_at"))
        if done is not None:
            steps.append(_step("moved_done", done))
    reviewed = _stamp(card.get("reviewed_at"))
    if reviewed is not None:
        steps.append(_step("reviewed", reviewed))
    return _dedupe(steps)


def _dedupe(steps: list) -> list:
    """Two records of one moment — the outcome ledger's paired revision
    rows — are one step. Keyed on kind and second; the first wins."""
    seen = set()
    out = []
    for step in steps:
        key = (step["kind"], None if step["at"] is None else round(step["at"], 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(step)
    return out


def _attempt_windows(steps: list) -> list:
    """The observed start times, ascending — an ask is folded into the
    attempt whose start is the latest one at or before it."""
    return sorted(s["at"] for s in steps
                  if s["kind"] == "started" and s["at"] is not None)


def _log_steps(card: dict, facts: dict, log_rows, durable: list) -> list:
    card_id = str(card.get("id") or "")
    session_ids = {str(s) for s in (facts.get("session_ids") or []) if s}
    for key in ("session_id", "refine_session_id"):
        sid = str(card.get(key) or "")
        if sid:
            session_ids.add(sid)
    starts = _attempt_windows(durable)
    asks: dict = {}
    out = []
    for row in log_rows or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "")
        ts = _stamp(row.get("ts"))
        if ts is None:
            continue
        if kind == "permission_ask":
            if str(row.get("session_id") or "") not in session_ids:
                continue
            window = None
            for start in starts:
                if start <= ts:
                    window = start
            bucket = asks.setdefault(window, {"n": 0, "first": ts, "tool": ""})
            bucket["n"] += 1
            bucket["first"] = min(bucket["first"], ts)
            detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
            tool = str(detail.get("tool") or "")
            if tool:
                bucket["tool"] = tool
        elif kind == "card_dispatch_failed":
            if card_id and str(row.get("card_id") or "") == card_id:
                out.append(_step("start_failed", ts, note=str(row.get("text") or ""),
                                 durable=False))
    for bucket in asks.values():
        note = f"{bucket['n']} asks"
        if bucket["tool"]:
            note += f" · {bucket['tool']}"
        out.append(_step("permission_asks", bucket["first"], note=note,
                         durable=False, label=_asks_label(bucket["n"])))
    return out


def _order(steps: list) -> list:
    """Observed steps by `(at, rank)`; a step with no time sits at its rank
    among its neighbours — after the last observed step of a lower rank —
    never at the end."""
    observed = sorted((s for s in steps if s["at"] is not None),
                      key=lambda s: (s["at"], _RANK.get(s["kind"], 99)))
    unobserved = sorted((s for s in steps if s["at"] is None),
                        key=lambda s: _RANK.get(s["kind"], 99))
    out = list(observed)
    for step in unobserved:
        rank = _RANK.get(step["kind"], 99)
        place = 0
        for i, other in enumerate(out):
            if other["at"] is not None and _RANK.get(other["kind"], 99) < rank:
                place = i + 1
        out.insert(place, step)
    return out


def _gaps(steps: list, facts: dict) -> None:
    left_censored = bool(facts.get("left_censored"))
    tracking_since = _stamp(facts.get("tracking_since"))
    previous = None
    for step in steps:
        step["since_previous_seconds"] = None
        step["elapsed"] = ""
        step["gap"] = ""
        if previous is not None:
            if step["at"] is None or previous["at"] is None:
                step["gap"] = NOT_OBSERVED
            elif (left_censored and tracking_since is not None
                  and step["at"] < tracking_since):
                step["gap"] = BEFORE_TRACKING
            else:
                since = max(0.0, float(step["at"]) - float(previous["at"]))
                step["since_previous_seconds"] = since
                step["elapsed"] = elapsed_text(since)
        previous = step


def _truncate(steps: list) -> tuple:
    if len(steps) <= MAX_TIMELINE_STEPS:
        return steps, False
    kept = list(steps)
    # Oldest durable first; the list is already in time order.
    for step in steps:
        if len(kept) <= MAX_TIMELINE_STEPS:
            break
        if step["durable"]:
            kept.remove(step)
    while len(kept) > MAX_TIMELINE_STEPS:
        kept.pop(0)
    return kept, True


def open_state(card: dict, steps: list, now) -> Optional[dict]:
    """Where the card is *now* and since when — `{"kind", "label", "since"}`
    or `None` where nothing is waiting. `since` may be `None`; the client
    then draws the label with "not observed". One table, top rung wins."""
    def last(kind):
        hits = [s["at"] for s in steps if s["kind"] == kind and s["at"] is not None]
        return hits[-1] if hits else None

    column = str(card.get("column_name") or "")
    link = str(card.get("link_state") or "")
    refine = str(card.get("refine_state") or "")
    if refine in ("dispatching", "live"):
        return {"kind": "planning", "label": "Being planned",
                "since": last("refine_started")}
    if str(card.get("queue_state") or "") == "queued":
        return {"kind": "queued", "label": "Waiting in the queue",
                "since": _stamp(card.get("queued_at"))}
    if link == "dispatching":
        return {"kind": "starting", "label": "Starting",
                "since": _stamp(card.get("dispatched_at"))}
    if link == "live":
        return {"kind": "working", "label": "Assistant working",
                "since": last("picked_up") or _stamp(card.get("dispatched_at"))}
    if str(card.get("manual_steps") or "").strip() and column != "done":
        return {"kind": "manual_check", "label": "Waiting for your check",
                "since": last("manual_flagged")}
    if column == "in_progress" and link == "ended":
        return {"kind": "ended", "label": "Finished, waiting on you",
                "since": _stamp(card.get("session_ended_at"))}
    # Only a *declared* close awaits a review: `mark_reviewed` refuses a
    # card with no `closed_by` (a hand drag into Done), the same rule
    # `_done_scope_token` and the panel's `awaitsReview` read.
    if (column == "done" and _stamp(card.get("reviewed_at")) is None
            and str(card.get("closed_by") or "")):
        return {"kind": "review", "label": "Waiting for your review",
                "since": _stamp(card.get("done_at"))}
    if (column == "done" and str(card.get("outcome_status") or "") != "accepted"
            and str(card.get("intended_benefit") or "").strip()):
        return {"kind": "acceptance", "label": "Waiting for acceptance",
                "since": _stamp(card.get("reviewed_at"))}
    if column == "backlog" and str(card.get("plan_path") or ""):
        return {"kind": "planned", "label": "Planned, waiting to be started",
                "since": last("plan_attached")}
    return None


def compose(card: dict, facts, log_rows, now) -> dict:
    """The `timeline` dict for one card. `facts` may be `None` (no ledger
    row at all): the card's own stamps still draw."""
    facts = dict(facts or {})
    card = dict(card or {})
    durable = _durable_steps(card, facts)
    steps = _order(durable + _log_steps(card, facts, log_rows, durable))
    steps, truncated = _truncate(steps)
    _gaps(steps, facts)
    created = _stamp(card.get("created_at"))
    older_than_diary = (created is not None
                        and float(now) - created > RETENTION_SECONDS)
    tracking_since = _stamp(facts.get("tracking_since"))
    out = {
        "available": True,
        "reason": "",
        "generated_at": float(now),
        "tracking_since": tracking_since,
        "left_censored": bool(facts.get("left_censored")),
        "recent_only_note": RECENT_ONLY_NOTE if older_than_diary else "",
        "truncated": truncated,
        "steps": steps,
        "open": open_state(card, steps, now),
    }
    return out


def unavailable(reason: str) -> dict:
    """The shape a client decodes when the timeline could not be read."""
    return {"available": False, "reason": str(reason or ""),
            "steps": [], "open": None}
