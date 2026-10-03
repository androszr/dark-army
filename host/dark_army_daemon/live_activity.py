"""Who is at the top of Needs you right now — the phone's Live Activity subject.

Pure, stdlib only. The buzz (`alerts.py`) is a *transition*: it fires at the
moment an alert is raised and knows nothing about the standing state. A Live
Activity is the opposite — a standing picture on the phone's Lock Screen that
must be **updated on change and ended on empty** — so the daemon needs a
second, pure reading of "who is waiting on a person at this instant", taken
from the buckets it already publishes and nothing else.

The rule restates the phone's own (`PhoneInbox.items(from:)`, restricted to
session entries of the three kinds it can draw a face for):

* a row from the live buckets — ``running``, ``waiting``, ``sleeping``, the
  phone's ``liveBuckets`` and the Mac's ``Category.live``; a finished or
  abandoned row is never a subject whatever it still carries;
* admitted by membership of ``waiting``, an open permission prompt, or an
  active notification card naming the session — the phone's ``waiting``,
  ``prompts`` and ``notifyIds``, the Mac's ``needsHuman``; a non-empty
  ``questions`` list decides the *kind* and admits nothing by itself (a
  stale list on a row that went quiet would otherwise push a question card
  for an agent nobody lists);
* its kind decided prompt → question → attention (`KINDS`, most urgent
  first — the phone's ``permission = 0``, ``question``, ``waiting`` order);
* **one entry per subject**, the phone's ``oneEntryPerSubject``: a board
  card that needs a person (``needs_you``) or awaits its manual check
  (``manual_check_due``) and names the session (``session_id``) outranks
  the session's own *attention* entry — a card entry is a ``LOOK AT``, an
  agent that merely stopped a ``STOPPED`` — so that session is no subject;
  a prompt or a question outranks the card and the session stays;
* **a review run waiting on picks is a subject of its own** (kind
  `picks`, `docs/review-runs.md`): read off the published `review` runs
  (`review_runs`), ranked after a prompt and a question and before a row
  that merely stopped — the phone's `reviewPicks` wire sits between
  `question` and `waiting`. The session bound to a run speaks through it:
  a bound row that would be *attention* is no subject (the run takes its
  entry), while a bound row holding a prompt or a question keeps its own
  entry and the run yields — the phone's one-entry rule. A run wears its
  bound row's face where it has one; an unbound run (Codex on a hosted
  pty) has `session_id` and `slug` empty and says its project, so no
  stranger's portrait is drawn. `run_id` names it on the wire;
* ranked exactly as the phone sorts its list (`PhoneInbox.before`): kind,
  then the row's title — nickname, else name, else session id, compared as
  `localizedStandardCompare` does for the cast's plain names (case-folded,
  digit runs by value) — then ``session_id``. Never by idle time: the phone
  starts the card for the head of *its* list, and a Mac that ranked a tie
  differently would flip the card to the other agent on its first update.

What rides the wire for a face-only card (shape 1, an older phone) is
`content_state`'s six keys and nothing else. A shape-2 card — the phone
said so when it registered — carries those six plus the fleet: `working`,
`needs_you`, `standing_by`, and `cost_usd` / `tokens_k` only when the Mac
measured them. Absent is not zero. The closed set is the contract
(`docs/transport-contract.md`, *The buzz has a live-card leg*). The ask's
own words never travel.

``since`` is the row's own ``quiet_since`` — the moment it went quiet,
stamped once by the daemon when the snapshot was built (`_collect_agent_stubs`)
and read verbatim by the phone (`Agent.quietSince`), so the two ends agree to
the second and the Mac's first update never rewrites the clock the phone
started. A row from an older daemon carries no stamp, and ``now -
idle_seconds`` stands in.
"""

from __future__ import annotations

import re

from . import cast, inbox_ack

#: The kind words a live card may wear, most urgent first. A subset of
#: `alerts.KINDS` / `relay_client.PUSH_KINDS` minus the two that have no
#: standing subject: `security` is about the machine, `finished` waits on
#: nobody. `relay_client.ACTIVITY_KINDS` restates it at the wire.
KINDS = ("permission", "question", "picks", "attention")

