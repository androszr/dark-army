"""The Bearings digest: four fixed sections, composed once on the daemon.

Pure. Stdlib plus `live_activity` and `inbox_ack`. No I/O, no clock of its
own (`now` is a parameter), nothing imported from `daemon`. Mission Control
and the phone read the same body; neither composes it.

Four sections, always in `SECTIONS` order, each always present: an empty
one carries its sentence rather than disappearing. A digest is a complete
snapshot, never a delta. Needs your call admits only actionable entries
(the Inbox's five wire kinds plus a closed card awaiting review).

Worst case `MAX_ITEMS` × 4 × `MAX_WORDS` is well under `CARD_SYNC_MAX_BYTES`
(300_000); there is no second cap here.
"""

from __future__ import annotations

from . import inbox_ack, live_activity

SECTIONS = (
    ("needs_your_call", "Needs your call", "Nobody needs your call."),
    ("recently_landed", "Recently landed", "Nothing has landed lately."),
    ("underway", "Underway", "Nothing is underway."),
    ("coming_next", "Coming next", "Nothing is lined up next."),
)

LANDED_KINDS = ("card_done", "card_plan_attached", "card_plan_approved")

#: Rank of Needs your call, most urgent first.
CALL_KINDS = (
    "permission", "question", "ended_work", "manual_check", "waiting",
    "awaiting_review",
)

MAX_ITEMS = 40
MAX_EVENTS = 200
MAX_WORDS = 160

ITEM_KEYS = ("text", "kind", "card_id", "session_id", "project", "at")

_BUCKET_STATE = {
    "running": "working",
    "waiting": "waiting on you",
    "sleeping": "asleep",
}


def compose(*, snapshot, prompts, notified, cards, acks, events,
            diary_available, since=None, now, skip_sessions=()) -> dict:
    """One digest. Equal inputs produce equal dicts and equal ``text``."""
    skip = _skip_set(skip_sessions)
    sections = [
        _section("needs_your_call", needs_your_call(
            snapshot, prompts, notified, cards, acks, skip)),
        _section("recently_landed", recently_landed(events, since)),
        _section("underway", underway(snapshot, cards, skip)),
        _section("coming_next", coming_next(cards)),
    ]
    return {
        "available": True,
        "generated_at": now,
        "since": since,
        "diary_available": bool(diary_available),
        "sections": sections,
        "text": render(sections),
    }


