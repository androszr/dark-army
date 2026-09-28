"""One conversation shape for Claude Code, Codex and Grok transcripts.

Grok's journal carries no clock on any record: every Grok turn is emitted
with ``ts`` 0.0, and the phone draws no time for those rows. The path of a
journal is an argument to the reader and never leaves this module — not on
the page, not in an INFO log line.
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .ai_title import _SYNTHETIC_PROMPT_PREFIXES, _first_text
from .codex_rollouts import _is_synthetic_prompt, _text, _tool_input
from .grok_chat import _USER_QUERY_RE, _genuine_user_prompt, _user_content
from .grok_usage import DeltaCache
from .session_stats import (
    _ACTIONS_RE,
    _CHANNEL_USER_RE,
    _TLDR_RE,
    _is_channel_user_message,
    _is_tool_result,
)

KINDS = ("user", "agent", "tool", "result", "note")
PAGE_TURNS = 150
PAGE_BYTES = 200_000
#: Transcripts held folded per provider. A phone reads one session at a
#: time and the Mac's detail pane one more; 256 kept every transcript a
#: browsing thumb had ever opened resident.
CACHE_FILES = 16
TURN_TEXT_CHARS = 12_000
TOOL_BRIEF_CHARS = 120
RESULT_SUMMARY_CHARS = 160
UNKNOWN_SESSION_REFUSAL = "Dark Army is not watching that session"
NO_TRANSCRIPT_REASON = "Dark Army has not found this session's transcript yet"
NO_HELPER_REASON = "Dark Army has not found this helper's transcript yet"
HELPER_PROVIDER_REASON = "Only a Claude Code session's helpers can be read here"
#: A helper id as the wire may carry it: Claude Code's are hex. The shape is
#: the path guard — the id becomes ``agent-<id>.jsonl`` under the session's
#: own ``subagents`` folder and can name nothing else.
AGENT_ID_RE = r"[A-Za-z0-9_-]{1,64}"
#: The reader's cache key for helpers' journals — not a provider.
SIDECHAIN = "claude-sidechain"

_TOOL_BRIEF_KEYS = (
    "command", "file_path", "path", "pattern", "query", "url",
    "description", "prompt",
)
#: The one line a compaction summary folds to.
COMPACTED_NOTE = "context compacted — the agent continues from a summary"
#: The close of the wrapper Dark Army's channel puts around a reply.
CHANNEL_CLOSE_TAG = "</channel>"


def _ts_epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _collapse(text: str) -> str:
    return " ".join(str(text or "").split())


def clamp_text(text: str) -> tuple[str, bool]:
    raw = str(text or "")
    if len(raw) <= TURN_TEXT_CHARS:
        return raw, False
    return raw[:TURN_TEXT_CHARS], True


def tool_brief(name: str, payload) -> str:
    data = payload if isinstance(payload, dict) else {}
    picked = ""
    for key in _TOOL_BRIEF_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            picked = value
            break
    if not picked:
        for value in data.values():
            if isinstance(value, str) and value.strip():
                picked = value
                break
    return _collapse(picked)[:TOOL_BRIEF_CHARS]


def result_summary(text: str) -> str:
    for line in str(text or "").splitlines():
        collapsed = _collapse(line)
        if collapsed:
            return collapsed[:RESULT_SUMMARY_CHARS]
    return _collapse(text)[:RESULT_SUMMARY_CHARS]


def file_key(path: str, first_ts: float) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_ino}:{int(first_ts)}"


def _strip_markers(text: str) -> str:
    return _ACTIONS_RE.sub(" ", _TLDR_RE.sub(" ", text or ""))


def channel_reply_text(lead: str) -> str:
    """The person's words inside a channel `kind="user"` record: the opening
    tag and the trailing close removed, the ends trimmed. ``""`` when ``lead``
    does not open with the tag. Only the trailing close goes; an inner
    ``</channel>`` is the person's own text."""
    m = _CHANNEL_USER_RE.match(lead or "")
    if m is None:
        return ""
    body = lead[m.end():].strip()
    if body.endswith(CHANNEL_CLOSE_TAG):
        body = body[: -len(CHANNEL_CLOSE_TAG)].strip()
    # A card's question pushed straight to the session carries Dark Army's
    # own direction after it (`daemon_board.ask_direct_tail`): the person
    # never typed that, so it is not drawn as their turn.
    tail = _ASK_DIRECT_TAIL_RE.search(body)
    if tail is not None:
        body = body[: tail.start()].rstrip()
    return body