#: The buckets a subject may come from — the phone's `liveBuckets`.
LIVE_BUCKETS = ("running", "waiting", "sleeping")

#: The face, in the order `push.js` writes it. Shape 1's whole state.
FACE_KEYS = ("nickname", "slug", "kind", "work", "since", "session_id",
             "run_id")

#: The three bucket counts. `needs_you` is `counts["attention"]`,
#: `standing_by` is `counts["idle"]` — the strip's own numbers.
COUNT_KEYS = ("working", "needs_you", "standing_by")

#: Present only when measured. Absent is not zero. The two `_hour` keys
#: are the fleet's burn over the last hour (`fleet_figures.BurnMeter`),
#: absent until the Mac has watched long enough to say one.
FIGURE_KEYS = ("cost_usd", "tokens_k", "cost_usd_hour", "tokens_k_hour")

#: The wire shape, in the order `push.js` writes it: the face, the counts,
#: then the figures.
STATE_KEYS = FACE_KEYS + COUNT_KEYS + FIGURE_KEYS

#: Which card a phone draws. 1 is the face-only card (an older phone, and
#: the default for an entry that never said); 2 is the fleet card.
SHAPES = (1, 2)

#: A clock tick alone never sends: two states whose `since` differ by less
#: than this are the same picture.
SINCE_TOLERANCE_SECONDS = 2.0


