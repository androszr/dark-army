"""Serialized, failure-isolated capture. Never take a board lock in this worker."""
import asyncio
import copy
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from . import alerts, enrollment
from .decision_store import DecisionStore

logger = logging.getLogger("dark-army")


class DecisionCapture:
    def __init__(self, store=None):
        self.store = store or DecisionStore()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decisions")
        self.slots = set()
        self.session_items = {}
        self.failure_at = 0
        self.retry_at = 0
        self.hook_state = {}
        self.hook_lock = threading.RLock()

    def submit(self, method, *args, **kwargs):
        # Capture values before crossing threads, especially mutable permission
        # rows. Only named, non-secret facts are passed to this seam.
        args, kwargs = copy.deepcopy((args, kwargs))
        def run():
            try:
                if self.store.db is None and method not in ("open", "close"):
                    if time.monotonic() < self.retry_at:
                        return None
                    self.retry_at = time.monotonic() + 30
                    self.store.open()
                result = getattr(self.store, method)(*args, **kwargs)
                self.failure_at = 0
                return result
            except Exception:
                self.store.failed = True
                if not self.failure_at or time.monotonic() - self.failure_at > 600:
                    logger.warning("decision history unavailable; coverage is incomplete", exc_info=True)
                    self.failure_at = time.monotonic()
                return None
        return self.executor.submit(run)

    async def call(self, method, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(method, *args, **kwargs))

    def question_hook(self, sid, facts):
        with self.hook_lock:
            self.hook_state[sid] = copy.deepcopy(facts)
            self.submit("observe", "question:" + sid, facts)

    def question_clear(self, sid, *, forget=False):
        with self.hook_lock:
            old = self.hook_state.get(sid) or {}
            self.hook_state[sid] = {"cleared_source": old.get("source_id", "")}
            self.submit("finish", "question:" + sid)
            self.session_items.pop(sid, None)
            if forget:
                self.hook_state.pop(sid, None)

    def reconcile(self, snapshot, permissions=None):
        """Called by the one enrichment worker, never the daemon loop."""
        seen = set()
        for category in ("waiting", "running", "sleeping", "finished", "abandoned"):
            for row in snapshot.get(category, []):
                sid = row.get("session_id", "")
                slot = "question:" + sid
                question = row.get("question") or {}
                response_request = not question and category == "waiting" and alerts.offered_reply(row)
                if response_request:
                    question = {"text": row.get("last_summary") or row.get("last_text") or "Response requested",
                                "options": row.get("reply_options") or []}
                if not question or category in ("finished", "abandoned"):
                    continue
                with self.hook_lock:
                    hook = copy.deepcopy(self.hook_state.get(sid))
                if not response_request and hook is not None and "cleared_source" in hook and (not hook["cleared_source"] or hook["cleared_source"] == question.get("id", "")):
                    continue
                seen.add(slot)
                root = enrollment.root_enrolled(str(row.get("cwd") or ""))
                if not root:
                    continue
                facts = dict(root=root, project=row.get("project"), provider=row.get("provider"),
                             session_id=sid, source_id=question.get("id"), kind="response_request" if response_request else "question",
                             questions=row.get("questions") or [question],
                             question_text=question.get("text") or question.get("question"),
                             title=row.get("name"), provenance="observed pending question")
                # A hook arriving after this worker copied the snapshot wins.
                # Hold only the short submit section, never SQLite work.
                with self.hook_lock:
                    current = self.hook_state.get(sid)
                    if current != hook:
                        continue
                    future = self.submit("observe", slot, facts)
                item = future.result()
                if item:
                    row["decision_episode_id"] = item["id"]
                    self.session_items[sid] = item["id"]
        # A new hook can beat the snapshot that first contains its row.
        # Include authoritative held slots and enqueue the absence sweep under
        # the same short lock as hook submissions, so its ordering cannot close
        # a later ask. Neither thread waits on SQLite while holding this lock.
        with self.hook_lock:
            held = {"question:" + sid for sid, facts in self.hook_state.items()
                    if "cleared_source" not in facts}
            permission_slots = {"permission:" + str(row.get("request_id") or "")
                                for row in (permissions() if permissions else [])}
            pending = self.submit("reconcile_missing", seen | held | permission_slots,
                                  include_permissions=permissions is not None)
        pending.result()
        self.slots = seen

    def freeze_alert_targets(self, alerts, snapshot, cards, prompts):
        """Bind locators to the very inputs the alert policy evaluated."""
        by_sid = {entry.get("session_id"): entry for bucket in snapshot.values()
                  for entry in bucket if isinstance(entry, dict)}
        for alert in alerts:
            sid = alert["session_id"]
            entry = by_sid.get(sid, {})
            permission = alert.get("rule") == "permission"
            episode_id = entry.get("decision_episode_id", "")
            alert["_decision_ids"] = self.submit("targets",
                episode_id=episode_id if not permission else "",
                permission_id=str((prompts.get(sid) or {}).get("request_id") or "") if permission else "",
                card_ids=[card["id"] for card in cards if card.get("session_id") == sid]
                if not episode_id and not permission else []).result() or []

    def permission(self, row, kind, root, project, extra):
        with self.hook_lock:
            sid = str(row.get("session_id") or "")
            slot = "permission:" + str(row.get("request_id") or "")
            if kind == "permission_ask":
                self.submit("observe", slot, dict(root=enrollment.root_enrolled(root), project=project,
                            session_id=sid, source_id=row.get("request_id"), kind="permission",
                            title=row.get("tool_name"), question_text=row.get("description"),
                            provenance="observed permission request"))
            else:
                outcome = str(extra.get("outcome") or "")
                delivered = outcome in ("allow", "deny")
                self.submit("finish", slot,
                            "delivered_unconfirmed" if delivered else "ended_unknown",
                            outcome if delivered else "Outcome not observed.",
                            "verdict staged for hook" if row.get("via") == "hook" and delivered
                            else "verdict sent to channel" if delivered else str(extra.get("why") or "observed disappearance"))

    async def close(self):
        await self.call("close")
        self.executor.shutdown(wait=False)
