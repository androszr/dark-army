"""Lifecycle timing ledger in board.db. BoardStore owns the connection.

No foreign-key cascade to cards: ordinary deletion retains this ledger.
Observational writes use a SAVEPOINT so a collection failure cannot
invalidate a legitimate board action. In-memory checkpoints update only
after the outer transaction commits.
"""
from __future__ import annotations

import json
import logging
import math
import secrets
import sqlite3
from pathlib import Path

from . import board_lifecycle as metrics
from .board_outcomes import sample_delta

logger = logging.getLogger("dark-army.lifecycle")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_cards (
 card_id TEXT PRIMARY KEY, root TEXT NOT NULL, title TEXT NOT NULL,
 tracking_since REAL NOT NULL, deleted_at REAL,
 left_censored INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lifecycle_card_root ON lifecycle_cards(root, card_id);
CREATE TABLE IF NOT EXISTS lifecycle_attempts (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, started_at REAL NOT NULL,
 ended_at REAL, disposition TEXT NOT NULL DEFAULT 'open',
 session_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
 phase TEXT NOT NULL DEFAULT 'implementation', sequence INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lifecycle_attempt_card ON lifecycle_attempts(card_id, id);
CREATE TABLE IF NOT EXISTS lifecycle_episodes (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, kind TEXT NOT NULL,
 cause TEXT NOT NULL DEFAULT '', attempt_id INTEGER,
 start_boundary_id INTEGER, end_boundary_id INTEGER,
 started_at REAL, ended_at REAL,
 disposition TEXT NOT NULL DEFAULT 'open',
 coverage TEXT NOT NULL DEFAULT 'complete',
 provenance TEXT NOT NULL DEFAULT '',
 gap_reasons TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS lifecycle_episode_card ON lifecycle_episodes(card_id, kind, id);
CREATE TABLE IF NOT EXISTS lifecycle_boundaries (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, ts REAL NOT NULL,
 kind TEXT NOT NULL, attempt_id INTEGER, episode_id INTEGER,
 provenance TEXT NOT NULL DEFAULT '', disposition TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', request_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS lifecycle_boundary_card ON lifecycle_boundaries(card_id, ts, id);
CREATE INDEX IF NOT EXISTS lifecycle_boundary_episode ON lifecycle_boundaries(episode_id, ts, id);
CREATE TABLE IF NOT EXISTS lifecycle_spans (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, episode_id INTEGER,
 utc_start REAL NOT NULL, utc_end REAL NOT NULL,
 category TEXT NOT NULL, cause TEXT NOT NULL DEFAULT '',
 attempt_id INTEGER, coverage TEXT NOT NULL DEFAULT 'complete',
 process_generation TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS lifecycle_span_card ON lifecycle_spans(card_id, category, utc_start);
CREATE INDEX IF NOT EXISTS lifecycle_span_episode ON lifecycle_spans(episode_id, utc_start);
CREATE TABLE IF NOT EXISTS lifecycle_checkpoints (
 card_id TEXT PRIMARY KEY, utc REAL NOT NULL, memberships TEXT NOT NULL,
 span_ids TEXT NOT NULL DEFAULT '[]', sequence INTEGER NOT NULL DEFAULT 0,
 process_generation TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS lifecycle_retention (
 root TEXT NOT NULL, day TEXT NOT NULL, category TEXT NOT NULL,
 expired_count INTEGER NOT NULL DEFAULT 0, expired_seconds REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(root, day, category)
);
CREATE TABLE IF NOT EXISTS lifecycle_meta (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

PRUNE_BATCH = 1000
TITLE_CHARS = 200
#: How far apart, in seconds, one confirmed span's end and the next one's
#: start may be and still be one span. A span ends at `previous_utc +
#: monotonic_delta` and the next starts at a fresh `time()` read, so the two
#: never agree to the microsecond; below this bound `_observe_locked` has
#: already accepted the sample (its `utc_disagreement` gap starts at 2s), so
#: nothing under it is a hole in the evidence. An equality join here wrote
#: one row per checkpoint on a real Mac — 960k rows, a 27s report read under
#: the store lock and a phone's card write queued behind it.
SPAN_JOIN_TOLERANCE = 2.0


def _root_of(card):
    root = (card or {}).get("root") or ""
    try:
        return str(Path(root).resolve()) if root else ""
    except (OSError, ValueError):
        return str(root)


def _title_of(card):
    return str((card or {}).get("title") or "")[:TITLE_CHARS]


def _memberships_of(facts):
    """Category/cause/attempt triples that currently hold."""
    if not facts or facts.get("impl_unknown"):
        return ()
    attempt = facts.get("attempt_id")
    out = []
    if facts.get("queued"):
        out.append(("queue", "queue", None))
    if facts.get("executing"):
        out.append(("execution", "execution", attempt))
    if facts.get("review"):
        out.append(("review", "review", None))
    if facts.get("manual_check"):
        out.append(("review", "manual_check", None))
    if facts.get("rework"):
        out.append(("rework", "rework", attempt))
    return tuple(out)


class LifecycleStoreMixin:
    def _connect_lifecycle(self):
        self._conn.executescript(SCHEMA)
        self._lifecycle_samples = {}
        self._lifecycle_process = secrets.token_hex(8)
        self._lifecycle_pending_sample = {}
        # card_id -> `(memberships, sequence, process_generation, active)` of
        # the `lifecycle_checkpoints` row this process last committed, and the
        # key staged by `_observe_locked` until its transaction commits. The
        # row's `utc` is read by nobody; its existence (restart) and its
        # active memberships (`run_figures`) are, and those are the key.
        # Cleared beside the checkpoint DELETE below, so the first pass after
        # a restart writes every row once.
        self._lifecycle_written = {}
        self._lifecycle_pending_written = {}
        self._coalesce_spans_once()
        with self._conn:
            found = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
            # Seed every live card, and every retained outcome row, as
            # left-censored. No historical duration is invented.
            now = self._lifecycle_utc()
            seen = set()
            for card in self.cards():
                self._lifecycle_seed(card, historical=True, utc=now)
                seen.add(card["id"])
            for row in self._conn.execute("SELECT card_id, root, title FROM outcome_cards"):
                if row["card_id"] in seen:
                    continue
                self._lifecycle_seed(
                    {"id": row["card_id"], "root": row["root"], "title": row["title"]},
                    historical=True, utc=now)
            # A process boundary cannot prove the interval since its last sample.
            flagged = list(self._conn.execute("SELECT card_id FROM lifecycle_checkpoints"))
            if flagged:
                ids = [r["card_id"] for r in flagged]
                self._conn.executemany(
                    "UPDATE lifecycle_episodes SET coverage='partial',"
                    " gap_reasons=CASE WHEN gap_reasons='' THEN 'process_boundary'"
                    " ELSE gap_reasons || ',process_boundary' END"
                    " WHERE card_id=? AND disposition='open'",
                    [(i,) for i in ids])
                self._conn.executemany(
                    "UPDATE lifecycle_cards SET left_censored=1 WHERE card_id=?",
                    [(i,) for i in ids])
            health = self._conn.execute(
                "SELECT value FROM lifecycle_meta WHERE key='unavailable'").fetchone()
            if health and health["value"] == "1":
                self._conn.execute(
                    "UPDATE lifecycle_episodes SET coverage='partial',"
                    " gap_reasons=CASE WHEN gap_reasons='' THEN 'observer_failure'"
                    " ELSE gap_reasons || ',observer_failure' END"
                    " WHERE disposition='open'")
            self._conn.execute("DELETE FROM lifecycle_checkpoints")
        self._lifecycle_samples = {}
        try:
            self.lifecycle_prune()
        except Exception:
            logger.debug("lifecycle retention prune skipped", exc_info=True)

    def _lifecycle_utc(self):
        fn = getattr(self, "_clock_utc", None)
        return float(fn()) if fn else __import__("time").time()

    def _lifecycle_mono(self):
        fn = getattr(self, "_clock_mono", None)
        return float(fn()) if fn else __import__("time").monotonic()

    def _lifecycle_seed(self, card, historical=False, utc=None):
        utc = self._lifecycle_utc() if utc is None else utc
        self._conn.execute(
            "INSERT OR IGNORE INTO lifecycle_cards"
            "(card_id,root,title,tracking_since,left_censored) VALUES(?,?,?,?,?)",
            (card["id"], _root_of(card), _title_of(card), utc, int(historical)))
        # Root is immutable once retained. Title may follow a rename.
        self._conn.execute(
            "UPDATE lifecycle_cards SET title=? WHERE card_id=?",
            (_title_of(card), card["id"]))

    def _lifecycle_try(self, fn, *args, **kwargs):
        """Run observational writes in a SAVEPOINT. Outer transaction stays."""
        try:
            self._conn.execute("SAVEPOINT lifecycle")
            result = fn(*args, **kwargs)
            self._conn.execute("RELEASE SAVEPOINT lifecycle")
            return result, True
        except Exception:
            logger.warning("lifecycle observation rolled back", exc_info=True)
            try:
                self._conn.execute("ROLLBACK TO SAVEPOINT lifecycle")
            except Exception:
                logger.debug("lifecycle savepoint rollback failed", exc_info=True)
            try:
                self._conn.execute(
                    "INSERT INTO lifecycle_meta(key,value) VALUES('unavailable','1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'")
            except Exception:
                logger.debug("lifecycle unavailable marker failed", exc_info=True)
            self._lifecycle_samples.clear()
            self._lifecycle_pending_sample.clear()
            # Conservative: after any rollback every row is written again.
            self._lifecycle_written.clear()
            self._lifecycle_pending_written.clear()
            return None, False

    def _lifecycle_transition(self, before, after, *, utc=None):
        """Inside the card write's transaction. Derive from before/after."""
        if after is None:
            return
        utc = self._lifecycle_utc() if utc is None else utc
        _, ok = self._lifecycle_try(self._lifecycle_transition_locked, before, after, utc)
        return ok

    def _lifecycle_transition_locked(self, before, after, utc):
        self._lifecycle_seed(after, utc=utc)
        cid = after["id"]
        self._conn.execute(
            "UPDATE lifecycle_cards SET title=? WHERE card_id=?",
            (_title_of(after), cid))
        before = before or {}
        b_queue = str(before.get("queue_state") or "")
        a_queue = str(after.get("queue_state") or "")
        b_link = str(before.get("link_state") or "")
        a_link = str(after.get("link_state") or "")
        b_refine = str(before.get("refine_state") or "")
        a_refine = str(after.get("refine_state") or "")
        b_sid = str(before.get("session_id") or "")
        a_sid = str(after.get("session_id") or "")

        if a_queue == "queued" and b_queue != "queued":
            self._open_episode(after, "queue", utc, provenance="queue")
        dispatched = a_link == "dispatching" and b_link != "dispatching"
        if dispatched:
            attempt = self._open_attempt(after, utc)
            if b_queue == "queued":
                self._close_episode(after, "queue", utc, "completed", provenance="dispatch")
            else:
                self._zero_queue(after, utc)
            self._open_episode(after, "execution", utc, attempt_id=attempt,
                               provenance="dispatch")
            self._open_episode(after, "launch", utc, attempt_id=attempt,
                               provenance="dispatch")
        elif b_queue == "queued" and a_queue != "queued":
            self._close_episode(after, "queue", utc, "cancelled", provenance="unqueue")

        if a_link == "live" and b_link == "dispatching":
            self._close_episode(after, "launch", utc, "completed", provenance="bind")
            self._bind_attempt(after, utc)
        if a_link == "ended" and b_link != "ended":
            self._close_episode(after, "execution", utc, "completed", provenance="ended")
            self._close_episode(after, "launch", utc, "cancelled", provenance="ended")
            self._close_attempt(after, utc, "completed")
        if a_link == "live" and b_link == "ended":
            self._reopen_attempt(after, utc)
            self._reopen_episode(after, "execution", utc)
        # Reset / replacement: a live or dispatching link that loses its session.
        cleared = b_link in ("live", "dispatching") and a_link in ("",) and not a_sid
        replaced = (b_sid and a_sid and b_sid != a_sid
                    and a_link in ("live", "dispatching") and b_link in ("live", "dispatching"))
        if cleared or (replaced and not dispatched):
            self._close_episode(after, "execution", utc, "cancelled", provenance="reset")
            self._close_episode(after, "launch", utc, "cancelled", provenance="reset")
            self._close_attempt(after, utc, "cancelled")

        if a_refine == "dispatching" and b_refine != "dispatching":
            self._boundary(after, "refine_started", utc, provenance="refine")
        if a_refine in ("ended", "") and b_refine in ("dispatching", "live"):
            self._boundary(after, "refine_ended", utc, provenance="refine")

        seq = self._boundary_seq(cid)
        self._conn.execute(
            "INSERT INTO lifecycle_meta(key,value) VALUES('generation',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(seq),))

    def _lifecycle_on_submit(self, card, utc):
        self._lifecycle_try(self._on_submit_locked, card, utc)

    def _on_submit_locked(self, card, utc):
        self._lifecycle_seed(card, utc=utc)
        self._close_episode(card, "execution", utc, "completed", provenance="submitted")
        self._close_attempt(card, utc, "completed")
        self._close_episode(card, "rework", utc, "completed", provenance="submitted")
        self._open_episode(card, "review", utc, cause="review", provenance="submitted")

    def _lifecycle_on_rework(self, card, kind, utc):
        self._lifecycle_try(self._on_rework_locked, card, kind, utc)

    def _on_rework_locked(self, card, kind, utc):
        self._lifecycle_seed(card, utc=utc)
        self._close_episode(card, "review", utc, "completed", provenance=kind)
        # Repeated rejection in an already open rework adds no episode.
        open_row = self._open_row(card["id"], "rework")
        if open_row is None:
            self._open_episode(card, "rework", utc, cause=kind, provenance=kind)

    def _lifecycle_on_accept(self, card, utc):
        self._lifecycle_try(self._on_accept_locked, card, utc)

    def _on_accept_locked(self, card, utc):
        self._lifecycle_seed(card, utc=utc)
        self._close_episode(card, "review", utc, "completed", provenance="accepted")

    def _lifecycle_on_delete(self, cards, utc=None):
        utc = self._lifecycle_utc() if utc is None else utc
        self._lifecycle_try(self._on_delete_locked, cards, utc)

    def _on_delete_locked(self, cards, utc):
        for card in cards:
            if not card:
                continue
            self._lifecycle_seed(card, utc=utc)
            cid = card["id"]
            for kind in ("queue", "execution", "launch", "review", "manual_check", "rework"):
                self._close_episode(card, kind, utc, "removed", provenance="deleted")
            self._close_attempt(card, utc, "removed")
            self._conn.execute(
                "UPDATE lifecycle_cards SET deleted_at=? WHERE card_id=? AND deleted_at IS NULL",
                (utc, cid))
            self._lifecycle_samples.pop(cid, None)

    def observe_lifecycle(self, card_id, facts, *, utc=None, monotonic=None):
        """One card's observation: `observe_lifecycles` for a single card."""
        return self.observe_lifecycles([(card_id, facts)], utc=utc, monotonic=monotonic)

    def observe_lifecycles(self, samples, *, utc=None, monotonic=None) -> bool:
        """Observe a whole pass, `[(card_id, facts)]`, in one transaction.

        Each card runs in its own SAVEPOINT (`_lifecycle_try_card`), so one
        card's failure rolls back that card's writes, marks the ledger
        unavailable and forgets that card's sample and written row alone,
        while the rest of the pass still commits. If the transaction itself
        is lost, nothing from the pass is remembered. The in-memory samples
        and the written-row memo move only after the commit, and only for
        cards whose SAVEPOINT was released. Returns whether any card failed."""
        utc = self._lifecycle_utc() if utc is None else utc
        monotonic = self._lifecycle_mono() if monotonic is None else monotonic
        failed = False
        settled = []
        with self._lock:
            start = self._conn.total_changes
            try:
                with self._conn:
                    # Explicit: a SAVEPOINT outside a transaction opens its
                    # own, and releasing it would commit once per card.
                    if not self._conn.in_transaction:
                        self._conn.execute("BEGIN")
                    for card_id, facts in samples:
                        outcome = self._lifecycle_try_card(card_id, facts, utc, monotonic)
                        if outcome == "ok":
                            settled.append((
                                card_id, facts,
                                self._lifecycle_pending_sample.pop(card_id, None),
                                self._lifecycle_pending_written.pop(card_id, None)))
                            continue
                        failed = True
                        if outcome == "aborted":
                            # Rows settled above were rolled back with it.
                            settled = []
                            self._lifecycle_forget_all()
                            break
                    if failed and self._conn.in_transaction:
                        # Last word: a healthy card later in the pass wrote
                        # '0' over the failed card's marker.
                        try:
                            self._conn.execute(
                                "INSERT INTO lifecycle_meta(key,value) VALUES('unavailable','1') "
                                "ON CONFLICT(key) DO UPDATE SET value='1'")
                        except Exception:
                            logger.debug("lifecycle unavailable marker failed", exc_info=True)
            finally:
                observed = getattr(self, "_observed_writes", None)
                if observed is not None:
                    observed(start)
            for card_id, facts, pending, written in settled:
                if pending is not None:
                    self._lifecycle_samples[card_id] = pending
                elif not facts:
                    self._lifecycle_samples.pop(card_id, None)
                if written is not None:
                    self._lifecycle_written[card_id] = written
        return failed

    def _lifecycle_forget_all(self):
        self._lifecycle_samples.clear()
        self._lifecycle_pending_sample.clear()
        self._lifecycle_written.clear()
        self._lifecycle_pending_written.clear()

    def _lifecycle_try_card(self, card_id, facts, utc, monotonic):
        """`_lifecycle_try` for one card of a batch: `"ok"`, `"failed"`
        (this card's SAVEPOINT rolled back; only its own sample and memo
        are forgotten) or `"aborted"` (the outer transaction is gone)."""
        try:
            self._conn.execute("SAVEPOINT lifecycle")
            self._observe_locked(card_id, facts, utc, monotonic)
            self._conn.execute("RELEASE SAVEPOINT lifecycle")
            return "ok"
        except Exception:
            logger.warning("lifecycle observation rolled back", exc_info=True)
        try:
            self._conn.execute("ROLLBACK TO SAVEPOINT lifecycle")
            self._conn.execute("RELEASE SAVEPOINT lifecycle")
        except Exception:
            logger.warning("lifecycle savepoint rollback failed", exc_info=True)
        if not self._conn.in_transaction:
            return "aborted"
        try:
            self._conn.execute(
                "INSERT INTO lifecycle_meta(key,value) VALUES('unavailable','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")
        except Exception:
            logger.debug("lifecycle unavailable marker failed", exc_info=True)
        for memo in (self._lifecycle_samples, self._lifecycle_pending_sample,
                     self._lifecycle_written, self._lifecycle_pending_written):
            memo.pop(card_id, None)
        return "failed"

    def _observe_locked(self, card_id, facts, utc, monotonic):
        ledger = self._conn.execute(
            "SELECT * FROM lifecycle_cards WHERE card_id=?", (card_id,)).fetchone()
        if not ledger or ledger["deleted_at"] is not None:
            return
        if not math.isfinite(utc) or not math.isfinite(monotonic):
            self._mark_gap(card_id, None, "nonfinite_clock")
            self._lifecycle_pending_sample[card_id] = None
            return
        seq = self._boundary_seq(card_id)
        current_attempt = self._current_attempt(card_id)
        collected_seq = None if not facts else facts.get("boundary_seq")
        collected_attempt = None if not facts else facts.get("attempt_id")
        if facts and collected_seq is not None and int(collected_seq) != int(seq):
            self._mark_gap(card_id, current_attempt, "state_changed")
            self._lifecycle_pending_sample[card_id] = None
            return
        if (facts and collected_attempt is not None and current_attempt is not None
                and int(collected_attempt) != int(current_attempt["id"])):
            self._mark_gap(card_id, current_attempt, "state_changed")
            self._lifecycle_pending_sample[card_id] = None
            return
        if facts and facts.get("impl_unknown"):
            self._mark_gap(card_id, current_attempt, "impl_unknown")
            memberships = ()
        else:
            memberships = _memberships_of(facts)
        previous = self._lifecycle_samples.get(card_id)
        if previous is None:
            # A checkpoint that survived a crash already marked partial; a
            # first sample still cannot confirm the preceding interval.
            if self._conn.execute(
                    "SELECT 1 FROM lifecycle_checkpoints WHERE card_id=?",
                    (card_id,)).fetchone():
                self._mark_gap(card_id, current_attempt, "process_boundary")
        causes = set(memberships) if facts else None
        seconds, confirmed, gap = sample_delta(
            None if previous is None else (previous[0], previous[1], previous[2]),
            monotonic, utc, causes)
        if previous is not None:
            delta = monotonic - previous[0]
            wall = utc - previous[1]
            if delta < 0:
                self._mark_gap(card_id, current_attempt, "negative_delta")
            elif wall < 0 or abs(wall - delta) > 2:
                self._mark_gap(card_id, current_attempt, "utc_disagreement")
            elif delta > metrics.MAX_GAP:
                self._mark_gap(card_id, current_attempt, "clock_gap")
        if gap and previous is not None:
            pass  # reason already recorded above when we can name it
        elif seconds and confirmed:
            self._confirm_spans(card_id, previous[1], previous[1] + seconds,
                                confirmed, current_attempt)
        card = {"id": card_id}
        left = bool(ledger["left_censored"])
        if facts and facts.get("queued") and self._open_row(card_id, "queue") is None:
            self._open_episode(card, "queue", utc, provenance="observed",
                               left_censor=left)
        if facts and facts.get("review") and self._open_row(card_id, "review") is None:
            self._open_episode(card, "review", utc, cause="review",
                               provenance="observed", left_censor=left)
        if facts and facts.get("rework") and self._open_row(card_id, "rework") is None:
            self._open_episode(card, "rework", utc, provenance="observed",
                               left_censor=left)
        if facts and facts.get("executing"):
            attempt = current_attempt
            if attempt and self._open_row(card_id, "execution",
                                          attempt_id=attempt["id"]) is None:
                self._open_episode(card, "execution", utc, attempt_id=attempt["id"],
                                   provenance="observed", left_censor=left)
        open_manual = bool(facts and facts.get("manual_check"))
        if open_manual:
            self._ensure_manual(card_id, utc)
        elif facts is not None:
            self._close_episode(card, "manual_check", utc, "completed",
                                provenance="manual_clear")
        # Only where the row would change: its `utc` is read by nobody.
        row_key = (json.dumps(list(memberships)), seq, self._lifecycle_process,
                   int(bool(memberships)))
        if self._lifecycle_written.get(card_id) != row_key:
            self._conn.execute(
                "INSERT INTO lifecycle_checkpoints"
                "(card_id,utc,memberships,span_ids,sequence,process_generation,active) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET "
                "utc=excluded.utc, memberships=excluded.memberships, "
                "sequence=excluded.sequence, process_generation=excluded.process_generation, "
                "active=excluded.active",
                (card_id, utc, row_key[0], "[]", seq,
                 self._lifecycle_process, row_key[3]))
            self._lifecycle_pending_written[card_id] = row_key
        if facts is None:
            self._lifecycle_pending_sample[card_id] = None
        else:
            self._lifecycle_pending_sample[card_id] = (
                monotonic, utc, set(memberships), seq)
        if facts:
            # Healthy again — written only when it was not already '0'.
            health = self._conn.execute(
                "SELECT value FROM lifecycle_meta WHERE key='unavailable'").fetchone()
            if health is None or health["value"] != "0":
                self._conn.execute(
                    "INSERT INTO lifecycle_meta(key,value) VALUES('unavailable','0') "
                    "ON CONFLICT(key) DO UPDATE SET value='0'")

    def _coalesce_spans_once(self):
        """Fold the one-row-per-checkpoint spans an older build wrote into one
        span per contiguous run, once per file (`lifecycle_meta`
        `spans_coalesced`). The equality join in `_confirm_spans` never
        matched against real clocks, so a card sitting in review for a day
        gained a row every few seconds. Same rule as the live join: two rows
        of one key (card, category, cause, attempt, episode) whose end and
        start lie within `SPAN_JOIN_TOLERANCE` are one span. Gap rows are
        never touched, the summed seconds do not move, and the freed pages
        are given back with a VACUUM — outside the transaction, since SQLite
        refuses one inside — so the file shrinks on disk rather than only in
        its page count. Every failure is a log line: the store opens either
        way, and the mark stays unset so the next open tries again."""
        try:
            with self._conn:
                done = self._conn.execute(
                    "SELECT value FROM lifecycle_meta WHERE key='spans_coalesced'"
                ).fetchone()
                if done and done["value"] == "1":
                    return
                rows = self._conn.execute(
                    "SELECT id, card_id, category, cause, attempt_id, episode_id, "
                    "utc_start, utc_end FROM lifecycle_spans WHERE coverage='complete' "
                    "ORDER BY card_id, category, cause, IFNULL(attempt_id,-1), "
                    "IFNULL(episode_id,-1), utc_start, id")
                drop = []
                extend = []
                # The run being folded: its key, the surviving row's id, the
                # end reached so far, and whether it absorbed anything.
                head = None
                for row in rows:
                    key = (row["card_id"], row["category"], row["cause"],
                           row["attempt_id"], row["episode_id"])
                    if (head is not None and head[0] == key
                            and abs(float(row["utc_start"]) - head[2])
                            <= SPAN_JOIN_TOLERANCE):
                        drop.append((row["id"],))
                        head = (key, head[1], max(head[2], float(row["utc_end"])), True)
                        continue
                    if head is not None and head[3]:
                        extend.append((head[2], head[1]))
                    head = (key, row["id"], float(row["utc_end"]), False)
                if head is not None and head[3]:
                    extend.append((head[2], head[1]))
                if extend:
                    self._conn.executemany(
                        "UPDATE lifecycle_spans SET utc_end=? WHERE id=?", extend)
                if drop:
                    self._conn.executemany(
                        "DELETE FROM lifecycle_spans WHERE id=?", drop)
                self._conn.execute(
                    "INSERT INTO lifecycle_meta(key,value) VALUES('spans_coalesced','1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'")
        except sqlite3.Error:
            logger.warning("board.db: lifecycle spans not coalesced", exc_info=True)
            return
        if not drop:
            return
        logger.info("board.db: folded %d lifecycle span rows into %d spans",
                    len(drop) + len(extend), len(extend))
        try:
            self._conn.execute("VACUUM")
        except sqlite3.Error:
            logger.warning("board.db: VACUUM after coalescing failed", exc_info=True)

    def _confirm_spans(self, card_id, utc_start, utc_end, memberships, attempt):
        attempt_id = None if attempt is None else attempt["id"]
        for category, cause, att in memberships:
            ep = self._open_row(card_id, "review" if category == "review" else category,
                                attempt_id=att if category == "execution" else None,
                                cause=cause if category == "review" else None)
            if ep is None and category == "review" and cause == "manual_check":
                ep = self._open_row(card_id, "manual_check")
            eid = None if ep is None else ep["id"]
            use_attempt = att if att is not None else attempt_id
            # The newest complete span of this key ending where this one
            # starts, within `SPAN_JOIN_TOLERANCE`: extended, never duplicated.
            row = self._conn.execute(
                "SELECT id, utc_end FROM lifecycle_spans WHERE card_id=? AND category=? "
                "AND cause=? AND coverage='complete' "
                "AND IFNULL(attempt_id,-1)=IFNULL(?, -1) "
                "AND IFNULL(episode_id,-1)=IFNULL(?, -1) "
                "AND utc_end BETWEEN ? AND ? ORDER BY utc_end DESC LIMIT 1",
                (card_id, category, cause, use_attempt, eid,
                 utc_start - SPAN_JOIN_TOLERANCE,
                 utc_start + SPAN_JOIN_TOLERANCE)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE lifecycle_spans SET utc_end=? WHERE id=?",
                    (max(utc_end, float(row["utc_end"])), row["id"]))
            else:
                self._conn.execute(
                    "INSERT INTO lifecycle_spans"
                    "(card_id,episode_id,utc_start,utc_end,category,cause,attempt_id,"
                    "coverage,process_generation) VALUES(?,?,?,?,?,?,?,?,?)",
                    (card_id, eid, utc_start, utc_end, category, cause, use_attempt,
                     "complete", self._lifecycle_process))

    def _mark_gap(self, card_id, attempt, reason):
        attempt_id = None if attempt is None else attempt["id"]
        self._conn.execute(
            "UPDATE lifecycle_episodes SET coverage='partial',"
            " gap_reasons=CASE WHEN gap_reasons='' THEN ? "
            " WHEN instr(gap_reasons, ?)>0 THEN gap_reasons "
            " ELSE gap_reasons || ',' || ? END "
            "WHERE card_id=? AND disposition='open'",
            (reason, reason, reason, card_id))
        now = self._lifecycle_utc()
        open_eps = list(self._conn.execute(
            "SELECT id FROM lifecycle_episodes WHERE card_id=? AND disposition='open'",
            (card_id,)))
        targets = [row["id"] for row in open_eps] or [None]
        for eid in targets:
            self._conn.execute(
                "INSERT INTO lifecycle_spans"
                "(card_id,episode_id,utc_start,utc_end,category,cause,attempt_id,"
                "coverage,process_generation) VALUES(?,?,?,?,?,?,?,?,?)",
                (card_id, eid, now, now, "gap", reason, attempt_id, "gap",
                 self._lifecycle_process))

    def _ensure_manual(self, card_id, utc):
        if self._open_row(card_id, "manual_check") is None:
            card = {"id": card_id}
            self._open_episode(card, "manual_check", utc, cause="manual_check",
                               provenance="manual")

    def _open_attempt(self, card, utc):
        row = self._conn.execute(
            "SELECT id FROM lifecycle_attempts WHERE card_id=? AND disposition='open' "
            "AND phase='implementation'", (card["id"],)).fetchone()
        if row:
            return row["id"]
        seq = self._boundary_seq(card["id"]) + 1
        cur = self._conn.execute(
            "INSERT INTO lifecycle_attempts"
            "(card_id,started_at,disposition,session_id,provider,phase,sequence) "
            "VALUES(?,?,?,?,?,?,?)",
            (card["id"], utc, "open", str(card.get("session_id") or ""),
             str(card.get("tool") or ""), "implementation", seq))
        aid = cur.lastrowid
        self._boundary(card, "attempt_started", utc, attempt_id=aid, provenance="dispatch")
        return aid

    def _bind_attempt(self, card, utc):
        row = self._current_attempt(card["id"])
        if not row:
            return
        self._conn.execute(
            "UPDATE lifecycle_attempts SET session_id=?, provider=? WHERE id=?",
            (str(card.get("session_id") or ""), str(card.get("tool") or ""), row["id"]))
        self._boundary(card, "session_bound", utc, attempt_id=row["id"], provenance="bind")

    def _close_attempt(self, card, utc, disposition):
        row = self._current_attempt(card["id"])
        if not row:
            return
        self._conn.execute(
            "UPDATE lifecycle_attempts SET ended_at=?, disposition=? WHERE id=?",
            (utc, disposition, row["id"]))
        self._boundary(card, "attempt_" + disposition, utc, attempt_id=row["id"],
                       disposition=disposition)

    def _reopen_attempt(self, card, utc):
        row = self._conn.execute(
            "SELECT * FROM lifecycle_attempts WHERE card_id=? AND phase='implementation' "
            "ORDER BY id DESC LIMIT 1", (card["id"],)).fetchone()
        if not row:
            return self._open_attempt(card, utc)
        self._conn.execute(
            "UPDATE lifecycle_attempts SET disposition='open', ended_at=NULL WHERE id=?",
            (row["id"],))
        self._boundary(card, "attempt_reopened", utc, attempt_id=row["id"],
                       provenance="mark_live")
        return row["id"]

    def _current_attempt(self, card_id):
        return self._conn.execute(
            "SELECT * FROM lifecycle_attempts WHERE card_id=? AND disposition='open' "
            "AND phase='implementation' ORDER BY id DESC LIMIT 1",
            (card_id,)).fetchone()

    def _open_row(self, card_id, kind, attempt_id=None, cause=None):
        sql = ("SELECT * FROM lifecycle_episodes WHERE card_id=? AND kind=? "
               "AND disposition='open'")
        params = [card_id, kind]
        if attempt_id is not None:
            sql += " AND attempt_id=?"
            params.append(attempt_id)
        if cause is not None:
            sql += " AND cause=?"
            params.append(cause)
        sql += " ORDER BY id DESC LIMIT 1"
        return self._conn.execute(sql, tuple(params)).fetchone()

    def _open_episode(self, card, kind, utc, *, attempt_id=None, cause="",
                      provenance="", left_censor=False):
        existing = self._open_row(card["id"], kind, attempt_id=attempt_id if kind == "execution" else None)
        if existing:
            return existing["id"]
        cause = cause or kind
        coverage = "partial" if left_censor else "complete"
        started = None if left_censor else utc
        cur = self._conn.execute(
            "INSERT INTO lifecycle_episodes"
            "(card_id,kind,cause,attempt_id,started_at,disposition,coverage,provenance) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (card["id"], kind, cause, attempt_id, started, "open", coverage, provenance))
        eid = cur.lastrowid
        bid = self._boundary(card, kind + "_started", utc, attempt_id=attempt_id,
                             episode_id=eid, provenance=provenance)
        self._conn.execute(
            "UPDATE lifecycle_episodes SET start_boundary_id=? WHERE id=?",
            (bid, eid))
        return eid

    def _zero_queue(self, card, utc):
        """A direct successful start: a completed queue episode of measured zero."""
        existing = self._open_row(card["id"], "queue")
        if existing:
            self._close_episode(card, "queue", utc, "completed", provenance="dispatch")
            return
        eid = self._open_episode(card, "queue", utc, provenance="direct")
        self._close_episode(card, "queue", utc, "completed", provenance="direct")
        self._conn.execute(
            "UPDATE lifecycle_episodes SET started_at=?, ended_at=?, coverage='complete' "
            "WHERE id=?", (utc, utc, eid))

    def _close_episode(self, card, kind, utc, disposition, *, provenance=""):
        row = self._open_row(card["id"], kind)
        if not row:
            return
        bid = self._boundary(card, kind + "_" + disposition, utc,
                             attempt_id=row["attempt_id"], episode_id=row["id"],
                             provenance=provenance, disposition=disposition)
        self._conn.execute(
            "UPDATE lifecycle_episodes SET ended_at=?, disposition=?, end_boundary_id=? "
            "WHERE id=?", (utc, disposition, bid, row["id"]))

    def _reopen_episode(self, card, kind, utc):
        row = self._conn.execute(
            "SELECT * FROM lifecycle_episodes WHERE card_id=? AND kind=? "
            "AND disposition='completed' ORDER BY id DESC LIMIT 1",
            (card["id"], kind)).fetchone()
        if not row:
            return self._open_episode(card, kind, utc, provenance="mark_live")
        self._conn.execute(
            "UPDATE lifecycle_episodes SET disposition='open', ended_at=NULL, "
            "end_boundary_id=NULL WHERE id=?", (row["id"],))
        self._boundary(card, kind + "_reopened", utc, attempt_id=row["attempt_id"],
                       episode_id=row["id"], provenance="mark_live")
        return row["id"]

    def _boundary(self, card, kind, utc, *, attempt_id=None, episode_id=None,
                  provenance="", disposition="", reason="", request_key=None):
        if request_key:
            existing = self._conn.execute(
                "SELECT id FROM lifecycle_boundaries WHERE request_key=?",
                (request_key,)).fetchone()
            if existing:
                return existing["id"]
        cur = self._conn.execute(
            "INSERT INTO lifecycle_boundaries"
            "(card_id,ts,kind,attempt_id,episode_id,provenance,disposition,reason,request_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (card["id"], utc, kind, attempt_id, episode_id, provenance,
             disposition, reason, request_key))
        return cur.lastrowid

    def _boundary_seq(self, card_id):
        row = self._conn.execute(
            "SELECT COALESCE(MAX(id),0) AS n FROM lifecycle_boundaries WHERE card_id=?",
            (card_id,)).fetchone()
        return int(row["n"] if row else 0)

    def lifecycle_generation(self):
        with self._lock:
            return self._generation_locked()

    def _generation_locked(self):
        # Identity only: a span checkpoint must not invalidate paging.
        b = self._conn.execute(
            "SELECT COALESCE(MAX(id),0) AS n FROM lifecycle_boundaries").fetchone()["n"]
        e = self._conn.execute(
            "SELECT COALESCE(MAX(id),0) AS n FROM lifecycle_episodes").fetchone()["n"]
        return f"{int(b)}:{int(e)}"

    def lifecycle_facts_seq(self, card_id):
        with self._lock:
            attempt = self._current_attempt(card_id)
            return {
                "boundary_seq": self._boundary_seq(card_id),
                "attempt_id": None if attempt is None else int(attempt["id"]),
            }

    def _plan_attached_locked(self, card, utc):
        """The plan attach as a moment (`plans/2026-09-13-card-timeline.md`).

        Inside `attach_plan`'s own lock and SAVEPOINT, after its UPDATE
        landed. No `episode_id`, so the report's replays skip it and
        `lifecycle_prune` never selects it: it is a moment on the card's
        timeline, not episode bookkeeping.
        """
        self._lifecycle_seed(card, utc=utc)
        self._boundary(card, "plan_attached", utc, provenance="attach")

    def card_timeline_facts(self, card_id):
        """Everything the card timeline composes from, read under one lock.
        Read-only, no SAVEPOINT; `None` only when there is no such card in
        either table. Evidence text is never selected."""
        card_id = str(card_id or "")
        with self._lock:
            ledger = self._conn.execute(
                "SELECT * FROM lifecycle_cards WHERE card_id=?", (card_id,)).fetchone()
            if ledger is None and self._conn.execute(
                    "SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone() is None:
                return None
            boundaries = [dict(r) for r in self._conn.execute(
                "SELECT b.ts, b.kind, b.attempt_id, b.provenance, b.disposition, "
                "a.sequence FROM lifecycle_boundaries b "
                "LEFT JOIN lifecycle_attempts a ON a.id = b.attempt_id "
                "WHERE b.card_id=? ORDER BY b.ts, b.id", (card_id,))]
            events = [dict(r) for r in self._conn.execute(
                "SELECT ts, kind, actor FROM outcome_events WHERE card_id=? ORDER BY id",
                (card_id,))]
            session_ids = [str(r["session_id"]) for r in self._conn.execute(
                "SELECT session_id FROM outcome_runs WHERE card_id=?", (card_id,))]
        meta = dict(ledger) if ledger is not None else None
        return {
            "ledger": meta,
            "boundaries": boundaries,
            "outcome_events": events,
            "session_ids": session_ids,
            "left_censored": bool(meta and meta.get("left_censored")),
            "tracking_since": None if meta is None else meta.get("tracking_since"),
        }

    def lifecycle_report(self, *, root=None, card_id=None, start, end, as_of=None,
                         limit=25, offset=0, sort="queue", generation=None, now=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100 and offset nonnegative")
        if type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset nonnegative")
        now = self._lifecycle_utc() if now is None else now
        start, end, as_of = metrics.period_bounds(start, end, now=now, as_of=as_of)
        with self._lock:
            gen = self._generation_locked()
            if generation is not None and str(generation) != str(gen):
                return {
                    "supported": True, "available": False,
                    "measurements_available": True,
                    "reason": "report changed; refresh",
                    "generation": gen, "schema": metrics.SCHEMA,
                    "from": start, "to": end, "as_of": as_of,
                }
            health = self._conn.execute(
                "SELECT value FROM lifecycle_meta WHERE key='unavailable'").fetchone()
            unavailable = bool(health and health["value"] == "1")
            if card_id:
                meta = self._conn.execute(
                    "SELECT * FROM lifecycle_cards WHERE card_id=?", (card_id,)).fetchone()
                if meta is None:
                    snapshot = self._empty_snapshot(root="", card_id=card_id)
                else:
                    root = meta["root"]
                    snapshot = self._snapshot_locked(root=root, card_id=card_id)
            else:
                root = str(Path(root).resolve()) if root else ""
                snapshot = self._snapshot_locked(root=root, card_id="")
            snapshot["generation"] = gen
            snapshot["now"] = now
            snapshot["unavailable"] = unavailable
        report = metrics.build_report(
            root=snapshot["root"], card_id=snapshot["card_id"],
            start=start, end=end, as_of=as_of, now=now,
            cards=snapshot["cards"], episodes=snapshot["episodes"],
            spans=snapshot["spans"], boundaries=snapshot["boundaries"],
            attempts=snapshot["attempts"], generation=gen,
            tracking_since=snapshot["tracking_since"],
            retention=snapshot["retention"],
            deleted_ids=snapshot["deleted_ids"],
            limit=limit, offset=offset, sort=sort,
            available=True,
            measurements_available=not unavailable,
            reason="" if not unavailable else "lifecycle collection is unavailable",
        )
        return report

    def _empty_snapshot(self, root, card_id):
        return {
            "root": root, "card_id": card_id, "cards": {}, "episodes": [],
            "spans": [], "boundaries": [], "attempts": [],
            "tracking_since": None, "retention": {}, "deleted_ids": set(),
        }

    def _snapshot_locked(self, root, card_id):
        if card_id:
            card_rows = list(self._conn.execute(
                "SELECT * FROM lifecycle_cards WHERE card_id=?", (card_id,)))
        else:
            card_rows = list(self._conn.execute(
                "SELECT * FROM lifecycle_cards WHERE root=?", (root,)))
        cards = {r["card_id"]: dict(r) for r in card_rows}
        ids = list(cards)
        if not ids:
            return self._empty_snapshot(root, card_id)
        placeholders = ",".join("?" * len(ids))
        episodes = [dict(r) for r in self._conn.execute(
            f"SELECT * FROM lifecycle_episodes WHERE card_id IN ({placeholders}) ORDER BY id",
            ids)]
        spans = [dict(r) for r in self._conn.execute(
            f"SELECT * FROM lifecycle_spans WHERE card_id IN ({placeholders}) ORDER BY id",
            ids)]
        boundaries = [dict(r) for r in self._conn.execute(
            f"SELECT * FROM lifecycle_boundaries WHERE card_id IN ({placeholders}) "
            "ORDER BY ts, id", ids)]
        attempts = [dict(r) for r in self._conn.execute(
            f"SELECT * FROM lifecycle_attempts WHERE card_id IN ({placeholders}) ORDER BY id",
            ids)]
        tracking = min((c["tracking_since"] for c in cards.values()), default=None)
        retention = {}
        for row in self._conn.execute(
                "SELECT category, SUM(expired_count) AS n, SUM(expired_seconds) AS s "
                "FROM lifecycle_retention WHERE root=? GROUP BY category",
                (root,)):
            retention[row["category"]] = {
                "expired_count": int(row["n"] or 0),
                "expired_seconds": float(row["s"] or 0),
            }
        deleted = {cid for cid, meta in cards.items() if meta.get("deleted_at")}
        return {
            "root": root, "card_id": card_id, "cards": cards,
            "episodes": episodes, "spans": spans, "boundaries": boundaries,
            "attempts": attempts, "tracking_since": tracking,
            "retention": retention, "deleted_ids": deleted,
        }

    def lifecycle_prune(self, *, now=None, keep_days=None):
        """Drop expired completed evidence in batches of 1,000. Never a report."""
        now = self._lifecycle_utc() if now is None else now
        keep_days = metrics.RETENTION_DAYS if keep_days is None else keep_days
        cutoff = now - keep_days * 86400
        removed = 0
        with self._lock, self._conn:
            rows = list(self._conn.execute(
                "SELECT id, card_id, episode_id, category, utc_start, utc_end "
                "FROM lifecycle_spans "
                "WHERE utc_end < ? AND coverage != 'gap' "
                "AND episode_id NOT IN "
                "(SELECT id FROM lifecycle_episodes WHERE disposition='open') "
                "LIMIT ?", (cutoff, PRUNE_BATCH)))
            closed_touched = set()
            for row in rows:
                meta = self._conn.execute(
                    "SELECT root FROM lifecycle_cards WHERE card_id=?",
                    (row["card_id"],)).fetchone()
                root = meta["root"] if meta else ""
                day = metrics.utc_day(row["utc_end"])
                seconds = max(0.0, float(row["utc_end"]) - float(row["utc_start"]))
                self._conn.execute(
                    "INSERT INTO lifecycle_retention(root,day,category,expired_count,expired_seconds) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(root,day,category) DO UPDATE SET "
                    "expired_count=expired_count+1, expired_seconds=expired_seconds+excluded.expired_seconds",
                    (root, day, row["category"], 1, seconds))
                if row["episode_id"] is not None:
                    closed_touched.add(row["episode_id"])
            if rows:
                self._conn.executemany(
                    "DELETE FROM lifecycle_spans WHERE id=?",
                    [(r["id"],) for r in rows])
                removed += len(rows)
                for eid in closed_touched:
                    self._conn.execute(
                        "UPDATE lifecycle_episodes SET coverage='partial',"
                        " gap_reasons=CASE WHEN gap_reasons='' THEN 'retention' "
                        " WHEN instr(gap_reasons,'retention')>0 THEN gap_reasons "
                        " ELSE gap_reasons || ',retention' END WHERE id=?",
                        (eid,))
            # Trim old spans on still-open episodes, keeping the identity and
            # the initial boundary, and record an explicit retention gap.
            open_old = list(self._conn.execute(
                "SELECT s.id, s.card_id, s.episode_id, s.category, s.utc_start, s.utc_end "
                "FROM lifecycle_spans s JOIN lifecycle_episodes e ON e.id=s.episode_id "
                "WHERE e.disposition='open' AND s.utc_end < ? AND s.coverage != 'gap' "
                "LIMIT ?", (cutoff, PRUNE_BATCH)))
            touched = set()
            for row in open_old:
                meta = self._conn.execute(
                    "SELECT root FROM lifecycle_cards WHERE card_id=?",
                    (row["card_id"],)).fetchone()
                root = meta["root"] if meta else ""
                day = metrics.utc_day(row["utc_end"])
                seconds = max(0.0, float(row["utc_end"]) - float(row["utc_start"]))
                self._conn.execute(
                    "INSERT INTO lifecycle_retention(root,day,category,expired_count,expired_seconds) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(root,day,category) DO UPDATE SET "
                    "expired_count=expired_count+1, expired_seconds=expired_seconds+excluded.expired_seconds",
                    (root, day, row["category"], 1, seconds))
                touched.add(row["episode_id"])
            if open_old:
                self._conn.executemany(
                    "DELETE FROM lifecycle_spans WHERE id=?",
                    [(r["id"],) for r in open_old])
                removed += len(open_old)
                for eid in touched:
                    self._conn.execute(
                        "UPDATE lifecycle_episodes SET coverage='partial',"
                        " gap_reasons=CASE WHEN gap_reasons='' THEN 'retention' "
                        " WHEN instr(gap_reasons,'retention')>0 THEN gap_reasons "
                        " ELSE gap_reasons || ',retention' END WHERE id=?",
                        (eid,))
            old_closed = list(self._conn.execute(
                "SELECT id FROM lifecycle_boundaries WHERE ts < ? AND episode_id IN "
                "(SELECT id FROM lifecycle_episodes WHERE disposition!='open') "
                "AND id NOT IN (SELECT start_boundary_id FROM lifecycle_episodes "
                "WHERE start_boundary_id IS NOT NULL) "
                "AND id NOT IN (SELECT end_boundary_id FROM lifecycle_episodes "
                "WHERE end_boundary_id IS NOT NULL) "
                "LIMIT ?", (cutoff, PRUNE_BATCH)))
            if old_closed:
                self._conn.executemany(
                    "DELETE FROM lifecycle_boundaries WHERE id=?",
                    [(r["id"],) for r in old_closed])
                removed += len(old_closed)
        return removed
