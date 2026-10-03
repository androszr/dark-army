"""The action journal in board.db (`docs/action-journal.md`). BoardStore owns
the connection and the lock; this mixin adds three tables and the verbs on
them.

An *intent* is written and committed before a side effect; a *result* is
written under the same id after it. An intent with no result is an
interrupted step. Fulfilment is id existence, so a second result under one id
is ignored. No foreign key to cards: the journal is a history, pruned by age
and size, and ordinary card deletion leaves it alone.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Callable, Optional

from . import action_journal

logger = logging.getLogger("dark-army.journal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS action_intents (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL, kind TEXT NOT NULL,
 subject TEXT NOT NULL, step TEXT NOT NULL, replay TEXT NOT NULL,
 attempt INTEGER NOT NULL, payload TEXT NOT NULL DEFAULT '',
 process TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_intent_action ON action_intents(action_id);
CREATE TABLE IF NOT EXISTS action_results (
 intent_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_attempts (
 kind TEXT NOT NULL, subject TEXT NOT NULL, attempts INTEGER NOT NULL,
 last_at REAL NOT NULL, PRIMARY KEY (kind, subject)
);
"""

#: The crash-point seam. `None` in production; a test sets it to a callable
#: taking `(phase, kind, step, id)`, called right after an intent commits
#: (`"intent"`, the instant before the side effect) and right after a result
#: commits (`"result"`).
CRASH_HOOK: Optional[Callable[[str, str, str, str], None]] = None


def _fire(phase: str, kind: str, step: str, ident: str) -> None:
    if CRASH_HOOK is not None:
        CRASH_HOOK(phase, kind, step, ident)


