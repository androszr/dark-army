"""Outcome ledger in board.db. BoardStore owns its connection and lock.

No foreign-key cascade to cards: ordinary deletion retains this minimal ledger.
Only objective text, decision evidence and aggregate measurements live here.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import board_outcomes as metrics

logger = logging.getLogger("dark-army.outcomes")

#: How often the wait seconds accrued in memory (`_outcome_pending`) reach
#: `outcome_wait_days` when nothing else flushes them. A waiting card is the
#: steady state — a question nobody answered, a result awaiting review — and
#: adding its few seconds to the ledger on every observation pass rewrote
#: `board.db-wal` on every pass for as long as anything waited. A cause set
#: changing, the UTC day rolling and a clean close flush at once; this bounds
#: the rest, and it is what a crash can cost: at most this much waiting per
#: card, on a restart the ledger already marks partial. A minute is one WAL
#: write a minute at worst and well inside any report's resolution.
WAIT_FLUSH_SECONDS = 60.0


def _utc_day(utc: float) -> str:
    return datetime.fromtimestamp(utc, timezone.utc).date().isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_cards (
 card_id TEXT PRIMARY KEY, root TEXT NOT NULL, title TEXT NOT NULL,
 objective TEXT NOT NULL, tracking_since REAL NOT NULL,
 accepted INTEGER NOT NULL DEFAULT 0, first_accepted_at REAL,
 submission_at REAL, rework_open INTEGER NOT NULL DEFAULT 0,
 rework_count INTEGER NOT NULL DEFAULT 0,
 wait_coverage TEXT NOT NULL DEFAULT 'unknown', lifecycle_partial INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outcome_project ON outcome_cards(root, first_accepted_at);
CREATE TABLE IF NOT EXISTS outcome_events (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, ts REAL NOT NULL,
 kind TEXT NOT NULL, actor TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
 objective TEXT NOT NULL, submission_at REAL, revision INTEGER NOT NULL,
 request_key TEXT UNIQUE, request_payload TEXT, result TEXT
);
CREATE INDEX IF NOT EXISTS outcome_event_card ON outcome_events(card_id, id);
CREATE INDEX IF NOT EXISTS outcome_event_period ON outcome_events(kind, ts, card_id);
CREATE TABLE IF NOT EXISTS outcome_runs (
 card_id TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT NOT NULL,
 phase TEXT NOT NULL, root TEXT NOT NULL, bound_at REAL NOT NULL,
 late INTEGER NOT NULL DEFAULT 0, amount REAL, currency TEXT, source TEXT,
 observed_at REAL, cost_coverage TEXT NOT NULL DEFAULT 'unknown',
 cost_conflict INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(provider, session_id), UNIQUE(card_id, provider, session_id, phase)
);
CREATE INDEX IF NOT EXISTS outcome_run_card ON outcome_runs(card_id);
CREATE TABLE IF NOT EXISTS outcome_wait_days (
 card_id TEXT NOT NULL, day TEXT NOT NULL, cause TEXT NOT NULL, seconds REAL NOT NULL,
 PRIMARY KEY(card_id, day, cause)
);
CREATE TABLE IF NOT EXISTS outcome_checkpoints (
 card_id TEXT PRIMARY KEY, utc REAL NOT NULL, active INTEGER NOT NULL
);
"""


def objective(card):
    return {key: card.get(key, "") for key in metrics.OBJECTIVE_LIMITS}


def _card_cost(runs: list, partial: bool) -> dict:
    """`{"totals": {currency: amount}, "coverage": ...}` for the card's
    cost-and-time line — `run_figures()`'s grading, stated there."""
    totals: dict = {}
    complete = bool(runs) and not partial
    for run in runs:
        amount, currency = run.get("amount"), run.get("currency")
        if amount is None or not currency:
            complete = False
            continue
        totals[currency] = totals.get(currency, 0.0) + float(amount)
        if run.get("late") or run.get("cost_conflict"):
            complete = False
    complete = complete and len(totals) == 1
    return {"totals": totals, "coverage": "complete" if complete else
            ("partial" if totals else "unknown")}


