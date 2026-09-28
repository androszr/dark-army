"""Joining what the agents cost to whether the work was accepted.

Pure functions over dicts — no sqlite, no I/O, no clock — which is
`board_outcomes.py`'s own shape and for its reason: the arithmetic a person
will argue with should be readable and testable without a database.

Two halves arrive already computed and **neither is recomputed here**. The
efficiency half is `HistoryStore.by_agent` / `session_efficiency` over
`history.db`'s `turns`. The effectiveness half is
`board_outcomes.project_summary`, reached through
`BoardStore.outcome_project_report`, which stays the only place acceptance,
rework and wait arithmetic is written. What this file adds is the *join*:
which sessions worked which card, and therefore which spend sits under which
outcome.

The partial rules are `board_outcomes`': an unknown measurement stays
unavailable, an empty denominator is `None` and never `0`, a genuine zero
stays zero, and a figure whose inputs were incomplete is labelled `partial`
rather than presented as a total. A card whose session has no surviving
history row is reported and marked, never dropped — history prunes at 90 days
and the board does not, so an old card with no turns left is an ordinary,
expected state.
"""

from __future__ import annotations

from . import crew


def ledger_who(crew) -> str:
    """One character for a card, so a day column does not count it twice.

    The implementer when that stage was recorded, else the planner, else
    the first character in trail order, else nobody. `crew` is
    `board.parse_crew`'s ``{stage: character}``, order preserved.
    """
    if not isinstance(crew, dict):
        return ""
    for stage in ("bc-implementer", "bc-planner"):
        who = str(crew.get(stage) or "").strip()
        if who:
            return who
    for who in crew.values():
        who = str(who or "").strip()
        if who:
            return who
    return ""


#: Copied onto a session the history store still has. Absent on the rollup
#: stays JSON null — a 0 would draw as a measured nothing.
#:
#: `token_cost_usd` (counted tokens at the publisher's price) and
#: `reported_cost_usd` (the dollar Claude or Grok reported) are two kinds of
#: money and are copied side by side, never summed; the Mac ledger alone
#: draws them. The older keys keep their meaning for the phone.
_SESSION_FIGURES = (
    "model", "measured_cost_usd", "estimated_cost_usd", "output_tokens",
    "input_tokens", "cache_read", "cache_hit_ratio", "duration_ms",
    "token_cost_usd", "token_unpriced_turns", "reported_cost_usd",
    "cache_split_unknown_turns",
    # The token cost split by the provider of the turns priced — not by
    # the run's provider label, which can name Codex for a Claude session.
    "claude_token_cost_usd", "grok_token_cost_usd", "codex_token_cost_usd",
)


def role_of(name) -> str:
    """The crew role an `attr_agent` value names, or `""`.

    `crew.ROLES` is authoritative and this re-derives nothing: a name
    that is not one of its names has no role here, which is the common case
    (a project's own helper, or an agent type from another repository).
    """
    key = str(name or "").strip().lower()
    return key if key in crew.ROLES else ""


def _fold_cost(entries: list) -> tuple:
    """`(cost, unpriced_turns, priced_any)` over per-session rollups.

    `None` for a group that priced nothing at all, exactly as
    `HistoryStore.by_model` does — a `0.0` there would read as free.
    """
    cost, unpriced, priced_any = 0.0, 0, False
    for entry in entries:
        value = entry.get("cost_usd")
        unpriced += int(entry.get("unpriced_turns") or 0)
        if value is not None:
            cost += float(value)
            priced_any = True
    return (cost if priced_any else None), unpriced, priced_any


