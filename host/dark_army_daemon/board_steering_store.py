"""The steering queue's one table in board.db (`docs/steering-queue.md`).
BoardStore owns the connection and the lock; this mixin adds the table and
the verbs on it.

A row is written and committed **before** the words are typed, and resolved
afterwards. The words (`payload`) are blanked in the same statement that
resolves the row, so they stay on disk only while the message waits. A
`queued` row is never pruned: it is either typed, judged by freshness or
reported at the next launch. No foreign key to cards or sessions: the ids are
opaque, as in the action journal.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Optional

from . import action_journal, board_journal_store, steering

SCHEMA = """
CREATE TABLE IF NOT EXISTS steer_queue (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL, session_id TEXT NOT NULL,
 kind TEXT NOT NULL, source TEXT NOT NULL, dedupe TEXT NOT NULL DEFAULT '',
 payload TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', ok INTEGER NOT NULL DEFAULT 0,
 detail TEXT NOT NULL DEFAULT '', resolved_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS steer_queue_state ON steer_queue(state, created_at);
CREATE INDEX IF NOT EXISTS steer_queue_dedupe ON steer_queue(dedupe);
"""


def _row(row) -> dict:
    out = dict(row)
    try:
        payload = json.loads(out.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    out["payload"] = payload if isinstance(payload, dict) else {}
    out["ok"] = bool(out.get("ok"))
    return out


class SteeringStoreMixin:
    def _connect_steering(self):
        self._conn.executescript(SCHEMA)
        self.steer_prune()

    def steer_enqueue(self, entry: dict) -> dict:
        """Write one entry down before it is typed. `entry` carries
        `session_id`, `kind`, `source`, `dedupe`, `payload` (a dict), the
        `action_id` the verb's journal began and optionally `created_at`.
        Returns the row. Raises `ValueError` for a payload carrying a
        `FORBIDDEN_KEYS` name at any depth; nothing is written then."""
        payload = entry.get("payload") or {}
        bad = action_journal.forbidden_payload(payload)
        if bad:
            raise ValueError(f"steering payload carries a forbidden key: {bad}")
        ident = secrets.token_hex(16)
        created = float(entry.get("created_at") or time.time())
        text = json.dumps(payload, sort_keys=True, default=str)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO steer_queue(id, action_id, session_id, kind,"
                    " source, dedupe, payload, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ident, str(entry.get("action_id") or ""),
                     str(entry.get("session_id") or ""),
                     str(entry.get("kind") or ""),
                     str(entry.get("source") or "mac"),
                     str(entry.get("dedupe") or ""), text, created))
            row = self._conn.execute(
                "SELECT * FROM steer_queue WHERE id=?", (ident,)).fetchone()
        board_journal_store._fire("queued", "steer", "enqueue", ident)
        return _row(row)

    def steer_resolve(self, entry_id: str, state: str, ok: bool,
                      detail: str = "") -> bool:
        """Close a `queued` row and blank its words in the same statement.
        A second resolve of the same row returns False."""
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE steer_queue SET state=?, ok=?, detail=?,"
                    " payload='', resolved_at=? WHERE id=? AND state='queued'",
                    (str(state), 1 if ok else 0, str(detail or "")[:500],
                     time.time(), str(entry_id)))
                landed = cur.rowcount == 1
        if landed:
            board_journal_store._fire("resolved", "steer", "resolve",
                                      str(entry_id))
        return landed

    def steer_queued(self) -> list:
        """Every `queued` row, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steer_queue WHERE state='queued'"
                " ORDER BY created_at, rowid").fetchall()
        return [_row(r) for r in rows]

    def steer_get(self, entry_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM steer_queue WHERE id=?",
                (str(entry_id),)).fetchone()
        return _row(row) if row else None

    def steer_by_dedupe(self, dedupe: str, since: float) -> Optional[dict]:
        """The newest row carrying this phone-mark digest, created at or
        after `since`; `None` for an empty digest."""
        if not dedupe:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM steer_queue WHERE dedupe=? AND created_at>=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (str(dedupe), float(since))).fetchone()
        return _row(row) if row else None

    def steer_prune(self, now: Optional[float] = None) -> int:
        """Delete resolved rows past `STEER_RETENTION_SECONDS` and the oldest
        resolved ones beyond `MAX_STEER_ROWS`. A queued row is never pruned,
        whatever its age."""
        now = time.time() if now is None else now
        cutoff = now - steering.STEER_RETENTION_SECONDS
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM steer_queue WHERE state!='queued'"
                    " AND resolved_at<?", (cutoff,))
                removed = cur.rowcount
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM steer_queue").fetchone()[0]
                over = total - steering.MAX_STEER_ROWS
                if over > 0:
                    cur = self._conn.execute(
                        "DELETE FROM steer_queue WHERE id IN (SELECT id FROM"
                        " steer_queue WHERE state!='queued'"
                        " ORDER BY resolved_at, rowid LIMIT ?)", (over,))
                    removed += cur.rowcount
        return removed

    def steer_counts(self) -> dict:
        """Row counts by state, for tests."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM steer_queue"
                " GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}
