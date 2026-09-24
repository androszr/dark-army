"""Daemon-shared Needs you acknowledgements.

A small JSON list next to sessions.json, TitleShop's shape: atomic
tmp+replace, OrderedDict cap dropping the oldest, extra keys ignored,
missing file empty. Clients filter Inbox / Needs you against the published
set; they keep no hide list of their own.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dark-army")

# Dismiss is one verb with one meaning — hide this subject until it
# changes — and it covers every kind but one. A permission ask still blocks
# its tool call, so hiding it would be a press that quietly did nothing.
# `plan_ready` and `awaiting_review` are no longer inbox kinds (the Backlog
# tab and the Done column carry them); an old client's ack for either is
# refused as an unknown kind, never honoured.
ACK_KINDS = frozenset({"question", "waiting", "ended_work", "manual_check"})
NEVER_KINDS = frozenset({"permission"})
MAX_INBOX_ACKS = 64

INBOX_ACK_STALE_REFUSAL = "that item has changed or is already gone"
INBOX_ACK_KIND_REFUSAL = "that item cannot be acknowledged"
INBOX_ACK_MISSING_REFUSAL = "that item is not waiting on you"


def fingerprint(kind: str, material: str) -> str:
    """Kind-prefixed SHA-256 of UTF-8 material, except waiting's token."""
    if kind == "waiting":
        return "waiting"
    digest = hashlib.sha256((material or "").encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def question_material(questions: list) -> str:
    """Ids joined when every nonempty question has one; otherwise texts.

    A wording tweak with a stable tool_use id does not unhide. An empty id
    falls back to the text, so a new question without an id does.
    """
    nonempty = []
    for raw in questions or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("text") or ""):
            nonempty.append(raw)
    if nonempty and all(str(q.get("id") or "") for q in nonempty):
        return "|".join(str(q.get("id") or "") for q in nonempty)
    return "|".join(str(q.get("text") or "") for q in nonempty)


def valid_key(key: str) -> bool:
    return isinstance(key, str) and (
        (key.startswith("s:") or key.startswith("c:")) and len(key) > 2)


def session_kind_and_fp(*, permission: bool, questions: list,
                        waiting: bool) -> Optional[tuple[str, str]]:
    """Live session kind + fingerprint, permission first, then question."""
    if permission:
        return "permission", fingerprint("permission", "")
    material = question_material(questions)
    if material:
        return "question", fingerprint("question", material)
    if waiting:
        return "waiting", fingerprint("waiting", "")
    return None


def card_kind_and_fp(card: dict) -> Optional[tuple[str, str]]:
    """Live card kind + fingerprint, Inbox.items precedence: the two daemon
    judgments, ended work first. A finished card awaiting review and a ready
    plan are not inbox subjects."""
    if not isinstance(card, dict):
        return None
    if card.get("needs_you"):
        return "ended_work", fingerprint("ended_work", "")
    if card.get("manual_check_due"):
        steps = str(card.get("manual_steps") or "")
        return "manual_check", fingerprint("manual_check", steps)
    return None


class InboxAckStore:
    """Oldest-first acknowledgements, capped, restart-durable."""

    def __init__(self, path: Optional[Path] = None,
                 max_acks: int = MAX_INBOX_ACKS):
        self.path = path
        self.max_acks = max_acks
        self._lock = threading.Lock()
        # key -> (kind, fp), insertion order is oldest first.
        self._acks: "OrderedDict[str, tuple[str, str]]" = OrderedDict()

    def records(self) -> list[dict]:
        with self._lock:
            return [{"key": key, "kind": kind, "fp": fp}
                    for key, (kind, fp) in self._acks.items()]

    def ack(self, key: str, kind: str, fp: str) -> None:
        """Idempotent on the same triple; replace the row on a new fp."""
        if not key:
            return
        with self._lock:
            current = self._acks.get(key)
            if current == (kind, fp):
                return
            if key in self._acks:
                self._acks[key] = (kind, fp)
                self._acks.move_to_end(key)
            else:
                self._acks[key] = (kind, fp)
                self._trim_locked()
            self._save_locked()

    def forget_session(self, sid: str) -> None:
        if not sid:
            return
        self.forget_key("s:" + sid)

    def forget_key(self, key: str) -> None:
        with self._lock:
            if self._acks.pop(key, None) is None:
                return
            self._save_locked()

    def prune(self, live_keys) -> None:
        live = set(live_keys)
        with self._lock:
            dropped = [key for key in self._acks if key not in live]
            if not dropped:
                return
            for key in dropped:
                del self._acks[key]
            self._save_locked()

    def _trim_locked(self) -> None:
        while len(self._acks) > self.max_acks:
            self._acks.popitem(last=False)

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("inbox acks unreadable", exc_info=True)
            return
        rows = data.get("acks") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return
        loaded: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            kind = row.get("kind")
            fp = row.get("fp")
            if not (isinstance(key, str) and key
                    and isinstance(kind, str)
                    and isinstance(fp, str)):
                continue
            loaded[key] = (kind, fp)
            loaded.move_to_end(key)
        while len(loaded) > self.max_acks:
            loaded.popitem(last=False)
        with self._lock:
            self._acks = loaded

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        if self.path is None:
            return
        payload = {
            "schema": 1,
            "acks": [{"key": key, "kind": kind, "fp": fp}
                     for key, (kind, fp) in self._acks.items()],
        }
        tmp_path = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_path, str(self.path))
        except OSError:
            logger.debug("inbox acks unwritable", exc_info=True)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


__all__ = [
    "ACK_KINDS", "NEVER_KINDS", "MAX_INBOX_ACKS",
    "INBOX_ACK_STALE_REFUSAL", "INBOX_ACK_KIND_REFUSAL",
    "INBOX_ACK_MISSING_REFUSAL",
    "fingerprint", "question_material", "valid_key",
    "session_kind_and_fp", "card_kind_and_fp", "InboxAckStore",
]
