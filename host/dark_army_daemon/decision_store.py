"""Private, bounded decision history. All methods run on the history worker.

Absence is never an answer. A receipt is an immutable set of event identities,
owned by one paired device, and is only returned after its transaction commits.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .paths import STATE_DIR, ensure_state_dir

RETENTION_DAYS = 90
MAX_EVENTS = 20_000
MAX_RECORDS = 20_000
MAX_MEMBERS = 32
SCHEMA_VERSION = 1
STATUSES = {"open", "answered_observed", "delivered_unconfirmed", "superseded", "ended_unknown"}


def uuid_ok(value):
    try:
        return isinstance(value, str) and len(value) == 36 and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


#: How long a statement waits on another writer's lock before it fails.
#: A test of the failure path lowers it rather than waiting the five seconds.
BUSY_TIMEOUT_MS = 5000

class DecisionStore:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else STATE_DIR / "decisions.db"
        self.clock = clock
        self.lock = threading.RLock()
        self.db = None
        self.failed = False

    def open(self):
        if self.path.parent == STATE_DIR:
            ensure_state_dir()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if self.db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                self.close()
                raise ValueError("decision history belongs to a newer Dark Army")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, slot TEXT NOT NULL,
                fingerprint TEXT NOT NULL, active INTEGER NOT NULL, updated REAL NOT NULL,
                data TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS episode_slot ON episodes(slot,active);
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL, stamp REAL NOT NULL, root TEXT NOT NULL,
                dedupe TEXT UNIQUE, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, device TEXT NOT NULL,
                stamp REAL NOT NULL, members TEXT NOT NULL);
            """)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('observation_start',?)", (str(self.clock()),))
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.path) + suffix)
                if path.exists():
                    path.chmod(0o600)
        except Exception:
            self.close()
            raise

    def close(self):
        with self.lock:
            if self.db:
                self.db.close()
            self.db = None

    def _now(self):
        # Sequence is ordering authority; this floor only makes retention safe
        # when the wall clock moves backwards.
        row = self.db.execute("SELECT value FROM meta WHERE key='clock'").fetchone()
        now = max(self.clock(), float(row[0]) if row else 0)
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('clock',?)", (str(now),))
        return now

    def _bounded(self, facts):
        out = {key: str(facts.get(key) or "")[:cap] for key, cap in
               (("root", 1024), ("project", 400), ("provider", 80),
                ("session_id", 200), ("card_id", 200), ("source_id", 200),
                ("title", 400), ("kind", 80), ("provenance", 400), ("outcome", 2000))}
        raw = facts.get("questions") or []
        questions = []
        truncated = len(raw) > 8
        for q in raw[:8]:
            if not isinstance(q, dict):
                continue
            truncated |= (len(str(q.get("id") or "")) > 200 or
                          len(str(q.get("text") or q.get("question") or "")) > 4000 or
                          len(q.get("options") or []) > 8 or
                          any(len(option) > 400 for option in (q.get("options") or []) if isinstance(option, str)))
            questions.append({"id": str(q.get("id") or "")[:200],
                              "text": str(q.get("text") or q.get("question") or "")[:4000],
                              "options": [str(option)[:400] for option in (q.get("options") or [])[:8] if isinstance(option, str)],
                              "multi_select": q.get("multi_select") is True,
                              "has_preview": q.get("has_preview") is True})
        text = str(facts.get("question_text") or "")[:8000]
        truncated |= len(str(facts.get("question_text") or "")) > len(text)
        while questions and len(json.dumps(questions, ensure_ascii=False)) + len(text) > 8000:
            questions.pop()
            truncated = True
        out["questions"] = questions
        out["question_text"] = text
        out["truncated"] = truncated or any(
            len(str(facts.get(k) or "")) > cap for k, cap in (("title", 400), ("outcome", 2000)))
        return out

    def _append(self, item, now, dedupe=None):
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO events(episode_id,stamp,root,dedupe,data) VALUES(?,?,?,?,?)",
            (item["id"], now, item["root"], dedupe, json.dumps(item)))
        return cursor.lastrowid

    def observe(self, slot, facts):
        with self.lock, self.db:
            now = self._now()
            self._prune(now)
            item = self._bounded(facts)
            fingerprint = hashlib.sha256(json.dumps(
                [item["source_id"], item["questions"], item["question_text"]], sort_keys=True).encode()).hexdigest()
            old = self.db.execute("SELECT * FROM episodes WHERE slot=? AND active=1", (slot,)).fetchone()
            if old and old["fingerprint"] == fingerprint:
                previous = json.loads(old["data"])
                enriched = dict(previous)
                for key in ("root", "project", "provider", "title"):
                    if not enriched.get(key) and item.get(key):
                        enriched[key] = item[key]
                if enriched != previous:
                    self.db.execute("UPDATE episodes SET data=? WHERE id=?", (json.dumps(enriched), enriched["id"]))
                    self._append(enriched, now)
                return enriched
            if old:
                self._finish(old, "superseded", "A different question replaced this one.", "observed replacement", now)
            item.update(id=str(uuid.uuid4()), status="open", opened_at=now, updated_at=now)
            self.db.execute("INSERT INTO episodes(id,slot,fingerprint,active,updated,data) VALUES(?,?,?,?,?,?)", (item["id"], slot, fingerprint, 1, now, json.dumps(item)))
            self._append(item, now)
            self._prune(now)
            return item

    def _finish(self, row, status, outcome, provenance, now, dedupe=None):
        item = json.loads(row["data"])
        # A delivery completion can lag the authoritative transcript result.
        # It adds no evidence and must never downgrade the observed answer.
        if item["status"] == "answered_observed" and status == "delivered_unconfirmed":
            return
        if item["status"] == status and item["outcome"] == outcome:
            return
        disappeared = status in ("ended_unknown", "superseded")
        if disappeared and item["status"] in ("delivered_unconfirmed", "answered_observed"):
            status, outcome, provenance = item["status"], item["outcome"], item["provenance"]
        item["truncated"] |= len(outcome) > 2000 or len(provenance) > 400
        item.update(status=status, outcome=outcome[:2000], provenance=provenance[:400], updated_at=now)
        active = bool(row["active"]) and not disappeared and status in ("open", "delivered_unconfirmed")
        self.db.execute("UPDATE episodes SET active=?,updated=?,data=? WHERE id=?",
                        (int(active), now, json.dumps(item), item["id"]))
        self._append(item, now, dedupe)

    def finish(self, slot, status="ended_unknown", outcome="Outcome not observed.", provenance="observed disappearance", episode_id=None):
        if status not in STATUSES:
            raise ValueError("unknown decision status")
        with self.lock, self.db:
            now = self._now()
            row = self.db.execute("SELECT * FROM episodes WHERE slot=? AND id=?", (slot, episode_id)).fetchone() if episode_id else self.db.execute("SELECT * FROM episodes WHERE slot=? AND active=1", (slot,)).fetchone()
            if row and (episode_id is None or row["id"] == episode_id):
                self._finish(row, status, outcome, provenance, now)
            self._prune(now)

    def observed_question_result(self, sid, source_id, outcome, truncated=False):
        # Only enrich an episode Dark Army actually observed. Old transcript history
        # cannot bootstrap decisions, and a late result cannot answer a successor.
        if not source_id or not outcome:
            return
        with self.lock, self.db:
            for row in self.db.execute("SELECT * FROM episodes WHERE slot=?", ("question:" + sid,)).fetchall():
                item = json.loads(row["data"])
                if item["source_id"] == source_id:
                    if truncated and not item["truncated"]:
                        item["truncated"] = True
                        self.db.execute("UPDATE episodes SET data=? WHERE id=?", (json.dumps(item), item["id"]))
                        row = self.db.execute("SELECT * FROM episodes WHERE id=?", (item["id"],)).fetchone()
                    self._finish(row, "answered_observed", outcome, "observed matching question tool result", self._now())

    def card_event(self, card, kind, detail):
        if kind not in {"card_done", "card_manual", "card_manual_clear", "card_plan_attached", "card_dispatch_failed", "card_dispatched"}:
            return
        with self.lock, self.db:
            now = self._now()
            facts = dict(card, card_id=card.get("id"), kind=kind,
                         outcome=detail.get("note") or detail.get("error") or card.get("close_note") or card.get("manual_steps") or kind.replace("_", " "),
                         provenance="agent declaration" if card.get("closed_by") and detail.get("closed_by") != "user" else "observed card transition")
            if kind == "card_dispatch_failed":
                facts["question_text"] = facts["outcome"]
            if kind == "card_manual":
                facts["question_text"] = card.get("manual_steps") or detail.get("note") or ""
            item = self._bounded(facts)
            revision = [card.get("id"), kind, card.get("updated_at"), card.get("manual_steps"), card.get("plan_path"), detail]
            dedupe = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
            if self.db.execute("SELECT 1 FROM events WHERE dedupe=?", (dedupe,)).fetchone():
                return
            recovery = {
                "card_dispatched": "A refinement terminal opened on retry; plan completion is not yet observed."
                if detail.get("phase") == "refinement" else
                "An implementation terminal opened on retry; work completion is not yet observed.",
                "card_plan_attached": "A plan was attached after the launch failure.",
                "card_done": "The card was declared Done after the launch failure; human acceptance is separate.",
            }.get(kind)
            if recovery:
                failed = self.db.execute(
                    "SELECT * FROM episodes WHERE slot=? AND active=1",
                    ("card:" + str(card.get("id") or "") + ":card_dispatch_failed",)).fetchone()
                if failed:
                    previous_failure = json.loads(failed["data"])
                    # Keep the original failure and episode identity, including
                    # receipts minted before question_text carried the error.
                    # A launch is recovery of this failure, never proof of Done.
                    self._finish(failed, "superseded",
                                 recovery + " Earlier launch failure: " + previous_failure["outcome"],
                                 "observed card transition", now,
                                 dedupe=dedupe if kind == "card_dispatched" else None)
            if kind == "card_dispatched":
                # Successful launches only resolve existing failures. They do
                # not create a completed-work item in decision history.
                self._prune(now)
                return
            family = "manual" if kind in ("card_manual", "card_manual_clear") else kind
            slot = "card:" + str(card.get("id") or "") + ":" + family
            # Only an active cycle may receive another revision or its clear.
            # Completed cycles remain durable identities for old receipts.
            old = self.db.execute("SELECT * FROM episodes WHERE slot=? AND active=1 ORDER BY updated DESC,rowid DESC LIMIT 1", (slot,)).fetchone()
            if kind == "card_manual_clear" and old is None:
                return
            previous = json.loads(old["data"]) if old else None
            if kind == "card_manual_clear":
                item["question_text"] = previous["question_text"]
                item["truncated"] |= previous["truncated"]
            identity = old["id"] if old else str(uuid.uuid4())
            item.update(id=identity, status="open" if kind in ("card_manual", "card_dispatch_failed") else "answered_observed", opened_at=previous["opened_at"] if previous else now, updated_at=now)
            if old:
                self.db.execute("UPDATE episodes SET active=?,updated=?,data=? WHERE id=?", (int(item["status"] == "open"), now, json.dumps(item), identity))
            else:
                self.db.execute("INSERT INTO episodes(id,slot,fingerprint,active,updated,data) VALUES(?,?,?,?,?,?)", (identity, slot, dedupe, int(item["status"] == "open"), now, json.dumps(item)))
            self._append(item, now, dedupe)
            self._prune(now)

    def reconcile_missing(self, seen, include_permissions=False):
        with self.lock, self.db:
            now = self._now()
            pattern = "(slot LIKE 'question:%' OR slot LIKE 'permission:%')" if include_permissions else "slot LIKE 'question:%'"
            for row in self.db.execute("SELECT * FROM episodes WHERE active=1 AND " + pattern).fetchall():
                if row["slot"] not in seen:
                    self._finish(row, "ended_unknown", "Outcome not observed.", "observed disappearance", now)
            self._prune(now)

    def _prune(self, now):
        floor = now - RETENTION_DAYS * 86400
        old = self.db.execute("SELECT MAX(seq) FROM events WHERE stamp<? OR seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT ?)", (floor, MAX_EVENTS)).fetchone()[0]
        if old:
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('pruned_through',?)", (str(old),))
        self.db.execute("DELETE FROM events WHERE stamp<? OR seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT ?)", (floor, MAX_EVENTS))
        self.db.execute("DELETE FROM episodes WHERE updated<? OR id NOT IN (SELECT id FROM episodes ORDER BY updated DESC LIMIT ?)", (floor, MAX_RECORDS))
        self.db.execute("DELETE FROM receipts WHERE stamp<? OR id NOT IN (SELECT id FROM receipts ORDER BY stamp DESC LIMIT ?)", (floor, MAX_RECORDS))
        if self.failed:
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('storage_gap',?)", (str(now),))
            self.failed = False

    def targets(self, *, episode_id="", permission_id="", card_ids=()):
        with self.lock:
            ids = []
            if episode_id:
                ids.append(episode_id)
            if permission_id:
                row = self.db.execute("SELECT id FROM episodes WHERE slot=? AND active=1", ("permission:" + permission_id,)).fetchone()
                if row:
                    ids.append(row[0])
            for card_id in card_ids:
                for row in self.db.execute("SELECT data FROM events ORDER BY seq DESC"):
                    item = json.loads(row[0])
                    if item["card_id"] == card_id:
                        ids.append(item["id"])
                        break
            return ids

    def receipt(self, device, identities):
        with self.lock, self.db:
            now = self._now()
            self._prune(now)
            members = []
            for identity in dict.fromkeys(identities):
                row = self.db.execute("SELECT MAX(seq) FROM events WHERE episode_id=?", (identity,)).fetchone()
                if not row or row[0] is None:
                    return None
                members.append(row[0])
            if not members or len(members) > MAX_MEMBERS:
                return None
            rid = str(uuid.uuid4())
            self.db.execute("INSERT INTO receipts(id,device,stamp,members) VALUES(?,?,?,?)", (rid, device, now, json.dumps(members)))
            self._prune(now)
        return rid

    def query(self, *, roots, device="", receipt_id=None, cursor=0, upper_cursor=None,
              limit=100, project=None, since=0, until=None, page_cursor=None):
        with self.lock:
            now = max(self.clock(), float((self.db.execute("SELECT value FROM meta WHERE key='clock'").fetchone() or [0])[0]))
            floor = now - RETENTION_DAYS * 86400
            bounds = self.db.execute("SELECT MIN(seq), MAX(seq) FROM events WHERE stamp>=?", (floor,)).fetchone()
            oldest = bounds[0] or 0
            newest = (self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone() or [0])[0]
            upper = min(upper_cursor, newest) if upper_cursor is not None else newest
            meta = dict(self.db.execute("SELECT key,value FROM meta").fetchall())
            result = dict(available=True, items=[], next_cursor=None, upper_cursor=upper,
                          oldest_cursor=oldest, history_gap=bool(cursor and (oldest > cursor + 1 or cursor < int(meta.get("pruned_through", 0)) or (not oldest and newest > cursor))),
                          coverage={"observation_start": float(meta["observation_start"]),
                                    "storage_gap": bool(meta.get("storage_gap") or self.failed),
                                    "retention_days": RETENTION_DAYS, "event_limit": MAX_EVENTS,
                                    "complete_before_observation": False})
            members = None
            if receipt_id:
                receipt = self.db.execute("SELECT * FROM receipts WHERE id=? AND device=? AND stamp>=?", (receipt_id, device, floor)).fetchone()
                if not receipt:
                    return dict(result, available=False, reason="This notification is unavailable or has expired.")
                members = json.loads(receipt["members"])
            rows = self.db.execute("SELECT seq,data FROM events WHERE seq>? AND seq<=? AND stamp>=? AND stamp<=? ORDER BY seq", (0 if members is not None else cursor, newest if members is not None else upper, max(floor, since), until if until is not None else now)).fetchall()
            if members is None:
                latest = {}
                for row in rows:
                    latest[json.loads(row["data"])["id"]] = row
                rows = sorted(latest.values(), key=lambda row: (json.loads(row["data"])["status"] not in ("open", "delivered_unconfirmed"), -row["seq"]))
            selected = []
            for row in rows:
                item = json.loads(row["data"])
                if item["root"] not in roots or (project and item["root"] != project):
                    continue
                if members is not None and row["seq"] not in members:
                    continue
                current = self.db.execute("SELECT data FROM episodes WHERE id=? AND updated>=?", (item["id"], floor)).fetchone() if members is not None else None
                if current:
                    latest = json.loads(current[0])
                    # A moved card's current outcome belongs to its new root.
                    # Never overlay it onto a receipt scoped to the old root.
                    if latest["root"] not in roots or latest["root"] != item["root"]:
                        continue
                    item.update(status=latest["status"], outcome=latest["outcome"], provenance=latest["provenance"], updated_at=latest["updated_at"], truncated=item["truncated"] or latest["truncated"])
                selected.append(dict(item, cursor=row["seq"]))
            if members is None and page_cursor is not None:
                positions = [index for index, item in enumerate(selected) if item["cursor"] == page_cursor]
                if not positions:
                    return dict(result, available=False, history_gap=True, reason="This history page expired. Refresh Catch up.")
                selected = selected[positions[0] + 1:]
            if members is not None and len(selected) != len(members):
                result.update(available=False, reason="Some notification records have expired, moved projects, or are no longer enrolled.")
            page = []
            size = 0
            for item in selected[:limit]:
                encoded = len(json.dumps(item).encode())
                if members is not None and encoded > 8000:
                    item = dict(item, questions=[], question_text=item["question_text"][:500], outcome=item["outcome"][:500], truncated=True)
                    encoded = len(json.dumps(item).encode())
                if page and size + encoded > 300_000:
                    break
                page.append(item)
                size += encoded
            result["items"] = page
            if len(selected) > len(page):
                if members is not None:
                    result.update(available=False, reason="This notification group could not be loaded completely. Open Catch up to recover its records.")
                else:
                    result["next_cursor"] = page[-1]["cursor"]
            return result