class OutcomeStoreMixin:
    def _connect_outcomes(self):
        self._conn.executescript(SCHEMA)
        self._outcome_samples = {}
        # card_id -> the `active` this process last wrote to its
        # `outcome_checkpoints` row. Cleared beside the DELETE below, so the
        # first pass after a restart writes every row once: the row's
        # *existence* is what the next restart marks partial, and its `utc`
        # is read by nobody, so an unchanged `active` is an unchanged row.
        self._outcome_written = {}
        # card_id -> {(day, cause): seconds} confirmed but not yet in
        # `outcome_wait_days`, and the monotonic stamp of the last flush
        # (`WAIT_FLUSH_SECONDS`). Moved only after a commit; every reader of
        # the wait totals adds it (`_pending_waits`).
        self._outcome_pending = {}
        self._outcome_pending_flushed_at = None
        self._run_figures_memo = None
        # Rows the observation passes themselves changed (`_observed_writes`),
        # left out of `change_counter` so a pass's own checkpoints and spans
        # are never mistaken for a board write.
        self._observer_changes = 0
        with self._conn:
            for card in self.cards():
                self._outcome_seed(card, historical=True)
            # A process boundary cannot prove the interval since its last sample.
            self._conn.execute("UPDATE outcome_cards SET wait_coverage='partial' "
                               "WHERE card_id IN (SELECT card_id FROM outcome_checkpoints)")
            self._conn.execute("DELETE FROM outcome_checkpoints")

    def change_counter(self) -> int:
        """Rows this connection has inserted, updated or deleted since it
        opened (`total_changes`, every table), **minus** those the
        observation passes wrote (`observe_outcomes` / `observe_lifecycles`,
        each measured under the store lock), so the count moves on a board
        write and never on a pass's own bookkeeping. Read without the store
        lock: the daemon asks from the event loop, which must not wait out a
        reconcile holding it. The observer tally is read first, so a pass
        in flight can only make the count move early (one extra full push),
        never hide a write. 0 while closed."""
        conn = self._conn
        if conn is None:
            return 0
        observed = getattr(self, "_observer_changes", 0)
        return int(conn.total_changes) - observed

    def _observed_writes(self, start: int) -> None:
        """With the lock held: credit the rows written since `start` to the
        observation passes."""
        self._observer_changes = (getattr(self, "_observer_changes", 0)
                                  + self._conn.total_changes - start)

    def _outcome_seed(self, card, historical=False):
        self._conn.execute(
            "INSERT OR IGNORE INTO outcome_cards(card_id,root,title,objective,tracking_since,"
            "lifecycle_partial) VALUES(?,?,?,?,?,?)",
            (card["id"], str(Path(card["root"]).resolve()) if card.get("root") else "",
             card.get("title", ""), json.dumps(objective(card)), time.time(), int(historical)))

    def _outcome_event(self, card, kind, evidence="", *, actor="observer", ts=None,
                       submission_at=None, revision=None):
        self._conn.execute(
            "INSERT INTO outcome_events(card_id,ts,kind,actor,evidence,objective,submission_at,revision) "
            "VALUES(?,?,?,?,?,?,?,?)", (card["id"], time.time() if ts is None else ts,
             kind, actor, evidence, json.dumps(objective(card)), submission_at,
             card.get("outcome_revision", 0) if revision is None else revision))

    def _outcome_transition(self, before, after):
        """Inside the card write's transaction, covering both move routes."""
        self._outcome_seed(after)
        cid = after["id"]
        self._conn.execute("UPDATE outcome_cards SET title=?, objective=? WHERE card_id=?",
                           (after["title"], json.dumps(objective(after)), cid))
        next_sample = None
        resumed = before.get("outcome_status") == "accepted" and (
            after.get("link_state") == "dispatching" and before.get("link_state") != "dispatching"
            or before.get("session_id") and after.get("session_id") != before.get("session_id"))
        if resumed and not (before.get("column_name") == "done" and after.get("column_name") != "done"):
            now = self._lifecycle_utc() if hasattr(self, "_lifecycle_utc") else time.time()
            next_sample = self._outcome_review_boundary(cid, now)
            self._outcome_rework(after, "reopened", "", now)
            self._conn.execute("UPDATE cards SET outcome_revision=outcome_revision+1 WHERE id=?", (cid,))
        if before.get("column_name") == after.get("column_name"):
            return next_sample
        now = self._lifecycle_utc() if hasattr(self, "_lifecycle_utc") else time.time()
        if after.get("column_name") == "done":
            run = self._conn.execute("SELECT 1 FROM outcome_runs WHERE card_id=? "
                                     "AND phase='implementation' LIMIT 1", (cid,)).fetchone()
            if run:
                self._outcome_submit(after, now)
        elif before.get("column_name") == "done":
            next_sample = self._outcome_review_boundary(cid, now)
            self._outcome_rework(after, "reopened", "", now)
            self._conn.execute("UPDATE cards SET outcome_revision=outcome_revision+1 WHERE id=?", (cid,))
        return next_sample

    def _outcome_submit(self, card, now):
        self._outcome_event(card, "submitted", ts=now)
        self._conn.execute("UPDATE outcome_cards SET submission_at=?,rework_open=0 WHERE card_id=?",
                           (now, card["id"]))
        self._lifecycle_on_submit(card, now)

    def _outcome_rework(self, card, kind, reason, now):
        ledger = self._conn.execute("SELECT * FROM outcome_cards WHERE card_id=?", (card["id"],)).fetchone()
        if ledger["submission_at"] is not None and not ledger["rework_open"]:
            self._outcome_event(card, kind, reason, actor="user", ts=now,
                                submission_at=ledger["submission_at"])
            self._conn.execute("UPDATE outcome_cards SET rework_count=rework_count+1,rework_open=1 "
                               "WHERE card_id=?", (card["id"],))
            self._lifecycle_on_rework(card, kind, now)
        elif kind == "revision_requested":
            self._outcome_event(card, "revision_note", reason, actor="user", ts=now)
        self._conn.execute("UPDATE outcome_cards SET accepted=0 WHERE card_id=?", (card["id"],))
        self._conn.execute("UPDATE cards SET outcome_status='unaccepted' WHERE id=?", (card["id"],))

    def _objective_guard(self, current, fields):
        if not any(k in fields for k in metrics.OBJECTIVE_LIMITS):
            return ""
        refusal = metrics.objective_refusal(fields)
        if refusal:
            return refusal
        revision = fields.get("expected_outcome_revision")
        if type(revision) is not int or revision != current.get("outcome_revision", 0):
            return "the outcome changed — reload its revision and review your draft before saving"
        changed_scope = any(k in fields and fields[k] != current.get(k, "")
                            for k in ("beneficiary", "intended_benefit", "success_criterion"))
        if changed_scope and current.get("outcome_status") == "accepted" \
                and fields.get("confirm_outcome_scope_change") is not True:
            return "changing the accepted objective requires confirmation; acceptance will be removed"
        return ""

    def _objective_written(self, current, fields, updated):
        if not any(k in fields for k in metrics.OBJECTIVE_LIMITS):
            return
        self._conn.execute("UPDATE cards SET outcome_revision=outcome_revision+1 WHERE id=?", (current["id"],))
        if current.get("outcome_status") == "accepted" and any(
                k in fields and fields[k] != current.get(k, "") for k in
                ("beneficiary", "intended_benefit", "success_criterion")):
            self._outcome_event(updated, "scope_changed", actor="user",
                                revision=current.get("outcome_revision", 0) + 1)
            self._conn.execute("UPDATE cards SET outcome_status='unaccepted' WHERE id=?", (current["id"],))
            self._conn.execute("UPDATE outcome_cards SET accepted=0 WHERE card_id=?", (current["id"],))

    def outcome_replay(self, card_id, revision, key, evidence, accept):
        if not isinstance(key, str):
            return None
        payload = json.dumps([card_id, revision, evidence, accept])
        with self._lock:
            row = self._conn.execute("SELECT request_payload,result FROM outcome_events WHERE request_key=?", (key,)).fetchone()
            if not row:
                return None
            if row["request_payload"] != payload:
                return None, "that decision key was already used for different evidence"
            return json.loads(row["result"]), "decision already recorded"

    def accept_outcome(self, card_id, expected_revision, request_key, evidence):
        return self._outcome_decision(card_id, expected_revision, request_key, evidence, True)

    def request_outcome_revision(self, card_id, expected_revision, request_key, reason):
        return self._outcome_decision(card_id, expected_revision, request_key, reason, False)

    def _outcome_decision(self, card_id, revision, key, evidence, accept):
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
            return None, "a decision needs nonempty evidence or a reason, at most 4000 characters"
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            return None, "a decision needs an idempotency key of at most 100 characters"
        if type(revision) is not int or revision < 0:
            return None, "expected outcome revision must be a nonnegative integer"
        payload = json.dumps([card_id, revision, evidence, accept])
        with self._lock:
            with self._conn:
                replay = self._conn.execute("SELECT request_payload,result FROM outcome_events WHERE request_key=?", (key,)).fetchone()
                if replay:
                    if replay["request_payload"] != payload:
                        return None, "that decision key was already used for different evidence"
                    return json.loads(replay["result"]), "decision already recorded"
                card = self.get(card_id)
                if not card:
                    return None, "no such card"
                if card.get("outcome_revision", 0) != revision:
                    return None, "the outcome changed — reload its revision and review your evidence"
                if accept and (not card.get("intended_benefit", "").strip() or
                               not card.get("success_criterion", "").strip()):
                    return None, "acceptance needs an intended benefit and success criterion"
                if accept and card.get("manual_steps", "").strip():
                    return None, "mark the outstanding manual check before accepting the outcome"
                self._outcome_seed(card)
                now = self._lifecycle_utc() if hasattr(self, "_lifecycle_utc") else time.time()
                next_sample = self._outcome_review_boundary(card_id, now)
                if accept:
                    # Each explicit decision is a submission, even new evidence
                    # on an already accepted card. The first acceptance cohort is
                    # immutable; the submission cohort follows this new result.
                    self._outcome_submit(card, now)
                    self._conn.execute("UPDATE outcome_cards SET accepted=1,rework_open=0,"
                        "first_accepted_at=COALESCE(first_accepted_at,?) WHERE card_id=?", (now, card_id))
                    self._lifecycle_on_accept(card, now)
                else:
                    self._outcome_rework(card, "revision_requested", evidence, now)
                self._conn.execute("UPDATE cards SET outcome_revision=outcome_revision+1,outcome_status=?,updated_at=? WHERE id=?",
                                   ("accepted" if accept else "unaccepted", now, card_id))
                result = {"id": card_id, "outcome_revision": revision + 1,
                          "outcome_status": "accepted" if accept else "unaccepted"}
                self._conn.execute("INSERT INTO outcome_events(card_id,ts,kind,actor,evidence,objective,revision,"
                    "request_key,request_payload,result) VALUES(?,?,?,?,?,?,?,?,?,?)", (card_id, now,
                    "accepted" if accept else "decision_revision", "user", evidence,
                    json.dumps(objective(card)), revision+1, key, payload, json.dumps(result)))
            # The SQL transaction has committed. A rejected decision must
            # not consume the confirmed observation kept only in memory.
            if next_sample is not None:
                self._outcome_samples[card_id] = next_sample
            return result, "outcome accepted" if accept else "revision requested"

    def _outcome_review_boundary(self, cid, utc):
        previous = self._outcome_samples.get(cid)
        if previous is None or "review" not in previous[2]:
            return
        monotonic = time.monotonic()
        causes = previous[2] - {"review"}
        seconds, confirmed, gap = metrics.sample_delta(previous, monotonic, utc, causes)
        if gap:
            self._conn.execute("UPDATE outcome_cards SET wait_coverage='partial' WHERE card_id=?", (cid,))
        elif seconds:
            for day, part in metrics.day_parts(previous[1], seconds):
                for cause in confirmed | {"overall"}:
                    self._conn.execute("INSERT INTO outcome_wait_days VALUES(?,?,?,?) ON CONFLICT(card_id,day,cause) DO UPDATE SET seconds=seconds+excluded.seconds", (cid,day,cause,part))
        self._conn.execute("INSERT INTO outcome_checkpoints VALUES(?,?,?) ON CONFLICT(card_id) DO UPDATE SET utc=excluded.utc,active=excluded.active", (cid,utc,int(bool(causes))))
        # Written outside the observer: its next pass must not trust the memo.
        getattr(self, "_outcome_written", {}).pop(cid, None)
        return monotonic, utc, causes

    def record_outcome_run(self, card_id, provider, session_id, phase, *, late=False):
        if not session_id or provider not in ("claude", "codex", "grok") or phase not in ("implementation", "refinement"):
            return False
        if not late:
            return self._record_outcome_run(card_id, provider, session_id, phase, late=False)
        # `late=True` is the observation pass's call, made for every bound
        # card on every pass: its writes are the observer's, never a board
        # write the push's structural key should see (`change_counter`).
        with self._lock:
            start = self._conn.total_changes
            try:
                return self._record_outcome_run(card_id, provider, session_id, phase, late=True)
            finally:
                self._observed_writes(start)

    def _record_outcome_run(self, card_id, provider, session_id, phase, *, late):
        with self._lock, self._conn:
            card = self.get(card_id)
            if not card:
                return False
            self._outcome_seed(card)
            prior = self._conn.execute("SELECT card_id,phase FROM outcome_runs WHERE provider=? AND session_id=?",
                                       (provider, session_id)).fetchone()
            if prior:
                if prior["card_id"] == card_id and prior["phase"] == phase:
                    return True
                # Only where a value would change: a session bound to two
                # cards is re-seen on every pass, and rewriting the same
                # conflict marks each time touched the WAL every reconcile.
                self._conn.execute("UPDATE outcome_runs SET cost_coverage='partial',cost_conflict=1 WHERE provider=? AND session_id=?"
                                   " AND (cost_coverage!='partial' OR cost_conflict!=1)", (provider,session_id))
                self._conn.execute("UPDATE outcome_cards SET lifecycle_partial=1 WHERE card_id IN (?,?) AND lifecycle_partial!=1",
                                   (card_id,prior["card_id"]))
                return False
            root = str(Path(card["root"]).resolve()) if card.get("root") else ""
            self._conn.execute("INSERT INTO outcome_runs(card_id,provider,session_id,phase,root,bound_at,late) VALUES(?,?,?,?,?,?,?)",
                               (card_id, provider, session_id, phase, root, time.time(), int(late)))
            if late:
                self._conn.execute("UPDATE outcome_cards SET lifecycle_partial=1,wait_coverage='partial' WHERE card_id=?", (card_id,))
            return True

    def observe_outcome(self, card_id, causes, *, utc=None, monotonic=None, costs=()):
        utc = time.time() if utc is None else utc
        monotonic = time.monotonic() if monotonic is None else monotonic
        with self._lock:
            start = self._conn.total_changes
            try:
                with self._conn:
                    done = self._observe_outcome_locked(card_id, causes, utc, monotonic, costs)
                    staged = {} if done is None else {card_id: done}
                    flushed = self._waits_flush_locked(staged, monotonic, isolate=False)
            finally:
                self._observed_writes(start)
            if done is not None:
                self._outcome_settle(card_id, done, monotonic, utc)
            self._waits_settle(staged, flushed, monotonic)

    def observe_outcomes(self, samples, *, utc=None, monotonic=None) -> bool:
        """`observe_outcome` for a whole pass: `samples` is
        `[(card_id, causes_or_None, costs)]`. One lock, one transaction, a
        SAVEPOINT per card: a card that raises rolls back its own writes,
        is logged and skipped, and the rest of the pass still commits. The
        in-memory samples move only for cards whose SAVEPOINT was released,
        and only once the transaction has committed, so a committed row and
        a remembered sample never disagree. The pass's wait seconds are
        staged, not written: they join `_outcome_pending` after the commit
        and reach the ledger only when `_waits_flush_locked` says so, in a
        SAVEPOINT of its own. Returns whether any card, or that flush, failed."""
        utc = time.time() if utc is None else utc
        monotonic = time.monotonic() if monotonic is None else monotonic
        failed = False
        settled = []
        flushed: list = []
        with self._lock:
            start = self._conn.total_changes
            try:
                failed = self._observe_outcomes_locked(samples, utc, monotonic, settled, flushed)
            finally:
                self._observed_writes(start)
            for card_id, done in settled:
                self._outcome_settle(card_id, done, monotonic, utc)
            if flushed:
                self._waits_settle(dict(settled), flushed[0], monotonic)
        return failed

    def _observe_outcomes_locked(self, samples, utc, monotonic, settled, flushed) -> bool:
        """`observe_outcomes`' transaction, with the lock held; fills
        `settled` for cards whose SAVEPOINT was released and `flushed` with
        the wait flush's result, appended only once the batch is written."""
        failed = False
        with self._conn:
            # Explicit: a SAVEPOINT outside a transaction opens its own,
            # and releasing it would commit once per card.
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN")
            for card_id, causes, costs in samples:
                self._conn.execute("SAVEPOINT outcome")
                try:
                    done = self._observe_outcome_locked(
                        card_id, causes, utc, monotonic, costs or ())
                    self._conn.execute("RELEASE SAVEPOINT outcome")
                except Exception:
                    logger.warning("outcome observation rolled back", exc_info=True)
                    self._conn.execute("ROLLBACK TO SAVEPOINT outcome")
                    self._conn.execute("RELEASE SAVEPOINT outcome")
                    failed = True
                    continue
                if done is not None:
                    settled.append((card_id, done))
            result = self._waits_flush_locked(dict(settled), monotonic, isolate=True)
            failed = failed or result is None
            # A commit that raises on leaving the `with` propagates past the
            # caller's settle, so no pending second moves on a lost pass.
            flushed.append(result)
        return failed

    def _outcome_settle(self, card_id, done, monotonic, utc):
        """After the commit: remember the sample and the row just written."""
        causes, written = done[:2]
        if causes is None:
            self._outcome_samples.pop(card_id, None)
        else:
            self._outcome_samples[card_id] = (monotonic, utc, causes)
        if written is not None:
            self._outcome_written[card_id] = written

    def _observe_outcome_locked(self, card_id, causes, utc, monotonic, costs):
        """One card's observation, with the lock and a transaction held.

        Writes only where a row would change: the `wait_coverage` UPDATE when
        the coverage moved, the checkpoint upsert when its `active` differs
        from what this process last wrote (or it wrote none). Confirmed wait
        seconds are returned, never written here: `_waits_flush_locked`
        decides when they reach `outcome_wait_days`. Returns `(causes, active
        written or None, {(day, cause): seconds} accrued, flush now)` for
        `_outcome_settle` / `_waits_settle`, or None when the card has no
        ledger row. Flush now is a changed cause set, a gap or a missing
        sample, or a UTC day other than today's among its seconds."""
        ledger = self._conn.execute("SELECT * FROM outcome_cards WHERE card_id=?", (card_id,)).fetchone()
        if not ledger:
            return None
        causes = None if causes is None else set(causes) & set(metrics.CAUSES)
        if causes is not None and ledger["submission_at"] is not None and not ledger["accepted"] and not ledger["rework_open"]:
            causes.add("review")
        previous = self._outcome_samples.get(card_id)
        seconds, confirmed, gap = metrics.sample_delta(previous, monotonic, utc, causes)
        # A missing first observation established a coverage gap too.
        # Its checkpoint survives until the next positive sample even
        # though there is no prior positive sample in memory.
        if previous is None and causes is not None and self._conn.execute(
                "SELECT 1 FROM outcome_checkpoints WHERE card_id=?", (card_id,)).fetchone():
            gap = True
        coverage = "partial" if gap or ledger["lifecycle_partial"] else "complete_since_tracking"
        if causes is None and previous is None:
            coverage = "unknown" if ledger["wait_coverage"] == "unknown" else "partial"
        if ledger["wait_coverage"] == "partial":
            coverage = "partial"
        if coverage != ledger["wait_coverage"]:
            self._conn.execute("UPDATE outcome_cards SET wait_coverage=? WHERE card_id=?", (coverage,card_id))
        accrued = {}
        if seconds:
            for day, part in metrics.day_parts(previous[1], seconds):
                for cause in confirmed | {"overall"}:
                    accrued[(day, cause)] = accrued.get((day, cause), 0.0) + part
        today = _utc_day(utc)
        flush = (gap or causes is None or previous is None or causes != previous[2]
                 or any(day != today for day, _ in accrued)
                 or any(day != today for day, _ in self._outcome_pending.get(card_id, {})))
        active = int(bool(causes))
        written = None
        if self._outcome_written.get(card_id) != active:
            self._conn.execute("INSERT INTO outcome_checkpoints VALUES(?,?,?) ON CONFLICT(card_id) DO UPDATE "
                               "SET utc=excluded.utc,active=excluded.active", (card_id,utc,active))
            written = active
        for reading in costs:
            self._outcome_cost(card_id, reading, utc)
        return causes, written, accrued, flush

    def _waits_flush_locked(self, staged, monotonic, *, isolate):
        """Write the pending wait seconds that are due, with the lock and the
        pass's transaction held; `staged` is `{card_id: done}` for the cards
        this pass observed. Due: a card whose sample said flush now, and
        every card once `WAIT_FLUSH_SECONDS` lapsed since the last flush.
        Each flushed card writes its pending plus this pass's accrual.

        Returns `(written {card_id: {(day, cause): seconds}}, lapsed)` for
        `_waits_settle`. `isolate` runs the writes in a SAVEPOINT: a failure
        rolls back only them, is logged and returns None, so the seconds stay
        pending and are neither lost nor counted twice; without it the
        failure raises and the caller's transaction rolls back whole."""
        pending = self._outcome_pending
        stamp = self._outcome_pending_flushed_at
        lapsed = stamp is not None and not 0 <= monotonic - stamp < WAIT_FLUSH_SECONDS
        cards = set(pending) | set(staged) if lapsed else {
            cid for cid, done in staged.items() if done[3]}
        plan = {}
        for cid in cards:
            combined = dict(pending.get(cid, {}))
            done = staged.get(cid)
            for key, part in (done[2] if done is not None else {}).items():
                combined[key] = combined.get(key, 0.0) + part
            plan[cid] = combined
        rows = [(cid, day, cause, part) for cid, combined in plan.items()
                for (day, cause), part in combined.items() if part]
        if not rows:
            return plan, lapsed
        if isolate:
            self._conn.execute("SAVEPOINT wait_flush")
        try:
            self._conn.executemany(
                "INSERT INTO outcome_wait_days VALUES(?,?,?,?) ON CONFLICT(card_id,day,cause) "
                "DO UPDATE SET seconds=seconds+excluded.seconds", rows)
            if isolate:
                self._conn.execute("RELEASE SAVEPOINT wait_flush")
        except Exception:
            if not isolate:
                raise
            logger.warning("wait seconds flush rolled back; they stay pending", exc_info=True)
            self._conn.execute("ROLLBACK TO SAVEPOINT wait_flush")
            self._conn.execute("RELEASE SAVEPOINT wait_flush")
            return None
        return plan, lapsed

    def _waits_settle(self, staged, result, monotonic):
        """After the commit: fold the pass's accrual into `_outcome_pending`,
        then drop what `_waits_flush_locked` wrote. A failed flush (None)
        keeps everything pending for the next one."""
        pending = self._outcome_pending
        for cid, done in staged.items():
            if done[2]:
                mine = pending.setdefault(cid, {})
                for key, part in done[2].items():
                    mine[key] = mine.get(key, 0.0) + part
        if self._outcome_pending_flushed_at is None:
            self._outcome_pending_flushed_at = monotonic
        if result is None:
            return
        written, lapsed = result
        for cid in written:
            pending.pop(cid, None)
        if lapsed:
            self._outcome_pending_flushed_at = monotonic

    def _pending_waits(self, card_id) -> dict:
        """`{cause: seconds}` confirmed for this card but not yet in
        `outcome_wait_days`, `"overall"` included — what every reader of the
        wait totals adds to the ledger's sums. With the lock held."""
        totals: dict = {}
        for (_day, cause), part in getattr(self, "_outcome_pending", {}).get(card_id, {}).items():
            totals[cause] = totals.get(cause, 0.0) + part
        return totals

    def flush_outcome_waits(self) -> None:
        """Write every pending wait second now — the clean close's call
        (`BoardStore.close`), so only a crash costs the flush interval. A
        failure is logged and the seconds are dropped with the connection."""
        with self._lock:
            pending = getattr(self, "_outcome_pending", None)
            if not pending or self._conn is None:
                return
            try:
                with self._conn:
                    self._conn.executemany(
                        "INSERT INTO outcome_wait_days VALUES(?,?,?,?) ON CONFLICT(card_id,day,cause) "
                        "DO UPDATE SET seconds=seconds+excluded.seconds",
                        [(cid, day, cause, part) for cid, combined in pending.items()
                         for (day, cause), part in combined.items() if part])
            except Exception:
                logger.warning("could not flush the pending wait seconds", exc_info=True)
            pending.clear()

    def _outcome_cost(self, cid, reading, utc):
        value = metrics.cost_reading(reading.get("amount"), reading.get("currency"), reading.get("source"), complete=reading.get("complete") is True)
        if value is None:
            return
        run = self._conn.execute("SELECT * FROM outcome_runs WHERE card_id=? AND provider=? AND session_id=?",
                                 (cid,reading.get("provider"),reading.get("session_id"))).fetchone()
        if run is None:
            return
        conflict = bool(run["cost_conflict"])
        amount = value["amount"]
        currency = value["currency"]
        source = value["source"]
        if run["amount"] is not None:
            if source != run["source"]:
                conflict = True
            if currency != run["currency"] or amount < run["amount"]:
                conflict = True
                amount, currency, source = run["amount"], run["currency"], run["source"]
        coverage = "partial" if conflict or run["late"] else value["coverage"]
        if (run["amount"] == amount and run["currency"] == currency and run["source"] == source
                and run["cost_coverage"] == coverage and bool(run["cost_conflict"]) == conflict):
            # The same reading again: rewriting it would only move
            # `observed_at` (read by nobody) and the WAL, every pass, for
            # every card that ever ran.
            return
        self._conn.execute("UPDATE outcome_runs SET amount=?,currency=?,source=?,observed_at=?,cost_coverage=?,cost_conflict=? "
                           "WHERE provider=? AND session_id=?", (amount,currency,source,utc,coverage,int(conflict),run["provider"],run["session_id"]))

    def outcome_observation_states(self):
        with self._lock:
            return {r["card_id"]: dict(r) for r in self._conn.execute(
                "SELECT card_id,submission_at,accepted,rework_open FROM outcome_cards WHERE card_id IN (SELECT id FROM cards)")}

    def outcome_card_report(self, card_id, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset nonnegative")
        with self._lock:
            row = self._conn.execute("SELECT * FROM outcome_cards WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                return {"available": True, "supported": True, "card": None}
            card = dict(row)
            card["objective"] = json.loads(card["objective"])
            live = self.get(card_id)
            card["revision"] = live.get("outcome_revision", 0) if live else self._conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM outcome_events WHERE card_id=?", (card_id,)).fetchone()[0]
            card["accepted"] = bool(card["accepted"])
            run_page = [dict(r) for r in self._conn.execute("SELECT * FROM outcome_runs WHERE card_id=? ORDER BY bound_at,provider,session_id LIMIT ? OFFSET ?", (card_id,limit+1,offset))]
            card["runs"] = run_page[:limit]
            all_runs = [dict(r) for r in self._conn.execute("SELECT * FROM outcome_runs WHERE card_id=?", (card_id,))]
            card["cost"] = metrics.lifecycle_cost(all_runs, bool(card["lifecycle_partial"]))
            totals = {r["cause"]: r["seconds"] for r in self._conn.execute("SELECT cause,SUM(seconds) AS seconds FROM outcome_wait_days WHERE card_id=? GROUP BY cause", (card_id,))}
            for cause, part in self._pending_waits(card_id).items():
                totals[cause] = (totals.get(cause) or 0.0) + part
            card["wait_seconds"] = totals.pop("overall", 0.0)
            card["wait_causes"] = totals
            events = [dict(r) for r in self._conn.execute("SELECT id,card_id,ts,kind,actor,evidence,objective,revision,submission_at FROM outcome_events WHERE card_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (card_id,limit+1,offset))]
            more = len(events) > limit or len(run_page) > limit
            for event in events:
                event["objective"] = json.loads(event["objective"])
            return {"available": True, "supported": True, "card": card,
                    "events": events[:limit], "next_offset": offset+limit if more else None}

    def outcome_run_index(self, root="", start=None, end=None):
        """Which sessions worked which cards — the board half of the agent report.

        A **read**: no INSERT, no UPDATE, and it names no member of
        `_WRITABLE`, `SINGLE_WRITER`, `REVISED_COLUMNS` or
        `ApiServer._BOARD_FIELDS`. It exists so the join to `history.db` can
        be done in Python by the caller: the two databases have separate
        locks and separate lifetimes (history prunes at 90 days, the board
        does not), and one connection spanning both would hold the board's
        lock for the length of a multi-second history scan. So this returns
        its rows and releases.
        """
        sql = ("SELECT r.card_id, r.provider, r.session_id, r.phase, r.root,"
               " r.bound_at FROM outcome_runs r")
        params: list = []
        where = []
        if root:
            sql += " JOIN outcome_cards c ON c.card_id = r.card_id"
            where.append("c.root = ?")
            params.append(str(Path(root).resolve()))
        if start is not None:
            where.append("r.bound_at >= ?")
            params.append(float(start))
        if end is not None:
            where.append("r.bound_at < ?")
            params.append(float(end))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.bound_at, r.card_id, r.session_id"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params))]

    def run_figures(self) -> dict:
        """`{card_id: {cost, seconds, ticking, attempts}}` — the raw parts of
        every card's cost-and-time line, one locked read per frame.

        A **read** over the two retained ledgers, `outcome_run_index`'s
        discipline: no INSERT, no UPDATE, nothing in `_WRITABLE` or
        `ApiServer._BOARD_FIELDS`. Cost is the USD sum over `outcome_runs`
        (both phases: a refinement's spend is on the card), **graded for the
        card by this read and not by `metrics.lifecycle_cost`**: that
        function's `complete` is reserved for the outcome report's stricter
        question (a reading that proves child inclusion, which neither the
        live statusline nor `history.db` does, so every row's
        `cost_coverage` column reads `partial`). The card's line asks a
        plainer one — was every session's spend measured, and did nothing
        go wrong with the readings — so here `complete` means every run row
        carries a USD amount, none was a late bind (`late`) and none saw a
        decrease or a changed source (`cost_conflict`), and the card is not
        `lifecycle_partial`; `partial` is exactly what the plan reserved the
        word for; `unknown` is no amount at all (a Codex card). Working
        time is the sum of `lifecycle_spans` execution spans with
        `coverage='complete'` (implementation alone — that is the ledger's
        own definition of execution), and **`None` where the card has no
        execution span at all** — a refined-but-never-started card has a
        run row and no working time, which is not the same fact as under a
        minute of it; `ticking` is whether the card's active
        `lifecycle_checkpoints` row holds an `execution` membership;
        `attempts` is the distinct implementation session ids among the run
        rows — never `lifecycle_attempts`, which also counts a dispatch that
        never bound.

        A card appears **only** with a run row or an execution span, so an
        untouched card has no key and the snapshot carries nothing for it.
        `run_figures.compose` is applied at decorate time, where the live
        context percent can be joined in. Wrapped like `run_headlines`'s
        caller: a failure logs and returns `{}` so a frame is never lost to
        this read.

        **Memoised on the connection's `total_changes`**: with no row
        written since the last read, the last answer is returned and no SQL
        runs. A cache of this read, never a second source; any write to any
        table invalidates it (conservative). The memo's dict is handed to
        every caller as is — `_run_figures_drifted` and
        `_decorate_card_for_snapshot` only read it — so a caller must never
        mutate what this returns.
        """
        try:
            with self._lock:
                counter = self._conn.total_changes
                memo = getattr(self, "_run_figures_memo", None)
                if memo is not None and memo[0] == counter:
                    return memo[1]
                run_rows = [dict(r) for r in self._conn.execute(
                    "SELECT card_id, provider, session_id, phase, amount,"
                    " currency, late, cost_conflict FROM outcome_runs")]
                partial = {r["card_id"]: bool(r["lifecycle_partial"])
                           for r in self._conn.execute(
                               "SELECT card_id, lifecycle_partial FROM outcome_cards")}
                span_rows = self._conn.execute(
                    "SELECT card_id, SUM(utc_end - utc_start) AS seconds"
                    " FROM lifecycle_spans WHERE category='execution'"
                    " AND coverage='complete' GROUP BY card_id").fetchall()
                check_rows = self._conn.execute(
                    "SELECT card_id, memberships FROM lifecycle_checkpoints"
                    " WHERE active=1").fetchall()
        except Exception:
            logger.warning("could not read the run figures", exc_info=True)
            return {}
        runs_by_card: dict = {}
        for row in run_rows:
            runs_by_card.setdefault(str(row["card_id"]), []).append(row)
        seconds_by_card = {}
        for row in span_rows:
            try:
                seconds_by_card[str(row["card_id"])] = max(
                    0.0, float(row["seconds"] or 0.0))
            except (TypeError, ValueError):
                seconds_by_card[str(row["card_id"])] = 0.0
        ticking = set()
        for row in check_rows:
            try:
                members = json.loads(row["memberships"] or "[]")
            except (TypeError, ValueError):
                continue
            if any(isinstance(m, (list, tuple)) and m and m[0] == "execution"
                   for m in members):
                ticking.add(str(row["card_id"]))
        out = {}
        for cid in set(runs_by_card) | set(seconds_by_card):
            runs = runs_by_card.get(cid, [])
            out[cid] = {
                "cost": _card_cost(runs, partial.get(cid, False)),
                "seconds": seconds_by_card.get(cid),
                "ticking": cid in ticking,
                "attempts": len({r["session_id"] for r in runs
                                 if r.get("phase") == "implementation"
                                 and r.get("session_id")}),
            }
        with self._lock:
            # Stored against the counter the SELECTs saw; a write that landed
            # since moves the counter and the next read recomputes.
            self._run_figures_memo = (counter, out)
        return out

    def outcome_project_report(self, root, start, end, *, limit=100, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0
                or isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end) or start < 0
                or end <= start or end-start > 366*86400):
            raise ValueError("report needs a period up to 366 days, limit 1..100 and nonnegative offset")
        root = str(Path(root).resolve()) if root else ""
        with self._lock:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM outcome_cards WHERE root=? ORDER BY card_id", (root,))]
            runs = {}
            for run in self._conn.execute("SELECT r.* FROM outcome_runs r JOIN outcome_cards c ON c.card_id=r.card_id WHERE c.root=?", (root,)):
                runs.setdefault(run["card_id"], []).append(dict(run))
            waits = {r["card_id"]: r["seconds"] for r in self._conn.execute(
                "SELECT w.card_id,SUM(w.seconds) AS seconds FROM outcome_wait_days w JOIN outcome_cards c ON c.card_id=w.card_id WHERE c.root=? AND w.cause='overall' GROUP BY w.card_id", (root,))}
            for row in rows:
                row["cost"] = metrics.lifecycle_cost(runs.get(row["card_id"], []), bool(row["lifecycle_partial"]))
                row["wait_seconds"] = ((waits.get(row["card_id"]) or 0.0)
                                       + self._pending_waits(row["card_id"]).get("overall", 0.0))
            # The root index bounds the join; never scan history.db on a request.
            events = [dict(r) for r in self._conn.execute("SELECT e.card_id,e.ts,e.kind,e.submission_at FROM outcome_events e JOIN outcome_cards c ON c.card_id=e.card_id WHERE c.root=? AND e.ts>=? AND e.ts<? AND e.kind IN ('submitted','revision_requested','reopened')", (root,start,end))]
            summary = metrics.project_summary(rows, [e for e in events if e["kind"] == "submitted"],
                                              [e for e in events if e["kind"] != "submitted"], start,end)
            return {"available": True, "supported": True, "root": root, "from": start, "to": end,
                    "tracking_since": min((r["tracking_since"] for r in rows),default=None),
                    "summary": summary, "cards": [{k:r[k] for k in ("card_id","title","accepted","rework_count")} for r in rows[offset:offset+limit]],
                    "next_offset": offset+limit if len(rows)>offset+limit else None}
