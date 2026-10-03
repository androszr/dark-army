"""Launch-time recovery of the actions Dark Army was in the middle of
(`docs/action-journal.md`) — the daemon half of the intent journal.

`BobDaemon` inherits `RecoveryMixin` as it inherits `BoardVerbsMixin`. The
journal's tables and verbs are `board_journal_store.py`'s and the declarations
are `action_journal.py`'s; this module holds the three helpers the call sites
use, the stop verb's recorded wrapper, and `recover_actions`.

**Recovery is never a launcher.** Every branch writes a card or a memory map
and nothing else: it opens no terminal, signals no process, types no key and
moves no card to Done. A step that is `SAFE` to repeat (a bookkeeping write)
is finished; a step that is `NEVER` safe is recorded as interrupted and
said in words, on the card or in the diary.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import psutil

from . import action_journal
from . import dispatch

logger = logging.getLogger("dark-army.journal")

#: A `dispatching` card whose recorded write is within this of the receipt's
#: time is counted as having one (`journal_self_check`).
RECEIPT_TOLERANCE_SECONDS = 5.0


def journal_self_check(cards, open_after, actions=(), *, now=None,
                       isdir=os.path.isdir) -> dict:
    """The mismatch classes between the journal and the board, each a list
    of ids and never a path or a payload. Pure over dicts (`isdir` is the one
    seam, for tests)."""
    now = time.time() if now is None else now
    receipts: dict = {}
    recent_record: dict = {}
    for action in actions or ():
        if action.get("kind") != "card_start":
            continue
        for step in action.get("steps") or []:
            result = step.get("result") or {}
            if step.get("step") == "record" and result.get("outcome") in (
                    action_journal.COMPLETED, action_journal.REPLAYED):
                cid = str(action.get("subject") or "")
                receipts.setdefault(cid, []).append(
                    float((step.get("payload") or {}).get("now") or 0.0))
                recent_record[cid] = float(result.get("created_at") or 0.0)
    dispatching, record_unlinked, absent = [], [], []
    for card in cards or ():
        cid = str(card.get("id") or "")
        column = str(card.get("column_name") or "")
        link = str(card.get("link_state") or "")
        if link == "dispatching":
            at = float(card.get("dispatched_at") or 0.0)
            if not any(abs(t - at) <= RECEIPT_TOLERANCE_SECONDS
                       for t in receipts.get(cid, ())):
                dispatching.append(cid)
        elif (cid in recent_record and link != "live" and column != "done"
              and now - recent_record[cid] <= dispatch.DISPATCH_BIND_WINDOW):
            record_unlinked.append(cid)
        path = str(card.get("worktree_path") or "")
        if path and column != "done" and not isdir(path):
            absent.append(cid)
    return {
        "open_after": [str(a.get("action_id") or "") for a in open_after or ()],
        "dispatching_without_receipt": dispatching,
        "record_without_card_link": record_unlinked,
        "worktree_recorded_but_absent": absent,
    }


class RecoveryMixin:
    # --- the three helpers every journalled call site uses -----------------
    #
    # A journal that cannot be written never stops the thing the person asked
    # for: the failure is one warning and the action proceeds unjournalled,
    # which is exactly how it ran before the journal existed. (`CRASH_HOOK`'s
    # simulated crash is a `BaseException` and passes straight through.)

    async def _journal_begin(self, kind: str, subject: str) -> str:
        try:
            return str(await self._board_call("journal_begin", kind, subject)
                       or "")
        except Exception:
            logger.warning("journal: could not begin %s", kind, exc_info=True)
            return ""

    async def _journal_intent(self, action_id: str, kind: str, subject: str,
                              step: str, payload=None) -> str:
        if not action_id:
            return ""
        try:
            return str(await self._board_call(
                "journal_intent", action_id, kind, subject, step, payload or {})
                or "")
        except Exception:
            logger.warning("journal: could not write the %s/%s intent", kind,
                           step, exc_info=True)
            return ""

    async def _journal_result(self, intent_id: str, outcome: str,
                              detail: str = "", payload=None) -> bool:
        if not intent_id:
            return False
        try:
            return bool(await self._board_call(
                "journal_result", intent_id, outcome, detail, payload or {}))
        except Exception:
            logger.warning("journal: could not write a result", exc_info=True)
            return False

    # --- Stop ---------------------------------------------------------------

    async def stop_session_recorded(self, session_id: str) -> tuple:
        """`stop_session`, with its intent written before the signal and its
        result after. The sync verb and its identity guard are unchanged; a
        direct caller of `stop_session` (a test) leaves no row."""
        pid = None
        try:
            st = self._session_states.get(session_id)
            if st:
                pid = self._ensure_session_pid(session_id, st)
            if pid is None:
                rec = self._agent_records.get(session_id)
                grok = self._grok_records.get(session_id)
                pid = (rec.pid if rec is not None
                       else grok.pid if grok is not None else None)
        except Exception:
            pid = None
        intent = ""
        if pid is not None:
            intent = await self._journal_intent(
                await self._journal_begin("stop_session", session_id),
                "stop_session", session_id, "terminate",
                {"session_id": session_id, "pid": int(pid),
                 "provider": self._session_provider(session_id) or ""})
        try:
            ok, detail = self.stop_session(session_id)
        except BaseException:
            await self._journal_result(intent, "refused", "raised")
            raise
        outcome = ("gone" if detail == "already stopped"
                   else "signalled" if ok else "refused")
        await self._journal_result(intent, outcome, "" if ok else str(detail))
        return ok, detail

    # --- recovery -----------------------------------------------------------

    def _log_interrupted(self, kind: str, step: str, subject: str) -> None:
        """One warning and one diary line for a step that will not be
        repeated."""
        logger.warning("action journal: %s/%s on %s was interrupted by a "
                       "restart and is not repeated", kind, step, subject[:12])
        # Empty at startup (no snapshot has named anyone yet): the diary then
        # words the line without a subject instead of saying "a session".
        nick = ""
        try:
            nick = self._nicknames_shown.get(subject, "")
        except Exception:
            nick = ""
        self._log_event("action_interrupted", session_id=subject,
                        nickname=nick, detail={"kind": kind, "step": step})

    async def _jr(self, rep: dict, step: dict, outcome: str, detail: str = "",
                  payload=None) -> None:
        """Resolve one step and count it."""
        landed = await self._journal_result(step["id"], outcome, detail, payload)
        if not landed:
            return
        step["result"] = {"outcome": outcome, "detail": detail,
                          "payload": payload or {}}
        if outcome == action_journal.REPLAYED:
            rep["replayed"] += 1
        elif outcome == action_journal.INTERRUPTED:
            rep["interrupted"] += 1

    async def _note_card(self, cid: str, card: dict, note: str,
                         dequeue: bool) -> None:
        """The orange line on the card, and the diary's `card_dispatch_failed`.
        The column is never moved."""
        fields = {"dispatch_error": note}
        if dequeue:
            # `_queue_hard_refusal`'s rule: the drain must never replay a
            # press whose outcome is unknown.
            fields["queue_state"] = ""
            fields["queued_at"] = None
        await self._board_call("update", cid, fields, bump=False)
        self._log_card_event(card, "card_dispatch_failed", error=note)

    def _restore_receipts(self, cid: str, spawn_payload: dict, at: float) -> None:
        """Put the bind's receipts back, only inside the bind window and only
        for a pid that still exists (and, for a pty, still one the broker
        holds). The bind's own descent and candidate rules still decide."""
        if time.time() - at > dispatch.DISPATCH_BIND_WINDOW:
            return
        self._dispatch_baseline[cid] = set()
        shell = int(spawn_payload.get("shell_pid") or 0)
        pty_pid = int(spawn_payload.get("pty_pid") or 0)
        if shell and psutil.pid_exists(shell):
            self._spawn_shell_pids[cid] = shell
        if pty_pid and psutil.pid_exists(pty_pid):
            owns = getattr(getattr(self, "_pty", None), "owns", None)
            if owns is not None and owns(pty_pid) is not None:
                self._spawn_pty_pids[cid] = pty_pid

    def _restore_finished_receipts(self, cards, actions) -> None:
        """A Start whose record landed, then the process died inside the bind
        window: the card is `dispatching` and the bind has lost its receipts.
        Put them back (same rule, same window) so the bind does not have to
        fall back to the predicate alone."""
        by_id = {str(c.get("id") or ""): c for c in cards}
        for action in actions:
            if action.get("kind") != "card_start":
                continue
            steps = {s["step"]: s for s in action["steps"]}
            spawn, record = steps.get("spawn"), steps.get("record")
            if not spawn or not record or not spawn["result"] \
                    or not record["result"]:
                continue
            if spawn["result"].get("outcome") != "spawned" or record["result"] \
                    .get("outcome") not in (action_journal.COMPLETED,
                                            action_journal.REPLAYED):
                continue
            cid = str(action["subject"])
            card = by_id.get(cid)
            done = spawn["result"].get("payload") or {}
            now = float(done.get("now") or 0.0)
            if card is None or str(card.get("link_state") or "") != "dispatching" \
                    or abs(float(card.get("dispatched_at") or 0.0) - now) > 0.001:
                continue
            self._restore_receipts(cid, done, now)

    async def _recover_card_start(self, action: dict, rep: dict) -> None:
        cid = str(action["subject"])
        steps = {s["step"]: s for s in action["steps"]}
        spawn, record = steps.get("spawn"), steps.get("record")
        card = await self._board_call("get", cid)
        if spawn is None:
            return
        spawn_payload = spawn["payload"]
        if spawn["result"] is None:
            await self._jr(rep, spawn, action_journal.INTERRUPTED,
                           "restart during spawn")
            if card is not None and not str(card.get("link_state") or "") \
                    and not str(card.get("session_id") or ""):
                await self._note_card(cid, card,
                                      action_journal.RESTART_DURING_START_NOTE,
                                      True)
            return
        if spawn["result"].get("outcome") != "spawned":
            if record is not None and record["result"] is None:
                await self._jr(rep, record, action_journal.INTERRUPTED,
                               "spawn did not open a terminal")
            return
        done = spawn["result"].get("payload") or {}
        now = float(done.get("now") or 0.0)
        if record is None:
            # The crash fell between the spawn's result and the record intent.
            ident = await self._journal_intent(
                action["action_id"], "card_start", cid, "record",
                {"card_id": cid, "now": now,
                 "cwd": str(spawn_payload.get("cwd") or ""),
                 "root": str(spawn_payload.get("root") or ""),
                 "batch": bool(spawn_payload.get("batch"))})
            if not ident:
                return
            record = {"id": ident, "step": "record",
                      "replay": action_journal.declared("card_start", "record"),
                      "attempt": spawn.get("attempt", 1), "result": None,
                      "payload": {"batch": bool(spawn_payload.get("batch")),
                                  "cwd": str(spawn_payload.get("cwd") or ""),
                                  "root": str(spawn_payload.get("root") or "")}}
            action["steps"].append(record)
        if record["result"] is not None:
            return
        verdict = dict(action_journal.decide(action)).get("record")
        link = str((card or {}).get("link_state") or "")
        if card is None:
            await self._jr(rep, record, action_journal.INTERRUPTED, "card gone")
            return
        landed = (link == "dispatching" and abs(
            float(card.get("dispatched_at") or 0.0) - now) < 0.001)
        if landed:
            # The write reached the board; only its result was lost.
            await self._jr(rep, record, action_journal.COMPLETED,
                           "already written")
            self._restore_receipts(cid, done, now)
            return
        if link or str(card.get("session_id") or ""):
            # Reset or started again meanwhile: left alone.
            await self._jr(rep, record, action_journal.INTERRUPTED,
                           "card moved on")
            return
        if verdict != action_journal.REPLAYED or record["payload"].get("batch"):
            await self._jr(rep, record, action_journal.INTERRUPTED,
                           "not replayed")
            await self._note_card(cid, card,
                                  action_journal.RESTART_DURING_START_NOTE, True)
            return
        fields = {"link_state": "dispatching", "column_name": "in_progress",
                  "dispatched_at": now, "dispatch_error": "",
                  "queue_state": "", "queued_at": None}
        await self._board_call("update", cid, fields, bump=False)
        await self._jr(rep, record, action_journal.REPLAYED)
        self._restore_receipts(cid, done, now)
        self._schedule_work_baseline(
            cid, str(spawn_payload.get("cwd") or spawn_payload.get("root") or ""),
            now)
        self._log_card_event(card, "card_dispatched",
                             tool=str(spawn_payload.get("tool") or ""),
                             root=str(spawn_payload.get("root") or ""))

    async def _recover_worktree_prepare(self, action: dict, rep: dict) -> None:
        cid = str(action["subject"])
        steps = {s["step"]: s for s in action["steps"]}
        add, record = steps.get("add"), steps.get("record")
        if add is None:
            return
        loop = asyncio.get_running_loop()
        card = await self._board_call("get", cid)
        add_payload = add["payload"]
        starting = bool(add_payload.get("start"))
        dequeue = bool(add_payload.get("queued_replay"))
        note = action_journal.RESTART_DURING_PREPARE_NOTE
        if add["result"] is None:
            await self._jr(rep, add, action_journal.INTERRUPTED,
                           "restart during add")
            if card is not None and starting:
                await self._note_card(cid, card, note, dequeue)
            return
        if add["result"].get("outcome") not in ("added", "reused"):
            if record is not None and record["result"] is None:
                await self._jr(rep, record, action_journal.INTERRUPTED,
                               "add did not land")
            return
        if record is None:
            ident = await self._journal_intent(
                action["action_id"], "worktree_prepare", cid, "record",
                {"card_id": cid, "path": str(add_payload.get("path") or ""),
                 "branch": str(add_payload.get("branch") or "")})
            if not ident:
                return
            record = {"id": ident, "step": "record",
                      "replay": action_journal.declared("worktree_prepare",
                                                        "record"),
                      "attempt": add.get("attempt", 1), "result": None,
                      "payload": {}}
            action["steps"].append(record)
        if record["result"] is not None:
            return
        verdict = dict(action_journal.decide(action)).get("record")
        path = str(add_payload.get("path") or "")
        branch = str(add_payload.get("branch") or "")
        present = bool(path) and await loop.run_in_executor(
            None, os.path.isdir, path)
        if card is None:
            await self._jr(rep, record, action_journal.INTERRUPTED, "card gone")
            return
        if verdict == action_journal.REPLAYED and present:
            recorded = await self._board_call("record_worktree", cid, path,
                                              branch)
            if recorded and recorded[0] is not None:
                await self._jr(rep, record, action_journal.REPLAYED)
            else:
                await self._jr(rep, record, action_journal.INTERRUPTED,
                               "not recorded")
        else:
            await self._jr(rep, record, action_journal.INTERRUPTED,
                           "not replayed")
        if starting:
            # The press's own re-entry died with the process, and recovery
            # does not spawn: the person presses Start again.
            await self._note_card(cid, card, note, dequeue)

    async def _recover_keystroke_or_signal(self, action: dict, rep: dict) -> None:
        """Stop, auto-compact, typed answer: interrupted, never repeated."""
        for step in action["steps"]:
            if step["result"] is None:
                await self._jr(rep, step, action_journal.INTERRUPTED,
                               "restart")
                self._log_interrupted(action["kind"], step["step"],
                                      str(action["subject"]))

    _recover_stop_session = _recover_keystroke_or_signal
    _recover_autocompact = _recover_keystroke_or_signal
    _recover_answer_burst = _recover_keystroke_or_signal

    async def _seed_autocompact(self) -> None:
        """A session asked to compact just before a restart is not asked
        again until the settle window has passed: seed the policy with the
        episode the last process began."""
        from . import autocompact
        since = time.time() - autocompact.SETTLE_SECONDS
        recent = await self._board_call("journal_recent", "autocompact", "",
                                        since) or []
        policy = None
        for action in recent:
            for step in action["steps"]:
                outcome = (step["result"] or {}).get("outcome")
                if outcome == "not_landed":
                    continue
                if policy is None:
                    policy = self.__dict__.setdefault(
                        "_autocompact", autocompact.AutoCompactPolicy())
                policy.restore(str(action["subject"]), float(step["created_at"]))

    async def recover_actions(self) -> dict:
        """Finish the harmless half of every unfinished action and refuse to
        repeat the dangerous half. Loop; every store call is a `_board_call`
        hop. Idempotent: a second call finds nothing open. Returns the report
        and keeps it as `self._journal_report`."""
        rep = {"open": 0, "replayed": 0, "interrupted": 0, "mismatches": {}}
        opened = await self._board_call("journal_open")
        if opened is None:
            self._journal_report = rep
            return rep
        rep["open"] = len(opened)
        for action in opened:
            handler = getattr(self, "_recover_" + str(action["kind"]), None)
            try:
                if handler is not None:
                    await handler(action, rep)
            except Exception:
                logger.warning("action journal: recovering %s failed",
                               action.get("kind"), exc_info=True)
            # Whatever a handler left open is interrupted: no intent stays
            # open after a launch, and a kind this build does not know is
            # never replayed.
            for step in action["steps"]:
                if step["result"] is None:
                    await self._jr(rep, step, action_journal.INTERRUPTED,
                                   "unrecovered")
        try:
            await self._seed_autocompact()
        except Exception:
            logger.warning("action journal: could not seed auto-compact",
                           exc_info=True)
        cards = await self._board_call("cards") or []
        recent = await self._board_call(
            "journal_recent", "card_start", "",
            time.time() - 86400.0) or []
        open_after = await self._board_call("journal_open") or []
        self._restore_finished_receipts(cards, recent)
        loop = asyncio.get_running_loop()
        rep["mismatches"] = await loop.run_in_executor(
            None, lambda: journal_self_check(cards, open_after, recent))
        logger.info("action journal: %d open, %d replayed, %d interrupted, "
                    "%d mismatches", rep["open"], rep["replayed"],
                    rep["interrupted"],
                    sum(len(v) for v in rep["mismatches"].values()))
        for name, ids in rep["mismatches"].items():
            if ids:
                logger.warning("action journal: %s: %d card(s)/action(s)",
                               name, len(ids))
        self._journal_report = rep
        return rep