#: `daemon_board.ask_direct_tail`, either tool name; matched only at the end.
_ASK_DIRECT_TAIL_RE = re.compile(
    r"\s*Answer this question about your card through "
    r"\w+_answer_card\. Write one answer; change nothing else\.\Z")


def _join_text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            value = item.get("text")
            if isinstance(value, str) and value:
                parts.append(value)
    return "\n".join(parts)


def _result_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        joined = _text(output)
        if joined:
            return joined
        parts = []
        for item in output:
            if isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str) and value:
                    parts.append(value)
            elif isinstance(item, str) and item:
                parts.append(item)
        return "\n".join(parts)
    return ""


def _tool_input_from(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


@dataclass
class Turn:
    seq: int
    kind: str
    ts: float
    text: str = ""
    tool: str = ""
    brief: str = ""
    call_id: str = ""
    result_bytes: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "ts": self.ts,
            "text": self.text,
            "tool": self.tool,
            "brief": self.brief,
            "call_id": self.call_id,
            "result_bytes": self.result_bytes,
            "truncated": self.truncated,
        }


@dataclass
class _Accum:
    turns: list = field(default_factory=list)
    offset: int = 0
    folded: int = 0
    key: str = ""
    first_ts: float = 0.0


def _finalize(acc: _Accum) -> _Accum:
    return acc


def _push(acc: _Accum, kind: str, ts: float, **fields: Any) -> None:
    text = str(fields.pop("text", "") or "")
    truncated = bool(fields.pop("truncated", False))
    if text:
        text, clamped = clamp_text(text)
        truncated = truncated or clamped
    if acc.first_ts == 0.0 and ts:
        acc.first_ts = float(ts)
    acc.turns.append(Turn(
        seq=len(acc.turns),
        kind=kind,
        ts=float(ts),
        text=text,
        truncated=truncated,
        **fields,
    ))


def fold_claude(obj: dict, acc: _Accum, *, sidechain: bool = False) -> None:
    """One Claude Code journal line. ``sidechain`` is a helper's own
    journal (``subagents/agent-<id>.jsonl``), where **every** line carries
    ``isSidechain`` — the brief the helper was handed included — so the
    session reader's skip would hide the one prompt that says what the
    helper is doing."""
    otype = obj.get("type")
    ts = _ts_epoch(obj.get("timestamp"))
    msg = obj.get("message")
    msg = msg if isinstance(msg, dict) else {}
    content = msg.get("content")
    if otype == "user":
        if _is_tool_result(content):
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                raw = _join_text_blocks(block.get("content"))
                if not raw and isinstance(block.get("content"), str):
                    raw = block.get("content") or ""
                _push(
                    acc, "result", ts,
                    text=result_summary(raw),
                    call_id=str(block.get("tool_use_id") or ""),
                    result_bytes=len(raw),
                )
            return
        if obj.get("isSidechain") is True and not sidechain:
            return
        text = _join_text_blocks(content) if not isinstance(content, str) else content
        if not isinstance(text, str):
            text = ""
        lead = text.lstrip()
        if obj.get("isMeta") is True:
            # A reply typed in Dark Army's panel or on the phone:
            # `reply_to_session`'s `kind="user"` channel event, written as an
            # `isMeta` record with `origin.kind == "channel"`. It is the
            # person's turn, drawn without the wrapper. `kind="fleet"` and
            # every other meta record stay out.
            if _is_channel_user_message(obj, lead):
                body = channel_reply_text(lead)
                if body:
                    _push(acc, "user", ts, text=body)
            return
        # The summary `/compact` (or an automatic compaction) hands the agent
        # is a `user` record Claude Code wrote, not the person's words: drawn
        # as one it read "You said:" over pages of raw markdown. The turns it
        # summarises are still earlier in this same journal, so one line
        # saying where the break is covers it.
        if obj.get("isCompactSummary") is True:
            _push(acc, "note", ts, text=COMPACTED_NOTE)
            return
        if lead.startswith(_SYNTHETIC_PROMPT_PREFIXES):
            return
        if text.strip():
            _push(acc, "user", ts, text=text)
        return
    if otype != "assistant":
        return
    model = msg.get("model")
    if not isinstance(model, str) or model == "<synthetic>":
        return
    parts = []
    tools = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                raw = str(item.get("text") or "")
                parts.append(_strip_markers(raw))
            elif kind == "tool_use":
                tools.append(item)
    spoken = "\n".join(p for p in parts if p).strip()
    if spoken:
        _push(acc, "agent", ts, text=spoken)
    for item in tools:
        tin = item.get("input") if isinstance(item.get("input"), dict) else {}
        _push(
            acc, "tool", ts,
            tool=str(item.get("name") or ""),
            brief=tool_brief(str(item.get("name") or ""), tin),
            call_id=str(item.get("id") or ""),
        )