def _as_int(value, default: int = 0) -> int:
    """A count as an int, or `default` where the value is missing or not a
    number. A bool is not a count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return int(number)


def is_up(counts) -> bool:
    """Whether the fleet card should be up: someone working or waiting.

    A fleet of only standing-by agents is not up — a session left open for
    hours would otherwise pin a card until the eight-hour cap. A missing
    key is zero, not an error.
    """
    if not isinstance(counts, dict):
        return False
    return _as_int(counts.get("working")) + _as_int(counts.get("attention")) > 0


def empty_face(now: float) -> dict:
    """The six face keys when nobody is waiting.

    ``since`` is 0, not ``now``: an empty face has no clock, and a tick
    must not look like a new card. ``now`` is accepted so the call matches
    `content_state`'s clock.
    """
    del now
    return {
        "nickname": "",
        "slug": "",
        "kind": "attention",
        "work": "",
        "since": 0,
        "session_id": "",
        "run_id": "",
    }


def fleet_state(counts, figures, face) -> dict:
    """One shape-2 content state: the face, the three counts, the figures.

    Exactly `STATE_KEYS`. ``needs_you`` is ``counts["attention"]`` and
    ``standing_by`` is ``counts["idle"]``. ``cost_usd`` is the figure
    rounded to the cent **only when it is not None** — the key is absent
    otherwise, never ``0``. ``tokens_k`` likewise, and the two per-hour
    rates ``cost_usd_hour`` / ``tokens_k_hour`` on the same rules. ``face`` is a subject's
    `content_state`, or `empty_face` when nobody waits.
    """
    counts = counts if isinstance(counts, dict) else {}
    figures = figures if isinstance(figures, dict) else {}
    base = face if isinstance(face, dict) else empty_face(0)
    state = {
        "nickname": str(base.get("nickname") or ""),
        "slug": str(base.get("slug") or ""),
        "kind": str(base.get("kind") or "attention"),
        "work": str(base.get("work") or ""),
        "since": base.get("since", 0),
        "session_id": str(base.get("session_id") or ""),
        "run_id": str(base.get("run_id") or ""),
        "working": _as_int(counts.get("working")),
        "needs_you": _as_int(counts.get("attention")),
        "standing_by": _as_int(counts.get("idle")),
    }
    # `FIGURE_KEYS` order, so the wire order is the contract's.
    for key in FIGURE_KEYS:
        value = figures.get(key)
        if value is None or isinstance(value, bool):
            continue
        if key.startswith("cost_usd"):
            try:
                state[key] = round(float(value), 2)
            except (TypeError, ValueError):
                pass
        elif isinstance(value, int):
            state[key] = value
    return state


def _has_question(row: dict) -> bool:
    questions = row.get("questions")
    if not isinstance(questions, list):
        return False
    for question in questions:
        if isinstance(question, dict) and str(question.get("text") or "").strip():
            return True
    return False


def listed_sessions(snapshot: dict, prompts: dict, notified=None) -> set[str]:
    """The session ids the phone's Needs you list admits — `PhoneInbox`'s
    `sessionItems` (`docs/phone-contract.md`, *One decision list*) and
    `waiters`' admission line.

    A row in a live bucket (`LIVE_BUCKETS`) is listed when it is in the
    ``waiting`` bucket, holds a truthy open prompt in ``prompts``
    (`BobDaemon._prompts_by_session()`) or is named by ``notified`` (the
    sessions of the published notification cards). A row in any other
    bucket — finished, abandoned — is never listed. A prompt whose session
    has **no row at all** is listed (the phone's read-only item); a card
    with no row is not. The phone leg reads this set so it never sends what
    the phone would sweep away (`docs/transport-contract.md`).
    """
    snapshot = snapshot or {}
    prompts = prompts or {}
    notified_ids = {str(sid) for sid in (notified or ()) if str(sid or "")}
    live: set[str] = set()
    present: set[str] = set()
    for bucket, rows in snapshot.items():
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict):
                sid = str(row.get("session_id") or "")
                if sid:
                    present.add(sid)
                    if bucket in LIVE_BUCKETS:
                        live.add(sid)
    waiting_ids = {str(row.get("session_id") or "")
                   for row in snapshot.get("waiting") or []
                   if isinstance(row, dict)}
    prompted = {str(sid) for sid, prompt in prompts.items()
                if str(sid or "") and prompt}
    listed = {sid for sid in live
              if sid in waiting_ids or sid in prompted or sid in notified_ids}
    return listed | (prompted - present)


def shown_sessions(snapshot: dict, prompts: dict, notified=None,
                   cards=None, acks=None) -> set[str]:
    """`listed_sessions` less the sessions the person dismissed — the ones
    the phone's Needs you list still shows once its acks are applied.

    The phone's `PhoneInbox.items(from:)` removes every entry whose
    ``(target.key, wire.name, fingerprint)`` matches an ack **first**, then
    runs `oneEntryPerSubject`, which only de-duplicates: it picks which
    entry stands for a session and never hides one. So a listed session
    stays shown while its own entry is not dismissed, or while a board card
    naming it (``session_id``) has an entry that is not. Each entry is
    judged by `inbox_ack.session_kind_and_fp` / `card_kind_and_fp`, the
    functions the Dismiss verb and Bearings use, with ``waiting=True`` —
    every listed row's fallback wire on the phone is ``waiting``, whatever
    admitted it — and the question read off `inbox_ack.question_list`. A
    listed session with no row is an orphan prompt, never dismissable. A
    card only *keeps* a session `listed_sessions` admitted; it never adds
    one.

    The buzz gate reads this (`docs/transport-contract.md`, *A buzz names
    only what the phone lists*). `waiters` / `subject` deliberately do not:
    the Live Activity's accepted drift (`docs/phone-contract.md`).
    """
    snapshot = snapshot or {}
    prompts = prompts or {}
    listed = listed_sessions(snapshot, prompts, notified)
    triples = inbox_ack.ack_set(acks)
    if not triples or not listed:
        return listed
    rows: dict[str, dict] = {}
    order = list(LIVE_BUCKETS) + [bucket for bucket in snapshot
                                  if bucket not in LIVE_BUCKETS]
    for bucket in order:
        bucket_rows = snapshot.get(bucket)
        for row in bucket_rows if isinstance(bucket_rows, list) else ():
            if isinstance(row, dict):
                sid = str(row.get("session_id") or "")
                if sid and sid not in rows:
                    rows[sid] = row
    kept: set[str] = set()
    seen_cards: set[str] = set()
    for card in cards or ():
        if not isinstance(card, dict):
            continue
        # `cardItems`: a card with no id is no entry; the first of an id wins.
        cid = str(card.get("id") or "")
        if not cid or cid in seen_cards:
            continue
        seen_cards.add(cid)
        sid = str(card.get("session_id") or "")
        kind_fp = inbox_ack.card_kind_and_fp(card)
        if not sid or kind_fp is None:
            continue
        if ("c:" + cid, kind_fp[0], kind_fp[1]) not in triples:
            kept.add(sid)
    shown: set[str] = set()
    for sid in listed:
        row = rows.get(sid)
        if row is None or sid in kept:
            shown.add(sid)
            continue
        kind_fp = inbox_ack.session_kind_and_fp(
            permission=bool(prompts.get(sid)),
            questions=inbox_ack.question_list(row), waiting=True)
        # A permission ask is never dismissable (`inbox_ack.NEVER_KINDS`;
        # `ack_inbox` refuses it), so no row on disk may hide one.
        if kind_fp is None or kind_fp[0] in inbox_ack.NEVER_KINDS \
                or ("s:" + sid, kind_fp[0], kind_fp[1]) not in triples:
            shown.add(sid)
    return shown


def waiters(snapshot: dict, prompts: dict, notified=None,
            cards=None, review_runs=None) -> list[dict]:
    """The whole of Needs you's session half, in the phone's order; `subject`
    is its head.

    ``snapshot`` is the published agents snapshot (bucket name → rows);
    ``prompts`` is `BobDaemon._prompts_by_session()`'s answer, session id →
    the oldest open prompt; ``notified`` the session ids holding an active
    notification card (`BobDaemon._active_notifications`' keys, the
    phone's ``snapshot.notifications``); ``cards`` the published board's
    cards (`_board_state["cards"]`, the phone's ``snapshot.board.cards``).
    Each item is ``{"session_id", "nickname", "name", "kind", "idle_seconds",
    "quiet_since"}`` — the row's own figures, re-derived from nothing.
    ``review_runs`` is the published `review` section's runs: each one in
    the ``picks`` state is a further candidate of kind ``picks`` carrying
    its ``run_id`` (see the module docstring).
    """
    snapshot = snapshot or {}
    prompts = prompts or {}
    picks_runs = [run for run in review_runs or ()
                  if isinstance(run, dict) and run.get("state") == "picks"
                  and str(run.get("id") or "")]
    review_bound = {str(run.get("session_id")) for run in picks_runs
                    if run.get("session_id")}
    live_rows: dict[str, dict] = {}
    held: set[str] = set()      # bound sessions keeping their own entry
    carded_ids = carded_sessions(cards)
    listed = listed_sessions(snapshot, prompts, notified)
    seen: set[str] = set()
    candidates: list[tuple[int, tuple, str, dict]] = []
    for bucket in LIVE_BUCKETS:
        for row in snapshot.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("session_id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            live_rows[sid] = row
            has_prompt = bool(prompts.get(sid))
            if sid not in listed:
                continue
            if has_prompt or _has_question(row):
                held.add(sid)
            if has_prompt:
                kind = "permission"
            elif _has_question(row):
                kind = "question"
            elif sid in review_bound:
                # The run takes the entry of the session bound to it, as
                # `carded_ids` takes a stopped row's.
                continue
            elif sid in carded_ids:
                # The card's entry wins the phone's one-entry rule over a
                # row that merely stopped; the card draws no face, so
                # nobody is the subject for this session.
                continue
            else:
                kind = "attention"
            try:
                idle = float(row.get("idle_seconds") or 0.0)
            except (TypeError, ValueError):
                idle = 0.0
            if idle != idle or idle < 0:  # NaN or negative: undated
                idle = 0.0
            nickname = str(row.get("nickname") or "")
            name = str(row.get("name") or "")
            candidates.append((KINDS.index(kind), title_key(nickname or name or sid), sid, {
                "session_id": sid,
                "nickname": nickname,
                "name": name,
                "kind": kind,
                "idle_seconds": idle,
                "quiet_since": _finite_stamp(row.get("quiet_since")),
            }))
    for run in picks_runs:
        bound = str(run.get("session_id") or "")
        if bound in held:
            # A prompt or a question on the bound session keeps its own
            # entry; the run yields (the phone's `oneEntryPerSubject`).
            continue
        row = live_rows.get(bound) if bound else None
        sid = bound if row is not None else ""
        nickname = str((row or {}).get("nickname") or "")
        name = str(run.get("project") or "")
        run_id = str(run.get("id") or "")
        # Ranked by the phone's own entry title, "Review · <project, else
        # root>" (`PhoneInbox.reviewItems`), so a tie between two runs
        # breaks the same way on both ends whatever the bound rows are called.
        tie = "Review · " + (name or str(run.get("root") or ""))
        candidates.append((KINDS.index("picks"), title_key(tie), run_id, {
            "session_id": sid,
            "run_id": run_id,
            "nickname": nickname or name,
            "name": name,
            "kind": "picks",
            "idle_seconds": 0.0,
            "quiet_since": _finite_stamp(run.get("findings_at")),
        }))
    return [item[3] for item in sorted(candidates, key=lambda item: item[:3])]


def subject(snapshot: dict, prompts: dict, notified=None,
            cards=None, review_runs=None) -> dict | None:
    """The top session waiter, or ``None`` when nobody needs a person.

    ``snapshot`` is the published agents snapshot (bucket name → rows);
    ``prompts`` is `BobDaemon._prompts_by_session()`'s answer, session id →
    the oldest open prompt; ``notified`` the session ids holding an active
    notification card (`BobDaemon._active_notifications`' keys, the
    phone's ``snapshot.notifications``); ``cards`` the published board's
    cards (`_board_state["cards"]`, the phone's ``snapshot.board.cards``).
    Returns ``{"session_id", "nickname", "name", "kind", "idle_seconds",
    "quiet_since"}`` — the row's own figures, re-derived from nothing.
    """
    ranked = waiters(snapshot, prompts, notified, cards, review_runs)
    return ranked[0] if ranked else None


def fleet_face(snapshot: dict, prompts: dict, notified=None,
               cards=None, review_runs=None) -> dict | None:
    """The face on the fleet card (shape 2): `subject`, else the head of the
    published ``waiting`` bucket as an ``attention`` face.

    The fleet card's "need you" number is `_activity_counts()["attention"]`
    — every waiting row — while `subject` skips a waiter whose Needs-you
    entry is a board card (a hand-check due, a card needing you). Without
    this fallback the card read "1 need you" with no face at all. The
    face-only card (shape 1) keeps `subject` alone; the phone's
    `NeedsYouActivityRule.fleetState` restates this rule.
    """
    head = subject(snapshot, prompts, notified, cards, review_runs)
    if head is not None:
        return head
    for row in (snapshot or {}).get("waiting") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        try:
            idle = float(row.get("idle_seconds") or 0.0)
        except (TypeError, ValueError):
            idle = 0.0
        if idle != idle or idle < 0:
            idle = 0.0
        return {
            "session_id": sid,
            "nickname": str(row.get("nickname") or ""),
            "name": str(row.get("name") or ""),
            "kind": "attention",
            "idle_seconds": idle,
            "quiet_since": _finite_stamp(row.get("quiet_since")),
        }
    return None


def carded_sessions(cards) -> set[str]:
    """The session ids a board card entry would name on the phone's
    decision list — `PhoneInbox.cardItems`: a card with ``needs_you`` or
    ``manual_check_due`` set, keyed on its ``session_id`` alone (never
    ``refine_session_id``, which the phone's card entry does not carry)."""
    out: set[str] = set()
    for card in cards or ():
        if not isinstance(card, dict):
            continue
        if not (card.get("needs_you") or card.get("manual_check_due")):
            continue
        sid = str(card.get("session_id") or "")
        if sid:
            out.add(sid)
    return out


def _finite_stamp(value) -> float:
    """A row's ``quiet_since`` as a float ≥ 0, or 0.0 where the row carries
    none (an older daemon) or nonsense."""
    try:
        stamp = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if stamp != stamp or stamp in (float("inf"), float("-inf")) or stamp < 0:
        return 0.0
    return stamp


_DIGITS = re.compile(r"(\d+)")


def title_key(title: str) -> tuple:
    """The phone's ``localizedStandardCompare`` for the titles a row can
    wear: case-insensitive, with a run of digits ordered by its value
    (``Vex2`` before ``Vex10``), a case-only tie broken lowercase
    first as ICU does, so the order is total. Cast nicknames are plain
    ASCII words, which is the whole of what the two ends need to agree on."""
    parts = _DIGITS.split(str(title or ""))
    key = []
    for index, part in enumerate(parts):
        if index % 2:
            key.append((1, int(part), ""))
        elif part:
            key.append((0, 0, part.casefold()))
    return (tuple(key), str(title or "").swapcase())


def content_state(subject: dict, work: str, now: float) -> dict:
    """The wire dict for one subject — exactly the seven face keys
    (``run_id`` is ``""`` for every subject but a review run, and the wire
    leaves an empty one off).

    ``slug`` is `cast.character_for`, the phone's `Cast.character(for:)`
    rung for rung, so the face on the Lock Screen is the face on the row.
    ``since`` is the moment the row went quiet — the row's own
    ``quiet_since`` stamp where the daemon wrote one, else ``now -
    idle_seconds`` — whole seconds, the number the phone read off the same
    row. ``work`` is the caller's clamped line
    (`BobDaemon._compose_push_work`'s rule) and may be ``""``.
    """
    sid = str(subject.get("session_id") or "")
    nickname = str(subject.get("nickname") or "")
    kind = str(subject.get("kind") or "")
    if kind not in KINDS:
        kind = "attention"
    try:
        idle = float(subject.get("idle_seconds") or 0.0)
    except (TypeError, ValueError):
        idle = 0.0
    stamp = _finite_stamp(subject.get("quiet_since"))
    if stamp > 0:
        since = int(round(stamp))
    else:
        since = int(round(max(0.0, float(now) - max(0.0, idle))))
    return {
        "nickname": nickname,
        # An unbound run has no session: `cast.character_for("", "")`
        # answers a stranger's face, so it draws an initial instead.
        "slug": cast.character_for(nickname, sid) if sid else "",
        "kind": kind,
        "work": str(work or ""),
        "since": since,
        "session_id": sid,
        "run_id": str(subject.get("run_id") or ""),
    }


def same(a: dict | None, b: dict | None) -> bool:
    """Whether two content states draw the same card.

    Equality on every key but ``since``, which may drift by
    `SINCE_TOLERANCE_SECONDS` — the two ends compute it from a clock and an
    idle figure that are each a snapshot old, so a tick alone must never
    cost a push. A changed nickname, slug, kind, work, session id, count
    or figure is a different card. A key absent on one side and present on
    the other is a difference — nil is not zero.
    """
    if not a or not b:
        return (not a) and (not b)
    for key in STATE_KEYS:
        if key == "since":
            continue
        if a.get(key) != b.get(key):
            return False
    try:
        drift = abs(float(a.get("since") or 0) - float(b.get("since") or 0))
    except (TypeError, ValueError):
        return False
    return drift < SINCE_TOLERANCE_SECONDS


def figures_only_change(a: dict | None, b: dict | None) -> bool:
    """True when two states differ, and only in the figures (or `since`).

    Equal on every key but ``since`` and `FIGURE_KEYS`, and not `same`.
    A count, the face or the event is not this — those send at once. A
    dime of cost, or a quantised token step, is.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if same(a, b):
        return False
    figures = set(FIGURE_KEYS)
    for key in STATE_KEYS:
        if key == "since" or key in figures:
            continue
        if a.get(key) != b.get(key):
            return False
    return True


__all__ = ["KINDS", "LIVE_BUCKETS", "FACE_KEYS", "COUNT_KEYS", "FIGURE_KEYS",
           "STATE_KEYS", "SHAPES", "SINCE_TOLERANCE_SECONDS",
           "listed_sessions", "shown_sessions", "waiters", "subject", "carded_sessions", "content_state", "same",
           "title_key", "is_up", "empty_face", "fleet_state",
           "figures_only_change"]
