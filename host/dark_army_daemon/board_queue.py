"""Which queued card may go next, and what counts as work in flight.

Pure policy. Nothing here opens a socket, spawns a process, reads a file or
writes to the board — it is the decision in front of `dispatch.py`, and a
decision that can be read in one file with no I/O in it is one a later session
can still audit.

The rule is a count. A project may have `limit` assistants working at once
(`board_parallel`, overridden per project by `board_parallel_by_root`), so the
queue's head goes exactly when that project has a free place: `claims` says who
is holding one, `holds` / `run_active` / `needs_you` publish that per card, and
`slot_head` picks the one card to release in `queue_key` order.

The cost is stated rather than mitigated: **at a limit above 1, two agents may
edit one tree, the same file included.** The file-overlap comparison that once
guarded against it was retired from the decision 24 Aug 2026 and removed
5 Sep 2026; nothing here is a tie-break or an extra condition on the count.

**A card waiting on another card is not a barrier.** A queued card whose
dependencies are not all met (`dependency_met`) cannot start, so it must not
hold a place in the line either: `eligible` drops it before `slot_head` picks
and before the "no overtaking" rung counts who is ahead. Otherwise A, waiting
on B and queued first, would hold B for ever — the 23 Aug deadlock argument
one rung on. Among eligible cards the order is `queue_key`, unchanged.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence, Set

#: Link states that mean an assistant may be editing this project's tree right
#: now. `ended` is deliberately absent: a dead session cannot edit anything, so
#: a card sitting in In progress with an ended session holds no claim and its
#: files are immediately available to the next card. Holding the claim until
#: the card reaches Done would wedge the pipeline behind human verification,
#: which can take a day — at which point the queue adds nothing over pressing
#: Start by hand.
CLAIMING_LINK_STATES = ("dispatching", "live")


def claims(cards: Iterable[Mapping], project: str,
           active_sessions: Optional[Set[str]] = None) -> list:
    """The cards holding this project's tree right now.

    A claim is a card of the same project whose `link_state` says an assistant
    is starting or running. Sessions a person started by hand hold no claim and
    are deliberately invisible here: counting every live session as an
    undeclared total claim would wedge every pipeline behind the person's own
    interactive terminal, which is worse than the gap it closes. Stated in the
    plan's Risks, not smuggled.

    Two narrowings, both saying *this work is over even though the row is
    still there*, and both learned from one wedge in which four queued cards
    waited an hour on a session that had finished and gone quiet:

    **A card in Done holds nothing.** The column is a term here because the
    board's own promise is that Done means somebody said the work was
    finished; a session left sitting in its terminal afterwards is not
    editing anything, and until `board_wrap_up` was switched off the `/clear`
    that route typed was the only thing releasing the claim — by accident.

    **A card flagged for a manual check holds nothing.** `manual_steps` is the
    assistant's own declaration that its work is over and what is left is a
    person's eyeball check — the same sentence Done makes, arriving one gate
    earlier, and the gate that keeps such a card *out* of Done (`/ship`'s
    PASS-only rule) is precisely why it cannot rely on the Done narrowing
    above. Measured 24 Aug 2026: six cards sat QUEUED behind one card whose
    code was finished, whose tests were green, and which was held open solely
    for four on-screen checks. A pipeline that stalls until a human looks at a
    screen is not a pipeline, and nothing about a pending eyeball check means
    an assistant is still editing the tree.

    **A `live` card whose session Dark Army can no longer hear holds nothing.**
    `active_sessions` is the set of session ids the daemon still has a hook
    stream (or an equivalent register) for. A dispatched session that goes
    quiet is evicted from that stream after the staleness window and then
    comes *back* as a roster-only row for as long as its terminal stays open,
    so `link_state` never reaches `ended` and the claim outlived the work by
    hours. This is `CLAIMING_LINK_STATES`' own argument one rung further on:
    a dead session cannot edit anything, and neither can one that has emitted
    nothing for five minutes. `dispatching` cards are exempt — they have no
    session yet, which is the whole point of that state — and passing `None`
    keeps the old behaviour for a caller that cannot say which sessions are
    live.
    """
    name = str(project or "")
    return [card for card in (cards or ())
            if str(card.get("project") or "") == name
            and holds(card, active_sessions)]


def run_active(card: Optional[Mapping],
               active_sessions: Optional[Set[str]] = None) -> bool:
    """Whether an assistant is actually working this card, right now.

    `holds()`' sibling, and the split is deliberate: `holds()` answers "is
    the project's tree held" — the queue gate's question — while this answers
    "is an assistant working", which is what the pipeline band's RUN/DONE
    split draws. `manual_steps` is the only term they disagree on: a card
    flagged for a hand-check releases its claim (so queued work flows past
    it) while its session may still be busily working, and drawing that card
    as DONE while its assistant types is the bug that split the predicate.
    Published per card as `run_active` beside the gate's `work_active`.
    """
    card = card or {}
    state = str(card.get("link_state") or "")
    if state not in CLAIMING_LINK_STATES:
        return False
    if str(card.get("column_name") or "") == "done":
        return False
    if (active_sessions is not None and state == "live"
            and str(card.get("session_id") or "") not in active_sessions):
        return False
    return True


def holds(card: Optional[Mapping],
          active_sessions: Optional[Set[str]] = None) -> bool:
    """Whether this one card is holding its project's tree.

    `claims()`' test for a single card. The snapshot publishes it per card as
    `work_active` — the gate's answer, kept on the wire for older panels —
    while the band's RUN/DONE split reads `run_active()`, the sibling above.
    Delegates to it so the shared releases exist once: two implementations of
    the same release is the drift the module's docstrings already warn about.
    The one term added here is `manual_steps` — a card flagged for a
    hand-check holds nothing, whatever its session is still doing.
    """
    return (run_active(card, active_sessions)
            and not str((card or {}).get("manual_steps") or "").strip())


#: The session states that mean *this turn is not over*. A session Dark Army loses
#: sight of while it reads one of these was mid-tool when the silence started,
#: and silence during a turn is what a long build or a long test run sounds
#: like. Kept here, beside the predicate that reads it, because it is a policy
#: word rather than a fact about the hook stream.
MID_TURN_STATES = ("working", "thinking")


def needs_you(card: Optional[Mapping],
              active_sessions: Optional[Set[str]] = None,
              mid_turn: Optional[Set[str]] = None) -> bool:
    """The daemon's BoardCardView.endedBannerDrawn plus the roster-stub case.

    Dispatching never qualifies: a re-Start is the assistant coming back.
    None means no freshness reading, so only an explicitly ended link counts.

    **Silence is not a lost session, and this is the one predicate that must
    not confuse them.** `active_sessions` is `_claiming_session_ids()`, whose
    evidence is hook-stream freshness — and a session emits no hook for the
    whole of one long tool call, so five quiet minutes inside a `swift build`
    evicts it from that set while its terminal, its process and its work are
    all exactly where they were. The queue can afford that reading (a wrong
    "gone" only frees a slot early); this cannot, because what it publishes is
    a card in the person's Needs you offering *Mark done* and *Send back* — an
    invitation to bury work that is still being done. Measured 7 Sep 2026: a
    dispatched implementation session was evicted after 5 minutes inside one
    tool call, its card appeared under CARDS WITHOUT A SESSION on the phone,
    and it left again the moment the next hook landed.

    So the quiet-live rung asks for one more thing: that the last state Dark Army
    saw was **the end of a turn**. `mid_turn` is the set of sessions Dark Army lost
    sight of *during* one (`MID_TURN_STATES`, and only through wall-clock
    eviction — a dead process is a real end and is never in it), and a card
    whose session is in it is working, not abandoned. The intended case is
    untouched: a session that finished its turn and went quiet with its
    terminal open was last seen `idle`, so it is not in the set and its card
    still asks for a person. None means no such reading, which is the old
    behaviour exactly.
    """
    card = card or {}
    sid = str(card.get("session_id") or "")
    if (card.get("column_name") != "in_progress"
            or card.get("closed_by") or not sid):
        return False
    state = card.get("link_state")
    return (state == "ended"
            or (state == "live" and active_sessions is not None
                and sid not in active_sessions
                and sid not in (mid_turn or ())))


def slot_head(queued: Sequence[Mapping], running_count: int,
              limit: int) -> Optional[Mapping]:
    """The one card this project may start next under the slot rule, or None.

    The whole steady-state waiting rule as of 24 Aug 2026, and it is this
    short on purpose: a project may have `limit` agents working at once, so
    the queue's head goes exactly when the project has a free place. `queued`
    arrives already in drain order (`queue_key`), and the answer is its first
    element or nothing.

    The trade-off is stated in the module docstring: above a limit of 1, two
    agents may edit one tree. Nothing here mitigates that, and adding a
    file-overlap comparison as an extra condition is the one named anti-goal
    of the change that put this here.

    `limit` is floored at 1 rather than trusted: a 0 would park every queue
    for ever, which is `board_autostart` off wearing a number's clothes.

    One card, never a list: binding by elimination needs "the first new
    session in this project" to be singular.

    Pure, no I/O.
    """
    rows = list(queued or ())
    if not rows:
        return None
    return rows[0] if running_count < max(1, int(limit or 0)) else None


def queue_key(card: Mapping) -> tuple:
    """The drain's sort key: a hand-set rank on the enqueue-stamp axis, then id.

    A rank lives on the same axis as the enqueue stamps (epoch seconds), which
    is what lets NULL and non-NULL interleave and what makes SQLite's
    `ORDER BY COALESCE(queue_rank, queued_at, 0), id` the same order. A rank
    is always minted relative to neighbours' effective keys, so a fresh
    enqueue (rank NULL, stamp now) always lands at the back. The trailing `0`
    matches Python's `or 0.0` so a card with both NULL does not disagree
    across the two spellings (SQLite's two-argument `COALESCE` would leave
    NULL, which sorts first by a different rule).

    Pure, no I/O.
    """
    card = card or {}
    rank = card.get("queue_rank")
    stamp = card.get("queued_at") or 0.0
    return (rank if rank is not None else stamp, str(card.get("id") or ""))


def dependency_met(dep: Optional[Mapping], working: bool) -> bool:
    """Whether one card another waits on counts as finished.

    Three ways to be met, and the rule is the board's own reading of
    "finished": the card is gone (a deleted or cleared dependency can hold
    nobody, and the stored id is left alone — `parse_ids`' note); it is in
    Done, whatever its review state; or **the run that flagged a manual check
    is still the card's run and has stopped** — the moment the card's own
    MANUAL CHECK badge lights (`manual_check_due`), with three narrowings the
    badge does not need, because `manual_steps` outlives the run that wrote
    it: the card must still be In progress (a card reset to Backlog after a
    failed check is not finished), not `dispatching` (a re-start for rework
    is a new run), and bound to the session that flagged it
    (`manual_session_id`; a rework run re-binds a new one). A flag from
    before that column (`''`) keeps the column and link tests alone.
    `working` is the caller's `_card_session_working` answer — asked there
    because only the daemon holds the hook-stream freshness it needs.

    Pure, no I/O.
    """
    if dep is None:
        return True
    column = str(dep.get("column_name") or "")
    if column == "done":
        return True
    if not str(dep.get("manual_steps") or "").strip():
        return False
    if column != "in_progress":
        return False
    if str(dep.get("link_state") or "") == "dispatching":
        return False
    flagger = str(dep.get("manual_session_id") or "")
    if flagger and flagger != str(dep.get("session_id") or ""):
        return False
    return not working


def eligible(rows: Iterable[Mapping], held_ids) -> list:
    """`rows` minus the cards held by a dependency, order kept.

    What the drain's head selection and `_slot_refusal`'s "queued ahead"
    rung both see, so a dependency-held card neither starts nor stands in
    anybody's way (the module docstring). `held_ids` is the ids the caller
    resolved as waiting on an unmet dependency.

    Pure, no I/O.
    """
    held = {str(i) for i in (held_ids or ())}
    return [row for row in (rows or ())
            if str((row or {}).get("id") or "") not in held]