def fold_codex(obj: dict, acc: _Accum) -> None:
    if obj.get("type") != "response_item":
        return
    payload = obj.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    ts = _ts_epoch(obj.get("timestamp"))
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        text = _text(payload.get("content"))
        if role == "user":
            if _is_synthetic_prompt(text):
                return
            if text.strip():
                _push(acc, "user", ts, text=text)
        elif role == "assistant":
            spoken = _strip_markers(text).strip()
            if spoken:
                _push(acc, "agent", ts, text=spoken)
        return
    if ptype in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "")
        _push(
            acc, "tool", ts,
            tool=name,
            brief=tool_brief(name, _tool_input(payload)),
            call_id=str(payload.get("call_id") or payload.get("id") or ""),
        )
        return
    if ptype in ("function_call_output", "custom_tool_call_output"):
        raw = _result_text(payload.get("output"))
        _push(
            acc, "result", ts,
            text=result_summary(raw),
            call_id=str(payload.get("call_id") or payload.get("id") or ""),
            result_bytes=len(raw),
        )
        return
    if ptype == "agent_message":
        author = str(payload.get("author") or "")
        recipient = str(payload.get("recipient") or "")
        body = _text(payload.get("content")) or str(payload.get("text") or "")
        _push(acc, "note", ts, text=f"{author} → {recipient}: {body}")


def fold_grok(obj: dict, acc: _Accum) -> None:
    kind = obj.get("type")
    ts = 0.0
    if kind == "user":
        if not _genuine_user_prompt(obj):
            return
        text = _first_text(_user_content(obj))
        match = _USER_QUERY_RE.search(text)
        if match:
            text = match.group(1)
        if text.strip():
            _push(acc, "user", ts, text=text.strip())
        return
    if kind == "assistant":
        content = obj.get("content")
        spoken = ""
        if isinstance(content, str) and content:
            spoken = _strip_markers(content).strip()
        if spoken:
            _push(acc, "agent", ts, text=spoken)
        calls = obj.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "")
                _push(
                    acc, "tool", ts,
                    tool=name,
                    brief=tool_brief(name, _tool_input_from(call.get("arguments"))),
                    call_id=str(call.get("id") or ""),
                )
        return
    if kind == "tool_result":
        raw = obj.get("content")
        if not isinstance(raw, str):
            raw = _first_text(raw)
        raw = raw if isinstance(raw, str) else ""
        _push(
            acc, "result", ts,
            text=result_summary(raw),
            call_id=str(obj.get("tool_call_id") or obj.get("toolCallId") or ""),
            result_bytes=len(raw),
        )


def _fold_line(folder: Callable[[dict, _Accum], None], line: str, acc: _Accum) -> None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    acc.folded += 1
    folder(obj, acc)


