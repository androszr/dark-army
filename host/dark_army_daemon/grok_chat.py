"""Read-only rollup of a Grok session's ``chat_history.jsonl``.

Grok writes the conversation here as the file grows — assistant turns with
string ``content``, plus ``system`` / ``reasoning`` / ``backend_tool_call``
lines this reader ignores. ``user`` prompts clear ``last_report``; ``tool_result``
does not. The latest spoken assistant turn is the caption a waiting row already
knows how to draw, and a ``## Work done`` heading is kept as ``last_report``.

Unlike Claude Code, Grok flushes the asking turn *while the dialog is up*:
an ``ask_user_question`` lives on the assistant record as ``tool_calls``,
so this reader can populate ``question`` without waiting for the hook.
The matching ``tool_result`` (same ``tool_call_id``) is the moment the
person answered; a later assistant record is not, because Grok batches
other tools onto the same turn and may keep writing while the dialog
stands. A numbered list in the spoken text is the fallback when that
tool was refused and the model named the answers in prose.

Same incremental cache as ``grok_usage.UsageCache`` / ``grok_events.EventsCache``.
Nothing here writes to ``~/.grok/``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .ai_title import _SYNTHETIC_PROMPT_PREFIXES, _first_text
from .grok_usage import DeltaCache
from .session_stats import (
    MAX_SUMMARY_CHARS,
    _ACTIONS_RE,
    _TLDR_RE,
    _collapse_prose,
    _parse_actions,
    _parse_numbered_actions,
    _tail_prose,
    _work_report,
    questions_from_tool_input,
)


_ASK_TOOL = "ask_user_question"
_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE,
)


@dataclass
class GrokChat:
    last_text: str = ""
    last_summary: str = ""
    last_actions: list = field(default_factory=list)
    last_report: str = ""
    question: dict = field(default_factory=dict)
    questions: list = field(default_factory=list)


@dataclass
class _ChatAccum:
    last_text: str = ""
    last_summary: str = ""
    last_actions: list = field(default_factory=list)
    last_report: str = ""
    question: dict = field(default_factory=dict)
    questions: list = field(default_factory=list)
    offset: int = 0
    folded: int = 0


def _ask_call(obj: dict) -> dict | None:
    calls = obj.get("tool_calls")
    if not isinstance(calls, list):
        return None
    last = None
    for call in calls:
        if isinstance(call, dict) and call.get("name") == _ASK_TOOL:
            last = call
    return last


def _question_from_call(call: dict) -> tuple[dict, list]:
    raw = call.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}, []
    every = questions_from_tool_input(raw, str(call.get("id") or ""))
    if not every:
        return {}, []
    flat = {k: v for k, v in every[0].items() if k != "index"}
    return flat, every


def _result_clears_question(obj: dict, acc: _ChatAccum) -> bool:
    """True when this tool_result is the answer to the live ask.

    Matched on the tool_call id, never on neighbouring records: the next
    line after an ask is often a *different* tool's result.
    """
    held = acc.question.get("id") if acc.question else ""
    if not held:
        return False
    call_id = obj.get("tool_call_id") or obj.get("toolCallId")
    return call_id == held


def _user_content(obj: dict):
    content = obj.get("content")
    if content is None:
        message = obj.get("message")
        if isinstance(message, dict):
            content = message.get("content")
    return content


def _genuine_user_prompt(obj: dict) -> bool:
    """True when this user record is the person's next prompt.

    Live Grok prompts are a text list with ``<user_query>``. Skill dumps
    (``synthetic_reason``) and ``<user_info>`` wrappers are not a prompt
    and must not clear ``last_report``.
    """
    if obj.get("synthetic_reason"):
        return False
    text = " ".join(_first_text(_user_content(obj)).split())
    if not text:
        return False
    if _USER_QUERY_RE.search(text):
        return True
    if text.startswith("<user_info>"):
        return False
    if text.startswith(_SYNTHETIC_PROMPT_PREFIXES):
        return False
    return True


def _fold_line(line: str, acc: _ChatAccum) -> None:
    # Cheap reject: the file is mostly reasoning. tool_result is the
    # answer seam and must not be skipped. user lines clear last_report
    # on a genuine prompt, so they cannot be skipped either.
    if ('"assistant"' not in line and '"tool_result"' not in line
            and '"user"' not in line):
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    kind = obj.get("type")
    if kind == "tool_result":
        acc.folded += 1
        if _result_clears_question(obj, acc):
            acc.question = {}
            acc.questions = []
        return
    if kind == "user":
        acc.folded += 1
        if _genuine_user_prompt(obj):
            acc.last_report = ""
            acc.question = {}
            acc.questions = []
        return
    if kind != "assistant":
        return
    acc.folded += 1

    # The asking turn is this record's `tool_calls`. A later assistant
    # without a new ask leaves it standing: Grok puts other tools on the
    # same turn and keeps writing while the TUI dialog is up. The matching
    # tool_result (above) is the answer seam; a new ask replaces this one.
    ask = _ask_call(obj)
    if ask:
        question, questions = _question_from_call(ask)
        acc.question = question
        acc.questions = questions

    content = obj.get("content")
    if not isinstance(content, str) or not content:
        return
    summary = None
    actions = None
    marks = _TLDR_RE.findall(content)
    if marks:
        summary = " ".join(marks[-1].split())[:MAX_SUMMARY_CHARS]
        content = _TLDR_RE.sub(" ", content)
    offers = _ACTIONS_RE.findall(content)
    if offers:
        actions = _parse_actions(offers[-1])
        content = _ACTIONS_RE.sub(" ", content)
    elif not acc.question:
        # No marker and no live tool dialog: a numbered list is the
        # declaration Grok actually writes. Native-tool options stay on
        # `question`, not here — those buttons type a digit into the TUI
        # card; these send the label as a reply.
        guessed = _parse_numbered_actions(content)
        if guessed:
            actions = guessed
    text = _collapse_prose(content)
    spoke = bool(text)
    if spoke:
        acc.last_text = _tail_prose(text)
        report = _work_report(text)
        if report:
            acc.last_report = report
    if summary is not None:
        acc.last_summary = summary
    elif spoke:
        acc.last_summary = ""
    if actions is not None:
        acc.last_actions = actions
    elif spoke:
        acc.last_actions = []


def _finalize(acc: _ChatAccum) -> GrokChat:
    return GrokChat(
        last_text=acc.last_text,
        last_summary=acc.last_summary,
        last_actions=list(acc.last_actions),
        last_report=acc.last_report,
        question=dict(acc.question),
        questions=list(acc.questions),
    )


class ChatCache(DeltaCache):
    """Memoises ``chat_history.jsonl`` parsing by mtime, reading only the delta."""

    def __init__(self, max_entries: int = 512) -> None:
        super().__init__(_ChatAccum, _fold_line, _finalize, max_entries)

    def get(self, path: str) -> GrokChat:
        return super().get(path)


def parse_chat(path: str) -> GrokChat:
    return ChatCache().get(path)
