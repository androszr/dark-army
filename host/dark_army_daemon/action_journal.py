"""The action journal's pure half: declarations and decisions, no I/O.

Dark Army writes down what it is about to do before it does it (an *intent*),
and the outcome under the same id afterwards (a *result*). On launch every
intent with no result is an interrupted step, and the rule for it is the
step's own declaration: a step that is `SAFE` to repeat may be finished, a
step that is `NEVER` safe is recorded as interrupted and never fired again.
The tables are `board_journal_store.py`'s, the recovery is
`daemon_recovery.py`'s; the contract in full is `docs/action-journal.md`.
"""
from __future__ import annotations

from typing import Optional

from .event_log import FORBIDDEN_KEYS

SAFE = "safe"
NEVER = "never"

#: kind -> step -> replay declaration, in declaration order per kind. A step
#: that opens a terminal, sends a signal or presses keys is `NEVER`; a
#: bookkeeping write is `SAFE`.
ACTIONS = {
    "card_start": {"spawn": NEVER, "record": SAFE},
    "worktree_prepare": {"add": NEVER, "record": SAFE},
    "stop_session": {"terminate": NEVER, "kill": NEVER},
    "autocompact": {"type": NEVER},
    "answer_burst": {"type": NEVER},
}

#: An action that is not finished when a step resolved with one of these
#: outcomes and the named next step was never written: the crash fell between
#: the result and the next intent. kind -> (step, outcomes, next step).
CONTINUES = {
    "card_start": ("spawn", ("spawned",), "record"),
    "worktree_prepare": ("add", ("added", "reused"), "record"),
}

#: A safe step is replayed at most this many times for one subject, counted
#: in `action_attempts` (which a restart cannot reset).
MAX_SAFE_REPLAYS = 3
#: A subject's attempts count restarts at one after this long without an
#: intent, so a card started four times over a month is not out of replays.
ATTEMPT_DECAY_SECONDS = 86400.0
JOURNAL_RETENTION_DAYS = 30
MAX_JOURNAL_ROWS = 5000

INTERRUPTED = "interrupted"
REPLAYED = "replayed"
COMPLETED = "completed"

RESTART_DURING_START_NOTE = (
    "Dark Army restarted while starting this card — check for a terminal "
    "that opened, then press Start again")
RESTART_DURING_PREPARE_NOTE = (
    "Dark Army restarted while preparing this card's folder — press "
    "Start again; the folder is reused")

#: The diary's words per kind: "<who>: <words> interrupted by a Dark Army
#: restart".
INTERRUPTED_WORDS = {
    "card_start": "starting a card",
    "worktree_prepare": "preparing a card's folder",
    "stop_session": "stopping a session",
    "autocompact": "asking a session to compact",
    "answer_burst": "typing an answer",
}


def declared(kind: str, step: str) -> Optional[str]:
    """The current declaration for a step, or None when unknown."""
    return ACTIONS.get(kind, {}).get(step)


def may_replay(snapshot_replay: str, kind: str, step: str, attempts: int) -> bool:
    """The (c) rule: the replay declaration written with the intent **and**
    today's declaration must both say `SAFE`, and the attempts must not
    exceed `MAX_SAFE_REPLAYS`. An unknown kind or step answers False."""
    if snapshot_replay != SAFE or declared(kind, step) != SAFE:
        return False
    try:
        return int(attempts) <= MAX_SAFE_REPLAYS
    except (TypeError, ValueError):
        return False


def decide(action: dict) -> list:
    """`[(step, verdict)]` for every open step of one action, in declaration
    order. `action` is `{"kind", "steps": [{"step", "replay", "attempt",
    "attempts", "result"}]}`; a step with a result is not open. Verdict is
    `REPLAYED` (safe to finish) or `INTERRUPTED`; a step whose predecessor
    is interrupted or open-and-unreplayable is interrupted too, because a
    record never replays over an unknown spawn."""
    kind = str(action.get("kind") or "")
    order = list(ACTIONS.get(kind, {}))
    steps = {str(s.get("step") or ""): s for s in action.get("steps") or []}
    out = []
    blocked = False
    for name in order:
        s = steps.get(name)
        if s is None:
            continue
        if s.get("result"):
            if str(s["result"].get("outcome") or "") == INTERRUPTED:
                blocked = True
            continue
        attempts = s.get("attempts", s.get("attempt", 0))
        if not blocked and may_replay(str(s.get("replay") or ""), kind, name,
                                      attempts):
            out.append((name, REPLAYED))
        else:
            out.append((name, INTERRUPTED))
            blocked = True
    # Steps this build does not declare (written by a newer one) are never
    # replayed.
    for name, s in steps.items():
        if name not in order and not s.get("result"):
            out.append((name, INTERRUPTED))
    return out


def forbidden_payload(payload, _depth: int = 0) -> str:
    """The first `event_log.FORBIDDEN_KEYS` member found at any depth in a
    payload, or `""`."""
    if _depth > 8:
        return ""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k) in FORBIDDEN_KEYS:
                return str(k)
            found = forbidden_payload(v, _depth + 1)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for v in payload:
            found = forbidden_payload(v, _depth + 1)
            if found:
                return found
    return ""
