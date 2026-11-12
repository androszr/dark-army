"""The daemon half of the steering queue (`docs/steering-queue.md`).

`BobDaemon` inherits `SteeringMixin` as it inherits `RecoveryMixin`. Every
verb that types into a session hands its work to `_steer`, which gives the
session's *lane* the entry in arrival order, writes the words down before
they are typed (the table is `board_steering_store.py`'s, the lanes are
`steering.py`'s) and answers with what the verb's own body answered.

**Replay is never a launcher.** `recover_steering` types again only what
never started and is still fresh; it opens no terminal, starts no session and
never reaches `dispatch` or a consult.

Nothing here reaches `daemon` at import time (`daemon.py` imports this module
to build the class).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import steering

logger = logging.getLogger("dark-army.steering")

#: kind -> the `_now` body `_steer_run` calls for a durable entry. A kind not
#: here is memory-only and carries its own `work`. `answer_many` is the
#: batch verb's body, chosen by the payload's shape.
STEER_BODIES = {
    "reply": "_reply_to_session_now",
    "wrap_up": "_wrap_up_session_now",
    "low_priority": "_low_priority_session_now",
    "answer": "_answer_question_now",
    "answer_many": "_answer_questions_now",
    "card_message": "_message_card_now",
    "card_ask": "_ask_card_push_now",
    "terminal_line": "_terminal_line_now",
}

#: kind -> the action the verb journals itself (`journal == "own"`).
OWN_ACTIONS = {"answer": "answer_burst", "autocompact": "autocompact"}

_CARD_KINDS = ("card_message", "card_ask")


class SteeringMixin:
    # --- the lanes ---------------------------------------------------------

    def _steering_lanes(self) -> steering.Lanes:
        """Lazy, because tests build `BobDaemon(headless=True)` and some build
        one with `__new__`."""
        lanes = self.__dict__.get("_steer_lanes")
        if lanes is None:
            lanes = self.__dict__.setdefault(
                "_steer_lanes", steering.Lanes(self._steer_execute))
        return lanes

    def _steer_lane_key(self, session_id: str) -> str:
        """`pty:<handle>` when the session runs in a terminal Dark Army hosts
        (the input line is the real resource, so a `/clear`'d session's new
        id shares the lane), else `sid:<session id>`."""
        try:
            handle = self._pty_handle_for(session_id) if session_id else None
        except Exception:
            handle = None
        return ("pty:" + str(handle)) if handle else "sid:" + str(session_id)

    # --- one entry point ---------------------------------------------------

    async def _steer(self, kind: str, session_id: str, payload: dict, *,
                     work=None, key: Optional[str] = None) -> tuple:
        """Type `kind` into `session_id`, after anything already waiting for
        it. `(ok, detail)`: the body's own answer, or
        `STEER_QUEUED_NOTE` when the wait passes
        `STEER_REPLY_WAIT_SECONDS` (the entry is still typed).

        `work` is a memory-only coroutine function run in place of the
        kind's body; `key` names a lane that is not the session's."""
        lanes = self._steering_lanes()
        key = key or self._steer_lane_key(session_id)
        if lanes.running_key() == key:
            # Already inside this lane's entry: waiting for the lane would
            # wait for ourselves.
            return await self._steer_inline(kind, session_id, payload, work)
        kd = steering.KINDS[kind]
        caller = steering.CALLER.get()
        now = time.time()
        durable = (kd.durable and work is None
                   and getattr(self, "_board", None) is not None)
        if durable and caller.dedupe:
            seen = await self._steer_seen(lanes, caller.dedupe, now)
            if seen is not None:
                return seen
        twin = lanes.find_replay_twin(key, kind, payload, caller.source, now)
        if twin is not None and twin.future is not None:
            return await self._steer_wait(twin.future)
        entry = steering.Entry(
            kind=kind, session_id=session_id, payload=dict(payload or {}),
            source=caller.source, dedupe=caller.dedupe, created_at=now,
            work=work)
        # The place in the lane is taken now, before any store write, so
        # arrival order is the order of the calls.
        future = lanes.reserve(key, entry)
        if future is None:
            return False, steering.STEER_FULL_REFUSAL
        try:
            if durable:
                await self._steer_write_down(entry, kd)
        except BaseException:
            lanes.withdraw(entry)
            raise
        lanes.release(entry)
        return await self._steer_wait(future)

    def _steer_nowait(self, kind: str, session_id: str, work, *,
                      key: Optional[str] = None):
        """Queue a memory-only entry and do not wait for it: the future, or
        `None` when the lane is full."""
        lanes = self._steering_lanes()
        key = key or self._steer_lane_key(session_id)
        entry = steering.Entry(kind=kind, session_id=session_id, work=work,
                               created_at=time.time())
        return lanes.submit(key, entry)

    def _steer_raw(self, key: str, write) -> tuple:
        """Raw keys: written at once on an idle lane, in order behind
        anything else."""
        return self._steering_lanes().offer_raw(key, write)

    async def _steer_wait(self, future) -> tuple:
        """Await an entry without owning it: a caller that gives up (the
        client hung up, or the wait ran out) leaves the typing running."""
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), steering.STEER_REPLY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return True, steering.STEER_QUEUED_NOTE

    async def _steer_inline(self, kind, session_id, payload, work) -> tuple:
        if work is not None:
            return await work()
        return await self._steer_body(kind, session_id, payload or {})

    async def _steer_seen(self, lanes, dedupe: str, now: float):
        """A phone press whose one-time mark this queue has already recorded:
        its answer, or `None` when it is new."""
        try:
            row = await self._board_call(
                "steer_by_dedupe", dedupe,
                now - steering.STEER_RETENTION_SECONDS)
        except Exception:
            logger.warning("steering: could not look up a phone mark",
                           exc_info=True)
            return None
        if not row:
            return None
        state = str(row.get("state") or "")
        if state == "queued":
            live = lanes.entry(str(row.get("id") or ""))
            if live is not None and live.future is not None:
                return await self._steer_wait(live.future)
            return True, steering.STEER_QUEUED_NOTE
        if state == "interrupted":
            return False, steering.STEER_INTERRUPTED_REFUSAL
        return bool(row.get("ok")), str(row.get("detail") or "")

    async def _steer_write_down(self, entry: steering.Entry, kd) -> None:
        """Write the entry in the queue's table before anything is typed. A
        store that cannot be written is logged once per process and the entry
        runs memory-only: typing is never refused because the record could
        not be made."""
        try:
            if kd.journal == "steer":
                entry.action_id = await self._journal_begin(
                    "steer", entry.session_id)
            row = await self._board_call("steer_enqueue", {
                "session_id": entry.session_id, "kind": entry.kind,
                "source": entry.source, "dedupe": entry.dedupe,
                "payload": entry.payload, "action_id": entry.action_id,
                "created_at": entry.created_at})
        except Exception:
            if not self.__dict__.get("_steer_warned"):
                self.__dict__["_steer_warned"] = True
                logger.warning("steering: could not write an entry down; "
                               "typing it unrecorded", exc_info=True)
            return
        if row:
            entry.id = str(row.get("id") or "")
            entry.recorded = bool(entry.id)

    # --- the lane's worker -------------------------------------------------

    async def _steer_body(self, kind: str, session_id: str, payload: dict):
        """Run the kind's `_now` body with its recorded arguments."""
        p = payload or {}
        if kind == "reply":
            return await self._reply_to_session_now(session_id, p.get("text"))
        if kind == "wrap_up":
            return await self._wrap_up_session_now(session_id)
        if kind == "low_priority":
            return await self._low_priority_session_now(session_id)
        if kind == "answer":
            if "option_indexes" in p:
                return await self._answer_questions_now(
                    session_id, p.get("question_id"), p.get("option_indexes"))
            return await self._answer_question_now(
                session_id, p.get("question_id"), p.get("option_index"))
        if kind == "card_message":
            return await self._message_card_now(
                p.get("card_id"), p.get("text"))
        if kind == "card_ask":
            return await self._ask_card_push_now(
                p.get("card_id"), p.get("text"))
        if kind == "terminal_line":
            return await self._terminal_line_now(
                session_id, p.get("text"), bool(p.get("from_phone")))
        return False, steering.STEER_FAILED_REFUSAL

    async def _steer_run(self, entry: steering.Entry) -> tuple:
        if entry.work is not None:
            return await entry.work()
        return await self._steer_body(
            entry.kind, entry.session_id, entry.payload)

    async def _steer_execute(self, entry: steering.Entry) -> tuple:
        """One entry at its turn, on the loop, inside its lane's worker."""
        kd = steering.KINDS.get(entry.kind)
        if entry.replayed and not steering.is_fresh(entry.created_at,
                                                    time.time()):
            # Freshness is judged at the moment of typing: a replay that
            # waited out the settle pause past two minutes is dropped.
            await self._steer_settle(entry, "not_delivered", False,
                                     steering.STEER_NOT_DELIVERED_WHY)
            await self._steer_surface_entry(entry, "not_delivered", "")
            return False, steering.STEER_NOT_DELIVERED_WHY
        intent = ""
        if entry.recorded and kd is not None and kd.journal == "steer":
            # The words are never in the journal payload.
            intent = await self._journal_intent(
                entry.action_id, "steer", entry.session_id, "type",
                {"entry_id": entry.id, "kind": entry.kind,
                 "source": entry.source})
        state = "typed"
        try:
            ok, detail = await self._steer_run(entry)
        except asyncio.CancelledError:
            # Shutdown: the intent stays open and the next launch calls it
            # interrupted; the row stays queued.
            raise
        except Exception:
            logger.warning("steering: a %s entry raised", entry.kind,
                           exc_info=True)
            ok, detail, state = False, steering.STEER_FAILED_REFUSAL, "failed"
        else:
            ok = bool(ok)
            detail = str(detail or "")
            state = "typed" if ok else "refused"
        await self._journal_result(
            intent, "typed" if ok else "refused", detail)
        await self._steer_settle(entry, state, ok, detail)
        if entry.replayed:
            if ok:
                self._steer_log_replayed(entry)
            else:
                await self._steer_surface_entry(entry, "not_delivered", detail)
        return ok, detail

    async def _steer_settle(self, entry: steering.Entry, state: str, ok: bool,
                            detail: str) -> None:
        """Resolve the entry's row, which blanks the words with it."""
        if not entry.recorded:
            return
        try:
            await self._board_call("steer_resolve", entry.id, state, ok,
                                   detail)
        except Exception:
            logger.warning("steering: could not resolve an entry",
                           exc_info=True)

    # --- the diary and the card's thread -------------------------------------

    def _steer_nickname(self, session_id: str) -> str:
        try:
            return str(self._nicknames_shown.get(session_id, "") or "")
        except Exception:
            return ""

    def _steer_detail(self, kind: str, source: str, created_at, why: str):
        try:
            age = max(0, int(time.time() - float(created_at)))
        except (TypeError, ValueError):
            age = 0
        return {"kind": str(kind), "source": str(source), "age_s": str(age),
                "why": str(why or "")}

    def _steer_log_replayed(self, entry: steering.Entry) -> None:
        self._log_event(
            "steer_replayed", session_id=entry.session_id,
            nickname=self._steer_nickname(entry.session_id),
            detail=self._steer_detail(entry.kind, entry.source,
                                      entry.created_at, ""))

    async def _steer_surface_entry(self, entry: steering.Entry, state: str,
                                   why: str) -> None:
        """A replayed entry that was not typed: the diary line, and for a
        card kind a note on the card's thread."""
        row = {"kind": entry.kind, "session_id": entry.session_id,
               "source": entry.source, "created_at": entry.created_at,
               "payload": entry.payload}
        await self._steer_surface(row, state, why)

    async def _steer_surface(self, row: dict, state: str, why: str = "") -> None:
        """Say that a message was not delivered (`not_delivered`, in the
        diary and on the card) or may have only partly landed (`interrupted`,
        on the card alone: `recover_actions` already wrote its diary line).
        Words reach the card's thread only, as an 80-character excerpt."""
        kind = str(row.get("kind") or "")
        sid = str(row.get("session_id") or "")
        payload = row.get("payload") or {}
        try:
            if state == "not_delivered":
                self._log_event(
                    "steer_not_delivered", session_id=sid,
                    nickname=self._steer_nickname(sid),
                    detail=self._steer_detail(
                        kind, row.get("source") or "mac",
                        row.get("created_at"), why))
            if kind in _CARD_KINDS and payload.get("card_id"):
                note = (steering.STEER_CARD_INTERRUPTED_NOTE
                        if state == "interrupted"
                        else steering.STEER_CARD_NOT_DELIVERED_NOTE.format(
                            excerpt=str(payload.get("text") or "")[:80]))
                await self._board_call(
                    "add_message", str(payload["card_id"]), "bob", note,
                    "note", "")
        except Exception:
            logger.warning("steering: could not say a message was not "
                           "delivered", exc_info=True)

    # --- restart -------------------------------------------------------------

    async def _steer_journal_state(self, row: dict) -> str:
        """`none`, `open`, `interrupted` or `finished:<outcome>` for a row the
        queue journals itself; `none` or `own` for one the verb does."""
        kd = steering.KINDS.get(str(row.get("kind") or ""))
        sid = str(row.get("session_id") or "")
        created = float(row.get("created_at") or 0.0)
        if kd is not None and kd.journal == "own":
            action = OWN_ACTIONS.get(str(row["kind"]), "answer_burst")
            recent = await self._board_call(
                "journal_recent", action, sid, created) or []
            return "own" if recent else "none"
        actions = await self._board_call(
            "journal_recent", "steer", sid, created - 1.0) or []
        for action in actions:
            if action.get("action_id") != row.get("action_id"):
                continue
            for step in action.get("steps") or []:
                if step.get("step") != "type":
                    continue
                result = step.get("result")
                if result is None:
                    return "open"
                outcome = str(result.get("outcome") or "")
                return ("interrupted" if outcome == "interrupted"
                        else "finished:" + outcome)
        return "none"

    async def recover_steering(self) -> dict:
        """Settle every row the last process left `queued`, after
        `recover_actions` has closed its journal. Loop; idempotent (a second
        call finds every row resolved or already in a lane). Returns
        `{"replayed", "dropped", "interrupted", "finished"}`.

        **Never a launcher**: nothing here reaches `dispatch`, a consult or a
        start. A replayed entry re-runs its verb's own guards at its turn."""
        rep = {"replayed": 0, "dropped": 0, "interrupted": 0, "finished": 0}
        if getattr(self, "_board", None) is None:
            return rep
        lanes = self._steering_lanes()
        rows = await self._board_call("steer_queued") or []
        now = time.time()
        replays = []
        for row in rows:
            if lanes.has(str(row.get("id") or "")):
                continue
            state = await self._steer_journal_state(row)
            verdict = steering.replay_verdict(row, state, now)
            ident = str(row["id"])
            if verdict == "finished":
                outcome = state.split(":", 1)[1] if ":" in state else ""
                ok = outcome == "typed"
                await self._board_call(
                    "steer_resolve", ident, "typed" if ok else "refused", ok,
                    "")
                rep["finished"] += 1
            elif verdict == "interrupted":
                await self._steer_surface(row, "interrupted")
                await self._board_call(
                    "steer_resolve", ident, "interrupted", False,
                    steering.STEER_INTERRUPTED_REFUSAL)
                rep["interrupted"] += 1
            elif verdict == "own":
                await self._board_call(
                    "steer_resolve", ident, "interrupted", False,
                    steering.STEER_INTERRUPTED_REFUSAL)
                rep["interrupted"] += 1
            elif verdict == "not_delivered":
                await self._steer_surface(row, "not_delivered")
                await self._board_call(
                    "steer_resolve", ident, "not_delivered", False,
                    steering.STEER_NOT_DELIVERED_WHY)
                rep["dropped"] += 1
            else:
                replays.append(row)
        by_lane: dict = {}
        for row in sorted(replays, key=lambda r: float(r["created_at"])):
            by_lane.setdefault(
                self._steer_lane_key(str(row["session_id"])), []).append(row)
        for key, group in by_lane.items():
            lanes.hold_until(
                key, time.monotonic() + steering.STEER_REPLAY_SETTLE_SECONDS)
            for row in group:
                entry = steering.Entry(
                    kind=str(row["kind"]), session_id=str(row["session_id"]),
                    payload=dict(row.get("payload") or {}),
                    source=str(row.get("source") or "mac"),
                    dedupe=str(row.get("dedupe") or ""),
                    created_at=float(row["created_at"]),
                    id=str(row["id"]), action_id=str(row.get("action_id") or ""),
                    replayed=True, recorded=True)
                lanes.submit(key, entry, force=True)
                rep["replayed"] += 1
        logger.info("steering: %d replayed, %d not delivered, %d interrupted, "
                    "%d finished", rep["replayed"], rep["dropped"],
                    rep["interrupted"], rep["finished"])
        return rep

    async def _stop_steering(self) -> None:
        """Shutdown: the entry typing now gets a short grace to finish, then
        is cancelled (the next launch calls it interrupted); everything
        queued stays on disk for the next launch's freshness rule."""
        lanes = self.__dict__.get("_steer_lanes")
        if lanes is None:
            return
        try:
            await lanes.stop(steering.STEER_SHUTDOWN_GRACE_SECONDS)
        except Exception:
            logger.warning("steering: stopping the lanes failed", exc_info=True)