def _fold_claude_line(line: str, acc: _Accum) -> None:
    _fold_line(fold_claude, line, acc)


def _fold_claude_sidechain_line(line: str, acc: _Accum) -> None:
    _fold_line(lambda obj, a: fold_claude(obj, a, sidechain=True), line, acc)


def _fold_codex_line(line: str, acc: _Accum) -> None:
    _fold_line(fold_codex, line, acc)


def _fold_grok_line(line: str, acc: _Accum) -> None:
    _fold_line(fold_grok, line, acc)


class ConversationReader:
    """Paged turns from a journal, one ``DeltaCache`` per provider.

    ``page`` serialises on a lock: ``DeltaCache`` is not itself thread-safe,
    and two phones can ask for the same file on concurrent executor hops.
    ``since`` is a slice index into the list this reader owns; it never
    rewinds the cache.
    """

    def __init__(self) -> None:
        self._caches = {
            "claude": DeltaCache(_Accum, _fold_claude_line, _finalize, CACHE_FILES),
            "codex": DeltaCache(_Accum, _fold_codex_line, _finalize, CACHE_FILES),
            "grok": DeltaCache(_Accum, _fold_grok_line, _finalize, CACHE_FILES),
            # A helper's own journal: Claude's shape, sidechain lines kept.
            SIDECHAIN: DeltaCache(_Accum, _fold_claude_sidechain_line,
                                  _finalize, CACHE_FILES),
        }
        self._lock = threading.Lock()

    def page(self, provider: str, path: str, since: int, key: str,
             *, sidechain: bool = False) -> dict:
        """``sidechain`` reads a Claude Code helper's own journal; the page
        still says ``provider: "claude"``."""
        provider = provider or "claude"
        if not path:
            return {
                "available": False,
                "reason": NO_TRANSCRIPT_REASON,
                "provider": provider,
            }
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            return {
                "available": False,
                "reason": NO_TRANSCRIPT_REASON,
                "provider": provider,
            }
        cache = (self._caches[SIDECHAIN] if sidechain
                 else self._caches.get(provider) or self._caches["claude"])
        with self._lock:
            acc = cache.get(path)
            acc.key = file_key(path, acc.first_ts)
            reset = bool(key) and key != acc.key
            if since > len(acc.turns):
                reset = True
            start = 0 if reset else max(int(since or 0), 0)
            window = [turn.to_dict()
                      for turn in acc.turns[start:start + PAGE_TURNS]]
            # The byte bound is met in one pass: each turn is serialised
            # once for its length, the envelope once with the turns out,
            # and the page is the longest prefix that fits. Dropping a turn
            # and re-serialising the whole page cost 0.65 s per page on a
            # transcript of long turns. The envelope is measured with the
            # full window's `next_seq` and `more`, which is never shorter
            # than a prefix's, so the bound errs by a byte, never over.
            envelope = len(json.dumps(
                self._page_body(provider, acc, reset, start, [], len(window)),
                allow_nan=False))
            keep = 0
            total = envelope
            for turn in window:
                # `json.dumps` joins list items with ", ": two bytes each.
                total += len(json.dumps(turn, allow_nan=False)) + (2 if keep else 0)
                if total > PAGE_BYTES:
                    break
                keep += 1
            # One turn always goes, however large: a page that never
            # advances would pin the cursor for good.
            kept = window[:max(keep, 1)]
            return self._page_body(provider, acc, reset, start, kept, len(kept))

    @staticmethod
    def _page_body(provider: str, acc: _Accum, reset: bool,
                   start: int, turns: list, shown: int) -> dict:
        """``shown`` is how far the cursor moves — the turns on the page,
        except when the envelope is being measured with none on it."""
        return {
            "available": True,
            "provider": provider,
            "key": acc.key,
            "reset": reset,
            "turns": turns,
            "next_seq": start + shown,
            "total": len(acc.turns),
            "more": start + shown < len(acc.turns),
            "generated_at": time.time(),
        }
