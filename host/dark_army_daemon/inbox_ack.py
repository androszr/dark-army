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
# `start_asked` is Mission Control asking for a card to be started: Dismiss
# is the person's No, and the daemon drops the ask with it.
ACK_KINDS = frozenset({"question", "waiting", "ended_work", "manual_check",
                       "start_asked"})
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


def question_list(row) -> list:
    """The row's questions as the phone reads them — `Agent.questionList`:
    ``questions`` when it is a non-empty list, else the flat ``question``
    when that dict carries text (what a Grok or Codex row publishes), else
    nothing. The one place the fallback lives on the Python side."""
    if not isinstance(row, dict):
        return []
    questions = row.get("questions")
    if isinstance(questions, list) and questions:
        return list(questions)
    question = row.get("question")
    if isinstance(question, dict) and str(question.get("text") or ""):
        return [question]
    return []


def ack_set(records) -> set[tuple[str, str, str]]:
    """``{(key, kind, fp)}`` from ack rows — `InboxAckStore.records()` or
    the published ``inbox.acks``. A row missing any of the three is
    skipped, as is anything that is not a dict."""
    out = set()
    for row in records or ():
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        kind = str(row.get("kind") or "")
        fp = str(row.get("fp") or "")
        if key and kind and fp:
            out.add((key, kind, fp))
    return out


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
    # Last, and only on a card that is neither: an asked start is a card
    # nobody has started, so it cannot be ended work or a hand-check.
    # The material is the ask's own id, so a second ask is a new subject.
    ask_id = str(card.get("start_ask_id") or "")
    if ask_id:
        return "start_asked", fingerprint("start_asked", ask_id)
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

    def forget_waiting(self, sid: str) -> None:
        """Drop the session's hide when it is a `waiting` one. Waiting's
        fingerprint is the fixed token `"waiting"`, so without this a
        dismissed wait — or one settled by Mark reviewed, Mark checked or
        Acknowledge & close — would hide every later wait for the life of
        the session. A new prompt starts a new turn, and its wait is news."""
        if not sid:
            return
        key = "s:" + sid
        with self._lock:
            current = self._acks.get(key)
            if current is None or current[0] != "waiting":
                return
            del self._acks[key]
            self._save_locked()

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
    "fingerprint", "question_material", "question_list", "ack_set",
    "valid_key",
    "session_kind_and_fp", "card_kind_and_fp", "InboxAckStore",
]