def _loads(text) -> dict:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class JournalStoreMixin:
    def _connect_journal(self):
        self._conn.executescript(SCHEMA)
        self._journal_process = secrets.token_hex(8)
        self.journal_prune()

    def journal_begin(self, kind: str, subject: str) -> str:
        """A fresh action id. Nothing is written until the first intent."""
        return secrets.token_hex(8)

    def journal_intent(self, action_id: str, kind: str, subject: str,
                       step: str, payload=None) -> str:
        """Write one intent, snapshotting the step's replay declaration, and
        step the subject's attempts in the same transaction. Returns the
        provisioned id. Raises `ValueError` for a payload carrying a
        forbidden key; nothing is written then."""
        payload = payload or {}
        bad = action_journal.forbidden_payload(payload)
        if bad:
            raise ValueError(f"journal payload carries a forbidden key: {bad}")
        replay = action_journal.declared(kind, step) or action_journal.NEVER
        first = next(iter(action_journal.ACTIONS.get(kind, {})), "")
        ident = secrets.token_hex(16)
        now = time.time()
        text = json.dumps(payload, sort_keys=True, default=str)
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT attempts, last_at FROM action_attempts"
                    " WHERE kind=? AND subject=?", (kind, subject)).fetchone()
                attempts = int(row["attempts"]) if row else 0
                if row and now - float(row["last_at"]) \
                        > action_journal.ATTEMPT_DECAY_SECONDS:
                    attempts = 0
                # Counted once per action, at its first step: the count is
                # how many times the action was begun for this subject.
                if step == first or not row:
                    attempts += 1
                self._conn.execute(
                    "INSERT INTO action_attempts(kind, subject, attempts, last_at)"
                    " VALUES (?,?,?,?) ON CONFLICT(kind, subject) DO UPDATE SET"
                    " attempts=excluded.attempts, last_at=excluded.last_at",
                    (kind, subject, attempts, now))
                self._conn.execute(
                    "INSERT INTO action_intents(id, action_id, kind, subject,"
                    " step, replay, attempt, payload, process, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ident, action_id, kind, subject, step, replay, attempts,
                     text, self._journal_process, now))
        _fire("intent", kind, step, ident)
        return ident

    def journal_result(self, intent_id: str, outcome: str, detail: str = "",
                       payload=None) -> bool:
        """Write the outcome under an intent's id. A second result under the
        same id is ignored; returns whether this one landed."""
        payload = payload or {}
        bad = action_journal.forbidden_payload(payload)
        if bad:
            raise ValueError(f"journal payload carries a forbidden key: {bad}")
        text = json.dumps(payload, sort_keys=True, default=str)
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO action_results(intent_id, outcome,"
                    " detail, payload, created_at) VALUES (?,?,?,?,?)",
                    (intent_id, str(outcome), str(detail or "")[:500], text,
                     time.time()))
                landed = cur.rowcount == 1
                meta = self._conn.execute(
                    "SELECT kind, step FROM action_intents WHERE id=?",
                    (intent_id,)).fetchone()
        if landed:
            _fire("result", meta["kind"] if meta else "",
                  meta["step"] if meta else "", intent_id)
        return landed

    def _journal_actions(self, where: str, args: tuple) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT i.id, i.action_id, i.kind, i.subject, i.step, i.replay,"
                " i.attempt, i.payload, i.process, i.created_at,"
                " r.outcome AS r_outcome, r.detail AS r_detail,"
                " r.payload AS r_payload, r.created_at AS r_at"
                " FROM action_intents i LEFT JOIN action_results r"
                " ON r.intent_id = i.id " + where +
                " ORDER BY i.created_at, i.rowid", args).fetchall()
        actions: dict = {}
        for row in rows:
            action = actions.setdefault(row["action_id"], {
                "action_id": row["action_id"], "kind": row["kind"],
                "subject": row["subject"], "created_at": row["created_at"],
                "steps": []})
            result = None
            if row["r_outcome"] is not None:
                result = {"outcome": row["r_outcome"], "detail": row["r_detail"],
                          "payload": _loads(row["r_payload"]),
                          "created_at": row["r_at"]}
            action["steps"].append({
                "id": row["id"], "step": row["step"], "replay": row["replay"],
                "attempt": row["attempt"], "payload": _loads(row["payload"]),
                "process": row["process"], "created_at": row["created_at"],
                "result": result})
        return list(actions.values())

    def journal_open(self) -> list:
        """Every unfinished action, each with all its steps and results,
        oldest first: one with an intent lacking a result, or one whose last
        step resolved as `action_journal.CONTINUES` names with the next step
        never written (a crash between a result and the next intent)."""
        open_ids = ("WHERE i.action_id IN (SELECT i2.action_id FROM"
                    " action_intents i2 LEFT JOIN action_results r2"
                    " ON r2.intent_id = i2.id WHERE r2.intent_id IS NULL)")
        found = {a["action_id"]: a for a in self._journal_actions(open_ids, ())}
        for kind, (step, outcomes, nxt) in action_journal.CONTINUES.items():
            marks = ",".join("?" for _ in outcomes)
            where = ("WHERE i.action_id IN (SELECT i3.action_id FROM"
                     " action_intents i3 JOIN action_results r3"
                     " ON r3.intent_id = i3.id WHERE i3.kind=? AND i3.step=?"
                     " AND r3.outcome IN (" + marks + ") AND NOT EXISTS"
                     " (SELECT 1 FROM action_intents n WHERE"
                     " n.action_id = i3.action_id AND n.step=?))")
            for action in self._journal_actions(
                    where, (kind, step) + tuple(outcomes) + (nxt,)):
                found.setdefault(action["action_id"], action)
        return sorted(found.values(), key=lambda a: a["created_at"])

    def journal_recent(self, kind: str, subject: str = "",
                       since: float = 0.0) -> list:
        """Actions of one kind (one subject, or all when `subject` is empty)
        with an intent at or after `since`, resolved or not."""
        where = "WHERE i.kind=? AND i.created_at>=?"
        args = [kind, float(since)]
        if subject:
            where += " AND i.subject=?"
            args.append(subject)
        return self._journal_actions(where, tuple(args))

    def journal_attempts(self, kind: str, subject: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM action_attempts WHERE kind=? AND subject=?",
                (kind, subject)).fetchone()
        return int(row["attempts"]) if row else 0

    def journal_prune(self, now: Optional[float] = None) -> int:
        """Delete resolved actions older than the retention and the oldest
        resolved ones beyond the row cap. An open intent is never pruned."""
        now = time.time() if now is None else now
        cutoff = now - action_journal.JOURNAL_RETENTION_DAYS * 86400
        # An action is resolved when every one of its intents has a result.
        removed = 0
        with self._lock:
            with self._conn:
                done = self._conn.execute(
                    "SELECT action_id, MIN(created_at) AS first_at,"
                    " MAX(created_at) AS last_at, COUNT(*) AS n"
                    " FROM action_intents GROUP BY action_id"
                    " HAVING SUM(CASE WHEN id IN (SELECT intent_id FROM"
                    " action_results) THEN 0 ELSE 1 END) = 0"
                    " ORDER BY first_at").fetchall()
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM action_intents").fetchone()[0]
                over = max(0, total - action_journal.MAX_JOURNAL_ROWS)
                doomed = []
                for row in done:
                    if row["last_at"] < cutoff:
                        doomed.append(row["action_id"])
                        over -= row["n"]
                    elif over > 0:
                        doomed.append(row["action_id"])
                        over -= row["n"]
                for aid in doomed:
                    self._conn.execute(
                        "DELETE FROM action_results WHERE intent_id IN"
                        " (SELECT id FROM action_intents WHERE action_id=?)",
                        (aid,))
                    cur = self._conn.execute(
                        "DELETE FROM action_intents WHERE action_id=?", (aid,))
                    removed += cur.rowcount
        return removed

    def journal_counts(self) -> dict:
        """Row counts, for tests and the self-check's summary line."""
        with self._lock:
            c = self._conn
            return {
                "intents": c.execute(
                    "SELECT COUNT(*) FROM action_intents").fetchone()[0],
                "results": c.execute(
                    "SELECT COUNT(*) FROM action_results").fetchone()[0],
                "attempts": c.execute(
                    "SELECT COUNT(*) FROM action_attempts").fetchone()[0],
            }