def render(sections) -> str:
    """``## <title>`` then items or the empty sentence, trailing newline."""
    blocks = []
    for section in sections or ():
        title = str(section.get("title") or "")
        items = section.get("items") or []
        lines = [f"## {title}"]
        if items:
            for item in items:
                lines.append(f"- {item.get('text') or ''}")
        else:
            lines.append(str(section.get("empty") or ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def needs_your_call(snapshot, prompts, notified, cards, acks,
                    skip_sessions=()) -> list[dict]:
    """Actionable waiters only: permission, question, look, check, stopped,
    awaiting review. Nothing FYI."""
    snapshot = snapshot or {}
    prompts = prompts or {}
    skip = _skip_set(skip_sessions)
    ack_set = _ack_set(acks)
    rows = _row_map(snapshot)
    items: list[tuple[int, tuple, str, dict]] = []

    for waiter in live_activity.waiters(snapshot, prompts, notified, cards):
        sid = str(waiter.get("session_id") or "")
        if not sid or sid in skip:
            continue
        kind = str(waiter.get("kind") or "")
        wire = "waiting" if kind == "attention" else kind
        if wire not in ("permission", "question", "waiting"):
            continue
        found = rows.get(sid)
        row = found[1] if found else waiter
        who = _who(row)
        if wire == "permission":
            prompt = prompts.get(sid) if isinstance(prompts.get(sid), dict) else {}
            tool = str((prompt or {}).get("tool_name") or "a tool").strip() or "a tool"
            description = str((prompt or {}).get("description") or "").strip()
            text = f"{who} wants to run {tool}"
            if description:
                text += f": {description}"
        elif wire == "question":
            asked = _first_question(row)
            text = f"{who} asked: {asked}" if asked else f"{who} asked"
        else:
            text = f"{who} stopped and is waiting on you{_in(row.get('project'))}"
        fp_kind = inbox_ack.session_kind_and_fp(
            permission=bool(prompts.get(sid)),
            questions=row.get("questions") if isinstance(row.get("questions"), list)
            else [],
            waiting=True,
        )
        if fp_kind and ("s:" + sid, fp_kind[0], fp_kind[1]) in ack_set:
            continue
        items.append(_rank_item(
            wire, who, sid,
            _item(text=text, kind=wire, session_id=sid,
                  project=str(row.get("project") or "")),
        ))

    seen_cards: set[str] = set()
    for card in _card_list(cards):
        cid = str(card.get("id") or "")
        if not cid or cid in seen_cards:
            continue
        seen_cards.add(cid)
        title = str(card.get("title") or "").strip() or "a card"
        project = str(card.get("project") or "")
        kind_fp = inbox_ack.card_kind_and_fp(card)
        if kind_fp:
            kind, fp = kind_fp
            if ("c:" + cid, kind, fp) in ack_set:
                continue
            if kind == "ended_work":
                text = f"{title} finished and wants a look"
            else:
                text = f"{title} needs a manual check"
        elif _awaits_review(card):
            closer = str(card.get("closed_by_name") or "").strip() or "its assistant"
            text = (f"{title} was closed by {closer} and awaits your review")
            kind = "awaiting_review"
        else:
            continue
        items.append(_rank_item(
            kind, title, cid,
            _item(text=text, kind=kind, card_id=cid,
                  session_id=str(card.get("session_id") or ""),
                  project=project),
        ))

    items.sort(key=lambda row: row[:3])
    return [row[3] for row in items[:MAX_ITEMS]]


def recently_landed(events, since=None) -> list[dict]:
    """Diary rows already newest-first; ``since`` narrows this section alone."""
    out = []
    bound = None if since is None else _finite(since)
    for event in events or ():
        if not isinstance(event, dict):
            continue
        if str(event.get("kind") or "") not in LANDED_KINDS:
            continue
        ts = _finite(event.get("ts"))
        if bound is not None and not (ts > bound):
            continue
        out.append(_item(
            text=str(event.get("text") or ""),
            kind=str(event.get("kind") or ""),
            card_id=str(event.get("card_id") or ""),
            session_id=str(event.get("session_id") or ""),
            project=str(event.get("project") or ""),
            at=ts if ts else None,
        ))
        if len(out) >= MAX_ITEMS:
            break
    return out


def underway(snapshot, cards, skip_sessions=()) -> list[dict]:
    """In-progress cards in board order, then unbound running sessions."""
    snapshot = snapshot or {}
    skip = _skip_set(skip_sessions)
    rows = _row_map(snapshot)
    card_list = _card_list(cards)
    bound_sids = {str(card.get("session_id") or "")
                  for card in card_list if card.get("session_id")}
    refine_of = {}
    for card in card_list:
        rid = str(card.get("refine_session_id") or "")
        if rid:
            refine_of.setdefault(rid, card)

    out: list[dict] = []
    for card in card_list:
        if str(card.get("column_name") or "") != "in_progress":
            continue
        title = str(card.get("title") or "").strip() or "a card"
        project = str(card.get("project") or "")
        sid = str(card.get("session_id") or "")
        bucket_row = rows.get(sid)
        if bucket_row:
            bucket, row = bucket_row
            who = str(row.get("nickname") or "").strip() or "nobody yet"
        else:
            bucket, row = "", None
            who = "nobody yet"
        link = str(card.get("link_state") or "")
        if link == "dispatching":
            state = "starting"
        elif bucket in _BUCKET_STATE:
            state = _BUCKET_STATE[bucket]
        else:
            state = "its assistant has gone"
        text = f"{who} is on {title}{_in(project)} — {state}"
        health = card.get("run_health") if isinstance(card.get("run_health"), dict) else {}
        klass = str((health or {}).get("class") or "").strip()
        if klass:
            text += f", {klass} run"
        if (health or {}).get("attention"):
            text += ", needs attention"
        out.append(_item(
            text=text, kind="card", card_id=str(card.get("id") or ""),
            session_id=sid, project=project,
        ))
        if len(out) >= MAX_ITEMS:
            return out

    extras: list[tuple[tuple, str, dict]] = []
    for row in snapshot.get("running") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "")
        if not sid or sid in skip or sid in bound_sids:
            continue
        who = _who(row)
        project = str(row.get("project") or "")
        refine_card = refine_of.get(sid)
        if refine_card is not None:
            if str(refine_card.get("column_name") or "") != "prep":
                continue
            title = str(refine_card.get("title") or "").strip() or "a card"
            text = f"{who} is refining {title}{_in(project)}"
            kind = "refining"
            card_id = str(refine_card.get("id") or "")
        else:
            work = str(row.get("card_title") or row.get("name") or "").strip() or "something"
            text = f"{who} is working{_in(project)} on {work}"
            kind = "session"
            card_id = ""
        extras.append((
            live_activity.title_key(who), sid,
            _item(text=text, kind=kind, card_id=card_id, session_id=sid,
                  project=project),
        ))
    extras.sort(key=lambda row: row[:2])
    for _key, _sid, item in extras:
        out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def coming_next(cards) -> list[dict]:
    """Backlog in board order. Prep is not next: it has no plan."""
    out = []
    for card in _card_list(cards):
        if str(card.get("column_name") or "") != "backlog":
            continue
        title = str(card.get("title") or "").strip() or "a card"
        project = str(card.get("project") or "")
        text = f"{title}{_in(project)}"
        if str(card.get("queue_state") or "") == "queued":
            reason = str(card.get("queue_reason") or "").strip()
            if reason:
                text += f" — {reason}"
        else:
            error = str(card.get("dispatch_error") or "").strip()
            if error:
                text += f" — {error}"
        out.append(_item(
            text=text, kind="card", card_id=str(card.get("id") or ""),
            session_id=str(card.get("session_id") or ""),
            project=project,
        ))
        if len(out) >= MAX_ITEMS:
            break
    return out


def _section(key: str, items: list) -> dict:
    _key, title, empty = next(s for s in SECTIONS if s[0] == key)
    return {"key": key, "title": title, "items": items, "empty": empty}


def _item(*, text, kind, card_id="", session_id="", project="", at=None) -> dict:
    return {
        "text": _clip_words(text),
        "kind": str(kind or ""),
        "card_id": str(card_id or ""),
        "session_id": str(session_id or ""),
        "project": str(project or ""),
        "at": at,
    }


def _rank_item(kind: str, title: str, ident: str, item: dict):
    order = CALL_KINDS.index(kind) if kind in CALL_KINDS else len(CALL_KINDS)
    return (order, live_activity.title_key(title), ident, item)


def _who(row) -> str:
    """Nickname, else card title, else name, else "an agent"."""
    if not isinstance(row, dict):
        return "an agent"
    nickname = str(row.get("nickname") or "").strip()
    if nickname:
        return nickname
    title = str(row.get("card_title") or "").strip()
    if title:
        return title
    name = str(row.get("name") or "").strip()
    if name:
        return name
    return "an agent"


def _in(project) -> str:
    project = str(project or "").strip()
    return f" in {project}" if project else ""


def _clip_words(text: str) -> str:
    """Flatten interior newlines, then clip to `MAX_WORDS`.

    `event_log.sentence`'s manner: `" ".join(text.split())` so a description
    that carries `## Recently landed` cannot split `render` into extra
    headings. A clip is the ellipsis alone.
    """
    words = str(text or "").split()
    if len(words) <= MAX_WORDS:
        return " ".join(words)
    return " ".join(words[:MAX_WORDS]) + "…"


def _first_question(row) -> str:
    questions = row.get("questions") if isinstance(row, dict) else None
    if not isinstance(questions, list):
        return ""
    for question in questions:
        if isinstance(question, dict):
            text = str(question.get("text") or "").strip()
            if text:
                return text
    return ""


def _awaits_review(card: dict) -> bool:
    if str(card.get("column_name") or "") != "done":
        return False
    if not str(card.get("closed_by") or ""):
        return False
    return not card.get("reviewed_at")


def _card_list(cards) -> list:
    return [card for card in (cards or ()) if isinstance(card, dict)]


def _row_map(snapshot: dict) -> dict:
    out = {}
    for bucket in live_activity.LIVE_BUCKETS + ("finished",):
        for row in snapshot.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("session_id") or "")
            if sid and sid not in out:
                out[sid] = (bucket, row)
    return out


def _ack_set(acks) -> set:
    out = set()
    for row in acks or ():
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        kind = str(row.get("kind") or "")
        fp = str(row.get("fp") or "")
        if key and kind and fp:
            out.add((key, kind, fp))
    return out


def _skip_set(skip_sessions) -> set:
    return {str(sid) for sid in (skip_sessions or ()) if str(sid or "")}


def _finite(value) -> float:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return 0.0
    if stamp != stamp or stamp in (float("inf"), float("-inf")):
        return 0.0
    return stamp


__all__ = [
    "SECTIONS", "LANDED_KINDS", "CALL_KINDS", "MAX_ITEMS", "MAX_EVENTS",
    "MAX_WORDS", "ITEM_KEYS",
    "compose", "render", "needs_your_call", "recently_landed", "underway",
    "coming_next",
]