def join(runs, agent_rows, session_efficiency, project_summary, roster) -> dict:
    """The joined report.

    `runs` are `BoardStore.outcome_run_index` rows; `agent_rows` are
    `HistoryStore.by_agent`'s, already narrowed to those sessions;
    `session_efficiency` is `{session_id: rollup}`; `project_summary` is
    `outcome_project_report(...)["summary"]`, carried through verbatim;
    `roster` is `{card_id: {title, root, accepted, rework_count,
    manual_steps}}` — what the board itself says about each card.
    """
    runs = list(runs or [])
    roster = dict(roster or {})
    efficiency = dict(session_efficiency or {})

    agents = []
    for row in agent_rows or []:
        entry = dict(row)
        entry["role"] = role_of(entry.get("name"))
        agents.append(entry)

    by_card: dict = {}
    for run in runs:
        card_id = str(run.get("card_id") or "")
        if not card_id:
            continue
        card = by_card.setdefault(card_id, {
            "card_id": card_id,
            "root": str(run.get("root") or ""),
            "sessions": [],
        })
        sid = str(run.get("session_id") or "")
        known = sid in efficiency
        session = {
            "provider": str(run.get("provider") or ""),
            "session_id": sid,
            "phase": str(run.get("phase") or ""),
            "bound_at": run.get("bound_at"),
            "known": known,
        }
        rollup = efficiency.get(sid) if known else None
        for key in _SESSION_FIGURES:
            session[key] = None if rollup is None else rollup.get(key)
        card["sessions"].append(session)

    seen_sessions, known_sessions = set(), set()
    cards = []
    for card_id, card in by_card.items():
        stated = roster.get(card_id) or {}
        rollups = []
        for session in card["sessions"]:
            sid = session["session_id"]
            if sid:
                seen_sessions.add(sid)
            rollup = efficiency.get(sid)
            if rollup is not None:
                known_sessions.add(sid)
                rollups.append(rollup)
        cost, unpriced, priced_any = _fold_cost(rollups)
        card["title"] = str(stated.get("title") or "")
        card["who"] = str(stated.get("who") or "")
        if not card["root"]:
            card["root"] = str(stated.get("root") or "")
        # `None`, never `False`, where acceptance was never asked for. The
        # board's acceptance arithmetic is per project and per period, so a
        # report over every project carries no acceptance at all — and "nobody
        # asked" must stay distinguishable from "somebody said no", which a
        # `False` here would quietly erase.
        accepted = stated.get("accepted")
        card["accepted"] = None if accepted is None else bool(accepted)
        rework = stated.get("rework_count")
        card["rework_count"] = None if rework is None else int(rework or 0)
        card["manual_check_outstanding"] = bool(
            str(stated.get("manual_steps") or "").strip())
        card["turns"] = sum(int(r.get("turns") or 0) for r in rollups)
        card["duration_ms"] = sum(int(r.get("duration_ms") or 0) for r in rollups)
        card["cost_usd"] = cost
        card["unpriced_turns"] = unpriced
        card["sessions_with_history"] = len(rollups)
        # Partial where Dark Army cannot see every session's spend: some of this
        # card's work is outside what history still holds, so the cost on the
        # row is a floor, not a total.
        card["partial"] = len(rollups) < len(card["sessions"]) or (
            not priced_any and bool(card["sessions"]))
        cards.append(card)

    cards.sort(key=lambda c: (-(c["cost_usd"] or 0.0), c["card_id"]))

    outstanding = sum(1 for stated in roster.values()
                      if str(stated.get("manual_steps") or "").strip())
    partial = any(card["partial"] for card in cards)

    summary = dict(project_summary or {})
    summary.update({
        "joined_cards": len(cards),
        "joined_sessions": len(seen_sessions),
        "sessions_with_history": len(known_sessions),
        # Named for exactly what it is. `manual_steps` is set by `flag_manual`
        # and emptied by `clear_manual`, so a historical rate is unrecoverable
        # from the store and is deliberately not published: this is the count
        # of cards carrying steps *now*.
        "manual_checks_outstanding": outstanding,
        "partial": partial,
    })

    return {
        "agents": agents,
        "cards": cards,
        "summary": summary,
        "coverage": {
            "cards": len(cards),
            "sessions": len(seen_sessions),
            "sessions_with_history": len(known_sessions),
            "cards_partial": sum(1 for card in cards if card["partial"]),
            "partial": partial,
        },
    }
